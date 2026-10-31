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
from dataclasses import dataclass, replace
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


# Eigenes Bild-Backend (Experten): ein Server, den Orbwise nicht startet – nur ansprechen
API_KINDS = {"openai": "OpenAI Images API", "a1111": "Automatic1111 / Forge", "sdcpp": "stable-diffusion.cpp (sd-server)"}


def is_api(entry: dict) -> bool:
    return entry.get("backend") in API_KINDS


def api_id(label: str) -> str:
    return "api-" + (re.sub(r"[^a-z0-9]+", "-", label.lower()).strip("-")[:40] or "server")


def add_api_model(cfg, label: str, kind: str, base_url: str, model: str = "") -> str:
    """Eigenes Bild-Backend eintragen (der API-Schlüssel liegt im Schlüsselbund, nicht hier) und aktiv machen."""
    if kind not in API_KINDS:
        raise ValueError("unbekannte Schnittstelle")
    if not re.match(r"^https?://[^\s/]+", base_url.strip()):
        raise ValueError(T("Adresse bitte mit http:// oder https://", "Address please with http:// or https://"))
    directory = image_dir(cfg)
    model_id = api_id(label)
    data = manifest(directory) or {"models": {}}
    data["models"][model_id] = {"backend": kind, "base_url": base_url.strip().rstrip("/"), "model": model.strip(),
                                "label": label.strip() or model_id, "files": {}}
    data["active"] = model_id
    _write_manifest(directory, data)
    return model_id


def spec_of(directory: Path, model_id: str) -> ImageModel | None:
    """Katalogeintrag – für ein frei von Hugging Face geladenes Modell der seiner Familie (Bauteile, Optionen,
    Schritte), für ein eigenes Backend ein schlichter Eintrag ohne Dateien."""
    if model_id in BY_ID:
        return BY_ID[model_id]
    entry = model_entry(directory, model_id)
    if is_api(entry):
        kind = API_KINDS[entry["backend"]]
        return ImageModel(model_id, entry.get("label") or model_id, f"Eigenes Backend: {kind} · {entry.get('base_url')}",
                          f"Own backend: {kind} · {entry.get('base_url')}", {}, (), (), lambda t, v: {}, 0, 0,
                          25 if entry["backend"] == "a1111" else 20, 7.0 if entry["backend"] == "a1111" else 0.0,
                          edit=entry["backend"] == "sdcpp")
    base = FAMILIES.get(entry.get("family", ""))
    if base is None:
        return None
    label = entry.get("label") or model_id
    return replace(base, id=model_id, label=label, note_de=f"Von Hugging Face: {entry.get('repo', '')} "
                   f"({base.label}-Familie)", note_en=f"From Hugging Face: {entry.get('repo', '')} ({base.label} family)",
                   search=(), new=False)


def model_installed(directory: Path, model_id: str) -> bool:
    if is_api(model_entry(directory, model_id)):
        return True  # eigenes Backend: keine Dateien
    spec = spec_of(directory, model_id)
    paths = model_paths(directory, model_id)
    need = spec.required if spec else ("diffusion",)
    return bool(paths) and all(paths.get(r) and paths[r].exists() for r in need)


def is_set_up(directory: Path) -> bool:
    model = active_id(directory)
    if model and is_api(model_entry(directory, model)):
        return True  # eigenes Backend braucht kein sd-server hier
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
    search: tuple = ()   # Suchbegriffe: Repositories, die beim Laden auf Hugging Face gesucht werden (neue Modelle,
                         # deren GGUF-Hochlader noch wechseln) – ihre Dateilisten kommen zu den festen dazu
    new: bool = False    # in der Oberfläche als „neu“ markiert

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


FLUX_DEV_GGUF_REPO = "city96/FLUX.1-dev-gguf"
QWEN3_4B_REPOS = ("unsloth/Qwen3-4B-GGUF", ZIMAGE_TEXT_REPO)
QWEN3_8B_REPOS = ("unsloth/Qwen3-8B-GGUF", "Qwen/Qwen3-8B-GGUF")
FLUX2_VAE_REPOS = ("Comfy-Org/flux2-dev", "Comfy-Org/flux2-klein")


