"""Modell-Vorauswahl: bekannte Ollama-Modelle mit Größe und Grafikspeicher-Bedarf, GPU-Erkennung und eine
interaktive Auswahl (Installer und `orbwise model add`). Zusätzlich geladene Modelle werden als Profile gemerkt
(state.json → added_models) und erscheinen im Modell-Menü der Oberfläche."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path

import httpx

from . import state
from .lang import T


@dataclass(frozen=True)
class Preset:
    tag: str            # Ollama-Name
    label: str
    download_gb: float  # ungefähre Downloadgröße
    vram_gb: float      # komfortabel komplett im Grafikspeicher
    note_de: str
    note_en: str

    def note(self) -> str:
        return T(self.note_de, self.note_en)


PRESETS: list[Preset] = [
    Preset("qwen3:4b", "Qwen 3 4B", 2.6, 4, "klein und flott – für schwache GPUs oder nur CPU",
           "small and quick – for weak GPUs or CPU only"),
    Preset("qwen3:8b", "Qwen 3 8B", 5.2, 7, "gutes Tool-Calling, ideal für 8 GB",
           "good tool calling, ideal for 8 GB"),
    Preset("qwen3:14b", "Qwen 3 14B", 9.3, 12, "deutlich klüger – Empfehlung für 12–16 GB",
           "noticeably smarter – recommended for 12–16 GB"),
    Preset("qwen3:30b", "Qwen 3 30B-A3B (MoE)", 19, 20, "sehr schnell dank MoE; mit 16 GB teilweise im RAM",
           "very fast thanks to MoE; partly in RAM with 16 GB"),
    Preset("qwen3:32b", "Qwen 3 32B", 20, 24, "stark, braucht 24 GB", "strong, needs 24 GB"),
    Preset("qwen3.5:4b", "Qwen 3.5 4B", 3.4, 5, "neuere Qwen-Generation, klein", "newer Qwen generation, small"),
    Preset("qwen3.5:9b", "Qwen 3.5 9B", 6.6, 9, "neuere Qwen-Generation – für 10–16 GB",
           "newer Qwen generation – for 10–16 GB"),
    Preset("qwen3.5:27b", "Qwen 3.5 27B", 17, 22, "neuere Qwen-Generation, groß", "newer Qwen generation, large"),
    Preset("gpt-oss:20b", "gpt-oss 20B (OpenAI)", 14, 16, "starkes Tool-Calling, denkt immer ein wenig",
           "strong tool calling, always reasons a little"),
    Preset("mistral-small3.2:24b", "Mistral Small 3.2 24B", 15, 18, "gut in Deutsch/Englisch, 16 GB knapp",
           "good in German/English, 16 GB is tight"),
    Preset("llama3.1:8b", "Llama 3.1 8B", 4.9, 7, "bewährt, Tool-Calling solide", "proven, solid tool calling"),
]
BY_TAG = {p.tag: p for p in PRESETS}
TAG_RE = re.compile(r"^[a-z0-9][a-z0-9._/-]*(:[a-z0-9._-]+)?$")
# GGUF-Modelle von Hugging Face: hf.co/<nutzer>/<repo>[:<quant>] (Groß/Klein bleibt erhalten)
HF_TAG_RE = re.compile(r"^hf\.co/[A-Za-z0-9][\w.-]*/[\w.-]+(:[\w.-]+)?$")
HF_URL_RE = re.compile(r"^(?:https?://)?(?:www\.)?(?:huggingface\.co|hf\.co)/([A-Za-z0-9][\w.-]*)/([\w.-]+)"
                       r"(?:/(?:blob|resolve)/[^/]+/(?:[^?#]*/)?([^/?#]+\.gguf)|/tree/[^?#]*)?(?::([\w.-]+))?/?(?:[?#].*)?$",
                       re.IGNORECASE)
QUANT_RE = re.compile(r"(?:^|[-_.])((?:UD-)?(?:I?Q\d(?:_[A-Z0-9]+)*|TQ\d_\d|BF16|F16|F32|MXFP4(?:_MOE)?))\.gguf$",
                      re.IGNORECASE)
REPO_RE = re.compile(r"^[A-Za-z0-9][\w-]*(?:\.[\w-]+)*/[A-Za-z0-9][\w-]*(?:\.[\w-]+)*$")  # nutzer/repo, ohne ".."
SPLIT_RE = re.compile(r"-\d{5}-of-\d{5}\.gguf$", re.IGNORECASE)


def normalize_tag(text: str) -> str | None:
    """Ollama-Name, Hugging-Face-Link oder hf.co-Name → gültiger Name (sonst None). hf.co/… lädt Orbwise selbst und
    startet es mit llama.cpp (siehe llamacpp.py), alles andere geht an Ollama.
    'https://huggingface.co/unsloth/Qwen3-8B-GGUF/blob/main/Qwen3-8B-Q4_K_M.gguf' → 'hf.co/unsloth/Qwen3-8B-GGUF:Q4_K_M'"""
    text = (text or "").strip()
    m = HF_URL_RE.match(text)
    if m:
        user, repo, file, quant = m.groups()
        if file:
            q = QUANT_RE.search(SPLIT_RE.sub(".gguf", file))  # geteilte Dateien: llama.cpp lädt alle Teile
            quant = q.group(1) if q else None
        return f"hf.co/{user}/{repo}" + (f":{quant}" if quant else "")
    if HF_TAG_RE.match(text):
        return text
    return text.lower() if TAG_RE.match(text.lower()) else None


def is_hf(tag: str) -> bool:
    return tag.startswith("hf.co/")


def label_of(tag: str) -> str:
    """Anzeigename: Preset-Name oder bei Hugging Face 'Qwen3-8B Q4_K_M' statt des langen hf.co-Namens."""
    if tag in BY_TAG:
        return BY_TAG[tag].label
    if is_hf(tag):
        repo, _, quant = tag.split("/", 2)[2].partition(":")
        repo = re.sub(r"[-_.]?gguf$", "", repo, flags=re.IGNORECASE)
        return f"{repo} {quant}".strip()
    return tag


def slug(tag: str) -> str:
    """Profilname aus einem Ollama-Namen: 'qwen3:14b' → 'qwen3-14b'."""
    return re.sub(r"[^a-z0-9.-]+", "-", tag.lower()).strip("-")


def recommend(vram_gb: float) -> str:
    if vram_gb >= 22:
        return "qwen3:30b"
    if vram_gb >= 12:
        return "qwen3:14b"
    if vram_gb >= 6:
        return "qwen3:8b"
    return "qwen3:4b"


def fit(p: Preset, vram_gb: float) -> str:
    """ok = passt komplett in den Grafikspeicher, tight = teilweise im RAM (langsamer), big = zu groß."""
    return fit_need(p.vram_gb, vram_gb)


def fit_need(need_gb: float, vram_gb: float) -> str:
    if vram_gb >= need_gb:
        return "ok"
    if vram_gb >= need_gb * 0.7 or (vram_gb == 0 and need_gb <= 5):
        return "tight"
    return "big"


# ---------------------------------------------------------------- GPU-Erkennung

def detect_gpus() -> list[dict]:
    """Alle Grafikkarten: [{'index', 'vendor', 'name', 'vram_gb'}] – NVIDIA über nvidia-smi, AMD aus sysfs (Karten unter
    2 GB, meist die im Prozessor eingebaute Grafik, zählen nicht)."""
    if shutil.which("nvidia-smi"):
        try:
            out = subprocess.run(["nvidia-smi", "--query-gpu=index,name,memory.total", "--format=csv,noheader,nounits"],
                                 capture_output=True, text=True, timeout=10).stdout.strip().splitlines()
            gpus = []
            for line in out:
                idx, rest = line.split(",", 1)
                name, mib = [x.strip() for x in rest.rsplit(",", 1)]
                gpus.append({"index": int(idx), "vendor": "nvidia", "name": name, "vram_gb": round(float(mib) / 1024, 1)})
            if gpus:
                return gpus
        except (OSError, ValueError, subprocess.SubprocessError):
            pass
    gpus = []
    cards = sorted(Path("/sys/class/drm").glob("card*/device/mem_info_vram_total"),
                   key=lambda f: int(re.sub(r"\D", "", f.parent.parent.name) or 0))
    for f in cards:
        try:
            total = int(f.read_text().strip())
        except (OSError, ValueError):
            continue
        if total >= 2 * 1024 ** 3:
            gpus.append({"index": len(gpus), "vendor": "amd", "name": _amd_name(), "vram_gb": round(total / 1024 ** 3, 1)})
    return gpus


def gpu_label(gpus: list[dict]) -> str:
    """„RTX 3090“, „2× RTX 3090“ oder „RTX 3090 + RTX 3060“."""
    names = [g["name"] for g in gpus]
    if len(names) > 1 and len(set(names)) == 1:
        return f"{len(names)}× {names[0]}"
    return " + ".join(names)


def detect_gpu() -> dict:
    """{'vendor': 'nvidia'|'amd'|'none', 'name': str, 'vram_gb': float, 'gpus': [...]} – bei mehreren Karten zählt der
    gemeinsame Grafikspeicher (llama.cpp verteilt das Modell auf alle)."""
    gpus = detect_gpus()
    if not gpus:
        return {"vendor": "none", "name": "", "vram_gb": 0.0, "gpus": []}
    return {"vendor": gpus[0]["vendor"], "name": gpu_label(gpus),
            "vram_gb": round(sum(g["vram_gb"] for g in gpus), 1), "gpus": gpus}


def _amd_name() -> str:
    if not shutil.which("lspci"):
        return "AMD"
    try:
        out = subprocess.run(["lspci"], capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return "AMD"
    for line in out.splitlines():
        if ("VGA" in line or "Display" in line) and ("AMD" in line or "ATI" in line):
            return line.split(": ", 1)[-1]
    return "AMD"


# ---------------------------------------------------------------- Ollama

def installed_models(base_url: str = "http://localhost:11434") -> set[str]:
    try:
        r = httpx.get(f"{base_url}/api/tags", timeout=5)
        return {m.get("name", "") for m in r.json().get("models", [])}
    except (httpx.HTTPError, ValueError):
        return set()


def is_installed(tag: str, installed: set[str]) -> bool:
    if is_hf(tag):  # Ollama schreibt hf.co-Namen je nach Version anders groß
        return tag.lower() in {t.lower() for t in installed} or \
            (":" not in tag and f"{tag}:latest".lower() in {t.lower() for t in installed})
    return tag in installed or (":" not in tag and f"{tag}:latest" in installed)


def preset_list(vram_gb: float, installed: set[str]) -> list[dict]:
    rec = recommend(vram_gb)
    return [{**asdict(p), "note": p.note(), "fit": fit(p, vram_gb), "installed": is_installed(p.tag, installed),
             "recommended": p.tag == rec, "profile": slug(p.tag)} for p in PRESETS]


# ---------------------------------------------------------------- Hugging Face: suchen, Quantisierung wählen

# HF_ENDPOINT wie bei huggingface_hub: anderer Server bzw. Spiegel (z. B. https://hf-mirror.com)
HF_API = (os.environ.get("HF_ENDPOINT") or "https://huggingface.co").rstrip("/") + "/api"
HF_SORTS = {"downloads": "downloads", "likes": "likes", "updated": "lastModified", "trending": "trendingScore"}
# Felder, die die HF-API zusätzlich liefern soll: GGUF-Kopf (Architektur, Kontext, Parameter, Chat-Vorlage) u. a.
HF_EXPAND = ("downloads", "likes", "lastModified", "gguf", "gated", "tags", "pipeline_tag", "trendingScore")
TEXT_PIPELINES = {"text-generation", "image-text-to-text", "conversational", None, ""}


def hf_token() -> str:
    """Token für zugangsbeschränkte Modelle (gated): HF_TOKEN aus der Umgebung, sonst in den Einstellungen gespeichert."""
    tok = os.environ.get("HF_TOKEN") or os.environ.get("HUGGING_FACE_HUB_TOKEN")
    if tok:
        return tok.strip()
    try:
        from . import keystore
        return keystore.get("hf_token").strip()
    except Exception:  # noqa: BLE001 – kein Schlüsselbund: ohne Token
        return ""


def _hf_get(path: str, params: dict | list | None = None, base: str | None = None):
    tok = hf_token()
    headers = {"User-Agent": "orbwise", **({"Authorization": f"Bearer {tok}"} if tok else {})}
    r = httpx.get(f"{base or HF_API}{path}", params=params, timeout=15, follow_redirects=True, headers=headers)
    r.raise_for_status()
    return r.json()


def _expand() -> list[tuple[str, str]]:
    return [("expand[]", f) for f in HF_EXPAND]


def q4_gb(params: int | float | None) -> float:
    """Ungefähre Dateigröße bei Q4_K_M (~4,85 Bit je Gewicht) – für Vorschläge, bevor die Dateiliste geladen ist."""
    return round((params or 0) * 4.85 / 8 / 1e9, 1)


def hf_summary(m: dict) -> dict:
    """Das Wichtige eines Modells für die Anzeige: Größe, Architektur, Kontext, Lizenz, Bild/Werkzeuge, gated."""
    rid = m.get("id") or m.get("modelId") or ""
    gguf = m.get("gguf") or {}
    tags = [t for t in m.get("tags") or [] if isinstance(t, str)]
    template = str(gguf.get("chat_template") or "")
    lic = next((t.split(":", 1)[1] for t in tags if t.startswith("license:")), "")
    pipeline = m.get("pipeline_tag") or ""
    params = gguf.get("total") or 0
    return {"repo": rid, "downloads": m.get("downloads", 0) or 0, "likes": m.get("likes", 0) or 0,
            "updated": str(m.get("lastModified") or "")[:10], "gated": bool(m.get("gated")),
            "params": params, "params_b": round(params / 1e9, 1) if params else None,
            "arch": gguf.get("architecture") or "", "context": gguf.get("context_length") or 0,
            "license": lic, "pipeline": pipeline,
            "vision": pipeline == "image-text-to-text" or "vision" in tags or "image-text-to-text" in tags,
            "tools": bool(re.search(r"\btools\b", template)), "est_gb": q4_gb(params) if params else None}


def hf_search(query: str, limit: int = 20, base: str | None = None, sort: str = "downloads") -> list[dict]:
    """GGUF-Modelle auf Hugging Face (Sortierung: downloads, likes, updated, trending). Mehrere Wörter („qwen 3.6
    27b“) müssen alle im Namen vorkommen – die HF-Suche selbst kennt nur Teilstrings, daher wird bei Bedarf mit dem
    längsten Wort gesucht."""
    words = [w for w in re.split(r"\s+", query.strip().lower()) if w]
    if not words:
        return []
    params = [("filter", "gguf"), ("sort", HF_SORTS.get(sort, "downloads")), ("direction", "-1"), *_expand()]
    found = _hf_get("/models", [*params, ("search", " ".join(words)), ("limit", str(limit))], base)
    if not found and len(words) > 1:
        found = _hf_get("/models", [*params, ("search", max(words, key=len)), ("limit", "100")], base)
    out = []
    for m in found if isinstance(found, list) else []:
        rid = m.get("id") or m.get("modelId") or ""
        if "/" not in rid or not all(w in rid.lower() for w in words):
            continue
        out.append(hf_summary(m))
    return out[:limit]


def _family(repo: str) -> str:
    """Modell ohne Hochlader und GGUF-Zusätze – gleiche Modelle mehrerer Hochlader zählen als eins."""
    name = repo.split("/", 1)[-1].lower()
    name = re.sub(r"[-_.](gguf|i1|imatrix|imat)$", "", name)
    name = re.sub(r"[-_.](gguf|i1|imatrix|imat)(?=[-_.]|$)", "", name)
    return re.sub(r"^(qwen|meta-llama|mistralai|google|microsoft)[_-]", "", name)


def hf_suggest(vram_gb: float, limit: int = 12, base: str | None = None) -> list[dict]:
    """Vorschläge ohne Suchbegriff: gerade beliebte GGUF-Sprachmodelle von Hugging Face, die in den Grafikspeicher
    passen (bzw. teilweise) – je Modell nur der beliebteste Hochlader."""
    found = _hf_get("/models", [("filter", "gguf"), ("sort", "trendingScore"), ("direction", "-1"),
                                ("limit", "100"), *_expand()], base)
    out, seen = [], set()
    for m in found if isinstance(found, list) else []:
        s = hf_summary(m)
        if "/" not in s["repo"] or s["pipeline"] not in TEXT_PIPELINES or not s["params"]:
            continue
        if s["params"] < 0.5e9 or "embed" in s["repo"].lower() or "rerank" in s["repo"].lower():
            continue
        fam = _family(s["repo"])
        if fam in seen:
            continue
        seen.add(fam)
        s["fit"] = fit_need(need_gb(s["est_gb"]), vram_gb)
        if s["fit"] == "big":
            continue
        out.append(s)
        if len(out) >= limit:
            break
    return out


def hf_info(repo: str, base: str | None = None) -> dict:
    """Details eines Repos (für die Quant-Auswahl); leer, wenn HF sie nicht liefert."""
    if not REPO_RE.match(repo):
        raise ValueError("ungültiges Repo")
    try:
        data = _hf_get(f"/models/{repo}", _expand(), base)
    except httpx.HTTPError:
        return {}
    return hf_summary(data) if isinstance(data, dict) else {}


# Kurze Einordnung der Quantisierungen (ungefähre Bit je Gewicht, Qualität)
QUANT_NOTES = (
    (r"^(F32|F16|BF16)$", 16, ("unquantisiert – riesig, nur zum Weiterverarbeiten", "unquantized – huge, for conversion")),
    (r"^Q8_0$", 8.5, ("nahezu verlustfrei", "nearly lossless")),
    (r"Q6_K", 6.6, ("sehr nah am Original", "very close to the original")),
    (r"Q5_K", 5.7, ("hohe Qualität", "high quality")),
    (r"Q4_K_XL|Q4_K_L", 5.0, ("guter Kompromiss, wichtige Teile genauer", "good trade-off, key parts more precise")),
    (r"Q4_K|Q4_K_M", 4.85, ("guter Kompromiss – meist die beste Wahl", "good trade-off – usually the best pick")),
    (r"MXFP4", 4.25, ("Originalformat des Modells (4 Bit)", "the model's native 4-bit format")),
    (r"IQ4|Q4_0|Q4_1", 4.5, ("etwas kleiner, kaum schlechter", "a bit smaller, barely worse")),
    (r"Q3|IQ3", 3.5, ("merklich ungenauer – nur wenn nichts Größeres passt", "noticeably less precise – only if "
                                                                              "nothing bigger fits")),
    (r"Q2|IQ2|IQ1|TQ", 2.5, ("stark verkleinert, deutlich schlechter", "heavily shrunk, clearly worse")),
)


def quant_note(quant: str) -> str:
    q = re.sub(r"^UD-", "", quant.upper())
    for pattern, _bits, (de, en) in QUANT_NOTES:
        if re.search(pattern, q):
            return T(de, en)
    return ""


def context_fit(size_gb: float, vram_gb: float, gpus: int = 1, train: int = 0) -> int:
    """Wie viel Kontext neben dem Modell grob in den Grafikspeicher passt (Schätzung ohne die Datei: ~160 KB je
    Token wie llamacpp.pick_context, wenn es nichts Genaueres weiß). 0 = nicht einmal das Modell."""
    from .llamacpp import CONTEXT_STEPS
    budget = vram_gb * 0.95 - max(1, gpus) - size_gb
    if vram_gb <= 0 or budget <= 0:
        return 0
    for ctx in sorted({*CONTEXT_STEPS, 131072}, reverse=True):
        if train and ctx > train:
            continue
        if ctx * 160 * 1024 / 1024 ** 3 <= budget:
            return ctx
    return 0


def need_gb(size_gb: float) -> float:
    """Grafikspeicher für Gewichte + etwas Kontext (~8k) und Puffer."""
    return round(size_gb + 1.5, 1)


def is_sidecar(name: str) -> bool:
    """Beidateien neben einem Modell (Bildmodul mmproj, MTP-Kopf mtp-*) – selbst kein Modell."""
    name = name.rsplit("/", 1)[-1].lower()
    return "mmproj" in name or "mtp-" in name


def _pick_mtp(files: list[dict], quant: str) -> dict | None:
    """MTP-Kopf eines Repos (mtp-*.gguf, für spekulatives Decoding): gleiche Quantisierung, sonst Q8_0, sonst der erste."""
    found = sorted((f for f in files if "mtp-" in f["path"].rsplit("/", 1)[-1].lower()), key=lambda f: f["path"])
    for pref in (quant, "q8_0", "f16"):
        hit = next((f for f in found if pref.lower() in f["path"].rsplit("/", 1)[-1].lower()), None)
        if hit:
            return hit
    return found[0] if found else None


def _pick_mmproj(files: list[dict]) -> dict | None:
    """Bildmodul eines Repos: F16 bevorzugt (BF16 können nicht alle Builds), sonst das erste."""
    found = sorted((f for f in files if "mmproj" in f["path"].rsplit("/", 1)[-1].lower()), key=lambda f: f["path"])
    for pref in ("f16", "bf16", "f32"):
        hit = next((f for f in found if re.search(rf"[-_.]{pref}\.gguf$", f["path"], re.I)), None)
        if hit:
            return hit
    return found[0] if found else None


def hf_quants(repo: str, vram_gb: float, models_dir: Path | None = None, base: str | None = None) -> list[dict]:
    """Die Quantisierungen eines Repos mit Größe und ob sie in den Grafikspeicher passen (kleinste zuerst). Geteilte
    Dateien (-00001-of-00003) werden zusammengefasst; ein Bildmodul (mmproj) wird mitgeladen und mitgezählt.
    installed: liegt die Datei schon in models_dir/<nutzer>_<repo>/?"""
    if not REPO_RE.match(repo):
        raise ValueError("ungültiges Repo")
    tree = _hf_get(f"/models/{repo}/tree/main", {"recursive": "1"}, base)
    files = [{"path": f.get("path", ""), "size": int((f.get("lfs") or {}).get("size") or f.get("size") or 0)}
             for f in tree if isinstance(tree, list) and f.get("type") == "file"
             and f.get("path", "").lower().endswith(".gguf")] if isinstance(tree, list) else []
    mmproj = _pick_mmproj(files)
    extra = (mmproj["size"] if mmproj else 0) / 1e9
    groups: dict[str, dict] = {}
    for f in files:
        name = f["path"].rsplit("/", 1)[-1]
        if is_sidecar(name):
            continue
        q = QUANT_RE.search(SPLIT_RE.sub(".gguf", name))
        if not q:
            continue
        quant = q.group(1).upper()
        key = SPLIT_RE.sub(".gguf", f["path"])  # alle Teile einer Datei
        g = groups.setdefault(quant, {"key": key, "files": []})
        if g["key"] == key:  # gleiche Quantisierung eines anderen Modells im Repo: nur die erste
            g["files"].append(f)
    items = []
    target = (models_dir / repo.replace("/", "_")) if models_dir else None
    for quant, g in groups.items():
        parts = sorted(g["files"], key=lambda f: f["path"])
        size = sum(f["size"] for f in parts) / 1e9
        tag = f"hf.co/{repo}:{quant}"
        p = Preset(tag, label_of(tag), round(size, 1), need_gb(size + extra), "", "")
        items.append({"tag": tag, "quant": quant, "file": parts[0]["path"], "files": parts, "parts": len(parts),
                      "mmproj": mmproj, "mtp": _pick_mtp(files, quant), "download_gb": round(size + extra, 1), "vram_gb": p.vram_gb,
                      "fit": fit(p, vram_gb), "note": quant_note(quant),
                      "installed": bool(target and all((target / f["path"].rsplit("/", 1)[-1]).exists()
                                                       for f in parts))})
    items.sort(key=lambda x: x["download_gb"])
    ok = [x for x in items if x["fit"] == "ok"]
    if ok:  # größte, die noch komplett passt = beste Qualität ohne RAM-Auslagerung
        max(ok, key=lambda x: x["download_gb"])["recommended"] = True
    return items


# ---------------------------------------------------------------- Zusätzliche Modelle merken (state.json)

def added_profiles(state_path: Path) -> dict[str, dict]:
    """Vollständige Profile (z. B. llama.cpp-Modelle mit eigenem Server), gespeichert von `orbwise model add`."""
    data = state.load(state_path).get("added_profiles", {})
    return data if isinstance(data, dict) else {}


def register_profile(state_path: Path, name: str, profile: dict, activate: bool = False) -> str:
    def change(data: dict) -> None:
        data.setdefault("added_profiles", {})[name] = profile
        if activate:
            data["active_profile"] = name
    state.update(state_path, change)
    return name


def set_active(state_path: Path, name: str) -> None:
    """Profil für den nächsten Start merken (wenn Orbwise gerade nicht läuft)."""
    state.update(state_path, lambda data: data.update(active_profile=name))


def added_models(state_path: Path) -> list[str]:
    return [t for t in state.load(state_path).get("added_models", []) if isinstance(t, str) and normalize_tag(t) == t]


def register_model(state_path: Path, tag: str, activate: bool = False) -> str:
    name = slug(tag)

    def change(data: dict) -> None:
        models = data.setdefault("added_models", [])
        if tag not in models:
            models.append(tag)
        if activate:
            data["active_profile"] = name
    state.update(state_path, change)
    return name


def removable(state_path: Path, name: str) -> bool:
    """Per Oberfläche/`orbwise model add` hinzugefügt (state.json) – nicht aus der config.yaml."""
    return name in added_profiles(state_path) or any(slug(t) == name for t in added_models(state_path))


def unregister_model(state_path: Path, name_or_tag: str) -> str | None:
    def change(data: dict) -> str | None:
        profiles = data.get("added_profiles", {})
        if name_or_tag in profiles:
            profiles.pop(name_or_tag)
            if data.get("active_profile") == name_or_tag:
                data.pop("active_profile")
            return name_or_tag
        models = data.get("added_models", [])
        for tag in models:
            if tag == name_or_tag or slug(tag) == name_or_tag:
                models.remove(tag)
                if data.get("active_profile") == slug(tag):
                    data.pop("active_profile")
                return tag
        return None
    return state.update(state_path, change)


# ---------------------------------------------------------------- Interaktive Auswahl (Terminal)

FIT_MARK = {"ok": "✔", "tight": "~", "big": "✘"}


def choose_interactive(vram_gb: float, installed: set[str] | None = None, ask=input, out=print) -> str | None:
    """Nummerierte Liste; Enter = Empfehlung, Zahl = Preset, eigener Name = beliebiges Ollama-Modell."""
    installed = installed or set()
    rec = recommend(vram_gb)
    out(T(f"\nModell auswählen (Grafikspeicher: {vram_gb:g} GB)", f"\nChoose a model (video memory: {vram_gb:g} GB)"))
    out(T("  ✔ passt komplett  ~ teilweise im RAM (langsamer)  ✘ zu groß",
          "  ✔ fits completely  ~ partly in RAM (slower)  ✘ too big"))
    for i, p in enumerate(PRESETS, 1):
        tags = []
        if p.tag == rec:
            tags.append(T("EMPFOHLEN", "RECOMMENDED"))
        if is_installed(p.tag, installed):
            tags.append(T("installiert", "installed"))
        extra = f"  [{', '.join(tags)}]" if tags else ""
        out(f" {i:>2}) {FIT_MARK[fit(p, vram_gb)]} {p.label:<24} ~{p.download_gb:g} GB  {p.note()}{extra}")
    out(T("  Oder einen eigenen Ollama-Namen oder Hugging-Face-Link eingeben (z. B. qwen3:1.7b oder\n"
          "  https://huggingface.co/unsloth/Qwen3-8B-GGUF – Suche: orbwise model search <begriff>).",
          "  Or type any Ollama model name or Hugging Face link (e.g. qwen3:1.7b or\n"
          "  https://huggingface.co/unsloth/Qwen3-8B-GGUF – search: orbwise model search <term>)."))
    while True:
        answer = ask(T(f"Auswahl [Enter = {rec}]: ", f"Choice [Enter = {rec}]: ")).strip()
        if not answer:
            return rec
        if answer.isdigit() and 1 <= int(answer) <= len(PRESETS):
            return PRESETS[int(answer) - 1].tag
        if normalize_tag(answer):
            return normalize_tag(answer)
        out(T("Bitte eine Nummer oder einen Modellnamen eingeben.", "Please enter a number or a model name."))
