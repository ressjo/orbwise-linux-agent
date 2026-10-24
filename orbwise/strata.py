"""Strata (github.com/Niko1221/Strata, MIT) als Modell-Engine: für Qwen3.8-Flash-Next, ein MoE-Modell mit ~125B
Parametern, das sonst nicht auf eine Spieler-Grafikkarte passt. Strata hält die meistgenutzten Experten im
Grafikspeicher, alle im RAM und die große Lookup-Tabelle auf der SSD (eigene Engine, nutzt Teile von ggml).

Orbwise installiert das Strata-„Terminal“ (die `strata`-Kommandozeile) genau wie dessen install.sh:
install-manifest.txt des Releases lesen, Archiv laden, SHA-256 prüfen, entpacken nach ~/.local/share/strata/terminal/,
die signierte Release-Liste mit dem entpackten Programm prüfen (`strata update --check --no-engine`) und erst dann
übernehmen. Engine und Modell lädt `strata launch` selbst (signiert) beim ersten Start. Gestartet wird Strata als
abgelöster Modell-Server (ServerConfig.detached): `strata launch … --no-tui` kehrt zurück, `strata launch --stop`
beendet, bereit ist es, wenn /health „loaded“ meldet."""

from __future__ import annotations

import hashlib
import os
import re
import secrets
import shlex
import shutil
import subprocess
import tarfile
from collections.abc import Awaitable, Callable
from pathlib import Path

import httpx

from .lang import T

FEED = "https://github.com/Niko1221/Strata/releases/latest/download"
PLATFORM = "linux-x86_64"
KEY_ENV = "ORBWISE_STRATA_KEY"  # API-Schlüssel über die Umgebung – nicht in der Befehlszeile (ps, Log)
TRANSPORT: httpx.AsyncBaseTransport | None = None  # Tests

# Größen laut Stratas Katalog (setup.py, Terminal 0.1.48): Download, RAM-Bedarf (RAM + VRAM zusammen), Experten (arena)
MODELS = {
    "iq1_m": {"family": "coder", "label": "Qwen3.8-Flash-Next Coder IQ1_M", "download_gb": 58.4, "ram_gb": 32, "arena_gb": 23.4,
              "about": T("Coder: die Hälfte der Experten (Code, Werkzeuge, Bilder) – stark bei Code, schwächer bei "
                         "anderen Texten", "Coder: half the experts (code, tools, images) – strong at code, weaker "
                         "at other text")},
    "q2_0": {"family": "qwen", "label": "Qwen3.8-Flash-Next Q2_0", "download_gb": 66.4, "ram_gb": 48, "arena_gb": 34.0,
             "about": T("2 Bit, am schnellsten", "2-bit, the fastest")},
    "iq2_xs": {"family": "qwen", "label": "Qwen3.8-Flash-Next IQ2_XS", "download_gb": 68.0, "ram_gb": 48, "arena_gb": 35.5,
               "about": T("2 Bit, etwas besser, fast so schnell", "2-bit, a little better, close in speed")},
    "iq3_xxs": {"family": "qwen", "label": "Qwen3.8-Flash-Next IQ3_XXS", "download_gb": 75.8, "ram_gb": 60, "arena_gb": 42.9,
                "about": T("3 Bit, besser, langsamer", "3-bit, better, slower")},
    "iq3_s": {"family": "qwen", "label": "Qwen3.8-Flash-Next IQ3_S", "download_gb": 83.6, "ram_gb": 62, "arena_gb": 50.3,
              "about": T("3,5 Bit, beste Qualität (wie das volle Modell), am langsamsten",
                         "3.5-bit, the best quality (matches the full model), the slowest")},
}


