"""Einrichtung des Bild-Modus: stable-diffusion.cpp (fertiger Vulkan-Build – läuft auf AMD und NVIDIA ohne ROCm/CUDA)
und Qwen-Image-2.1 (Bildmodell, Text-Encoder Qwen3-VL-8B, VAE) von Hugging Face.

    orbwise model add qwen-image      # ~10–15 GB, je nach Grafikkarte
    orbwise model remove qwen-image

Die Dateinamen werden beim Einrichten aus den Hugging-Face-Repositories gelesen und nach Muster gewählt (Quantisierung
passend zum VRAM) – so übersteht die Einrichtung umbenannte oder neue Dateien. Was gewählt wurde, steht in
<dir>/manifest.json; ein eigener Build lässt sich über bin/sd-server (oder image.dir) einsetzen.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import stat
import zipfile
from collections.abc import Callable
from pathlib import Path

import httpx

from .lang import T

NAME = "qwen-image"
SD_REPO = "leejet/stable-diffusion.cpp"
DIFFUSION_REPO = "leejet/Qwen-Image-2.1-GGUF"
TEXT_REPO = "Qwen/Qwen3-VL-8B-Instruct-GGUF"
VAE_REPO = "Comfy-Org/Qwen-Image-2.1"
RELEASE_ASSET = re.compile(r"bin-Linux-.*x86_64-vulkan\.zip$", re.I)

Out = Callable[[str], None]


def image_dir(cfg=None) -> Path:
    raw = (getattr(getattr(cfg, "image", None), "dir", "") or os.environ.get("ORBWISE_IMAGE_DIR")
           or "~/orbwise-image")
    return Path(os.path.expanduser(raw))


def server_binary(directory: Path) -> Path:
    return directory / "bin" / "sd-server"


def manifest(directory: Path) -> dict:
    try:
        return json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def model_paths(directory: Path) -> dict[str, Path]:
    """Rolle → Datei (diffusion, llm, vae, vision) laut manifest.json."""
    return {role: directory / "models" / name for role, name in (manifest(directory).get("files") or {}).items()}


def is_set_up(directory: Path) -> bool:
    paths = model_paths(directory)
    return server_binary(directory).exists() and all(
        paths.get(r) and paths[r].exists() for r in ("diffusion", "llm", "vae"))


# ---------------------------------------------------------------- Auswahl der Dateien
def quant_preference(vram_gb: float) -> list[str]:
    """Quantisierung des Bildmodells nach VRAM – kleiner = schneller ladbar, etwas weniger Detail."""
    if vram_gb >= 20:
        return ["Q8_0", "Q6_K", "Q5_K_M", "Q5_K", "Q4_K_M", "Q4_K"]
    if vram_gb >= 12:
        return ["Q6_K", "Q5_K_M", "Q5_K", "Q4_K_M", "Q4_K", "Q4_0"]
    return ["Q4_K_M", "Q4_K", "Q4_0", "Q3_K", "Q3_K_M"]


def pick(files: list[str], suffixes: list[str], exclude: str = "mmproj") -> str | None:
    """Erste Datei, deren Name auf eine der Quantisierungen endet (Reihenfolge = Vorliebe)."""
    for q in suffixes:
        for f in files:
            name = f.rsplit("/", 1)[-1]
            if exclude and exclude in name.lower():
                continue
            if re.search(rf"[-_.]{re.escape(q)}\.gguf$", name, re.I):
                return f
    return None


def pick_files(trees: dict[str, list[str]], vram_gb: float) -> dict[str, tuple[str, str]]:
    """Rolle → (Repository, Pfad). trees: Repository → Dateiliste."""
    out: dict[str, tuple[str, str]] = {}
    diffusion = pick(trees.get(DIFFUSION_REPO, []), quant_preference(vram_gb))
    if diffusion:
        out["diffusion"] = (DIFFUSION_REPO, diffusion)
    text = pick(trees.get(TEXT_REPO, []), ["Q4_K_M", "Q4_K", "Q5_K_M", "Q8_0"] if vram_gb < 20 else ["Q8_0", "Q6_K",
                                                                                                    "Q4_K_M"])
    if text:
        out["llm"] = (TEXT_REPO, text)
    vision = next((f for f in trees.get(TEXT_REPO, []) if "mmproj" in f.lower() and re.search(r"f16\.gguf$", f, re.I)),
                  None) or next((f for f in trees.get(TEXT_REPO, []) if "mmproj" in f.lower()), None)
    if vision:
        out["vision"] = (TEXT_REPO, vision)
    vaes = [f for f in trees.get(VAE_REPO, []) if "vae" in f.lower() and f.endswith(".safetensors")]
    vae = next((f for f in vaes if "2.1" in f and "bf16" in f), None) or next((f for f in vaes if "2.1" in f), None) \
        or (vaes[0] if vaes else None)
    if vae:
        out["vae"] = (VAE_REPO, vae)
    return out


# ---------------------------------------------------------------- Netz
def _client() -> httpx.Client:
    return httpx.Client(follow_redirects=True, timeout=httpx.Timeout(60, read=300),
                        headers={"User-Agent": "orbwise"})


def hf_tree(repo: str, client: httpx.Client) -> list[str]:
    r = client.get(f"https://huggingface.co/api/models/{repo}/tree/main", params={"recursive": "1"})
    r.raise_for_status()
    return [f["path"] for f in r.json() if f.get("type") == "file"]


def download(url: str, target: Path, client: httpx.Client, out: Out, label: str = "") -> None:
    """Mit Fortsetzen (Range) – abgebrochene Downloads müssen nicht von vorn beginnen."""
    if target.exists():
        return
    part = target.with_suffix(target.suffix + ".part")
    target.parent.mkdir(parents=True, exist_ok=True)
    have = part.stat().st_size if part.exists() else 0
    headers = {"Range": f"bytes={have}-"} if have else {}
    with client.stream("GET", url, headers=headers) as r:
        if r.status_code == 416:  # schon vollständig
            part.rename(target)
            return
        r.raise_for_status()
        if have and r.status_code != 206:
            have = 0  # Server kann nicht fortsetzen
        total = have + int(r.headers.get("content-length") or 0)
        shown = -1
        with part.open("ab" if have else "wb") as f:
            for chunk in r.iter_bytes(1 << 20):
                f.write(chunk)
                have += len(chunk)
                pct = int(100 * have / total) if total else 0
                if pct // 5 != shown:
                    shown = pct // 5
                    out(f"  {label or target.name}: {have / 1e9:.1f} / {total / 1e9:.1f} GB ({pct} %)")
    part.rename(target)


def install_binary(directory: Path, client: httpx.Client, out: Out) -> bool:
    """Neuesten fertigen Linux-Vulkan-Build von stable-diffusion.cpp nach <dir>/bin."""
    if server_binary(directory).exists():
        return True
    r = client.get(f"https://api.github.com/repos/{SD_REPO}/releases/latest")
    r.raise_for_status()
    asset = next((a for a in r.json().get("assets", []) if RELEASE_ASSET.search(a.get("name", ""))), None)
    if not asset:
        out(T("✘ Kein fertiger Linux-Vulkan-Build gefunden – stable-diffusion.cpp selbst bauen (docs/build.md) und "
              f"sd-server nach {server_binary(directory)} legen.",
              "✘ No prebuilt Linux Vulkan build found – build stable-diffusion.cpp yourself (docs/build.md) and put "
              f"sd-server at {server_binary(directory)}."))
        return False
    archive = directory / asset["name"]
    out(T(f"Lade {asset['name']} …", f"Downloading {asset['name']} …"))
    download(asset["browser_download_url"], archive, client, out)
    bin_dir = directory / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive) as z:
        for member in z.infolist():
            name = Path(member.filename).name
            if not name or member.is_dir():
                continue
            target = bin_dir / name
            with z.open(member) as src, target.open("wb") as dst:
                shutil.copyfileobj(src, dst)
            target.chmod(target.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP)  # Programme und Bibliotheken
    archive.unlink(missing_ok=True)
    if not server_binary(directory).exists():
        out(T("✘ Im Build fehlt sd-server.", "✘ sd-server is missing from the build."))
        return False
    return True


def install(gpu: dict, cfg=None, out: Out = print, client: httpx.Client | None = None) -> bool:
    directory = image_dir(cfg)
    directory.mkdir(parents=True, exist_ok=True)
    vram = float(gpu.get("vram_gb") or 0)
    own = client is None
    client = client or _client()
    try:
        if not install_binary(directory, client, out):
            return False
        trees = {repo: hf_tree(repo, client) for repo in (DIFFUSION_REPO, TEXT_REPO, VAE_REPO)}
        chosen = pick_files(trees, vram)
        missing = [r for r in ("diffusion", "llm", "vae") if r not in chosen]
        if missing:
            out(T("✘ Modelldateien nicht gefunden: ", "✘ Model files not found: ") + ", ".join(missing))
            return False
        files = {}
        for role, (repo, path) in chosen.items():
            name = path.rsplit("/", 1)[-1]
            out(T(f"Lade {name} ({role}) …", f"Downloading {name} ({role}) …"))
            download(f"https://huggingface.co/{repo}/resolve/main/{path}", directory / "models" / name, client, out)
            files[role] = name
        (directory / "manifest.json").write_text(json.dumps({"files": files, "vram_gb": vram}, indent=1),
                                                 encoding="utf-8")
        out(T(f"✔ Bild-Modus eingerichtet ({directory}). Im Dashboard oben links „Bild“ wählen.",
              f"✔ Image mode set up ({directory}). Choose \"Image\" at the top left of the dashboard."))
        return True
    except httpx.HTTPError as e:
        out(T(f"✘ Download fehlgeschlagen: {e} – erneut starten setzt fort.",
              f"✘ Download failed: {e} – running it again resumes."))
        return False
    finally:
        if own:
            client.close()


def remove(cfg=None) -> int:
    """Modelldateien löschen (das Programm bleibt, es ist klein) → freigegebene Bytes."""
    directory = image_dir(cfg)
    freed = 0
    for path in (directory / "models").glob("*") if (directory / "models").exists() else []:
        freed += path.stat().st_size
        path.unlink()
    (directory / "manifest.json").unlink(missing_ok=True)
    return freed