def _choose_flux_dev(trees: dict[str, list[str]], vram: float) -> dict[str, tuple[str, str]]:
    out = _choose_flux({**trees, FLUX_GGUF_REPO: trees.get(FLUX_DEV_GGUF_REPO, [])}, vram)
    if "diffusion" in out:
        out["diffusion"] = (FLUX_DEV_GGUF_REPO, out["diffusion"][1])
    return out


def _first(trees: dict[str, list[str]], repos, choose) -> tuple[str, str] | None:
    """(Repository, Datei) aus dem ersten der Repositories, in dem choose(Dateien) etwas findet."""
    for repo in repos:
        hit = choose(trees.get(repo, []))
        if hit:
            return repo, hit
    return None


def _searched(trees: dict[str, list[str]]) -> list[str]:
    """Die beim Laden gesuchten Repositories (siehe ImageModel.search), beliebteste zuerst."""
    return trees.get("__search__", [])


def _choose_flux2_klein(size: str):
    """FLUX.2 [klein] 4B/9B: GGUF des Diffusionsmodells (gesucht), Text-Encoder Qwen3-4B bzw. -8B, FLUX.2-VAE."""
    text_repos = QWEN3_4B_REPOS if size == "4b" else QWEN3_8B_REPOS

    def choose(trees: dict[str, list[str]], vram: float) -> dict[str, tuple[str, str]]:
        out = {}
        quants = quant_preference(vram) + ["Q8_0", "Q4_0"]

        def model_file(files: list[str]) -> str | None:
            mine = [f for f in files if "klein" in f.lower() and size in f.lower().replace("-", "").replace("_", "")]
            return pick(mine or files, quants)
        if (hit := _first(trees, _searched(trees), model_file)):
            out["diffusion"] = hit
        if (hit := _first(trees, text_repos, lambda files: pick(files, _small_text(vram)))):
            out["llm"] = hit
        vae = _first(trees, FLUX2_VAE_REPOS, lambda files: next(
            (f for f in files if re.search(r"flux2[-_]?(vae|ae)\.safetensors$", f.rsplit("/", 1)[-1], re.I)), None))
        if vae:
            out["vae"] = vae
        return out
    return choose


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
    ImageModel("flux2-klein-4b", "FLUX.2 klein 4B",
               "Neueste FLUX-Generation, klein und schnell (4 Schritte), Apache-Lizenz – gut für 8–12 GB.",
               "Newest FLUX generation, small and fast (4 steps), Apache license – good for 8–12 GB.",
               {"diffusion": "--diffusion-model", "vae": "--vae", "llm": "--llm"},
               ("diffusion", "llm", "vae"), (*QWEN3_4B_REPOS, *FLUX2_VAE_REPOS), _choose_flux2_klein("4b"),
               7, 8, 4, 1.0, license="apache-2.0", search=("FLUX.2-klein-4B",), new=True),
    ImageModel("flux2-klein-9b", "FLUX.2 klein 9B",
               "Neueste FLUX-Generation, größer und detailreicher (4 Schritte) – für 12–20 GB; nicht kommerziell.",
               "Newest FLUX generation, larger and more detailed (4 steps) – for 12–20 GB; non-commercial.",
               {"diffusion": "--diffusion-model", "vae": "--vae", "llm": "--llm"},
               ("diffusion", "llm", "vae"), (*QWEN3_8B_REPOS, *FLUX2_VAE_REPOS), _choose_flux2_klein("9b"),
               12, 12, 4, 1.0, license="flux-non-commercial", search=("FLUX.2-klein-9B",), new=True),
    ImageModel("flux-dev", "FLUX.1 dev",
               "Die große FLUX.1-Variante – sehr detailreich, 20 Schritte; ab 12 GB, mit 20 GB in Q8. Nicht kommerziell.",
               "The large FLUX.1 variant – very detailed, 20 steps; from 12 GB, Q8 with 20 GB. Non-commercial.",
               {"diffusion": "--diffusion-model", "vae": "--vae", "clip_l": "--clip_l", "t5xxl": "--t5xxl"},
               ("diffusion", "vae", "clip_l", "t5xxl"), (FLUX_DEV_GGUF_REPO, FLUX_TEXT_REPO, FLUX_REPO),
               _choose_flux_dev, 12, 12, 20, 1.0, license="flux-1-dev-non-commercial"),
    ImageModel("sdxl", "Stable Diffusion XL 1.0",
               "Der Klassiker – klein, läuft auch auf schwächeren Karten; Prompts am besten auf Englisch.",
               "The classic – small, runs on weaker cards too; prompts work best in English.",
               {"diffusion": "--model", "vae": "--vae"},
               ("diffusion", "vae"), (SDXL_REPO, SDXL_VAE_REPO), _choose_sdxl,
               7.2, 6, 25, 7.0, sampler="euler_a", license="openrail++"),
)
BY_ID = {m.id: m for m in CATALOG}

