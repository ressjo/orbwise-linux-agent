"""Bonsai 2 27B automatisch einrichten: offizielles Bonsai-demo-Repo (PrismML-Fork von llama.cpp + Modell-Download)
nach ~/bonsai holen, `setup.sh` ausführen und ein Orbwise-Profil anlegen, das den llama-server selbst startet.

Bonsai 2 läuft nur mit den llama.cpp-Binaries aus dem PrismML-Fork – `setup.sh` lädt die passenden für
CUDA/ROCm/Vulkan/CPU. Die Modelle sind öffentlich (kein Hugging-Face-Token nötig).
"""

from __future__ import annotations

import os
import re
import secrets
import shutil
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

from .config import env as setting_env
from .lang import T

REPO = "https://github.com/PrismML-Eng/Bonsai-demo.git"
START_SCRIPT = "scripts/start_llama_server.sh"
PROFILE_NAME = "bonsai"
# Kompakte Variante: dieselben 27B, aber 1,75 statt 2,13 Bit pro Gewicht (5,9 statt 7,2 GB) – auf 8-GB-Karten bleibt
# Platz für 16k Kontext. Liest den Prompt etwas langsamer ein. Eigener Ordner, damit sich die Varianten nicht stören.
COMPACT_NAME = "bonsai-kompakt"
COMPACT_REPO = "prism-ml/Ternary-Bonsai-2-27B-gguf"
COMPACT_FILE = "Ternary-Bonsai-2-27B-PTQ1_0.gguf"
COMPACT_DIR = "models/bonsai2-ptq1"
VARIANTS = (PROFILE_NAME, COMPACT_NAME)
CUDA_SERVER = "bin/cuda/llama-server"
CUDA_LIBS = "cuda-libs"
_NOT_FOUND = re.compile(r"^\s*(\S+)\s+=>\s+not found", re.M)


def bonsai_dir() -> Path:
    return Path(setting_env("BONSAI_DIR") or Path.home() / "bonsai").expanduser()


def model_files(directory: Path | None = None, variant: str = PROFILE_NAME) -> list[Path]:
    """Modelldateien einer Variante (Standard: alles außer der kompakten, kompakt: nur deren Ordner)."""
    d = directory or bonsai_dir()
    compact = d / COMPACT_DIR
    if variant == COMPACT_NAME:
        return sorted(compact.glob("*.gguf")) if compact.is_dir() else []
    models = d / "models"
    return sorted(f for f in models.rglob("*.gguf") if compact not in f.parents) if models.is_dir() else []


def remove_model_files(directory: Path | None = None, variant: str = PROFILE_NAME) -> int:
    """Heruntergeladene Modelldateien einer Variante löschen (~7 bzw. ~6 GB) – der Bonsai-Ordner mit dem Server
    bleibt. Liefert die freigegebenen Bytes."""
    freed = 0
    for f in model_files(directory, variant):
        freed += f.stat().st_size
        f.unlink()
    return freed


def is_set_up(directory: Path | None = None, variant: str = PROFILE_NAME) -> bool:
    d = directory or bonsai_dir()
    return (d / START_SCRIPT).exists() and bool(model_files(d, variant))


def hsa_override(gpu_name: str) -> str:
    """RDNA2/RDNA3-Karten ohne offiziellen ROCm-Support (RX 6600/6650/6700, RX 7600) brauchen einen Override."""
    if re.search(r"navi 2[234]", gpu_name, re.I):
        return "10.3.0"
    if re.search(r"navi 3[23]", gpu_name, re.I):
        return "11.0.0"
    return ""


def make_profile(gpu: dict, directory: Path, api_key: str | None = None, lib_path: str = "",
                 compact: bool = False) -> dict:
    """Profil passend zur Grafikkarte: wenig VRAM → komprimierter KV-Cache, Bildmodul in den RAM, kleiner Kontext.
    compact: die 1,75-Bit-Variante (1,3 GB kleiner) – auf 8-GB-Karten mit 16k statt 8k Kontext."""
    vram = float(gpu.get("vram_gb") or 0)
    key = api_key or secrets.token_hex(20)
    env: dict[str, str] = {}
    if gpu.get("vendor") == "amd" and hsa_override(gpu.get("name", "")):
        env["HSA_OVERRIDE_GFX_VERSION"] = hsa_override(gpu["name"])
    if compact:
        env["BONSAI_GGUF"] = f"{COMPACT_DIR}/{COMPACT_FILE}"  # relativ zum Bonsai-Ordner; ohne Bildmodul
    if vram <= 9:
        env.update({"BONSAI_CTX": "16384" if compact else "8192", "BONSAI_KV4": "1", "BONSAI_MMPROJ_CPU": "1"})
    elif vram <= 13:
        env.update({"BONSAI_CTX": "32768", "BONSAI_MMPROJ_CPU": "1"})
    else:
        env["BONSAI_CTX"] = "65536"
    if lib_path:  # CUDA-Laufzeit aus ~/bonsai/cuda-libs (siehe ensure_cuda_libs)
        env["LD_LIBRARY_PATH"] = lib_path
    tight = vram <= 13
    home = str(Path.home())
    script = str(directory / START_SCRIPT)
    if script.startswith(home + "/"):
        script = "~" + script[len(home):]
    return {
        "label": "Bonsai 2 27B kompakt" if compact else "Bonsai 2 27B",
        "backend": "openai",
        "base_url": "http://127.0.0.1:8080/v1",
        "model": "bonsai",
        "api_key": key,
        "embed_on_cpu": tight,
        "unload_ollama": tight,
        "server": {"command": f"{script} -np 1 --api-key {key}", "env": env, "startup_timeout": 300},
    }


