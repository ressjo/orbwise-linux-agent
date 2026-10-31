"""Einrichtung des Bild-Modus: stable-diffusion.cpp (fertiger Vulkan-Build – läuft auf AMD und NVIDIA ohne ROCm/CUDA)
und die Bildmodelle von Hugging Face. Mehrere Modelle können installiert sein, eins ist aktiv:

    orbwise model add qwen-image      # Qwen-Image-2.1 (auch: z-image-turbo, flux-schnell, sdxl)
    orbwise model remove qwen-image

Jedes Modell besteht aus Bauteilen (Diffusionsmodell, Text-Encoder, VAE …) – CATALOG sagt, woher sie kommen und mit
welcher Option sd-server sie bekommt. Die Dateinamen werden beim Einrichten aus den Hugging-Face-Repositories gelesen
und nach Muster gewählt (Quantisierung passend zum VRAM) – so übersteht die Einrichtung umbenannte oder neue Dateien.
Was gewählt wurde, steht in <dir>/manifest.json; ein eigener Build lässt sich über bin/sd-server (oder image.dir)
einsetzen. Dateien, die mehrere Modelle nutzen (z. B. das FLUX-VAE), liegen nur einmal in <dir>/models.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import stat
import threading
import zipfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import httpx

from .lang import T

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
    """{"active": id, "models": {id: {"files": {Rolle: Datei}, "vram_gb": …}}} – ältere Einrichtungen (nur
    Qwen-Image, {"files": …}) werden so gelesen, als wäre es dieses Format."""
    try:
        data = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    if "models" not in data and "files" in data:  # alte Einrichtung: nur Qwen-Image
        data = {"active": "qwen-image", "models": {"qwen-image": {"files": data.get("files") or {},
                                                                 "vram_gb": data.get("vram_gb", 0)}}}
    data.setdefault("models", {})
    return data


def _write_manifest(directory: Path, data: dict) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    tmp = directory / "manifest.json.tmp"
    tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
    tmp.replace(directory / "manifest.json")


def active_id(directory: Path) -> str:
    """Das aktive Bildmodell – sonst das erste installierte, sonst leer."""
    data = manifest(directory)
    models = data.get("models") or {}
    active = data.get("active") or ""
    return active if active in models else next(iter(models), "")


def model_entry(directory: Path, model_id: str | None = None) -> dict:
    data = manifest(directory)
    return (data.get("models") or {}).get(model_id or active_id(directory)) or {}


def model_paths(directory: Path, model_id: str | None = None) -> dict[str, Path]:
    """Rolle → Datei (diffusion, llm, vae, vision, clip_l …) des aktiven bzw. genannten Modells."""
    return {role: directory / "models" / name for role, name in (model_entry(directory, model_id).get("files") or {}).items()}


def model_installed(directory: Path, model_id: str) -> bool:
    spec = BY_ID.get(model_id)
    paths = model_paths(directory, model_id)
    need = spec.required if spec else ("diffusion",)
    return bool(paths) and all(paths.get(r) and paths[r].exists() for r in need)


def is_set_up(directory: Path) -> bool:
    model = active_id(directory)
    return server_binary(directory).exists() and bool(model) and model_installed(directory, model)


def set_active(directory: Path, model_id: str) -> None:
    data = manifest(directory)
    if model_id not in data.get("models", {}):
        raise ValueError(f"{model_id} ist nicht installiert")
    data["active"] = model_id
    _write_manifest(directory, data)


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


# ---------------------------------------------------------------- Katalog
@dataclass(frozen=True)
class ImageModel:
    id: str
    label: str
    note_de: str
    note_en: str
    flags: dict          # Rolle → Option von sd-server
    required: tuple      # Rollen, ohne die das Modell nicht läuft
    repos: tuple         # Repositories, deren Dateilisten die Auswahl braucht
    choose: Callable[[dict[str, list[str]], float], dict[str, tuple[str, str]]]
    size_gb: float       # ungefähre Downloadgröße (kleinste passende Variante) – nur zur Anzeige
    min_vram_gb: float   # darunter wird es sehr langsam (Teile laufen dann auf der CPU)
    steps: int
    cfg_scale: float
    sampler: str = "euler"
    license: str = ""
    edit: bool = False   # kann Vorlagen bearbeiten (Rolle vision)

    def note(self) -> str:
        return T(self.note_de, self.note_en)


def _exact(files: list[str], *names: str) -> str | None:
    """Erste Datei, deren Name (ohne Ordner) einer der Wunschnamen ist."""
    for want in names:
        hit = next((f for f in files if f.rsplit("/", 1)[-1].lower() == want.lower()), None)
        if hit:
            return hit
    return None


def _small_text(vram_gb: float) -> list[str]:
    return ["Q4_K_M", "Q4_K", "Q5_K_M", "Q8_0"] if vram_gb < 20 else ["Q8_0", "Q6_K", "Q4_K_M"]


FLUX_REPO = "black-forest-labs/FLUX.1-schnell"   # VAE (ae.safetensors) – auch für Z-Image
ZIMAGE_REPO = "leejet/Z-Image-Turbo-GGUF"
ZIMAGE_TEXT_REPO = "unsloth/Qwen3-4B-Instruct-2507-GGUF"
FLUX_GGUF_REPO = "city96/FLUX.1-schnell-gguf"
FLUX_TEXT_REPO = "comfyanonymous/flux_text_encoders"
SDXL_REPO = "stabilityai/stable-diffusion-xl-base-1.0"
SDXL_VAE_REPO = "madebyollin/sdxl-vae-fp16-fix"


def _choose_zimage(trees: dict[str, list[str]], vram: float) -> dict[str, tuple[str, str]]:
    out = {}
    if (f := pick(trees.get(ZIMAGE_REPO, []), quant_preference(vram) + ["Q8_0"])):
        out["diffusion"] = (ZIMAGE_REPO, f)
    if (f := pick(trees.get(ZIMAGE_TEXT_REPO, []), _small_text(vram))):
        out["llm"] = (ZIMAGE_TEXT_REPO, f)
    if (f := _exact(trees.get(FLUX_REPO, []), "ae.safetensors")):
        out["vae"] = (FLUX_REPO, f)
    return out


def _choose_flux(trees: dict[str, list[str]], vram: float) -> dict[str, tuple[str, str]]:
    out = {}
    quants = (["Q8_0", "Q6_K", "Q5_K_S", "Q5_1", "Q5_0", "Q4_K_S"] if vram >= 20 else
              ["Q6_K", "Q5_K_S", "Q5_0", "Q4_K_S", "Q4_0"] if vram >= 12 else
              ["Q4_K_S", "Q4_0", "Q4_1", "Q3_K_S"])
    if (f := pick(trees.get(FLUX_GGUF_REPO, []), quants)):
        out["diffusion"] = (FLUX_GGUF_REPO, f)
    texts = trees.get(FLUX_TEXT_REPO, [])
    if (f := _exact(texts, "clip_l.safetensors")):
        out["clip_l"] = (FLUX_TEXT_REPO, f)
    t5 = ("t5xxl_fp16.safetensors", "t5xxl_fp8_e4m3fn.safetensors") if vram >= 20 else \
        ("t5xxl_fp8_e4m3fn.safetensors", "t5xxl_fp8_e4m3fn_scaled.safetensors", "t5xxl_fp16.safetensors")
    if (f := _exact(texts, *t5)):
        out["t5xxl"] = (FLUX_TEXT_REPO, f)
    if (f := _exact(trees.get(FLUX_REPO, []), "ae.safetensors")):
        out["vae"] = (FLUX_REPO, f)
    return out


def _choose_sdxl(trees: dict[str, list[str]], vram: float) -> dict[str, tuple[str, str]]:
    out = {}
    if (f := _exact(trees.get(SDXL_REPO, []), "sd_xl_base_1.0.safetensors")):
        out["diffusion"] = (SDXL_REPO, f)
    if (f := _exact(trees.get(SDXL_VAE_REPO, []), "sdxl_vae.safetensors", "diffusion_pytorch_model.safetensors")):
        out["vae"] = (SDXL_VAE_REPO, f)
    return out


CATALOG: tuple[ImageModel, ...] = (
    ImageModel("qwen-image", "Qwen-Image 2.1",
               "Sehr gute Bilder und Schrift im Bild; kann Bilder bearbeiten und mehrere Vorlagen kombinieren. Groß.",
               "Very good images and text in images; edits pictures and combines several references. Large.",
               {"diffusion": "--diffusion-model", "vae": "--vae", "llm": "--llm", "vision": "--llm_vision"},
               ("diffusion", "llm", "vae"), (DIFFUSION_REPO, TEXT_REPO, VAE_REPO), lambda t, v: pick_files(t, v),
               15, 8, 20, 6.0, license="apache-2.0", edit=True),
    ImageModel("z-image-turbo", "Z-Image Turbo",
               "Sehr schnell (8 Schritte) und klein – fotorealistisch; Prompts am besten auf Englisch (oder Chinesisch).",
               "Very fast (8 steps) and small – photorealistic; prompts work best in English (or Chinese).",
               {"diffusion": "--diffusion-model", "vae": "--vae", "llm": "--llm"},
               ("diffusion", "llm", "vae"), (ZIMAGE_REPO, ZIMAGE_TEXT_REPO, FLUX_REPO), _choose_zimage,
               6.5, 6, 8, 1.0, license="apache-2.0"),
    ImageModel("flux-schnell", "FLUX.1 schnell",
               "Bekanntes FLUX-Modell in 4 Schritten – vielseitig, gute Details.",
               "The well-known FLUX model in 4 steps – versatile, good detail.",
               {"diffusion": "--diffusion-model", "vae": "--vae", "clip_l": "--clip_l", "t5xxl": "--t5xxl"},
               ("diffusion", "vae", "clip_l", "t5xxl"), (FLUX_GGUF_REPO, FLUX_TEXT_REPO, FLUX_REPO), _choose_flux,
               12, 8, 4, 1.0, license="apache-2.0"),
    ImageModel("sdxl", "Stable Diffusion XL 1.0",
               "Der Klassiker – klein, läuft auch auf schwächeren Karten; Prompts am besten auf Englisch.",
               "The classic – small, runs on weaker cards too; prompts work best in English.",
               {"diffusion": "--model", "vae": "--vae"},
               ("diffusion", "vae"), (SDXL_REPO, SDXL_VAE_REPO), _choose_sdxl,
               7.2, 6, 25, 7.0, sampler="euler_a", license="openrail++"),
)
BY_ID = {m.id: m for m in CATALOG}


class InstallCancelled(Exception):
    pass


# ---------------------------------------------------------------- Netz
def _client() -> httpx.Client:
    return httpx.Client(follow_redirects=True, timeout=httpx.Timeout(60, read=300),
                        headers={"User-Agent": "orbwise"})


def hf_tree(repo: str, client: httpx.Client) -> list[str]:
    r = client.get(f"https://huggingface.co/api/models/{repo}/tree/main", params={"recursive": "1"},
                   headers=_auth_headers())
    r.raise_for_status()
    return [f["path"] for f in r.json() if f.get("type") == "file"]


def download(url: str, target: Path, client: httpx.Client, out: Out, label: str = "",
             stop: threading.Event | None = None) -> None:
    """Mit Fortsetzen (Range) – abgebrochene Downloads müssen nicht von vorn beginnen. stop: Abbrechen (die
    .part-Datei bleibt, der nächste Versuch macht dort weiter)."""
    if target.exists():
        return
    part = target.with_suffix(target.suffix + ".part")
    target.parent.mkdir(parents=True, exist_ok=True)
    have = part.stat().st_size if part.exists() else 0
    headers = {**({"Range": f"bytes={have}-"} if have else {}),
               **(_auth_headers() if "huggingface.co" in url else {})}
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
                if stop is not None and stop.is_set():
                    raise InstallCancelled()
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


def _auth_headers() -> dict:
    from .models import hf_token
    tok = hf_token()
    return {"Authorization": f"Bearer {tok}"} if tok else {}


def install(gpu: dict, cfg=None, out: Out = print, client: httpx.Client | None = None, model_id: str = "qwen-image",
            stop: threading.Event | None = None) -> bool:
    """Programm (einmalig) und die Dateien eines Bildmodells laden; das Modell wird danach das aktive."""
    spec = BY_ID.get(model_id)
    if spec is None:
        out(T(f"✘ Unbekanntes Bildmodell {model_id} – vorhanden: ", f"✘ Unknown image model {model_id} – available: ")
            + ", ".join(BY_ID))
        return False
    directory = image_dir(cfg)
    directory.mkdir(parents=True, exist_ok=True)
    vram = float(gpu.get("vram_gb") or 0)
    own = client is None
    client = client or _client()
    try:
        if not install_binary(directory, client, out):
            return False
        trees = {repo: hf_tree(repo, client) for repo in spec.repos}
        chosen = spec.choose(trees, vram)
        missing = [r for r in spec.required if r not in chosen]
        if missing:
            out(T("✘ Modelldateien nicht gefunden: ", "✘ Model files not found: ") + ", ".join(missing))
            return False
        files = {}
        for i, (role, (repo, path)) in enumerate(chosen.items(), 1):
            name = path.rsplit("/", 1)[-1]
            out(T(f"Lade {name} ({role}, {i}/{len(chosen)}) …", f"Downloading {name} ({role}, {i}/{len(chosen)}) …"))
            download(f"https://huggingface.co/{repo}/resolve/main/{path}", directory / "models" / name, client, out,
                     stop=stop)
            files[role] = name
        data = manifest(directory) or {"models": {}}
        data["models"][model_id] = {"files": files, "vram_gb": vram}
        data["active"] = model_id
        _write_manifest(directory, data)
        out(T(f"✔ {spec.label} eingerichtet ({directory}). Im Dashboard oben links „Bild“ wählen.",
              f"✔ {spec.label} set up ({directory}). Choose \"Image\" at the top left of the dashboard."))
        return True
    except httpx.HTTPStatusError as e:
        if e.response.status_code in (401, 403):
            out(T("✘ Hugging Face verlangt eine Anmeldung für eine der Dateien – Lizenz auf der Modellseite akzeptieren "
                  "und unter Einstellungen → Allgemein → Hugging Face ein Token eintragen.",
                  "✘ Hugging Face requires a login for one of the files – accept the license on the model page and "
                  "enter a token under Settings → General → Hugging Face."))
        else:
            out(T(f"✘ Download fehlgeschlagen: {e} – erneut starten setzt fort.",
                  f"✘ Download failed: {e} – running it again resumes."))
        return False
    except httpx.HTTPError as e:
        out(T(f"✘ Download fehlgeschlagen: {e} – erneut starten setzt fort.",
              f"✘ Download failed: {e} – running it again resumes."))
        return False
    finally:
        if own:
            client.close()


def disk_size(directory: Path, model_id: str) -> int:
    return sum(p.stat().st_size for p in model_paths(directory, model_id).values() if p.exists())


def remove_model(cfg, model_id: str) -> int:
    """Ein Bildmodell löschen → freigegebene Bytes. Dateien, die ein anderes installiertes Modell auch nutzt (z. B.
    das FLUX-VAE), bleiben. War es das aktive, wird ein anderes installiertes aktiv."""
    directory = image_dir(cfg)
    data = manifest(directory)
    entry = (data.get("models") or {}).pop(model_id, None)
    if entry is None:
        return 0
    keep = {name for other in data["models"].values() for name in (other.get("files") or {}).values()}
    freed = 0
    for name in set((entry.get("files") or {}).values()) - keep:
        for path in (directory / "models" / name, directory / "models" / (name + ".part")):
            if path.is_file():
                freed += path.stat().st_size
                path.unlink()
    if data.get("active") == model_id:
        data["active"] = next(iter(data["models"]), "")
    _write_manifest(directory, data)
    return freed


def remove(cfg=None) -> int:
    """Alle Modelldateien löschen (das Programm bleibt, es ist klein) → freigegebene Bytes."""
    directory = image_dir(cfg)
    freed = 0
    for path in (directory / "models").glob("*") if (directory / "models").exists() else []:
        if path.is_file():
            freed += path.stat().st_size
            path.unlink()
    (directory / "manifest.json").unlink(missing_ok=True)
    return freed


def catalog(cfg, vram_gb: float) -> list[dict]:
    """Für die Oberfläche: alle Bildmodelle mit Zustand (installiert, aktiv, Platz auf der Platte, passt es)."""
    directory = image_dir(cfg)
    active = active_id(directory)
    out = []
    for m in CATALOG:
        installed = model_installed(directory, m.id)
        fit = "ok" if vram_gb >= m.min_vram_gb + 2 else "tight" if vram_gb >= m.min_vram_gb or vram_gb == 0 else "big"
        out.append({"id": m.id, "label": m.label, "note": m.note(), "size_gb": m.size_gb, "steps": m.steps,
                    "edit": m.edit, "license": m.license, "installed": installed, "active": installed and m.id == active,
                    "disk_gb": round(disk_size(directory, m.id) / 1e9, 1) if installed else 0, "fit": fit,
                    "min_vram_gb": m.min_vram_gb})
    return out