# Familien für frei gesuchte Modelle: Bauteile und Startoptionen kommen vom passenden Katalogeintrag. SD 1.5 steht
# nicht im Katalog (zu alt für eine Empfehlung), Feinabstimmungen davon laufen aber.
SD15 = ImageModel("sd15", "Stable Diffusion 1.5", "", "", {"diffusion": "--model"}, ("diffusion",), (),
                  lambda t, v: {}, 2, 4, 25, 7.0, sampler="euler_a", license="openrail")
FAMILIES: dict[str, ImageModel] = {**BY_ID, "sd15": SD15}
# Ungefährer Zusatzbedarf an Grafikspeicher neben der Modelldatei (Text-Encoder teils auf der CPU, VAE, Puffer)
FAMILY_EXTRA_GB = {"sd15": 0.8, "sdxl": 1.2}


def detect_family(repo: str, tags: list[str] | tuple = ()) -> tuple[str, str]:
    """(Familie, Grund wenn nicht unterstützt) aus Repo-Namen und Hugging-Face-Tags (base_model:…, lora …)."""
    tags = [t.lower() for t in tags if isinstance(t, str)]
    text = " ".join([repo.lower(), *tags])
    if "lora" in tags or re.search(r"(^|[-_/ ])lora([-_ ]|$)", repo.lower()):
        return "", T("LoRA – braucht ein Grundmodell, läuft nicht allein", "LoRA – needs a base model, does not run alone")
    if re.search(r"controlnet|ip-?adapter|inpaint|kontext|[-_]edit\b|video|wan2|ltx|hunyuan", text):
        return "", T("Sonderform (Steuerung, Bearbeiten, Video) – wird nicht unterstützt",
                     "special variant (control, editing, video) – not supported")
    flux1_base = re.search(r"base_model:[^ ]*flux\.?1", text)
    rules = (
        (r"klein[-_.]?9b", "flux2-klein-9b"),
        (r"klein[-_.]?4b", "flux2-klein-4b"),
        (r"klein", "flux2-klein"),  # Größe (4B/9B) ergibt sich aus der Datei (siehe klein_size)
        (r"flux[-_.]?[2-9]", ""),  # FLUX.2 dev, FLUX.3 … – andere Bauteile, noch nicht unterstützt
        (r"flux[-_.]?1[-_.]?schnell", "flux-schnell"),
        (r"flux[-_.]?1|flux1", "flux-dev"),
        (r"flux", "flux-dev" if flux1_base else "?flux"),
        (r"qwen[-_.]?image", "qwen-image"),
        (r"z[-_.]?image", "z-image-turbo"),
        (r"stable-diffusion-xl|sdxl|sd[-_.]?xl|\bpony|illustrious|noobai|playground-v2", "sdxl"),
        (r"stable-diffusion-v1|sd[-_.]?1[-_.]?5|\bsd15\b|stable-diffusion-1|dreamshaper(?!.*xl)", "sd15"),
    )
    for pattern, family in rules:
        if re.search(pattern, text):
            if family == "?flux":
                return "", T("FLUX-Version unklar (weder FLUX.1 noch klein erkennbar) – wird nicht unterstützt",
                             "FLUX version unclear (neither FLUX.1 nor klein recognisable) – not supported")
            return (family, "") if family else ("", T("neuere Modellgeneration – wird noch nicht unterstützt",
                                                       "newer model generation – not supported yet"))
    return "", T("unbekannte Modellfamilie – nur FLUX, Qwen-Image, Z-Image, SDXL und SD 1.5 laufen",
                 "unknown model family – only FLUX, Qwen-Image, Z-Image, SDXL and SD 1.5 run")


