"""Piper (Open Home Foundation) – schnelle Sprachausgabe auf der CPU, liest Zahlen, Datum und Uhrzeit richtig (espeak-ng).

Piper steht unter GPL-3.0, Orbwise unter MIT: Orbwise lädt Piper deshalb nie in den eigenen Prozess. Es bekommt eine
eigene Python-Umgebung (<data>/piper/venv, `piper-tts[http]` von PyPI) und läuft als lokaler Server
(`python -m piper.http_server`), den Orbwise per HTTP anspricht – wie Pocket TTS. Nur CPU (ONNX Runtime), damit der
Grafikspeicher für Sprach- und Bildmodell frei bleibt.

Stimmen kommen von huggingface.co/rhasspy/piper-voices (je Stimme eine .onnx mit .onnx.json, 20–120 MB) nach
<data>/piper/voices. Jede Stimme spricht genau ihre Sprache.
"""

from __future__ import annotations

import logging
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path

import httpx

log = logging.getLogger(__name__)

PACKAGE = "piper-tts[http]>=1.3,<2"
VOICES_REVISION = "v1.0.0"
START_TIMEOUT_S = 120
SYNTH_TIMEOUT_S = 60

Out = Callable[[str], None]
Runner = Callable[..., subprocess.CompletedProcess]


@dataclass(frozen=True)
class PiperVoice:
    id: str          # z. B. de_DE-thorsten-high
    label: str
    male: bool
    language: str    # de, en, …
    size_mb: int     # ungefähr
    note_de: str = ""
    note_en: str = ""

    @property
    def quality(self) -> str:
        return self.id.rsplit("-", 1)[-1]

    def url(self, base: str, suffix: str) -> str:
        code, name, quality = self.id.split("-", 2)
        family = code.split("_", 1)[0]
        return f"{base}/rhasspy/piper-voices/resolve/{VOICES_REVISION}/{family}/{code}/{name}/{quality}/{self.id}{suffix}"


# Auswahl aus rhasspy/piper-voices (high/medium klingen deutlich besser als low/x_low)
VOICES = [
    PiperVoice("de_DE-thorsten-high", "Thorsten", True, "de", 110, "klar und natürlich – empfohlen",
               "clear and natural – recommended"),
    PiperVoice("de_DE-thorsten-medium", "Thorsten (kompakt)", True, "de", 60, "etwas einfacher, halb so groß",
               "a bit simpler, half the size"),
    PiperVoice("de_DE-thorsten_emotional-medium", "Thorsten (lebhaft)", True, "de", 75, "betonter, lebendiger",
               "more expressive"),
    PiperVoice("de_DE-kerstin-low", "Kerstin", False, "de", 60, "weiblich, einfachere Qualität",
               "female, simpler quality"),
    PiperVoice("de_DE-ramona-low", "Ramona", False, "de", 60, "weiblich, einfachere Qualität",
               "female, simpler quality"),
    PiperVoice("de_DE-eva_k-x_low", "Eva", False, "de", 20, "weiblich, sehr klein", "female, very small"),
    PiperVoice("de_DE-karlsson-low", "Karlsson", True, "de", 60, "", ""),
    PiperVoice("de_DE-pavoque-low", "Pavoque", True, "de", 60, "", ""),
    PiperVoice("en_US-lessac-high", "Lessac", False, "en", 110, "clear US English – recommended",
               "clear US English – recommended"),
    PiperVoice("en_US-ryan-high", "Ryan", True, "en", 110, "US English", "US English"),
    PiperVoice("en_US-amy-medium", "Amy", False, "en", 60, "US English", "US English"),
    PiperVoice("en_GB-alba-medium", "Alba", False, "en", 60, "British English", "British English"),
    PiperVoice("fr_FR-siwis-medium", "Siwis", False, "fr", 60, "", ""),
    PiperVoice("fr_FR-tom-medium", "Tom", True, "fr", 60, "", ""),
    PiperVoice("es_ES-davefx-medium", "Davefx", True, "es", 60, "", ""),
    PiperVoice("it_IT-paola-medium", "Paola", False, "it", 60, "", ""),
    PiperVoice("pt_BR-faber-medium", "Faber", True, "pt", 60, "", ""),
    PiperVoice("nl_BE-nathalie-medium", "Nathalie", False, "nl", 60, "", ""),
]
BY_ID = {v.id: v for v in VOICES}
# Empfohlene Stimme je Sprache
NATIVE = {"de": "de_DE-thorsten-high", "en": "en_US-lessac-high", "fr": "fr_FR-siwis-medium",
          "es": "es_ES-davefx-medium", "it": "it_IT-paola-medium", "pt": "pt_BR-faber-medium",
          "nl": "nl_BE-nathalie-medium"}


# ---------------------------------------------------------------- Orte
def home(data_dir: Path) -> Path:
    return data_dir / "piper"


def python(data_dir: Path) -> Path:
    return home(data_dir) / "venv" / "bin" / "python"


