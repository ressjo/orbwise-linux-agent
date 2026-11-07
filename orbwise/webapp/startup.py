"""Startanzeige: was beim Start gerade lädt (Gedächtnis, Modell, Stimme …)."""

from __future__ import annotations

from ..config import Config

STARTUP_STEPS = [("memory", "Gedächtnis", "Memory"), ("model", "Sprachmodell", "Language model"),
                 ("stt", "Spracherkennung", "Speech recognition"), ("tts", "Sprachausgabe", "Speech output"),
                 ("wake", "Wake-Word", "Wake word"), ("telegram", "Telegram", "Telegram")]


class Startup:
    """Was beim Start von Orbwise noch lädt – für die Startanzeige der Oberfläche (live per WebSocket).
    Zustände: pending · running · ok · warn · error · off."""

    def __init__(self, cfg: Config, notify):
        en = cfg.language == "en"
        self.notify = notify
        self.steps = {k: {"key": k, "label": e if en else d, "state": "pending", "text": ""} for k, d, e in STARTUP_STEPS}

    @property
    def ready(self) -> bool:
        return all(s["state"] not in ("pending", "running") for s in self.steps.values())

    def snapshot(self) -> dict:
        return {"type": "startup", "steps": list(self.steps.values()), "ready": self.ready}

    async def set(self, key: str, state: str, text: str = "") -> None:
        self.steps[key].update(state=state, text=text)
        await self.notify(self.snapshot())