Runner = Callable[..., subprocess.CompletedProcess]


def setup(directory: Path | None = None, run: Runner = subprocess.run, out=print, skip_model: bool = False) -> bool:
    """Repo holen/aktualisieren und setup.sh ausführen (lädt Binaries und ~7 GB Modell – mit skip_model nur die
    Binaries, z. B. für die kompakte Variante)."""
    d = directory or bonsai_dir()
    if not shutil.which("git"):
        out(T("git fehlt – bitte installieren.", "git is missing – please install it."))
        return False
    if (d / ".git").exists():
        out(T(f"Aktualisiere {d} …", f"Updating {d} …"))
        run(["git", "-C", str(d), "pull", "--ff-only"])
    elif d.exists() and any(d.iterdir()):
        out(T(f"{d} existiert bereits und ist kein Bonsai-demo-Checkout – bitte umbenennen oder ORBWISE_BONSAI_DIR setzen.",
              f"{d} already exists and is not a Bonsai-demo checkout – rename it or set ORBWISE_BONSAI_DIR."))
        return False
    else:
        out(T(f"Lade Bonsai-demo nach {d} …", f"Cloning Bonsai-demo to {d} …"))
        if run(["git", "clone", "--depth", "1", REPO, str(d)]).returncode != 0:
            return False
    out(T("Richte Bonsai ein (llama.cpp-Binaries + Modell, ~7 GB – dauert eine Weile) …",
          "Setting up Bonsai (llama.cpp binaries + model, ~7 GB – takes a while) …"))
    env = {**os.environ, "BONSAI_OPENWEBUI": "0", "BONSAI_CODE_INTERPRETER": "0"}
    if skip_model:
        env["BONSAI_SKIP_GGUF"] = "1"
    result = run(["sh", "./setup.sh"], cwd=str(d), env=env)
    if result.returncode != 0 or not (d / START_SCRIPT).exists():
        out(T("✘ Bonsai-Einrichtung fehlgeschlagen – Details siehe oben (FAQ: ~/bonsai/FAQ.md).",
              "✘ Bonsai setup failed – see the output above (FAQ: ~/bonsai/FAQ.md)."))
        return False
    return True


# ---------------------------------------------------------------- CUDA-Laufzeit (NVIDIA)
# Die fertigen CUDA-Builds von llama.cpp bringen libcudart/libcublas nicht mit, sondern erwarten das
# CUDA-Toolkit auf dem System. Mit nur dem NVIDIA-Treiber fehlen sie → offizielle NVIDIA-Pakete von PyPI
# nach ~/bonsai/cuda-libs laden (kein root, jede Distribution) und per LD_LIBRARY_PATH einbinden.

def missing_libs(binary: Path, lib_path: str = "", run: Runner = subprocess.run) -> list[str]:
    """Bibliotheken, die der Loader für `binary` nicht findet (leer = alles da oder ldd fehlt)."""
    env = dict(os.environ)
    if lib_path:
        env["LD_LIBRARY_PATH"] = lib_path + (":" + env["LD_LIBRARY_PATH"] if env.get("LD_LIBRARY_PATH") else "")
    try:
        result = run(["ldd", str(binary)], capture_output=True, text=True, env=env)
    except OSError:
        return []
    return sorted(set(_NOT_FOUND.findall(result.stdout or "")))


def driver_cuda_version(run: Runner = subprocess.run) -> tuple[int, int] | None:
    """Höchste CUDA-Version, die der installierte Treiber kann (nvidia-smi: 'CUDA Version: 12.8')."""
    try:
        text = run(["nvidia-smi"], capture_output=True, text=True).stdout or ""
    except OSError:
        return None
    m = re.search(r"CUDA Version:\s*(\d+)\.(\d+)", text)
    return (int(m.group(1)), int(m.group(2))) if m else None


