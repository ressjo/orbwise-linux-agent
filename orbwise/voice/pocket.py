"""Pocket TTS (Kyutai) als zweite Sprachausgabe neben Piper – deutlich natürlicher, läuft auf der CPU.

Orbwise nutzt das fertige Programm aus den GitHub-Releases (Rust, ohne Python/PyTorch) und startet es als lokalen
Server (`pocket-tts serve`). Das Sprachmodell (~100 Mio. Parameter) und die Stimmen lädt das Programm beim ersten Start
selbst von Hugging Face (Cache wie bei `huggingface-cli`). Stimmen sind vorberechnete Sprecher-Profile aus Kyutais
Katalog – sie sprechen jede Sprache des geladenen Modells, mit der Klangfarbe des Originals.

Stimmennamen in Orbwise: „pocket:<name>“ (z. B. pocket:juergen), damit sie neben den Piper-Stimmen stehen können.
"""

from __future__ import annotations

import hashlib
import io
import logging
import os
import platform
import shutil
import signal
import socket
import stat
import subprocess
import tarfile
import threading
import time
import wave
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path

import httpx

log = logging.getLogger(__name__)

REPO = "kyutai-labs/pocket-tts"
PREFIX = "pocket:"
START_TIMEOUT_S = 900  # erster Start lädt das Modell herunter
SYNTH_TIMEOUT_S = 120
# Orbwise-Sprache → Pocket-Modell (die „_24l“-Varianten sind größer, langsamer und noch Vorschau)
LANGUAGES = {"de": "german", "en": "english", "fr": "french", "es": "spanish", "it": "italian",
             "pt": "portuguese", "nl": "dutch"}
# Kyutais eigene Standardstimme je Sprache – aus einer Aufnahme in dieser Sprache, klingt daher ohne Akzent
NATIVE = {"de": "juergen", "en": "alba", "fr": "estelle", "es": "lola", "it": "giovanni", "pt": "rafael",
          "nl": "daan"}

Out = Callable[[str], None]


@dataclass(frozen=True)
class PocketVoice:
    name: str
    male: bool
    origin: str  # Sprache der Originalaufnahme

    @property
    def label(self) -> str:
        return self.name.replace("_", " ").title()


# Kyutais Stimmenkatalog (pocket_tts.utils.utils._ORIGINS_OF_PREDEFINED_VOICES), Lizenzen: huggingface.co/kyutai/tts-voices
VOICES = [
    PocketVoice("juergen", True, "de"),
    PocketVoice("alba", False, "en"), PocketVoice("marius", True, "en"), PocketVoice("javert", True, "en"),
    PocketVoice("jean", True, "en"), PocketVoice("cosette", False, "en"), PocketVoice("anna", False, "en"),
    PocketVoice("vera", False, "en"), PocketVoice("fantine", False, "en"), PocketVoice("charles", True, "en"),
    PocketVoice("paul", True, "en"), PocketVoice("eponine", False, "en"), PocketVoice("azelma", False, "en"),
    PocketVoice("george", True, "en"), PocketVoice("mary", False, "en"), PocketVoice("jane", False, "en"),
    PocketVoice("michael", True, "en"), PocketVoice("eve", False, "en"), PocketVoice("bill_boerst", True, "en"),
    PocketVoice("peter_yearsley", True, "en"), PocketVoice("stuart_bell", True, "en"),
    PocketVoice("caro_davy", False, "en"), PocketVoice("estelle", False, "fr"), PocketVoice("giovanni", True, "it"),
    PocketVoice("lola", False, "es"), PocketVoice("rafael", True, "pt"), PocketVoice("daan", True, "nl"),
]
BY_NAME = {v.name: v for v in VOICES}

ORIGIN_NAMES = {"de": ("Deutsch", "German"), "en": ("Englisch", "English"), "fr": ("Französisch", "French"),
                "es": ("Spanisch", "Spanish"), "it": ("Italienisch", "Italian"), "pt": ("Portugiesisch", "Portuguese"),
                "nl": ("Niederländisch", "Dutch")}


def is_pocket(name: str | None) -> bool:
    return bool(name) and name.startswith(PREFIX)


def bare(name: str) -> str:
    return name.removeprefix(PREFIX)


def model_language(language: str) -> str:
    return LANGUAGES.get(language, "english")


def voice_list(language: str, current: str, installed: bool) -> list[dict]:
    """Stimmen für das Menü: die Stimme aus der eigenen Sprache zuerst (klingt ohne Akzent), dann alle anderen."""
    en = language == "en"
    native = NATIVE.get(language, "alba")
    out = []
    for v in sorted(VOICES, key=lambda v: (v.name != native, v.origin != language, v.label)):
        origin = ORIGIN_NAMES[v.origin][1 if en else 0]
        if v.name == native:
            desc = "Recorded in your language – no accent" if en else "Aus einer deutschen Aufnahme – ohne Akzent"
        else:
            desc = f"Voice from a {origin} recording" if en else f"Klangfarbe aus einer Aufnahme auf {origin}"
        out.append({**asdict(v), "name": PREFIX + v.name, "label": v.label, "description": desc,
                    "engine": "pocket", "recommended": v.name == native, "installed": installed,
                    "current": PREFIX + v.name == current, "locale": v.origin, "speaker": "", "download_mb": None})
    return out