_QUANT_BITS = (("BF16", 16), ("F16", 16), ("F32", 32), ("Q8", 8.5), ("Q6", 6.6), ("Q5", 5.7), ("Q4", 4.85),
               ("Q3", 3.5), ("Q2", 2.6))


def klein_size(file: str, size_bytes: int) -> str:
    """FLUX.2 klein ohne Größe im Namen: aus Dateigröße und Quantisierung auf 4B oder 9B schließen."""
    name = file.rsplit("/", 1)[-1].upper()
    if re.search(r"9B", name):
        return "flux2-klein-9b"
    if re.search(r"4B", name):
        return "flux2-klein-4b"
    bits = next((b for q, b in _QUANT_BITS if re.search(rf"(^|[-_.]){q}", name)), 16 if name.endswith(".SAFETENSORS")
                else 8.5)
    params = size_bytes * 8 / bits
    return "flux2-klein-9b" if params > 6.5e9 else "flux2-klein-4b"


_NOT_A_MODEL = re.compile(r"(^|[-_/.])(vae|ae|clip[-_]?[lg]?|t5(xxl)?|text[-_]?encoder|mmproj|lora|refiner|"
                          r"controlnet|embedding|tokenizer)([-_.]|$)", re.I)


def model_files(family: str, files: list[dict]) -> list[dict]:
    """Dateien eines Repos, die als Bildmodell taugen: GGUF bzw. ganze Checkpoints – ohne VAE, Text-Encoder, LoRA,
    Diffusers-Unterordner und geteilte Dateien."""
    out = []
    for f in files:
        path, name = f["path"], f["path"].rsplit("/", 1)[-1]
        low = name.lower()
        if not low.endswith((".gguf", ".safetensors")) or _NOT_A_MODEL.search(name.rsplit(".", 1)[0]):
            continue
        if "/" in path and path.split("/", 1)[0].lower() in ("unet", "transformer", "vae", "text_encoder",
                                                              "text_encoder_2", "text_encoder_3", "tokenizer"):
            continue  # Diffusers-Aufteilung – sd.cpp braucht eine ganze Datei
        if re.search(r"-\d{5}-of-\d{5}\.", low):
            continue
        if family in ("sd15", "sdxl") and f["size"] and f["size"] < 1.5e9:
            continue  # zu klein für einen ganzen Checkpoint (LoRA, Embedding)
        out.append(f)
    return out


def custom_id(repo: str, file: str) -> str:
    stem = file.rsplit("/", 1)[-1].rsplit(".", 1)[0]
    return "hf-" + re.sub(r"[^a-z0-9]+", "-", f"{repo.split('/')[-1]}-{stem}".lower()).strip("-")[:80]


class InstallCancelled(Exception):
    pass


# ---------------------------------------------------------------- Netz
def _client() -> httpx.Client:
    return httpx.Client(follow_redirects=True, timeout=httpx.Timeout(60, read=300),
                        headers={"User-Agent": "orbwise"})


def hf_tree(repo: str, client: httpx.Client) -> list[str]:
    """Dateien eines Repositories – leer, wenn es (noch) nicht existiert (Ausweich-Repositories im Katalog)."""
    r = client.get(f"{_hf_base()}/api/models/{repo}/tree/main", params={"recursive": "1"},
                   headers=_auth_headers())
    if r.status_code == 404:
        return []
    r.raise_for_status()
    return [f["path"] for f in r.json() if f.get("type") == "file"]


