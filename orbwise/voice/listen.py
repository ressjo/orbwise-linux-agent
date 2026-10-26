"""Spracheingabe: Wake-Word (openWakeWord, Modell aus voice.wakeword_model), Sprachaktivitätserkennung und Transkription.

Der Browser streamt 16-kHz-Mono-PCM (Int16) über den WebSocket; pro Verbindung gibt es eine
AudioSession, die Wake-Word erkennt, die Äußerung aufnimmt und mit Whisper transkribiert.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable

import numpy as np

from ..config import VoiceConfig

log = logging.getLogger(__name__)

SAMPLE_RATE = 16000
WAKE_FRAME = 1280           # 80 ms – Blockgröße für openWakeWord
VAD_FRAME = 480             # 30 ms – Blockgröße für webrtcvad


class WhisperSTT:
    def __init__(self, cfg: VoiceConfig):
        self.cfg = cfg
        self.language: str | None = cfg.language or None  # None = Whisper erkennt die Sprache selbst
        self._model = None
        self.error: str | None = None

    def available(self) -> bool:
        try:
            import faster_whisper  # noqa: F401
        except ImportError:
            self.error = "faster-whisper ist nicht installiert"
            return False
        return True

    def _load(self):
        if self._model is None:
            from faster_whisper import WhisperModel
            log.info("Lade Whisper-Modell '%s' (%s/%s) …", self.cfg.stt_model, self.cfg.stt_device,
                     self.cfg.stt_compute_type)
            self._model = WhisperModel(self.cfg.stt_model, device=self.cfg.stt_device,
                                       compute_type=self.cfg.stt_compute_type)
        return self._model

    def warmup(self) -> None:
        self._load()

    def transcribe(self, pcm: np.ndarray) -> str:
        audio = pcm.astype(np.float32) / 32768.0
        segments, _ = self._load().transcribe(
            audio, language=self.language, beam_size=3, vad_filter=True,
            initial_prompt="Orbwise, Linux, Arch, pacman, NAS.",
        )
        return " ".join(s.text.strip() for s in segments).strip()


class WakeWordFactory:
    def __init__(self, cfg: VoiceConfig):
        self.cfg = cfg
        self.error: str | None = None
        self._ok: bool | None = None

    def available(self) -> bool:
        if self._ok is None:
            try:
                self.create()
                self._ok = True
            except Exception as e:  # noqa: BLE001
                self.error = f"openWakeWord nicht verfügbar: {e}"
                self._ok = False
        return self._ok

    def create(self):
        from openwakeword.model import Model
        return Model(wakeword_models=[self.cfg.wakeword_model], inference_framework="onnx")


class EnergyVAD:
    """Einfacher Fallback, falls webrtcvad fehlt: adaptiver Rauschpegel."""

    def __init__(self):
        self.noise = 300.0

    def is_speech(self, frame: bytes, rate: int) -> bool:
        x = np.frombuffer(frame, dtype=np.int16).astype(np.float32)
        rms = float(np.sqrt(np.mean(x * x))) if len(x) else 0.0
        speech = rms > max(3.0 * self.noise, 500.0)
        if not speech:
            self.noise = 0.95 * self.noise + 0.05 * rms
        return speech


def make_vad():
    try:
        import webrtcvad
        return webrtcvad.Vad(2)
    except ImportError:
        return EnergyVAD()


class AudioSession:
    """Zustände: idle → (Wake-Word | Push-to-talk | listen) → recording → transcribing → idle"""

    def __init__(self, cfg: VoiceConfig, stt: WhisperSTT | None, wake: WakeWordFactory | None,
                 send: Callable[[dict], Awaitable[None]], on_text: Callable[[str], Awaitable[None]]):
        self.cfg = cfg
        self.stt = stt
        self.wake_factory = wake
        self.send = send
        self.on_text = on_text
        self.wake_enabled = False
        self.wake_model = None
        self.mode = "idle"            # idle | recording | transcribing
        self.ptt = False
        self.vad = make_vad()
        self.pending = np.zeros(0, dtype=np.int16)
        self.recorded: list[np.ndarray] = []
        self.speech_seen = False
        self.silence_ms = 0
        self.recorded_ms = 0
        self.task: asyncio.Task | None = None

    async def set_wake(self, enabled: bool) -> None:
        if enabled and self.wake_factory and self.wake_model is None:
            try:
                self.wake_model = await asyncio.to_thread(self.wake_factory.create)
            except Exception as e:  # noqa: BLE001
                await self.send({"type": "voice", "state": "error", "text": f"Wake-Word nicht verfügbar: {e}"})
                enabled = False
        self.wake_enabled = enabled
        await self.send({"type": "voice", "state": "wake_on" if enabled else "wake_off"})

    async def start_recording(self, ptt: bool = False) -> None:
        if self.mode == "transcribing":
            return
        self.mode = "recording"
        self.ptt = ptt
        self.recorded = []
        self.speech_seen = False
        self.silence_ms = 0
        self.recorded_ms = 0
        await self.send({"type": "voice", "state": "recording"})

    async def stop_recording(self) -> None:
        if self.mode == "recording":
            await self._finish()

    async def cancel(self) -> None:
        self.mode = "idle"
        self.recorded = []
        await self.send({"type": "voice", "state": "idle"})

    async def feed(self, data: bytes) -> None:
        if len(data) % 2:
            data = data[:-1]
        samples = np.frombuffer(data, dtype=np.int16)
        self.pending = np.concatenate([self.pending, samples])
        while len(self.pending) >= WAKE_FRAME:
            frame, self.pending = self.pending[:WAKE_FRAME], self.pending[WAKE_FRAME:]
            await self._process(frame)

    async def _process(self, frame: np.ndarray) -> None:
        if self.mode == "idle" and self.wake_enabled and self.wake_model is not None:
            scores = self.wake_model.predict(frame)
            if max(scores.values(), default=0) >= self.cfg.wakeword_threshold:
                self.wake_model.reset()
                await self.send({"type": "voice", "state": "wake"})
                await self.start_recording()
            return
        if self.mode != "recording":
            return
        self.recorded.append(frame)
        self.recorded_ms += 80
        if self.ptt:
            if self.recorded_ms >= self.cfg.max_record_seconds * 1000 * 2:
                await self._finish()
            return
        raw = frame.tobytes()
        voiced = any(self.vad.is_speech(raw[i:i + VAD_FRAME * 2], SAMPLE_RATE)
                     for i in range(0, len(raw) - VAD_FRAME * 2 + 1, VAD_FRAME * 2))
        if voiced:
            self.speech_seen = True
            self.silence_ms = 0
        else:
            self.silence_ms += 80
        if not self.speech_seen and self.recorded_ms >= self.cfg.no_speech_timeout_seconds * 1000:
            await self.send({"type": "voice", "state": "timeout"})
            await self.cancel()
        elif self.speech_seen and self.silence_ms >= self.cfg.silence_ms:
            await self._finish()
        elif self.recorded_ms >= self.cfg.max_record_seconds * 1000:
            await self._finish()

    async def _finish(self) -> None:
        audio = np.concatenate(self.recorded) if self.recorded else np.zeros(0, dtype=np.int16)
        self.recorded = []
        if len(audio) < SAMPLE_RATE * 0.3 or not self.stt:
            await self.cancel()
            return
        self.mode = "transcribing"
        await self.send({"type": "voice", "state": "transcribing"})
        self.task = asyncio.create_task(self._transcribe(audio))

    async def _transcribe(self, audio: np.ndarray) -> None:
        text = ""
        try:
            text = await asyncio.to_thread(self.stt.transcribe, audio)
        except Exception as e:  # noqa: BLE001
            log.exception("Transkription fehlgeschlagen")
            await self.send({"type": "voice", "state": "error", "text": f"Spracherkennung fehlgeschlagen: {e}"})
        finally:
            # auch bei Abbruch: sonst bleibt „Transkribiere“ stehen und neue Aufnahmen werden ignoriert
            self.mode = "idle"
            with contextlib.suppress(Exception):
                await self.send({"type": "voice", "state": "idle"})
        if text:
            await self.send({"type": "transcript", "text": text})
            await self.on_text(text)