# ---------------------------------------------------------------- Installation
def home(data_dir: Path) -> Path:
    return data_dir / "pocket-tts"


def binary(data_dir: Path) -> Path:
    return home(data_dir) / "bin" / "pocket-tts"


def is_installed(data_dir: Path) -> bool:
    return binary(data_dir).exists()


def target_triple() -> str | None:
    machine = platform.machine().lower()
    if machine in ("x86_64", "amd64"):
        return "x86_64-unknown-linux-gnu"
    if machine in ("aarch64", "arm64"):
        return "aarch64-unknown-linux-gnu"
    return None


def cpu_supported(cpuinfo: str | None = None) -> bool:
    """Die x86-Builds brauchen AVX2 (x86-64-v3) – auf CPUs ohne startet das Programm nicht."""
    if target_triple() != "x86_64-unknown-linux-gnu":
        return target_triple() is not None
    if cpuinfo is None:
        try:
            cpuinfo = Path("/proc/cpuinfo").read_text(encoding="utf-8", errors="replace")
        except OSError:
            return True  # nicht prüfbar – versuchen
    return " avx2" in cpuinfo


def pick_asset(assets: list[dict], triple: str) -> tuple[dict | None, dict | None]:
    """(Archiv, SHA256SUMS) aus den Release-Assets."""
    archive = next((a for a in assets if a.get("name", "").endswith(f"-{triple}.tar.gz")), None)
    sums = next((a for a in assets if a.get("name") == "SHA256SUMS"), None)
    return archive, sums


def expected_sha256(sums: str, filename: str) -> str | None:
    for line in sums.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1].lstrip("*") == filename:
            return parts[0].lower()
    return None


def install(data_dir: Path, out: Out = print, client: httpx.Client | None = None) -> bool:
    """Neuestes fertiges Programm von GitHub nach <data>/pocket-tts/bin (wenige MB). Prüfsumme wird kontrolliert."""
    triple = target_triple()
    if not triple or not cpu_supported():
        out("✘ Pocket TTS braucht eine x86-64-CPU mit AVX2 oder ARM64.")
        return False
    own = client is None
    client = client or httpx.Client(follow_redirects=True, timeout=httpx.Timeout(30, read=300),
                                    headers={"User-Agent": "orbwise"})
    try:
        r = client.get(f"https://api.github.com/repos/{REPO}/releases/latest")
        r.raise_for_status()
        release = r.json()
        archive, sums = pick_asset(release.get("assets") or [], triple)
        if not archive:
            out(f"✘ Kein fertiges Pocket-TTS-Programm für {triple} im Release {release.get('tag_name', '')}.")
            return False
        out(f"Lade {archive['name']} …")
        data = client.get(archive["browser_download_url"]).raise_for_status().content
        if sums:
            want = expected_sha256(client.get(sums["browser_download_url"]).raise_for_status().text, archive["name"])
            if want and hashlib.sha256(data).hexdigest() != want:
                out("✘ Prüfsumme stimmt nicht – Download verworfen.")
                return False
        target = binary(data_dir)
        target.parent.mkdir(parents=True, exist_ok=True)
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
            for member in tar.getmembers():  # nur die zwei bekannten Dateien, nie Pfade aus dem Archiv
                name = Path(member.name).name
                if member.isfile() and name in ("pocket-tts", "LICENSE"):
                    src = tar.extractfile(member)
                    dest = target if name == "pocket-tts" else target.parent.parent / "LICENSE"
                    tmp = dest.with_name(dest.name + ".part")
                    with src, tmp.open("wb") as f:
                        shutil.copyfileobj(src, f)
                    tmp.replace(dest)
        if not target.exists():
            out("✘ Im Archiv fehlt das Programm pocket-tts.")
            return False
        target.chmod(target.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP)
        (home(data_dir) / "version").write_text(str(release.get("tag_name") or ""), encoding="utf-8")
        out(f"✔ Pocket TTS {release.get('tag_name', '')} installiert.")
        return True
    except httpx.HTTPError as e:
        out(f"✘ Download fehlgeschlagen: {e}")
        return False
    finally:
        if own:
            client.close()


def hf_cache_dirs() -> list[Path]:
    hub = Path(os.environ.get("HF_HUB_CACHE") or Path(os.environ.get("HF_HOME") or "~/.cache/huggingface")
               .expanduser() / "hub")
    return [hub / "models--kyutai--pocket-tts", hub / "models--kyutai--pocket-tts-without-voice-cloning"]