def hf_find(query: str, client: httpx.Client, limit: int = 5) -> list[str]:
    """GGUF-Repositories zu einem Suchbegriff, meiste Downloads zuerst (für neue Modelle im Katalog)."""
    r = client.get(f"{_hf_base()}/api/models", headers=_auth_headers(),
                   params={"search": query, "filter": "gguf", "sort": "downloads", "direction": "-1", "limit": limit})
    if r.status_code >= 400:
        return []
    return [m.get("id", "") for m in r.json() if isinstance(m, dict) and "/" in m.get("id", "")]


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
               **(_auth_headers() if url.startswith(_hf_base()) else {})}
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


def runtime_version(directory: Path) -> str:
    """Release von stable-diffusion.cpp, das in <dir>/bin liegt (leer: unbekannt bzw. eigener Build)."""
    try:
        return (directory / "bin" / ".version").read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def install_binary(directory: Path, client: httpx.Client, out: Out, update: bool = False) -> bool:
    """Neuesten fertigen Linux-Vulkan-Build von stable-diffusion.cpp nach <dir>/bin. update=True: auch wenn schon einer
    da ist – neuere Modelle (z. B. FLUX.2 klein) brauchen oft einen neueren sd-server. Der alte bleibt, bis der neue
    vollständig ausgepackt ist."""
    if server_binary(directory).exists() and not update:
        return True
    r = client.get("https://api.github.com/repos/" + SD_REPO + "/releases/latest")
    r.raise_for_status()
    release = r.json()
    tag = str(release.get("tag_name") or release.get("name") or "")
    asset = next((a for a in release.get("assets", []) if RELEASE_ASSET.search(a.get("name", ""))), None)
    if not asset:
        out(T("✘ Kein fertiger Linux-Vulkan-Build gefunden – stable-diffusion.cpp selbst bauen (docs/build.md) und "
              f"sd-server nach {server_binary(directory)} legen.",
              "✘ No prebuilt Linux Vulkan build found – build stable-diffusion.cpp yourself (docs/build.md) and put "
              f"sd-server at {server_binary(directory)}."))
        return False
    if update and tag and tag == runtime_version(directory) and server_binary(directory).exists():
        out(T(f"✔ sd-server ist aktuell ({tag}).", f"✔ sd-server is up to date ({tag})."))
        return True
    archive = directory / asset["name"]
    out(T(f"Lade {asset['name']} …", f"Downloading {asset['name']} …"))
    directory.mkdir(parents=True, exist_ok=True)
    download(asset["browser_download_url"], archive, client, out)
    staging = directory / "bin.new"
    shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True)
    with zipfile.ZipFile(archive) as z:
        for member in z.infolist():
            name = Path(member.filename).name
            if not name or member.is_dir():
                continue
            target = staging / name
            with z.open(member) as src, target.open("wb") as dst:
                shutil.copyfileobj(src, dst)
            target.chmod(target.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP)  # Programme und Bibliotheken
    archive.unlink(missing_ok=True)
    if not (staging / "sd-server").exists():
        shutil.rmtree(staging, ignore_errors=True)
        out(T("✘ Im Build fehlt sd-server.", "✘ sd-server is missing from the build."))
        return False
    if tag:
        (staging / ".version").write_text(tag + "\n", encoding="utf-8")
    old = directory / "bin"
    if old.exists():
        backup = directory / "bin.old"
        shutil.rmtree(backup, ignore_errors=True)
        old.rename(backup)
        staging.rename(old)
        shutil.rmtree(backup, ignore_errors=True)
    else:
        staging.rename(old)
    if update:
        out(T(f"✔ sd-server aktualisiert ({tag or '?'}).", f"✔ sd-server updated ({tag or '?'})."))
    return True


def _hf_base() -> str:
    """https://huggingface.co – oder der Spiegel aus HF_ENDPOINT (wie bei den Sprachmodellen)."""
    from .models import HF_API
    return HF_API[:-4] if HF_API.endswith("/api") else "https://huggingface.co"


def _auth_headers() -> dict:
    from .models import hf_token
    tok = hf_token()
    return {"Authorization": f"Bearer {tok}"} if tok else {}


