"""Sprachausgabe mit Pocket TTS (lokal) + satzweises Streaming."""

from __future__ import annotations

import asyncio
import base64
import logging
import re
from collections.abc import Awaitable, Callable
from pathlib import Path

from .. import state
from ..config import DATA_DIR, VoiceConfig
from . import pocket

log = logging.getLogger(__name__)


# Inline-Code, der wie ein Befehl oder Pfad aussieht, wird nicht vorgelesen (steht ja im Chat)
_COMMANDISH = re.compile(r"[\s/|$=~]|--")


def _inline_code(m: re.Match) -> str:
    code = m.group(1)
    return " " if _COMMANDISH.search(code) or len(code) > 30 else code


def clean_for_speech(text: str) -> str:
    text = re.sub(r"```.*?```", " ", text, flags=re.S)
    text = re.sub(r"https?://\S+", "Link", text)
    text = re.sub(r"`([^`]*)`", _inline_code, text)
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
    text = re.sub(r"[*_#>|]+", " ", text)
    text = re.sub(r"^\s*[-•]\s+", "", text, flags=re.M)
    text = re.sub(r"\s+", " ", text).strip()
    text = re.sub(r"\s+([.,!?;:])", r"\1", text)  # „führe aus .“ → „führe aus.“
    text = re.sub(r":\s*([.!?])", r"\1", text)  # „Befehl: .“ → „Befehl.“
    return re.sub(r"^[\s:.,;]+$", "", text)


class SentenceSplitter:
    """Sammelt Tokens und gibt vollständige Sätze zurück, sobald sie fertig sind. Codeblöcke werden übersprungen."""

    MIN_CHARS = 30
    BOUNDARY = re.compile(r"(?<=[.!?:;])\s+|\n+")

    def __init__(self):
        self.buf = ""
        self.in_code = False

    def feed(self, text: str) -> list[str]:
        self.buf += text
        out: list[str] = []
        while True:
            if self.in_code:
                end = self.buf.find("```")
                if end < 0:
                    return out
                self.buf = self.buf[end + 3:]
                self.in_code = False
                continue
            start = self.buf.find("```")
            scan = self.buf if start < 0 else self.buf[:start]
            cut = None
            for m in self.BOUNDARY.finditer(scan):
                if scan.count("`", 0, m.start()) % 2 and "\n" not in m.group(0):
                    continue  # nicht mitten in `Inline-Code` trennen (z. B. `a; b`)
                if m.start() >= self.MIN_CHARS or "\n" in m.group(0):
                    cut = m
                    break
            if cut:
                sentence, self.buf = self.buf[:cut.start()], self.buf[cut.end():]
                if sentence.strip():
                    out.append(sentence.strip())
                continue
            if start >= 0:
                if scan.strip():
                    out.append(scan.strip())
                self.buf = self.buf[start + 3:]
                self.in_code = True
                continue
            return out

    def flush(self) -> list[str]:
        rest, self.buf = ("" if self.in_code else self.buf), ""
        self.in_code = False
        return [rest.strip()] if rest.strip() else []