def voices_dir(data_dir: Path) -> Path:
    return home(data_dir) / "voices"


def is_installed(data_dir: Path) -> bool:
    return python(data_dir).exists() and (home(data_dir) / "installed").exists()


def voice_path(data_dir: Path, voice_id: str) -> Path:
    return voices_dir(data_dir) / f"{voice_id}.onnx"


def has_voice(data_dir: Path, voice_id: str) -> bool:
    p = voice_path(data_dir, voice_id)
    return p.exists() and p.with_suffix(".onnx.json").exists()


def downloaded(data_dir: Path) -> list[str]:
    return [v.id for v in VOICES if has_voice(data_dir, v.id)]


def _hf_base() -> str:
    from ..models import HF_API  # HF_ENDPOINT gilt auch hier (Spiegel)
    return HF_API[:-4] if HF_API.endswith("/api") else "https://huggingface.co"


# ---------------------------------------------------------------- Installation
def install(data_dir: Path, out: Out = print, run: Runner = subprocess.run) -> bool:
    """Eigene Python-Umgebung mit piper-tts anlegen (getrennt von Orbwise – GPL). Mit uv, sonst venv + pip."""
    venv = home(data_dir) / "venv"
    home(data_dir).mkdir(parents=True, exist_ok=True)
    uv = shutil.which("uv")
    env = {**os.environ, "UV_PYTHON_PREFERENCE": os.environ.get("UV_PYTHON_PREFERENCE", "managed")}
    out("Richte Piper ein (eigene Python-Umgebung, ~60 MB) …")
    try:
        if uv:
            steps = [[uv, "venv", "--quiet", "--allow-existing", str(venv)],
                     [uv, "pip", "install", "--quiet", "--python", str(python(data_dir)), PACKAGE]]
        else:
            steps = [[sys.executable, "-m", "venv", str(venv)],
                     [str(python(data_dir)), "-m", "pip", "install", "--quiet", "--upgrade", PACKAGE]]
        for step in steps:
            r = run(step, capture_output=True, text=True, env=env, timeout=900)
            if r.returncode != 0:
                out("✘ Piper-Installation fehlgeschlagen: " + ((r.stderr or r.stdout or "").strip()[-400:]))
                return False
    except (OSError, subprocess.SubprocessError) as e:
        out(f"✘ Piper-Installation fehlgeschlagen: {e}")
        return False
    (home(data_dir) / "installed").write_text(PACKAGE + "\n", encoding="utf-8")
    out("✔ Piper installiert.")
    return True


def download_voice(data_dir: Path, voice_id: str, out: Out = print, client: httpx.Client | None = None,
                   stop: threading.Event | None = None) -> bool:
    """Stimme (.onnx + .onnx.json) laden – fortsetzbar."""
    from ..imagegen_setup import InstallCancelled, download
    voice = BY_ID.get(voice_id)
    if voice is None:
        out(f"✘ Unbekannte Piper-Stimme: {voice_id}")
        return False
    own = client is None
    client = client or httpx.Client(follow_redirects=True, timeout=httpx.Timeout(60, read=300),
                                    headers={"User-Agent": "orbwise"})
    target = voice_path(data_dir, voice_id)
    try:
        download(voice.url(_hf_base(), ".onnx.json"), target.with_suffix(".onnx.json"), client, out,
                 label=f"{voice.label} (Einstellungen)", stop=stop)
        download(voice.url(_hf_base(), ".onnx"), target, client, out, label=voice.label, stop=stop)
        return True
    except InstallCancelled:
        raise
    except httpx.HTTPError as e:
        out(f"✘ Stimme {voice.label} nicht geladen: {e} – erneut laden setzt fort.")
        return False
    finally:
        if own:
            client.close()


def remove_voice(data_dir: Path, voice_id: str) -> int:
    freed = 0
    for p in (voice_path(data_dir, voice_id), voice_path(data_dir, voice_id).with_suffix(".onnx.json")):
        for f in (p, p.with_name(p.name + ".part")):
            if f.is_file():
                freed += f.stat().st_size
                f.unlink()
    return freed


def remove(data_dir: Path) -> int:
    """Piper samt Umgebung und Stimmen löschen → freigegebene Bytes."""
    folder = home(data_dir)
    if not folder.exists():
        return 0
    freed = sum(f.stat().st_size for f in folder.rglob("*") if f.is_file() and not f.is_symlink())
    shutil.rmtree(folder, ignore_errors=True)
    return freed


def voice_list(ui_language: str, current: str, data_dir: Path) -> list[dict]:
    """Für das Menü: Stimmen der eigenen Sprache zuerst, die empfohlene ganz oben."""
    en = ui_language == "en"
    have = set(downloaded(data_dir))
    native = NATIVE.get(ui_language)
    out = []
    for v in sorted(VOICES, key=lambda v: (v.language != ui_language, v.id != native, v.label)):
        out.append({**asdict(v), "name": "piper:" + v.id, "quality": v.quality, "note": v.note_en if en else v.note_de,
                    "downloaded": v.id in have, "recommended": v.id == native,
                    "current": current == "piper:" + v.id})
    return out