def install(gpu: dict, cfg=None, out: Out = print, client: httpx.Client | None = None, model_id: str = "qwen-image",
            stop: threading.Event | None = None, source: dict | None = None) -> bool:
    """Programm (einmalig) und die Dateien eines Bildmodells laden; das Modell wird danach das aktive.
    source = {"repo", "file", "family", "label"}: ein frei gesuchtes Modell – die Datei kommt von dort, Text-Encoder
    und VAE wie bei seiner Familie."""
    spec = FAMILIES.get(source["family"]) if source else BY_ID.get(model_id)
    if source and spec is None:
        out(T("✘ Diese Modellfamilie wird nicht unterstützt.", "✘ This model family is not supported."))
        return False
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
        found = [] if source else [r for q in spec.search for r in hf_find(q, client)]
        trees = {repo: hf_tree(repo, client) for repo in dict.fromkeys([*spec.repos, *found])}
        trees["__search__"] = found
        chosen = spec.choose(trees, vram)
        if source:  # die gewählte Datei statt der Katalog-Datei, Text-Encoder/VAE der Familie
            chosen["diffusion"] = (source["repo"], source["file"])
        missing = [r for r in spec.required if r not in chosen]
        if missing:
            out(T("✘ Modelldateien nicht gefunden: ", "✘ Model files not found: ") + ", ".join(missing))
            return False
        files = {}
        for i, (role, (repo, path)) in enumerate(chosen.items(), 1):
            name = path.rsplit("/", 1)[-1]
            out(T(f"Lade {name} ({role}, {i}/{len(chosen)}) …", f"Downloading {name} ({role}, {i}/{len(chosen)}) …"))
            download(f"{_hf_base()}/{repo}/resolve/main/{path}", directory / "models" / name, client, out,
                     stop=stop)
            files[role] = name
        data = manifest(directory) or {"models": {}}
        data["models"][model_id] = {"files": files, "vram_gb": vram, **({
            "family": source["family"], "repo": source["repo"], "label": source.get("label") or model_id}
            if source else {})}
        data["active"] = model_id
        _write_manifest(directory, data)
        label = (source or {}).get("label") or spec.label
        out(T(f"✔ {label} eingerichtet ({directory}). Im Dashboard oben links „Bild“ wählen.",
              f"✔ {label} set up ({directory}). Choose \"Image\" at the top left of the dashboard."))
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
                    "min_vram_gb": m.min_vram_gb, "new": m.new})
    for model_id, entry in (manifest(directory).get("models") or {}).items():  # frei von Hugging Face geladen
        spec = spec_of(directory, model_id) if model_id not in BY_ID else None
        if spec is None or not model_installed(directory, model_id):
            continue
        diff = model_paths(directory, model_id).get("diffusion")
        now, _ = detect_family(f"{entry.get('repo', '')} {diff.name if diff else ''}") if entry.get("family") else ("", "")
        if now == "flux2-klein" and diff is not None and diff.exists():
            now = klein_size(diff.name, diff.stat().st_size)
        misfiled = bool(entry.get("family")) and now != entry.get("family") and now in FAMILIES
        out.append({"id": model_id, "label": spec.label, "note": spec.note(), "size_gb": 0, "steps": spec.steps,
                    "misfiled": FAMILIES[now].label if misfiled else "",
                    "edit": spec.edit, "license": "", "installed": True, "active": model_id == active,
                    "disk_gb": round(disk_size(directory, model_id) / 1e9, 1), "fit": "ok",
                    "min_vram_gb": 0, "new": False, "custom": True, "repo": entry.get("repo", ""),
                    "family": entry.get("family", ""), "api": entry.get("backend") if is_api(entry) else "",
                    "base_url": entry.get("base_url", "") if is_api(entry) else ""})
    return out