def _size(path: Path) -> int:
    if path.is_file():
        return path.stat().st_size
    return sum(p.stat().st_size for p in path.rglob("*") if p.is_file() and not p.is_symlink())


def remove(data_dir: Path) -> int:
    """Programm, Modell und Stimmen löschen → freigegebene Bytes."""
    freed = 0
    for path in (home(data_dir), *hf_cache_dirs()):
        if path.exists():
            freed += _size(path)
            shutil.rmtree(path, ignore_errors=True)
    return freed


# ---------------------------------------------------------------- Server
def rewrap_wav(data: bytes) -> bytes:
    """Der Server streamt WAV mit Platzhalter-Länge (16 bit, mono) – für den Browser eine saubere Datei daraus machen."""
    if len(data) < 44 or data[:4] != b"RIFF" or data[8:12] != b"WAVE":
        raise ValueError("keine WAV-Antwort")
    rate = int.from_bytes(data[24:28], "little")
    pcm = data[44:]
    pcm = pcm[: len(pcm) - len(pcm) % 2]
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm)
    return buf.getvalue()


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class PocketTTS:
    """Hält einen `pocket-tts serve` im Hintergrund am Laufen und spricht darüber. Startet beim ersten Bedarf."""

    def __init__(self, data_dir: Path, language: str = "de", transport: httpx.BaseTransport | None = None):
        self.data_dir = data_dir
        self.language = language
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

    def command(self, voice: str) -> list[str]:
        return [str(binary(self.data_dir)), "serve", "--host", "127.0.0.1", "--port", str(self.port),
                "--language", model_language(self.language), "--default-voice", voice, "--quantize"]

    def ensure_running(self, voice: str = "", timeout: float = START_TIMEOUT_S) -> None:
        """Startet den Server (falls nötig) und wartet, bis er antwortet. Fehler → RuntimeError."""
        with self._lock:
            if self.proc and self.proc.poll() is None and self.state == "ready":
                return
            if not self.installed():
                self.state, self.error = "error", "Pocket TTS ist nicht installiert"
                raise RuntimeError(self.error)
            self._stop_locked()
            self.port = free_port()
            self.state, self.error = "starting", None
            voice = voice or NATIVE.get(self.language, "alba")
            env = {**os.environ, "RAYON_NUM_THREADS": os.environ.get("RAYON_NUM_THREADS", "4")}
            self.log_path().parent.mkdir(parents=True, exist_ok=True)
            with self.log_path().open("wb") as log_file:
                self.proc = subprocess.Popen(self.command(voice), stdout=log_file, stderr=subprocess.STDOUT,
                                             stdin=subprocess.DEVNULL, env=env, start_new_session=True)
            deadline = time.monotonic() + timeout
            with httpx.Client(timeout=2, transport=self.transport) as client:
                while time.monotonic() < deadline:
                    if self.proc.poll() is not None:
                        self.state = "error"
                        self.error = f"Pocket TTS beendet (Code {self.proc.returncode}): {self._log_tail()}"
                        self.proc = None
                        raise RuntimeError(self.error)
                    try:
                        if client.get(self.base_url + "/health").status_code == 200:
                            self.state = "ready"
                            log.info("Pocket TTS bereit (%s, Port %s)", model_language(self.language), self.port)
                            return
                    except httpx.HTTPError:
                        pass
                    time.sleep(0.3)
            self._stop_locked()
            self.state, self.error = "error", "Pocket TTS ist nicht rechtzeitig gestartet"
            raise RuntimeError(self.error)

    def warm(self, voice: str) -> None:
        """Server starten und die Stimme einmal benutzen (lädt ihr Profil) – im Hintergrund, Fehler nur ins Log."""
        def run():
            try:
                self.synth("Hallo." if self.language == "de" else "Hello.", voice)
            except Exception as e:  # noqa: BLE001
                log.warning("Pocket TTS nicht bereit: %s", e)
        threading.Thread(target=run, daemon=True, name="pocket-warm").start()

    def synth(self, text: str, voice: str) -> bytes:
        name = bare(voice)
        if name not in BY_NAME:
            raise ValueError(f"Unbekannte Pocket-Stimme: {voice}")
        self.ensure_running(name)
        with httpx.Client(timeout=SYNTH_TIMEOUT_S, transport=self.transport) as client:
            r = client.post(self.base_url + "/tts", data={"text": text, "voice_url": name})
        if r.status_code != 200:
            raise RuntimeError(f"Pocket TTS: HTTP {r.status_code} {r.text[:200]}")
        return rewrap_wav(r.content)

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
        version = ""
        try:
            version = (home(self.data_dir) / "version").read_text(encoding="utf-8").strip()
        except OSError:
            pass
        return {"installed": self.installed(), "state": self.state, "error": self.error, "version": version,
                "supported": cpu_supported(), "language": model_language(self.language)}