# ---------------------------------------------------------------- Server
def free_port() -> int:
    import socket
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class PiperTTS:
    """Hält `python -m piper.http_server` (eigene Umgebung, nur CPU) am Laufen und spricht darüber."""

    def __init__(self, data_dir: Path, transport: httpx.BaseTransport | None = None):
        self.data_dir = data_dir
        self.transport = transport
        self.proc: subprocess.Popen | None = None
        self.port = 0
        self.state = "stopped"  # stopped | starting | ready | error
        self.error: str | None = None
        self._lock = threading.Lock()

    def installed(self) -> bool:
        return is_installed(self.data_dir)

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def log_path(self) -> Path:
        return home(self.data_dir) / "server.log"

    def _log_tail(self, lines: int = 6) -> str:
        try:
            return "\n".join(self.log_path().read_text(encoding="utf-8", errors="replace").splitlines()[-lines:])
        except OSError:
            return ""

    def command(self, voice_id: str) -> list[str]:
        return [str(python(self.data_dir)), "-m", "piper.http_server", "--host", "127.0.0.1", "--port", str(self.port),
                "-m", str(voice_path(self.data_dir, voice_id)), "--data-dir", str(voices_dir(self.data_dir))]

    def ensure_running(self, voice_id: str, timeout: float = START_TIMEOUT_S) -> None:
        with self._lock:
            if self.proc and self.proc.poll() is None and self.state == "ready":
                return
            if not self.installed():
                self.state, self.error = "error", "Piper ist nicht installiert"
                raise RuntimeError(self.error)
            if not has_voice(self.data_dir, voice_id):
                self.state, self.error = "error", f"Stimme {voice_id} ist nicht geladen"
                raise RuntimeError(self.error)
            self._stop_locked()
            self.port = free_port()
            self.state, self.error = "starting", None
            # nur CPU – der Grafikspeicher gehört Sprach- und Bildmodell
            env = {**os.environ, "CUDA_VISIBLE_DEVICES": "", "HIP_VISIBLE_DEVICES": "",
                   "OMP_NUM_THREADS": os.environ.get("OMP_NUM_THREADS", "4")}
            self.log_path().parent.mkdir(parents=True, exist_ok=True)
            with self.log_path().open("wb") as log_file:
                self.proc = subprocess.Popen(self.command(voice_id), stdout=log_file, stderr=subprocess.STDOUT,
                                             stdin=subprocess.DEVNULL, env=env, start_new_session=True)
            deadline = time.monotonic() + timeout
            with httpx.Client(timeout=2, transport=self.transport) as client:
                while time.monotonic() < deadline:
                    if self.proc.poll() is not None:
                        self.state = "error"
                        self.error = f"Piper beendet (Code {self.proc.returncode}): {self._log_tail()}"
                        self.proc = None
                        raise RuntimeError(self.error)
                    try:
                        if client.get(self.base_url + "/voices").status_code == 200:
                            self.state = "ready"
                            log.info("Piper bereit (Port %s)", self.port)
                            return
                    except httpx.HTTPError:
                        pass
                    time.sleep(0.2)
            self._stop_locked()
            self.state, self.error = "error", "Piper ist nicht rechtzeitig gestartet"
            raise RuntimeError(self.error)

    def warm(self, voice_id: str) -> None:
        def run():
            try:
                self.synth("Hallo.", voice_id)
            except Exception as e:  # noqa: BLE001
                log.warning("Piper nicht bereit: %s", e)
        threading.Thread(target=run, daemon=True, name="piper-warm").start()

    def synth(self, text: str, voice_id: str) -> bytes:
        if voice_id not in BY_ID:
            raise ValueError(f"Unbekannte Piper-Stimme: {voice_id}")
        self.ensure_running(voice_id)
        with httpx.Client(timeout=SYNTH_TIMEOUT_S, transport=self.transport) as client:
            r = client.post(self.base_url + "/synthesize", json={"text": text, "voice": voice_id})
        if r.status_code != 200:
            raise RuntimeError(f"Piper: HTTP {r.status_code} {r.text[:200]}")
        return r.content

    def _stop_locked(self) -> None:
        proc, self.proc = self.proc, None
        self.state = "stopped"
        if not proc or proc.poll() is not None:
            return
        try:
            os.killpg(proc.pid, signal.SIGTERM)
            proc.wait(timeout=5)
        except (ProcessLookupError, PermissionError):
            pass
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.wait(timeout=5)

    def stop(self) -> None:
        with self._lock:
            self._stop_locked()

    def describe(self) -> dict:
        return {"installed": self.installed(), "state": self.state, "error": self.error,
                "downloaded": downloaded(self.data_dir)}
