"""Einlese-Tempo lernen und den Prompt-Cache im Leerlauf vorwärmen: die nächste Frage liest dann nur ihren neuen Teil.

Mixin für Agent – nutzt dessen Zustand (self.llm, self.lock, self._state_path …)."""

from __future__ import annotations

import logging
import time
import uuid
from collections.abc import Awaitable, Callable

from . import state
from .llm import LLMError

log = logging.getLogger(__name__)
Emit = Callable[[dict], Awaitable[None]]

PREWARM_RETRY_S = 600  # nach einem Ladefehler so lange nicht vorwärmen (je Profil)


class SpeedMixin:
    def _load_prefill(self) -> dict[str, float]:
        raw = state.load(self._state_path).get("prefill_tps") or {}
        return {str(k): float(v) for k, v in raw.items() if isinstance(v, (int, float)) and v > 0} \
            if isinstance(raw, dict) else {}

    def _save_prefill(self) -> None:
        """Gelernte Geschwindigkeit merken – nur bei spürbarer Änderung (state.json teilt sich das mit dem Router)."""
        key = self._profile_key()
        tps, saved = self._prefill_tps.get(key), self._saved_tps.get(key)
        if not tps or (saved and abs(tps - saved) / saved < 0.1):
            return
        try:
            def remember(data: dict) -> None:
                data.setdefault("prefill_tps", {})[key] = tps
            state.update(self._state_path, remember)
            self._saved_tps[key] = tps
        except (OSError, ValueError) as e:
            log.info("Einlese-Geschwindigkeit nicht gespeichert: %s", e)

    def prefill_speed(self, profile: str) -> float | None:
        """Gemessene Einlese-Geschwindigkeit eines Profils (Token/s, geglättet) – für die Modellseite."""
        return self._prefill_tps.get(profile)

    def forget_prefill(self, profile: str) -> None:
        """Nach einer Änderung am Modell-Server (z. B. Batch-Größe) neu messen statt den alten Wert nachzuziehen."""
        self._prefill_tps.pop(profile, None)
        self._saved_tps.pop(profile, None)
        if profile in (state.load(self._state_path).get("prefill_tps") or {}):
            try:
                state.update(self._state_path, lambda data: (data.get("prefill_tps") or {}).pop(profile, None))
            except OSError as e:
                log.info("Einlese-Geschwindigkeit nicht vergessen: %s", e)

    def _learn_prefill(self, stats: dict) -> None:
        key = self._profile_key()
        if stats.get("prompt_total"):
            self._last_prompt[key] = int(stats["prompt_total"])
        tps, n = stats.get("prompt_tps"), stats.get("prompt_tokens") or 0
        if tps and n >= 200:  # kleine Häppchen messen eher die Latenz als die Geschwindigkeit
            old = self._prefill_tps.get(key)
            self._prefill_tps[key] = round(tps if old is None else 0.7 * old + 0.3 * tps, 1)
            self._save_prefill()

    def _reread_info(self, stats: dict) -> dict | None:
        """Grund fürs Neueinlesen des letzten Aufrufs, abgeglichen mit dem, was der Server aus dem Cache nahm."""
        from . import reread
        if self._reread is None:
            return None
        info = reread.with_server(self._reread, stats, self.api_used)
        self.api_used = False
        if info.get("new", 0) >= 5000 and info["reason"] != "appended":
            log.info("%s Token neu eingelesen – Grund: %s %s", info["new"], info["reason"],
                     {k: v for k, v in info.items() if k not in ("reason", "new")})
        return info

    def _cache_key(self) -> tuple:
        conv = self.memory.conversation
        return (self._profile_key(), conv.chat_id, len(conv.epochs))

    def cache_cold(self) -> bool:
        """Hat der Modell-Server gerade etwas anderes im Cache als den offenen Chat?"""
        return self._cache_key() != self._cache_owner

    def busy(self) -> bool:
        """Läuft eine Anfrage? (Vorwärmen zählt nicht – es blockiert nichts, die nächste Frage nutzt es.)"""
        return self.lock.locked() and not self._prewarming

    async def prewarm(self, emit: Emit | None = None, think=None) -> bool:
        """Den Prompt-Anfang des offenen Chats im Leerlauf einlesen lassen (nach Telegram/Routinen, Chatwechsel,
        Start oder sobald getippt wird) – die nächste Frage liest dann nur noch ihren neuen Teil ein.
        think: Denkmodus der nächsten Frage (Denken-Knopf) – mit anderem Modus rendert die Vorlage einen anderen
        Prompt, das Vorgewärmte wäre nutzlos."""
        if self.lock.locked() or getattr(self.llm, "switching", None) or not self.cache_cold() \
                or getattr(self.llm, "gpu_borrowed", None) \
                or getattr(self.llm, "unloaded", False):  # Bild-Modus hat den Grafikspeicher bzw. von Hand entladen
            return False
        failed = self._prewarm_failed
        if failed and failed[0] == self._profile_key() and time.monotonic() - failed[1] < PREWARM_RETRY_S:
            return False  # Modell ließ sich eben nicht laden – nicht bei jedem Tastendruck erneut abstürzen lassen
        async with self.lock:
            self._prewarming = True
            try:
                return await self._prewarm_locked(emit, think)
            finally:
                self._prewarming = False

    async def _prewarm_locked(self, emit: Emit | None = None, think=None) -> bool:
        if not self.cache_cold():
            return False
        key = self._cache_key()  # vorher merken – der Nutzer könnte währenddessen den Chat wechseln
        await self._check_vision()
        self._set_think(think)
        await self._fit_think()
        self.choose_tools(set())
        messages = self.build_messages(display=False)
        if messages[-1]["role"] != "user":
            # Leere Nutzernachricht ans Ende: so liegt der Verlauf samt letzter Antwort im Cache, wie die nächste
            # Frage ihn sendet. Eine Antwort am Ende setzt llama-server sonst als angefangene Antwort fort (anders
            # formatiert, bei Werkzeug-Aufrufen ein Fehler); ein leerer Chat braucht für manche Vorlagen ohnehin eine.
            messages.append({"role": "user", "content": ""})
        pid, started = uuid.uuid4().hex[:8], time.monotonic()
        tokens = self._full_prompt
        if emit:
            phase = {"type": "llm_phase", "id": pid, "phase": "prewarm", "tokens": tokens}
            tps = self._prefill_tps.get(self._profile_key())
            if tps:  # höchstens so lange – liegt der Anfang (Systemprompt, Werkzeuge) im Cache, geht es schneller
                phase["eta_s"] = round(tokens / tps, 1)
            await emit(phase)
        stats: dict = {}
        try:
            async for ev in self._call_llm(messages, self.schemas, max_tokens=1, **self._think_kwargs()):
                if ev["type"] == "done":
                    stats = ev.get("stats") or {}
        except Exception as e:  # noqa: BLE001 – Vorwärmen ist nur eine Beschleunigung
            log.info("Vorwärmen übersprungen: %s", e)
            if isinstance(e, LLMError):  # Modell lädt nicht → eine Weile nicht mehr probieren (eine Frage schon)
                self._prewarm_failed = (self._profile_key(), time.monotonic())
            if emit:
                await emit({"type": "llm_phase", "id": pid, "phase": "done", "prewarm": True, "error": True,
                            "seconds": round(time.monotonic() - started, 1)})
            return False
        self._learn_prefill(stats)
        self._cache_owner = key
        info = self._reread_info(stats)
        if emit:
            done = {"type": "llm_phase", "id": pid, "phase": "done", "prewarm": True, "reread": info,
                    "tokens": stats.get("prompt_total") or tokens, "seconds": round(time.monotonic() - started, 1)}
            if stats.get("prompt_tokens") is not None and stats.get("prompt_total"):
                done["new"] = stats["prompt_tokens"]  # wirklich eingelesen – der Rest kam aus dem Cache
            await emit(done)
        return True