# ---------------------------------------------------------------- Alle Text-zu-Bild-Modelle auf Hugging Face
def hf_image_search(query: str = "", sort: str = "downloads", limit: int = 30) -> list[dict]:
    """Text-zu-Bild-Modelle auf Hugging Face (ohne Suchbegriff: gerade beliebte, nur unterstützte). Je Treffer die
    erkannte Familie – oder warum es nicht läuft."""
    from .models import HF_SORTS, _hf_get
    words = [w for w in re.split(r"\s+", query.strip().lower()) if w]
    params = [("pipeline_tag", "text-to-image"), ("direction", "-1"),
              ("sort", HF_SORTS.get(sort, "downloads") if words else "trendingScore"),
              *[("expand[]", f) for f in ("downloads", "likes", "lastModified", "gated", "tags", "trendingScore")]]
    found = _hf_get("/models", [*params, ("search", " ".join(words)), ("limit", str(limit))] if words
                    else [*params, ("limit", "100")])
    if not found and len(words) > 1:  # HF sucht Teilstrings – mit dem längsten Wort, dann filtern
        found = _hf_get("/models", [*params, ("search", max(words, key=len)), ("limit", "100")])
    out = []
    for m in found if isinstance(found, list) else []:
        repo = m.get("id") or ""
        if "/" not in repo or not all(w in repo.lower() for w in words):
            continue
        tags = [t for t in m.get("tags") or [] if isinstance(t, str)]
        family, reason = detect_family(repo, tags)
        if not words and not family:
            continue  # Vorschläge: nur, was hier läuft
        base = FAMILIES.get(family) or (FAMILIES["flux2-klein-4b"] if family == "flux2-klein" else None)
        out.append({"repo": repo, "downloads": m.get("downloads", 0) or 0, "likes": m.get("likes", 0) or 0,
                    "updated": str(m.get("lastModified") or "")[:10], "gated": bool(m.get("gated")),
                    "license": next((t.split(":", 1)[1] for t in tags if t.startswith("license:")), ""),
                    "gguf": "gguf" in tags, "family": family,
                    "family_label": "FLUX.2 klein" if family == "flux2-klein" else base.label if base else "",
                    "supported": bool(family), "reason": reason})
        if len(out) >= (limit if words else 20):
            break
    return out


def hf_image_files(repo: str, vram_gb: float, directory: Path | None = None) -> dict:
    """Die ladbaren Dateien eines Repos (GGUF-Quantisierungen bzw. Checkpoints) mit Größe und Passung."""
    from .models import QUANT_RE, REPO_RE, _hf_get, fit_need
    if not REPO_RE.match(repo):
        raise ValueError("ungültiges Repo")
    try:
        info = _hf_get(f"/models/{repo}", [("expand[]", "tags"), ("expand[]", "gated")])
    except httpx.HTTPError:
        info = {}
    tags = [t for t in (info or {}).get("tags") or [] if isinstance(t, str)]
    family, reason = detect_family(repo, tags)
    tree = _hf_get(f"/models/{repo}/tree/main", {"recursive": "1"})
    files = [{"path": f.get("path", ""), "size": int((f.get("lfs") or {}).get("size") or f.get("size") or 0)}
             for f in tree if isinstance(f, dict) and f.get("type") == "file"] if isinstance(tree, list) else []
    variants = []
    extra = FAMILY_EXTRA_GB.get(family, 1.5)
    installed = {e.get("repo", ""): set((e.get("files") or {}).values())
                 for e in (manifest(directory).get("models") or {}).values()} if directory else {}
    for f in model_files(family, files) if family else []:
        name = f["path"].rsplit("/", 1)[-1]
        fam = klein_size(f["path"], f["size"]) if family == "flux2-klein" else family
        q = QUANT_RE.search(name if name.lower().endswith(".gguf") else name.rsplit(".", 1)[0] + ".gguf")
        size = f["size"] / 1e9
        variants.append({"file": f["path"], "name": name, "quant": q.group(1).upper() if q else "", "family": fam,
                         "size_gb": round(size, 1), "need_gb": round(size + extra, 1),
                         "fit": fit_need(size + extra, vram_gb), "id": custom_id(repo, f["path"]),
                         "installed": name in installed.get(repo, set())})
    variants.sort(key=lambda v: v["size_gb"])
    ok = [v for v in variants if v["fit"] == "ok"]
    if ok:
        max(ok, key=lambda v: v["size_gb"])["recommended"] = True
    base = FAMILIES.get(family) or FAMILIES.get(variants[0]["family"] if variants else "")
    return {"repo": repo, "family": family, "family_label": base.label if base else "", "reason": reason,
            "steps": base.steps if base else 0, "gated": bool((info or {}).get("gated")), "variants": variants}