class TTS:
    """Sprachausgabe mit Pocket TTS. Gewählte Stimme und die Sprache je Stimme stehen in state.json („tts“); die
    Sprache der aktiven Stimme ist auch die Sprache der Antworten (siehe reply_language). Ohne Pocket TTS spricht der
    Browser (Web Speech API)."""

    def __init__(self, cfg: VoiceConfig, data_dir: Path = DATA_DIR, state_path: Path | None = None):
        self.cfg = cfg
        self.base_language = cfg.language or "de"
        self.state_path = state_path or data_dir / "state.json"
        saved = state.load(self.state_path).get("tts") or {}
        voice = saved.get("voice")
        self.voice = voice if voice in pocket.BY_NAME else pocket.NATIVE.get(self.base_language, "alba")
        self.languages: dict[str, str] = {k: v for k, v in (saved.get("languages") or {}).items()
                                          if k in pocket.BY_NAME and v in pocket.LANGUAGES}
        self.pocket = pocket.PocketTTS(data_dir, self.language_of(self.voice))
        self._error: str | None = None

    @property
    def current(self) -> str:
        return self.voice

    def language_of(self, voice: str) -> str:
        return self.languages.get(voice) or self.base_language

    @property
    def reply_language(self) -> str:
        """Sprache, in der Orbwise antwortet und vorliest – die der aktiven Stimme."""
        return self.language_of(self.voice)

    def select(self, voice: str, language: str | None = None) -> bool:
        """Stimme (und ihre Sprache) wählen, merken und den Server schon starten – der erste Satz soll nicht warten."""
        if voice not in pocket.BY_NAME or (language and language not in pocket.LANGUAGES):
            return False
        self.voice = voice
        if language:
            if language == self.base_language:
                self.languages.pop(voice, None)
            else:
                self.languages[voice] = language
        state.update(self.state_path, lambda data: data.__setitem__(
            "tts", {"voice": self.voice, "languages": dict(self.languages)}))
        self.pocket.set_language(self.reply_language)
        if self.pocket.installed():
            self.pocket.warm(voice)
        return True

    def available(self) -> bool:
        if not self.pocket.installed():
            self._error = "Pocket TTS ist nicht installiert"
            return False
        if not pocket.cpu_supported():
            self._error = "Pocket TTS braucht eine CPU mit AVX2"
            return False
        self._error = None
        return True

    @property
    def error(self) -> str | None:
        return self._error

    def warmup(self) -> None:
        self.pocket.ensure_running(self.voice)

    def synth(self, text: str, voice: str | None = None) -> bytes:
        return self.pocket.synth(text, voice or self.voice)


class Speaker:
    """Wandelt gestreamte Antworten satzweise in Audio um und verteilt es an die Clients.
    Ohne Pocket TTS werden nur die Sätze geschickt – der Browser spricht sie dann selbst (Web Speech API)."""

    def __init__(self, tts: TTS | None, send: Callable[[dict], Awaitable[None]],
                 wanted: Callable[[], bool]):
        self.tts = tts
        self.send = send
        self.wanted = wanted
        self.splitters: dict[str, SentenceSplitter] = {}
        self.queue: asyncio.Queue = asyncio.Queue()
        self.generation = 0
        self.seq = 0
        self.worker: asyncio.Task | None = None

    def start(self) -> None:
        self.worker = asyncio.create_task(self._run())

    async def close(self) -> None:
        if self.worker:
            self.worker.cancel()

    def feed(self, msg_id: str, text: str) -> None:
        if not self.wanted():
            return
        sp = self.splitters.setdefault(msg_id, SentenceSplitter())
        for s in sp.feed(text):
            self._enqueue(msg_id, s)

    def end(self, msg_id: str) -> None:
        sp = self.splitters.pop(msg_id, None)
        if sp and self.wanted():
            for s in sp.flush():
                self._enqueue(msg_id, s)

    def say(self, text: str, msg_id: str = "system") -> None:
        if self.wanted():
            self._enqueue(msg_id, text)

    def stop(self) -> None:
        self.generation += 1
        self.splitters.clear()
        while not self.queue.empty():
            self.queue.get_nowait()

    def _enqueue(self, msg_id: str, sentence: str) -> None:
        spoken = clean_for_speech(sentence)
        if re.search(r"\w", spoken):
            self.seq += 1
            self.queue.put_nowait((self.generation, msg_id, self.seq, spoken))

    async def _run(self) -> None:
        while True:
            gen, msg_id, seq, text = await self.queue.get()
            if gen != self.generation:
                continue
            event = {"type": "speak", "id": msg_id, "seq": seq, "text": text}
            if self.tts:
                event["lang"] = self.tts.reply_language  # für die Browser-Stimme, falls Pocket fehlt
            if self.tts and self.tts.available():
                try:
                    wav = await asyncio.to_thread(self.tts.synth, text)
                    event["audio"] = base64.b64encode(wav).decode()
                except Exception as e:  # noqa: BLE001
                    log.warning("TTS fehlgeschlagen: %s", e)
            if gen == self.generation:
                await self.send(event)
