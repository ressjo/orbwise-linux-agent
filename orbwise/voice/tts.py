"""Sprachausgabe mit Piper (lokal) + satzweises Streaming."""

from __future__ import annotations

import asyncio
import base64
import io
import json
import logging
import re
import wave
from collections.abc import Awaitable, Callable
from pathlib import Path

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


class PiperTTS:
    """Sprachausgabe: Piper mit mehreren installierbaren Stimmen, dazu optional Pocket TTS (Stimmen „pocket:<name>“,
    siehe pocket.py). `rate` < 1 bedeutet: die Oberfläche spielt das Audio langsamer (= tiefer) ab – Piper spricht
    dafür entsprechend schneller, damit das Tempo gleich bleibt. Pocket kennt kein Tempo – sein Audio läuft
    unverändert (Rate 1)."""

    def __init__(self, cfg: VoiceConfig, data_dir: Path = DATA_DIR):
        self.cfg = cfg
        self.voices_dir = cfg.tts_voice.parent
        self.current = cfg.tts_voice.stem.removesuffix(".onnx")
        self.rate = 1.0
        self._voices: dict[str, tuple[object, int | None]] = {}
        self._error: str | None = None
        self.pocket = pocket.PocketTTS(data_dir, cfg.language or "de")

    def path(self, name: str | None = None) -> Path:
        return self.voices_dir / f"{name or self.current}.onnx"

    def installed(self) -> list[str]:
        return sorted(p.stem for p in self.voices_dir.glob("*.onnx")) if self.voices_dir.exists() else []

    def usable(self, name: str) -> bool:
        if pocket.is_pocket(name):
            return pocket.bare(name) in pocket.BY_NAME and self.pocket.installed()
        return bool(name) and name in self.installed()

    def select(self, name: str) -> bool:
        if not self.usable(name):
            return False
        if pocket.is_pocket(name) and name != self.current:
            self.pocket.warm(name)  # Server schon jetzt starten – der erste Satz soll nicht warten
        self.current = name
        return True

    def piper_voice(self) -> str | None:
        """Piper-Stimme für den Notfall (Pocket gewählt, aber nicht nutzbar): die aus der Konfiguration, sonst irgendeine."""
        default = self.cfg.tts_voice.stem.removesuffix(".onnx")
        if self.path(default).exists():
            return default
        others = self.installed()
        return others[0] if others else None

    def forget(self, name: str) -> None:
        """Geladene Stimme aus dem Zwischenspeicher werfen (z. B. nach einem neuen Upload gleichen Namens)."""
        self._voices.pop(name, None)

    def remove(self, name: str) -> None:
        """Installierte Stimme löschen (Modell + Konfiguration) und aus dem Zwischenspeicher werfen."""
        self._voices.pop(name, None)
        for f in (self.path(name), self.voices_dir / f"{name}.onnx.json"):
            f.unlink(missing_ok=True)

    def set_rate(self, rate: float) -> None:
        self.rate = min(1.0, max(0.8, float(rate)))

    def available(self) -> bool:
        if pocket.is_pocket(self.current):
            if self.usable(self.current):
                self._error = None
                return True
            self.current = self.piper_voice() or self.cfg.tts_voice.stem.removesuffix(".onnx")
        if not self.path().exists():
            others = self.installed()
            if others:
                self.current = others[0]
            else:
                self._error = f"Stimme fehlt: {self.path()}"
                return False
        try:
            import piper  # noqa: F401
        except ImportError:
            self._error = "piper-tts ist nicht installiert"
            return False
        self._error = None
        return True

    @property
    def error(self) -> str | None:
        return self._error

    def _load(self, name: str):
        if pocket.is_pocket(name):
            self.pocket.ensure_running(pocket.bare(name))
            return None
        if name not in self._voices:
            from piper import PiperVoice
            voice = PiperVoice.load(str(self.path(name)))
            self._voices[name] = (voice, self._speaker_id(name))
        return self._voices[name]

    def _speaker_id(self, name: str) -> int | None:
        from .catalog import BY_NAME
        wanted = BY_NAME[name].speaker if name in BY_NAME else ""
        meta = self.path(name).with_suffix(".onnx.json")
        if not wanted or not meta.exists():
            return None
        ids = json.loads(meta.read_text(encoding="utf-8")).get("speaker_id_map") or {}
        return ids.get(wanted)

    def speak(self, text: str, name: str | None = None) -> tuple[bytes, float | None]:
        """(WAV, Abspielrate) – die Rate ist 1, wenn Pocket gesprochen hat, sonst None (= die der Oberfläche).
        Fällt Pocket aus, spricht Piper."""
        name = name or self.current
        if pocket.is_pocket(name):
            try:
                return self.pocket.synth(text, name), 1.0
            except Exception as e:  # noqa: BLE001
                fallback = self.piper_voice()
                if not fallback:
                    raise
                log.warning("Pocket TTS fehlgeschlagen (%s) – spreche mit Piper", e)
                name = fallback
        return self.synth(text, name), None

    def synth(self, text: str, name: str | None = None) -> bytes:
        voice, speaker_id = self._load(name or self.current)
        length_scale = self.cfg.tts_length_scale * self.rate
        buf = io.BytesIO()
        with wave.open(buf, "wb") as wav:
            try:
                from piper import SynthesisConfig
                voice.synthesize_wav(text, wav, syn_config=SynthesisConfig(
                    length_scale=length_scale, speaker_id=speaker_id))
            except ImportError:  # ältere piper-tts-Versionen
                voice.synthesize(text, wav, length_scale=length_scale, speaker_id=speaker_id)
        return buf.getvalue()


class Speaker:
    """Wandelt gestreamte Antworten satzweise in Audio um und verteilt es an die Clients.
    Ohne Piper werden nur die Sätze geschickt – der Browser spricht sie dann selbst (Web Speech API)."""

    def __init__(self, tts: PiperTTS | None, send: Callable[[dict], Awaitable[None]],
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
            if self.tts and self.tts.available():
                try:
                    wav, rate = await asyncio.to_thread(self.tts.speak, text)
                    event["audio"] = base64.b64encode(wav).decode()
                    if rate:
                        event["rate"] = rate
                except Exception as e:  # noqa: BLE001
                    log.warning("TTS fehlgeschlagen: %s", e)
            if gen == self.generation:
                await self.send(event)
