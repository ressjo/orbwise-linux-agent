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

# Größen laut Stratas Katalog (setup.py, Terminal 0.1.48): Download, RAM-Bedarf (RAM + VRAM zusammen)
MODELS = {
    "iq1_m": {"family": "coder", "label": "Qwen3.8-Flash-Next Coder IQ1_M", "download_gb": 58.4, "ram_gb": 32,
              "about": T("Coder: die Hälfte der Experten (Code, Werkzeuge, Bilder) – stark bei Code, schwächer bei "
                         "anderen Texten", "Coder: half the experts (code, tools, images) – strong at code, weaker "
                         "at other text")},
    "q2_0": {"family": "qwen", "label": "Qwen3.8-Flash-Next Q2_0", "download_gb": 66.4, "ram_gb": 48,
             "about": T("2 Bit, am schnellsten", "2-bit, the fastest")},
    "iq2_xs": {"family": "qwen", "label": "Qwen3.8-Flash-Next IQ2_XS", "download_gb": 68.0, "ram_gb": 48,
               "about": T("2 Bit, etwas besser, fast so schnell", "2-bit, a little better, close in speed")},
    "iq3_xxs": {"family": "qwen", "label": "Qwen3.8-Flash-Next IQ3_XXS", "download_gb": 75.8, "ram_gb": 60,
                "about": T("3 Bit, besser, langsamer", "3-bit, better, slower")},
    "iq3_s": {"family": "qwen", "label": "Qwen3.8-Flash-Next IQ3_S", "download_gb": 83.6, "ram_gb": 62,
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


def gpu_supported(gpu: dict) -> tuple[bool, str]:
    """Strata braucht eine NVIDIA- oder AMD-Karte mit mindestens 12 GB (Linux x86_64)."""
    if gpu.get("vendor") not in ("amd", "nvidia"):
        return False, T("Strata braucht eine NVIDIA- oder AMD-Grafikkarte.", "Strata needs an NVIDIA or AMD graphics card.")
    if float(gpu.get("vram_gb") or 0) < 11.5:
        return False, T("Strata braucht mindestens 12 GB Grafikspeicher.", "Strata needs at least 12 GB of video memory.")
    if gpu.get("vendor") == "amd":
        return True, T("AMD-Unterstützung ist bei Strata noch experimentell.", "AMD support is still experimental in Strata.")
    return True, ""


def options(vram_gb: float, ram_gb: float) -> list[dict]:
    """Alle Varianten mit „passt“ und der Empfehlung für diesen Rechner (wie Stratas eigene Anleitung: 32 GB RAM →
    Coder, 48–64 GB → IQ2_XS, ab 96 GB → IQ3_S). RAM als ganze GB gerechnet – 32 GB zeigt das System als ~31 an."""
    ram = round(ram_gb)
    pick = "iq3_s" if ram >= 96 else "iq2_xs" if ram >= 48 else "iq1_m" if ram >= 30 else None
    return [{"model": key, **m, "fits": ram >= m["ram_gb"] - 2, "recommended": key == pick} for key, m in MODELS.items()]


def make_profile(cfg, model: str, port: int, context: int = 65536) -> dict:
    """Profil für einen von Orbwise gestarteten Strata-Server (abgelöst, eigener Schlüssel über die Umgebung)."""
    m = MODELS[model]
    key = secrets.token_hex(16)
    exe = shlex.quote(str(binary()))
    folder = models_dir(cfg) / model  # je Variante ein Ordner – Fortschritt und Löschen beziehen sich nur auf sie
    data = shlex.quote(str(folder))
    command = (f"{exe} launch --model {model} --family {m['family']} --context {context} --port {port} "
               f"--api-key-env {KEY_ENV} --data-dir {data} --yes --no-tui --restart")
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


def add_model(cfg, state_path: Path, model: str, profiles: dict, context: int = 65536) -> str:
    """Strata-Modell als Profil anlegen → Profilname. Die Dateien lädt Strata beim ersten Start selbst."""
    from . import llamacpp
    from .models import register_profile
    if model not in MODELS:
        raise ValueError(T(f"Unbekannte Strata-Variante: {model}", f"Unknown Strata variant: {model}"))
    for name, p in profiles.items():
        if p.server and f"launch --model {model} " in p.server.command:
            raise ValueError(T(f"Diese Variante ist schon als „{p.label or name}“ angelegt.",
                               f"This variant already exists as “{p.label or name}”."))
    name = llamacpp.unique_name(MODELS[model]["label"], set(profiles))
    register_profile(state_path, name, make_profile(cfg, model, llamacpp.free_port(profiles), context))
    data = llamacpp._read(state_path)
    data.setdefault("local_models", {})[name] = {"files": [], "downloaded": False, "engine": "strata", "repo": "",
                                                 "strata_model": model, "dir": str(models_dir(cfg) / model)}
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