def data_dir() -> Path:
    base = os.environ.get("STRATA_DATA_DIR") or os.path.join(
        os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share"), "strata")
    return Path(base)


def binary() -> Path:
    return data_dir() / "terminal" / "current" / "bin" / "strata"


def installed() -> bool:
    return binary().is_file()


def version() -> str | None:
    try:
        return (data_dir() / "terminal" / "current" / "VERSION").read_text().strip() or None
    except OSError:
        cur = data_dir() / "terminal" / "current"
        return cur.resolve().name if cur.exists() else None


def models_dir(cfg) -> Path:
    from .llamacpp import models_dir as base
    return base(cfg) / "strata"


def parse_manifest(text: str, platform: str = PLATFORM) -> dict | None:
    """Zeile „<plattform> <version> <datei> <sha256> <bytes>“ aus install-manifest.txt."""
    for line in text.splitlines():
        parts = line.split()
        if len(parts) >= 4 and parts[0] == platform:
            return {"version": parts[1], "file": parts[2], "sha256": parts[3].lower(),
                    "size": int(parts[4]) if len(parts) > 4 and parts[4].isdigit() else 0}
    return None


def nvidia_info(run=subprocess.run) -> dict:
    """Treiber und Rechenfähigkeit der NVIDIA-Karte (nvidia-smi) – leer, wenn unbekannt."""
    if not shutil.which("nvidia-smi"):
        return {}
    try:
        out = run(["nvidia-smi", "--query-gpu=driver_version,compute_cap", "--format=csv,noheader"],
                  capture_output=True, text=True, timeout=10).stdout.strip().splitlines()
        driver, cap = [x.strip() for x in out[0].split(",")[:2]]
        return {"driver": driver, "compute_cap": float(cap)}
    except (OSError, ValueError, IndexError, subprocess.SubprocessError):
        return {}


def _version(text: str) -> tuple:
    return tuple(int(x) for x in str(text).split(".") if x.isdigit())


def gpu_supported(gpu: dict, nvidia: dict | None = None) -> tuple[bool, str]:
    """Strata braucht eine NVIDIA- (RTX 20 bis 50) oder AMD-Karte mit mindestens 12 GB (Linux x86_64). NVIDIA: ab
    Treiber 580 die CUDA-13-Engine, davor die CUDA-12-Engine – Strata wählt sie selbst."""
    if gpu.get("vendor") not in ("amd", "nvidia"):
        return False, T("Strata braucht eine NVIDIA- oder AMD-Grafikkarte.", "Strata needs an NVIDIA or AMD graphics card.")
    if float(gpu.get("vram_gb") or 0) < 11.5:
        return False, T("Strata braucht mindestens 12 GB Grafikspeicher.", "Strata needs at least 12 GB of video memory.")
    if gpu.get("vendor") == "amd":
        return True, T("AMD-Unterstützung ist bei Strata noch experimentell.", "AMD support is still experimental in Strata.")
    info = nvidia_info() if nvidia is None else nvidia
    if info.get("compute_cap") and info["compute_cap"] < 7.5:
        return False, T("Strata braucht eine NVIDIA RTX 20 oder neuer (GTX-Karten werden nicht unterstützt).",
                        "Strata needs an NVIDIA RTX 20 series or newer (GTX cards are not supported).")
    driver = info.get("driver")
    if driver and _version(driver) < (525,):
        return False, T(f"Der NVIDIA-Treiber {driver} ist zu alt für Strata – bitte Treiber 580 oder neuer installieren "
                        "(Ubuntu: sudo ubuntu-drivers install).",
                        f"NVIDIA driver {driver} is too old for Strata – please install driver 580 or newer "
                        "(Ubuntu: sudo ubuntu-drivers install).")
    if driver and _version(driver) < (580, 65):
        return True, T(f"NVIDIA-Treiber {driver}: Strata nutzt die CUDA-12-Engine (ab Treiber 580 die neuere CUDA-13-Engine).",
                       f"NVIDIA driver {driver}: Strata uses the CUDA 12 engine (the newer CUDA 13 engine from driver 580).")
    return True, ""


def options(vram_gb: float, ram_gb: float) -> list[dict]:
    """Alle Varianten mit „passt“ und der Empfehlung für diesen Rechner (wie Stratas eigene Anleitung: 32 GB RAM →
    Coder, 48–64 GB → IQ2_XS, ab 96 GB → IQ3_S). RAM als ganze GB gerechnet – 32 GB zeigt das System als ~31 an."""
    ram = round(ram_gb)
    pick = "iq3_s" if ram >= 96 else "iq2_xs" if ram >= 48 else "iq1_m" if ram >= 30 else None
    return [{"model": key, **m, "fits": ram >= m["ram_gb"] - 2, "recommended": key == pick} for key, m in MODELS.items()]


# Kontextstufen wie in Stratas Einrichtung; trainiert ist das Modell auf 262.144 Token (darüber nur mit RoPE-Skalierung,
# das bietet Orbwise nicht an)
CONTEXTS = (8192, 32768, 65536, 131072, 204800, 262144)
MAX_CONTEXT = 262144
KV_BYTES_PER_TOKEN = 13 * 1056  # 8-Bit-KV: 12 Attention-Schichten + Draft-Schicht, je 1056 Byte (≈ 13,7 KB pro Token)


def kv_gb(ctx: int) -> float:
    return ctx * KV_BYTES_PER_TOKEN / 1e9


# Grafikspeicher, den die Engine frei lässt (MiB). Stratas Standard 700 ist für eine Karte ohne Desktop gedacht: treibt
# dieselbe AMD-Karte unter Linux den Bildschirm an, verschiebt amdgpu bei vollem VRAM den Experten-Cache in den RAM, den
# die Experten schon füllen – der PC friert ein. Strata empfiehlt dafür 3072 (serve/server.py desktop_vram_note, #560).
RESERVE_STEPS = (700, 1024, 1536, 2048, 3072, 4096)
DEFAULT_RESERVE = 700
DESKTOP_RESERVE = 3072
_RESERVE_RE = re.compile(r"\s+--set\s+vram-reserve-mib=\d+")


def reserve_error() -> str:
    steps = ", ".join(str(n) for n in RESERVE_STEPS)
    return T(f"Reserve bitte eine dieser Stufen (MiB): {steps}", f"Reserve please one of these steps (MiB): {steps}")


def default_reserve(gpu: dict | None) -> int:
    """AMD: der Desktop läuft meist auf derselben Karte – Orbwise läuft als Dienst und sieht das nicht sicher."""
    return DESKTOP_RESERVE if (gpu or {}).get("vendor") == "amd" else DEFAULT_RESERVE


def reserve_of(command: str) -> int:
    m = re.search(r"--set\s+vram-reserve-mib=(\d+)", command or "")
    return int(m.group(1)) if m else DEFAULT_RESERVE


def with_reserve(command: str, mib: int) -> str:
    command = _RESERVE_RE.sub("", command)
    return command if mib == DEFAULT_RESERVE else f"{command} --set vram-reserve-mib={mib}"


def recommend_context(vram_gb: float, reserve_mib: int = DEFAULT_RESERVE) -> int:
    """Stratas eigene Regel: der KV-Cache jeder Karte nimmt Experten Platz weg – unter 14 GB 32K, unter 20 GB 64K, sonst
    128K. VRAM als ganze GB (eine 20-GB-Karte meldet etwas weniger), abzüglich einer größeren Reserve für den Desktop."""
    vram = round((vram_gb or 0) - max(0, reserve_mib - DEFAULT_RESERVE) / 1024)
    return 32768 if vram < 14 else 65536 if vram < 20 else 131072


def kv_in_ram(model: str, ctx: int, ram_gb: float) -> bool:
    """Ab 64K legt Strata den KV-Cache in den RAM (nur das gerade Gelesene bleibt im VRAM) – wenn er neben den Experten
    noch Platz hat (+1 GB). Sonst bleibt er im VRAM und verdrängt Experten."""
    return ctx >= 65536 and round(ram_gb or 0) >= MODELS[model]["ram_gb"] + kv_gb(ctx) + 1


def ram_need(model: str, ctx: int) -> float | None:
    """Stratas Schätzung für langen Kontext mit IQ3: Experten + KV + 24 GB für den Rest; sonst None."""
    if model not in ("iq3_xxs", "iq3_s"):
        return None
    return MODELS[model]["arena_gb"] + kv_gb(ctx) + 24


def _gb(x: float) -> str:
    return T(f"{x:.1f}".replace(".", ","), f"{x:.1f}")


def context_options(model: str, vram_gb: float, ram_gb: float, reserve_mib: int = DEFAULT_RESERVE) -> list[dict]:
    """Die Kontextstufen für die Modellseite: Empfehlung, KV-Größe und wo der KV-Cache liegt."""
    rec = recommend_context(vram_gb, reserve_mib)
    out = []
    for ctx in CONTEXTS:
        kv, in_ram = kv_gb(ctx), kv_in_ram(model, ctx, ram_gb)
        note = (T(f"KV {_gb(kv)} GB im RAM", f"KV {_gb(kv)} GB in RAM") if in_ram
                else T(f"KV {_gb(kv)} GB im VRAM", f"KV {_gb(kv)} GB in VRAM")
                + (T(" · kostet Experten-Platz", " · costs expert space") if kv >= 1 else ""))
        need = ram_need(model, ctx)
        fits = not (ctx > 131072 and need is not None and need > round(ram_gb or 0))
        if not fits:
            note += T(f" · braucht ~{need:.0f} GB RAM", f" · needs ~{need:.0f} GB RAM")
        out.append({"ctx": ctx, "recommended": ctx == rec, "kv_gb": round(kv, 2), "kv_in_ram": in_ram,
                    "fits": fits, "note": note})
    return out


def make_profile(cfg, model: str, port: int, context: int = 65536, reserve: int = DEFAULT_RESERVE) -> dict:
    """Profil für einen von Orbwise gestarteten Strata-Server (abgelöst, eigener Schlüssel über die Umgebung)."""
    m = MODELS[model]
    key = secrets.token_hex(16)
    exe = shlex.quote(str(binary()))
    folder = models_dir(cfg) / model  # je Variante ein Ordner – Fortschritt und Löschen beziehen sich nur auf sie
    data = shlex.quote(str(folder))
    command = with_reserve(f"{exe} launch --model {model} --family {m['family']} --context {context} --port {port} "
                           f"--api-key-env {KEY_ENV} --data-dir {data} --yes --no-tui --restart", reserve)
    return {
        "label": m["label"],
        "backend": "openai",
        "base_url": f"http://127.0.0.1:{port}/v1",
        "model": model,
        "api_key": key,
        "unload_ollama": True,
        "embed_on_cpu": True,  # der Grafikspeicher gehört den Experten
        "server": {"command": command, "env": {KEY_ENV: key}, "detached": True,
                   "stop_command": f"{exe} launch --stop", "ready_key": "loaded",
                   "progress_dir": str(folder), "progress_gb": m["download_gb"],
                   # beim ersten Start lädt Strata Engine und Modell (~60–85 GB) – das darf dauern
                   "startup_timeout": 6 * 3600},
    }


def add_model(cfg, state_path: Path, model: str, profiles: dict, context: int = 65536,
              reserve: int = DEFAULT_RESERVE) -> str:
    """Strata-Modell als Profil anlegen → Profilname. Die Dateien lädt Strata beim ersten Start selbst. `context` und
    `reserve`: der Server übergibt recommend_context bzw. default_reserve für diese Grafikkarte."""
    from . import llamacpp
    from .models import register_profile
    if model not in MODELS:
        raise ValueError(T(f"Unbekannte Strata-Variante: {model}", f"Unknown Strata variant: {model}"))
    for name, p in profiles.items():
        if p.server and f"launch --model {model} " in p.server.command:
            raise ValueError(T(f"Diese Variante ist schon als „{p.label or name}“ angelegt.",
                               f"This variant already exists as “{p.label or name}”."))
    name = llamacpp.unique_name(MODELS[model]["label"], set(profiles))
    register_profile(state_path, name, make_profile(cfg, model, llamacpp.free_port(profiles), context, reserve))
    data = llamacpp._read(state_path)
    data.setdefault("local_models", {})[name] = {"files": [], "downloaded": False, "engine": "strata", "repo": "",
                                                 "strata_model": model, "dir": str(models_dir(cfg) / model)}
    data.setdefault("strata_reserve", {})[name] = reserve  # gewählt – die Vorgabe beim Start überschreibt es nicht
    llamacpp._write(state_path, data)
    return name


def dir_bytes(path: Path) -> int:
    total = 0
    for root, _dirs, files in os.walk(path):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return total


async def install(progress: Callable[[int, int], Awaitable[None]] | None = None, feed: str = FEED,
                  run=subprocess.run) -> str:
    """Strata-Terminal installieren bzw. aktualisieren → Version. Wie install.sh: Prüfsumme laut Manifest, danach
    die signierte Release-Liste mit dem neuen Programm prüfen – schlägt eins fehl, wird nichts übernommen."""
    feed = feed.rstrip("/")
    async with httpx.AsyncClient(follow_redirects=True, timeout=httpx.Timeout(60, read=300), transport=TRANSPORT,
                                 headers={"User-Agent": "orbwise"}) as client:
        r = await client.get(f"{feed}/install-manifest.txt")
        r.raise_for_status()
        entry = parse_manifest(r.text)
        if not entry:
            raise ValueError(T("Dieses Strata-Release hat kein Terminal für Linux x86_64.",
                               "This Strata release has no terminal for Linux x86_64."))
        if version() == entry["version"] and installed():
            return entry["version"]
        terminal = data_dir() / "terminal"
        terminal.mkdir(parents=True, exist_ok=True)
        archive = terminal / f".{entry['file']}"
        digest, done = hashlib.sha256(), 0
        async with client.stream("GET", f"{feed}/{entry['file']}") as resp:
            resp.raise_for_status()
            with archive.open("wb") as f:
                async for chunk in resp.aiter_bytes(1 << 20):
                    f.write(chunk)
                    digest.update(chunk)
                    done += len(chunk)
                    if progress:
                        await progress(done, entry["size"] or done)
    try:
        if digest.hexdigest() != entry["sha256"]:
            raise ValueError(T("Die Strata-Datei passt nicht zur Prüfsumme des Releases – nichts installiert.",
                               "The Strata download does not match the release checksum – nothing installed."))
        dest = terminal / entry["version"]
        incoming = terminal / f"{entry['version']}.incoming"
        shutil.rmtree(incoming, ignore_errors=True)
        incoming.mkdir(parents=True)
        with tarfile.open(archive) as tar:
            tar.extractall(incoming, filter="data")
        if not (incoming / "bin" / "strata").is_file():  # Archiv mit einem Ordner obendrüber
            inner = next((d for d in incoming.iterdir() if (d / "bin" / "strata").is_file()), None)
            if inner:
                tmp = terminal / f"{entry['version']}.inner"
                inner.rename(tmp)
                shutil.rmtree(incoming)
                tmp.rename(incoming)
        exe = incoming / "bin" / "strata"
        if not exe.is_file():
            raise ValueError(T("Das Archiv ist kein Strata-Terminal (bin/strata fehlt).",
                               "The archive is not a Strata terminal (bin/strata missing)."))
        exe.chmod(0o755)
        check = run([str(exe), "update", "--check", "--no-engine", "--feed", feed], capture_output=True, text=True,
                    timeout=300)
        if check.returncode != 0:
            raise ValueError(T("Die Signatur des Strata-Releases ließ sich nicht prüfen – nichts installiert: ",
                               "The Strata release signature could not be verified – nothing installed: ")
                             + ((check.stdout or "") + (check.stderr or "")).strip()[-300:])
        shutil.rmtree(dest, ignore_errors=True)
        incoming.rename(dest)
        current = terminal / "current"
        if current.is_symlink() or current.exists():
            current.unlink()
        current.symlink_to(dest)
        return entry["version"]
    finally:
        archive.unlink(missing_ok=True)
        shutil.rmtree(terminal / f"{entry['version']}.incoming", ignore_errors=True)