def cuda_packages(missing: list[str], driver: tuple[int, int] | None = None) -> list[str]:
    """PyPI-Pakete passend zur CUDA-Hauptversion der fehlenden Bibliotheken (libcudart.so.12 → cu12) –
    nicht neuer als der Treiber kann, sonst meldet CUDA „driver version is insufficient“."""
    majors = {m.group(1) for name in missing if (m := re.match(r"libcu\w*\.so\.(\d+)", name))}
    if majors == {"12"}:
        pin = f"<12.{driver[1] + 1}" if driver and driver[0] == 12 else ""
        return [f"nvidia-cuda-runtime-cu12{pin}", f"nvidia-cublas-cu12{pin}"]
    if majors == {"13"}:
        pin = f">=13,<13.{driver[1] + 1}" if driver and driver[0] == 13 else "==13.*"
        return [f"nvidia-cuda-runtime{pin}", f"nvidia-cublas{pin}"]
    return []


def lib_dirs(root: Path) -> str:
    """Alle Ordner unter `root`, die Bibliotheken enthalten – als LD_LIBRARY_PATH-Wert."""
    if not root.is_dir():
        return ""
    return ":".join(sorted({str(f.parent) for f in root.rglob("lib*.so*") if f.is_file()}))


def ensure_cuda_libs(directory: Path | None = None, run: Runner = subprocess.run, out=print) -> str | None:
    """Leerer Text = nichts nötig (kein CUDA-Build oder System-CUDA da), Pfad = LD_LIBRARY_PATH, None = Fehler."""
    d = directory or bonsai_dir()
    binary = d / CUDA_SERVER
    if not binary.exists():
        return ""
    missing = missing_libs(binary, run=run)
    if not missing:
        return ""
    target = d / CUDA_LIBS
    path = lib_dirs(target)
    if path and not missing_libs(binary, path, run=run):
        return path  # schon früher geladen
    packages = cuda_packages(missing, driver_cuda_version(run))
    if not packages:
        out(T(f"✘ llama-server findet diese Bibliotheken nicht: {', '.join(missing)}",
              f"✘ llama-server cannot find these libraries: {', '.join(missing)}"))
        return None
    out(T(f"Lade die CUDA-Laufzeit für llama-server ({', '.join(missing)} fehlen; ~0,8 GB, ohne root) …",
          f"Downloading the CUDA runtime for llama-server ({', '.join(missing)} missing; ~0.8 GB, no root) …"))
    uv = shutil.which("uv")
    cmd = ([uv, "pip", "install", "--python", sys.executable] if uv else [sys.executable, "-m", "pip", "install"])
    run([*cmd, "--upgrade", "--target", str(target), *packages])
    path = lib_dirs(target)
    still = missing_libs(binary, path, run=run) if path else missing
    if still:
        out(T(f"✘ Weiterhin fehlend: {', '.join(still)}. Alternativ das CUDA-Toolkit der Distribution installieren "
              f"oder in {d} scripts/build_cuda_linux.sh ausführen.",
              f"✘ Still missing: {', '.join(still)}. Alternatively install your distribution's CUDA toolkit "
              f"or run scripts/build_cuda_linux.sh in {d}."))
        return None
    out(T("✔ CUDA-Laufzeit eingerichtet.", "✔ CUDA runtime set up."))
    return path


# ---------------------------------------------------------------- Prompt-Cache für Hybrid-Modelle
# Bonsai 2 ist ein Hybrid-Modell (Attention + rekurrente Schichten): Einen früheren Prompt kann llama-server nur über
# gespeicherte Zwischenstände (Checkpoints) weiterverwenden – sonst liest er nach jeder Änderung (Ausblenden,
# Zusammenfassen, Telegram dazwischen) viel neu ein. Der Bonsai-Starter setzt dafür nichts, reicht zusätzliche
# Argumente aber an llama-server durch (siehe Bonsai-demo PROMPT-CACHE.md).
CACHE_CHECKPOINTS = 32
_HELP: dict[str, str] = {}


def _mem_mib() -> int:
    try:
        with open("/proc/meminfo", encoding="utf-8") as f:
            for line in f:
                if line.startswith("MemTotal:"):
                    return int(line.split()[1]) // 1024
    except (OSError, ValueError, IndexError):
        pass
    return 0


def _server_help(directory: Path, env: dict | None, run: Runner, binaries: list[Path] | None = None) -> str:
    """Hilfetext eines llama-server aus dem Bonsai-Ordner bzw. der angegebenen Programmdatei (einmal je Ort) – leer,
    wenn keiner startet."""
    key = str(binaries[0]) if binaries else str(directory)
    if key not in _HELP:
        text = ""
        for binary in binaries or sorted((directory / "bin").glob("*/llama-server")):
            try:
                result = run([str(binary), "--help"], capture_output=True, text=True, timeout=30,
                             env={**os.environ, **(env or {})})
            except (OSError, subprocess.SubprocessError):
                continue
            text = (result.stdout or "") + (result.stderr or "")
            if "--ctx-size" in text or "-c," in text:
                break
            text = ""
        _HELP[key] = text
    return _HELP[key]


def cache_flags(command: str, env: dict | None = None, run: Runner = subprocess.run, mem_mib: int | None = None) -> str:
    """Zusätzliche Argumente für den Bonsai-Starter bzw. einen direkt gestarteten llama-server: mehr Checkpoints, ein Prompt-Cache im RAM (höchstens 4 GB bzw.
    15 % des Arbeitsspeichers) und das Sichern ruhender Slots. Nur Optionen, die der vorhandene llama-server kennt,
    und nur, wenn der Befehl sie nicht schon selbst setzt – sonst leer."""
    m = re.search(r"(\S*start_llama_server\.sh)\b", command)
    direct = re.match(r"\s*(?:\S+=\S*\s+)*(\S*llama-server)(\s|$)", command)  # llama-server direkt (eigenes Modell)
    if m:
        help_text = _server_help(Path(os.path.expanduser(m.group(1))).parent.parent, env, run)
    elif direct:
        binary = Path(os.path.expanduser(direct.group(1)))
        if not binary.is_absolute():
            binary = Path(shutil.which(str(binary)) or binary)
        help_text = _server_help(binary.parent, env, run, [binary])
    else:
        return ""
    if not help_text:
        return ""
    ram = mem_mib if mem_mib is not None else _mem_mib()
    wanted = [("--ctx-checkpoints", str(CACHE_CHECKPOINTS)),
              ("--cache-ram", str(min(4096, int(ram * 0.15)) if ram else 4096)),
              ("--cache-idle-slots", "")]
    parts = []
    for flag, value in wanted:
        if re.search(rf"(^|\s){re.escape(flag)}(\s|=|$)", command) or flag not in help_text:
            continue
        parts.append(f"{flag} {value}".strip())
    return " ".join(parts)


def download_compact(directory: Path, run: Runner = subprocess.run, out=print) -> bool:
    """Die kompakte Modelldatei (~5,9 GB) von Hugging Face laden – mit dem Python der Bonsai-Einrichtung
    (huggingface_hub, setzt abgebrochene Downloads fort)."""
    target = directory / COMPACT_DIR
    if (target / COMPACT_FILE).exists():
        return True
    out(T(f"Lade {COMPACT_FILE} (~5,9 GB) …", f"Downloading {COMPACT_FILE} (~5.9 GB) …"))
    target.mkdir(parents=True, exist_ok=True)
    venv = directory / ".venv" / "bin" / "python"
    python = str(venv) if venv.exists() else (shutil.which("python3") or sys.executable)
    code = ("import os, sys; from huggingface_hub import hf_hub_download; "
            "hf_hub_download(sys.argv[1], sys.argv[2], local_dir=sys.argv[3], token=os.environ.get('BONSAI_TOKEN'))")
    run([python, "-c", code, COMPACT_REPO, COMPACT_FILE, str(target)], cwd=str(directory))
    if not (target / COMPACT_FILE).exists():
        out(T(f"✘ {COMPACT_FILE} konnte nicht geladen werden – Netzwerk prüfen und erneut versuchen.",
              f"✘ Could not download {COMPACT_FILE} – check the network and try again."))
        return False
    return True


def install(state_path: Path, gpu: dict | None = None, activate: bool = True, directory: Path | None = None,
            run: Runner = subprocess.run, out=print, variant: str = PROFILE_NAME) -> str | None:
    from .models import added_profiles, detect_gpu, register_profile
    d = directory or bonsai_dir()
    compact = variant == COMPACT_NAME
    if not setup(d, run=run, out=out, skip_model=compact):
        return None
    if compact and not download_compact(d, run=run, out=out):
        return None
    lib_path = ensure_cuda_libs(d, run=run, out=out)
    if lib_path is None:
        return None
    old_key = added_profiles(state_path).get(variant, {}).get("api_key")  # erneutes add = Reparatur
    profile = make_profile(gpu or detect_gpu(), d, api_key=old_key if isinstance(old_key, str) and old_key else None,
                           lib_path=lib_path, compact=compact)
    return register_profile(state_path, variant, profile, activate=activate)
