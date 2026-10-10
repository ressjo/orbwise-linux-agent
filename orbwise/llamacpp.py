"""Eigene Modelle über llama.cpp – ohne config.yaml: Orbwise lädt eine GGUF-Datei von Hugging Face (oder nimmt eine aus
~/models), besorgt den offiziellen llama-server und legt ein Profil an, das ihn mit passendem Port, Kontext und
Bildmodul startet. Danach steht das Modell im Modell-Menü wie jedes andere.

Ablage:
  <daten>/llama.cpp/<tag>-<variante>/   fertige Builds von github.com/ggml-org/llama.cpp (ROCm/CUDA, sonst Vulkan)
  <daten>/llama.cpp/current             Verweis auf den aktiven Build – Profile starten current/llama-server, ein Update
                                        setzt nur den Verweis um (ein laufender Server läuft weiter)
  ~/models/<nutzer>_<repo>/             heruntergeladene Modelle (llm.models_dir)
  state.json → local_models             welche Dateien zu welchem Profil gehören und ob Orbwise sie geladen hat
"""

from __future__ import annotations

import json
import os
import re
import secrets
import shlex
import shutil
import socket
import stat
import subprocess
import tarfile
import zipfile
from collections.abc import Awaitable, Callable
from pathlib import Path
from urllib.parse import urlparse

import httpx

from .lang import T

REPO = "ggml-org/llama.cpp"
# Die fertigen Builds stehen in den täglichen Releases b1234 (als Vorabversion markiert) – „latest“ zeigt bei llama.cpp
# auf ein Release ohne Programme, deshalb die Liste der neuesten Releases
RELEASE_API = f"https://api.github.com/repos/{REPO}/releases?per_page=20"
FIRST_PORT = 8090
CONTEXT_STEPS = (65536, 49152, 32768, 24576, 16384, 12288, 8192)
SPLIT_RE = re.compile(r"-(\d{5})-of-(\d{5})\.gguf$", re.I)
# Varianten der fertigen Linux-Builds (Reihenfolge = Vorliebe je GPU; die CPU-Variante geht immer)
FLAVORS = {
    "rocm": re.compile(r"bin-ubuntu-.*(rocm|hip).*x64\.(zip|tar\.gz)$", re.I),
    "cuda": re.compile(r"^llama-.*bin-ubuntu-.*cuda.*x64\.(zip|tar\.gz)$", re.I),  # nicht cudart-… (nur Laufzeit)
    "vulkan": re.compile(r"bin-ubuntu-vulkan-x64\.(zip|tar\.gz)$", re.I),
    "cpu": re.compile(r"bin-ubuntu-x64\.(zip|tar\.gz)$", re.I),
}

Out = Callable[[str], None]
Progress = Callable[[int, int], Awaitable[None]]
Runner = Callable[..., subprocess.CompletedProcess]


# ---------------------------------------------------------------- Orte
def root(cfg) -> Path:
    """Neben state.json (<daten>/llama.cpp)."""
    return Path(cfg.memory.dir).expanduser().parent / "llama.cpp"


def models_dir(cfg) -> Path:
    return Path(os.path.expanduser(cfg.llm.models_dir or "~/models"))


def binary(cfg) -> Path:
    return root(cfg) / "current" / "llama-server"


def manifest(cfg) -> dict:
    try:
        return json.loads((root(cfg) / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def installed(cfg) -> bool:
    return binary(cfg).exists()


def _short(path: Path | str) -> str:
    """~/… statt /home/<name>/… (lesbarer, übersteht einen umbenannten Nutzer nicht – muss es auch nicht)."""
    path, home = str(path), str(Path.home())
    return "~/" + path[len(home) + 1:] if path.startswith(home + "/") else path


def _tilde(path: Path | str) -> str:
    """Pfad für den Startbefehl (bash): ~/… und sicher gequotet (Leerzeichen)."""
    short = _short(path)
    return "~/" + shlex.quote(short[2:]) if short.startswith("~/") else shlex.quote(short)


# ---------------------------------------------------------------- Build holen
def flavors_for(gpu: dict) -> list[str]:
    vendor = gpu.get("vendor")
    if vendor == "amd":
        return ["rocm", "vulkan", "cpu"]
    if vendor == "nvidia":
        return ["cuda", "vulkan", "cpu"]
    return ["cpu"]


def cudart_for(assets: list[dict], asset: dict) -> dict | None:
    """Die CUDA-Laufzeit zum CUDA-Build (cudart-llama-…-cuda-12.8-x64) – ohne sie fehlen libcudart/libcublas."""
    m = re.search(r"cuda-([\d.]+)-x64", asset.get("name", ""))
    if not m:
        return None
    return next((a for a in assets if a.get("name", "").startswith("cudart-")
                 and f"ubuntu-cuda-{m.group(1)}-x64" in a.get("name", "")), None)


def pick_release(releases, gpu: dict) -> tuple[dict | None, list[str]]:
    """Neuestes Release mit einem passenden Build → (Release, geprüfte Tags)."""
    if isinstance(releases, dict):
        releases = [releases]
    seen = []
    for rel in releases if isinstance(releases, list) else []:
        seen.append(str(rel.get("tag_name") or "?"))
        if pick_assets(rel.get("assets") or [], gpu):
            return rel, seen
    return None, seen


def pick_assets(assets: list[dict], gpu: dict) -> list[tuple[str, dict]]:
    """(Variante, Asset) in der Reihenfolge, in der sie probiert werden."""
    out = []
    for flavor in flavors_for(gpu):
        asset = next((a for a in assets if FLAVORS[flavor].search(a.get("name", ""))), None)
        if asset:
            out.append((flavor, asset))
    return out


def _extract(archive: Path, target: Path) -> None:
    """Alle Dateien flach nach target (Programme und Bibliotheken liegen im Archiv unter build/bin o. Ä.) – nur
    Dateinamen, keine Pfade aus dem Archiv (kein Schreiben außerhalb)."""
    target.mkdir(parents=True, exist_ok=True)
    if archive.name.endswith(".zip"):
        with zipfile.ZipFile(archive) as z:
            for member in z.infolist():
                name = Path(member.filename).name
                if not name or member.is_dir():
                    continue
                with z.open(member) as src, (target / name).open("wb") as dst:
                    shutil.copyfileobj(src, dst)
    else:
        with tarfile.open(archive) as t:
            for member in t.getmembers():
                name = Path(member.name).name
                if not name:
                    continue
                dest = target / name
                if member.issym():
                    link = Path(member.linkname).name
                    if dest.exists() or dest.is_symlink():
                        dest.unlink()
                    os.symlink(link, dest)
                elif member.islnk():  # Hardlink im Archiv: Ziel liegt schon (flach) im Ordner
                    origin = target / Path(member.linkname).name
                    if origin.is_file():
                        shutil.copyfile(origin, dest)
                elif member.isfile():
                    src = t.extractfile(member)
                    if src is None:
                        continue
                    with src, dest.open("wb") as dst:
                        shutil.copyfileobj(src, dst)
    for f in target.iterdir():
        if f.is_file() and not f.is_symlink():
            f.chmod(f.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP)


def _env(directory: Path) -> dict:
    lib = os.environ.get("LD_LIBRARY_PATH", "")
    return {**os.environ, "LD_LIBRARY_PATH": f"{directory}:{lib}" if lib else str(directory)}


def check_build(directory: Path, flavor: str, run: Runner = subprocess.run) -> str:
    """'' wenn der Build läuft (und bei GPU-Varianten eine GPU sieht), sonst der Grund."""
    exe = directory / "llama-server"
    if not exe.exists():
        return T("llama-server fehlt im Build", "llama-server is missing from the build")
    try:
        r = run([str(exe), "--version"], capture_output=True, text=True, timeout=60, env=_env(directory))
    except (OSError, subprocess.SubprocessError) as e:
        return str(e)
    text = (r.stdout or "") + (r.stderr or "")
    if r.returncode != 0:
        lib = re.search(r"error while loading shared libraries: ([^:\s]+)", text)
        return T(f"es fehlt {lib.group(1)}", f"{lib.group(1)} is missing") if lib else text.strip()[-200:]
    if flavor in ("rocm", "cuda", "vulkan"):
        try:
            d = run([str(exe), "--list-devices"], capture_output=True, text=True, timeout=60, env=_env(directory))
        except (OSError, subprocess.SubprocessError):
            return ""
        listed = (d.stdout or "") + (d.stderr or "")
        if d.returncode == 0 and "Available devices" in listed and not re.search(r"(ROCm|CUDA|Vulkan)\d+", listed):
            return T("findet keine Grafikkarte", "finds no GPU")
    return ""


def _client() -> httpx.Client:
    return httpx.Client(follow_redirects=True, timeout=httpx.Timeout(60, read=300), headers={"User-Agent": "orbwise"})


def install(cfg, gpu: dict, out: Out = print, update: bool = False, client: httpx.Client | None = None,
            run: Runner = subprocess.run, release_api: str | None = None) -> dict | None:
    """Neuesten fertigen Build laden (oder nichts tun, wenn schon einer da ist und update=False). Startet die
    GPU-Variante nicht (fehlende ROCm-/CUDA-Bibliotheken), wird die nächste probiert. → Manifest oder None."""
    from .imagegen_setup import download
    if installed(cfg) and not update:
        return manifest(cfg)
    base = root(cfg)
    base.mkdir(parents=True, exist_ok=True)
    own = client is None
    client = client or _client()
    try:
        r = client.get(release_api or RELEASE_API)
        r.raise_for_status()
        rel, seen = pick_release(r.json(), gpu)
        if rel is None:
            out(T(f"✘ Kein passender Linux-Build von llama.cpp gefunden (geprüft: {', '.join(seen[:5]) or 'nichts'}) – "
                  "später erneut versuchen: orbwise model llamacpp --update",
                  f"✘ No matching Linux build of llama.cpp found (checked: {', '.join(seen[:5]) or 'nothing'}) – "
                  "try again later: orbwise model llamacpp --update"))
            return None
        tag = str(rel.get("tag_name") or "unbekannt")
        old = manifest(cfg)
        if update and installed(cfg) and old.get("tag") == tag:
            out(T(f"llama.cpp {tag} ist aktuell.", f"llama.cpp {tag} is up to date."))
            return old
        assets = rel.get("assets") or []
        choices = pick_assets(assets, gpu)
        for flavor, asset in choices:
            target = base / f"{tag}-{flavor}"
            archive = base / asset["name"]
            out(T(f"Lade llama.cpp {tag} ({flavor}) …", f"Downloading llama.cpp {tag} ({flavor}) …"))
            download(asset["browser_download_url"], archive, client, out)
            if target.exists():
                shutil.rmtree(target)
            _extract(archive, target)
            archive.unlink(missing_ok=True)
            runtime = cudart_for(assets, asset) if flavor == "cuda" else None
            if runtime:  # CUDA-Laufzeit in denselben Ordner
                extra = base / runtime["name"]
                download(runtime["browser_download_url"], extra, client, out)
                _extract(extra, target)
                extra.unlink(missing_ok=True)
            why = check_build(target, flavor, run)
            if why:
                out(T(f"  {flavor}-Build läuft hier nicht ({why}) – versuche den nächsten …",
                      f"  {flavor} build does not run here ({why}) – trying the next one …"))
                shutil.rmtree(target, ignore_errors=True)
                continue
            link = base / "current.tmp"
            if link.is_symlink() or link.exists():
                link.unlink()
            os.symlink(target.name, link)
            os.replace(link, base / "current")  # atomar: ein laufender Server nutzt weiter den alten Build
            data = {"tag": tag, "flavor": flavor, "asset": asset["name"], "dir": target.name,
                    "previous": old.get("dir") if old.get("dir") != target.name else old.get("previous")}
            (base / "manifest.json").write_text(json.dumps(data, indent=1), encoding="utf-8")
            keep = {data["dir"], data.get("previous")}
            for d in base.iterdir():  # alte Builds weg (der vorige bleibt als Rückfall)
                if d.is_dir() and not d.is_symlink() and d.name not in keep:
                    shutil.rmtree(d, ignore_errors=True)
            out(T(f"✔ llama.cpp {tag} ({flavor}) eingerichtet.", f"✔ llama.cpp {tag} ({flavor}) set up."))
            return data
        return None
    finally:
        if own:
            client.close()


# ---------------------------------------------------------------- Modelldateien laden
def _auth(url: str) -> dict:
    token = os.environ.get("HF_TOKEN") or os.environ.get("HUGGING_FACE_HUB_TOKEN")
    host = urlparse(url).hostname or ""
    return {"Authorization": f"Bearer {token}"} if token and host.endswith("huggingface.co") else {}


async def fetch(url: str, target: Path, progress: Callable[[int], Awaitable[None]] | None = None,
                client: httpx.AsyncClient | None = None) -> None:
    """Datei laden mit Fortsetzen (.part + Range). Abbrechen (Task.cancel) lässt die .part-Datei liegen – der nächste
    Versuch macht dort weiter. progress(bytes_bisher)."""
    if target.exists():
        if progress:
            await progress(target.stat().st_size)
        return
    part = target.with_name(target.name + ".part")
    target.parent.mkdir(parents=True, exist_ok=True)
    have = part.stat().st_size if part.exists() else 0
    own = client is None
    client = client or httpx.AsyncClient(follow_redirects=True, timeout=httpx.Timeout(60, read=300),
                                         headers={"User-Agent": "orbwise"})
    try:
        headers = {**_auth(url), **({"Range": f"bytes={have}-"} if have else {})}
        async with client.stream("GET", url, headers=headers) as r:
            if r.status_code == 416:  # schon vollständig
                part.rename(target)
                return
            if r.status_code in (401, 403):
                raise ValueError(T("Hugging Face verlangt eine Anmeldung für dieses Modell (zugangsbeschränkt) – "
                                   "Token als HF_TOKEN setzen oder ein anderes Repo wählen.",
                                   "Hugging Face requires a login for this model (gated) – set HF_TOKEN or pick "
                                   "another repo."))
            r.raise_for_status()
            if have and r.status_code != 206:
                have = 0  # Server kann nicht fortsetzen
            with part.open("ab" if have else "wb") as f:
                async for chunk in r.aiter_bytes(1 << 20):
                    f.write(chunk)
                    have += len(chunk)
                    if progress:
                        await progress(have)
        part.rename(target)
    finally:
        if own:
            await client.aclose()


def repo_dir(cfg, repo: str) -> Path:
    return models_dir(cfg) / repo.replace("/", "_")


async def download_quant(cfg, repo: str, item: dict, progress: Callable[[int, int], Awaitable[None]] | None = None,
                         hf_base: str = "https://huggingface.co") -> tuple[Path, Path | None, list[Path]]:
    """Alle Teile einer Quantisierung (und das Bildmodul) nach ~/models/<nutzer>_<repo>/ → (erste Datei, mmproj,
    alle Dateien). Prüft vorher den freien Speicherplatz."""
    parts = [(f["path"], int(f.get("size") or 0)) for f in item["files"]]
    if item.get("mmproj"):
        parts.append((item["mmproj"]["path"], int(item["mmproj"].get("size") or 0)))
    target = repo_dir(cfg, repo)
    target.mkdir(parents=True, exist_ok=True)
    total = sum(s for _, s in parts)
    missing = sum(s - (target / (Path(p).name + ".part")).stat().st_size
                  if (target / (Path(p).name + ".part")).exists() else s
                  for p, s in parts if not (target / Path(p).name).exists())
    free = shutil.disk_usage(target).free
    if missing > free - 512 * 1024 ** 2:
        raise ValueError(T(f"Zu wenig Speicherplatz in {target}: nötig ~{missing / 1e9:.1f} GB, frei "
                           f"{free / 1e9:.1f} GB.", f"Not enough disk space in {target}: needs ~{missing / 1e9:.1f} "
                           f"GB, {free / 1e9:.1f} GB free."))
    done_before = 0
    paths = []
    async with httpx.AsyncClient(follow_redirects=True, timeout=httpx.Timeout(60, read=300),
                                 headers={"User-Agent": "orbwise"}) as client:
        for path, size in parts:
            dest = target / Path(path).name

            async def step(n: int, _before=done_before) -> None:
                if progress:
                    await progress(_before + n, total)
            await fetch(f"{hf_base}/{repo}/resolve/main/{path}", dest, step, client)
            done_before += size or dest.stat().st_size
            paths.append(dest)
    first = paths[0]
    mmproj = paths[-1] if item.get("mmproj") else None
    return first, mmproj, paths


# ---------------------------------------------------------------- Profil
def shards(path: Path) -> list[Path]:
    """Alle Teile einer geteilten GGUF-Datei (sonst nur die Datei)."""
    m = SPLIT_RE.search(path.name)
    if not m:
        return [path]
    count = int(m.group(2))
    stem = path.name[:m.start()]
    return [path.with_name(f"{stem}-{i:05d}-of-{count:05d}.gguf") for i in range(1, count + 1)]


def label_for(path: Path) -> str:
    from .models import QUANT_RE
    name = SPLIT_RE.sub(".gguf", path.name)
    q = QUANT_RE.search(name)
    if q:
        base = name[:q.start(1)].rstrip("-_. ")
        return f"{base} {q.group(1)}".strip()
    return name[:-5] if name.lower().endswith(".gguf") else name


def pick_context(gguf: Path, extra_bytes: int, vram_gb: float) -> tuple[int, bool]:
    """(Kontextgröße, Embeddings lieber auf der CPU?) – die größte Stufe, bei der Modell + KV-Cache + Bildmodul +
    ~1 GB Puffer in den Grafikspeicher passen und die das Modell kennt."""
    from .llm_memory import estimate_from_gguf, gguf_metadata
    model = sum(p.stat().st_size for p in shards(gguf) if p.exists())
    if vram_gb <= 0:
        return 8192, False
    budget = vram_gb * 1024 ** 3 * 0.95 - 1024 ** 3 - extra_bytes
    try:
        train = next((int(v) for k, v in gguf_metadata(str(gguf)).items() if k.endswith(".context_length")), 0)
    except Exception:  # noqa: BLE001 – unbekannte Datei: ohne Obergrenze
        train = 0
    for ctx in CONTEXT_STEPS:
        if train and ctx > train:
            continue
        est = estimate_from_gguf(str(gguf), ctx)
        kv = est["kv_vram"] if est else ctx * 160 * 1024  # ohne Angaben: grob 160 KB je Token
        if model + kv <= budget:
            return ctx, budget - model - kv < 2 * 1024 ** 3
    return 8192, True


def profile_paths(profile) -> list[Path]:
    """Modelldateien, die ein Profil startet (-m …), aus dem Startbefehl gelesen."""
    server = getattr(profile, "server", None)
    if not server:
        return []
    try:
        words = shlex.split(server.command)
    except ValueError:
        return []
    out = []
    for i, w in enumerate(words[:-1]):
        if w in ("-m", "--model"):
            out.append(Path(os.path.expanduser(words[i + 1])).resolve())
    return out


def free_port(profiles: dict) -> int:
    used = set()
    for p in profiles.values():
        try:
            used.add(urlparse(p.base_url).port)
        except ValueError:
            pass
    for port in range(FIRST_PORT, FIRST_PORT + 200):
        if port in used:
            continue
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", port))
            except OSError:
                continue
        return port
    raise ValueError(T("Kein freier Port für den Modell-Server gefunden.", "No free port for the model server."))


def unique_name(base: str, taken) -> str:
    from .models import slug
    name = slug(base) or "modell"
    n, out = 2, name
    while out in taken:
        out, n = f"{name}-{n}", n + 1
    return out


def make_profile(cfg, name: str, gguf: Path, mmproj: Path | None, gpu: dict, port: int) -> dict:
    ctx, cpu_embed = pick_context(gguf, mmproj.stat().st_size if mmproj and mmproj.exists() else 0,
                                  float(gpu.get("vram_gb") or 0))
    key = secrets.token_hex(16)  # llama-server erlaubt Anfragen von jeder Webseite (CORS *) – nur mit Schlüssel
    command = f"{_tilde(binary(cfg))} -m {_tilde(gguf)}"
    if mmproj:
        command += f" --mmproj {_tilde(mmproj)}"
    command += (f" -ngl 99 -c {ctx} -np 1 --jinja --host 127.0.0.1 --port {port} --alias {name} --api-key {key}")
    return {
        "label": label_for(gguf),
        "backend": "openai",
        "base_url": f"http://127.0.0.1:{port}/v1",
        "model": name,
        "api_key": key,
        "embed_on_cpu": cpu_embed,
        "unload_ollama": True,
        "server": {"command": command, "env": {"LD_LIBRARY_PATH": _short(root(cfg) / "current")},
                   "startup_timeout": 300},
    }


def add_model(cfg, state_path: Path, gguf: Path, mmproj: Path | None, gpu: dict, profiles: dict,
              downloaded: bool, repo: str = "") -> str:
    """Profil anlegen und merken, welche Dateien dazugehören → Profilname."""
    from .models import register_profile
    gguf = gguf.expanduser().resolve()
    if not gguf.is_file() or not gguf.name.lower().endswith(".gguf") or "mmproj" in gguf.name.lower():
        raise ValueError(T(f"Keine GGUF-Datei: {gguf}", f"Not a GGUF file: {gguf}"))
    if mmproj is None:
        mmproj = find_mmproj(gguf.parent)
    for name, p in profiles.items():
        if gguf in profile_paths(p):
            raise ValueError(T(f"Diese Datei nutzt schon das Modell „{p.label or name}“.",
                               f"The model “{p.label or name}” already uses this file."))
    name = unique_name(label_for(gguf), set(profiles))
    profile = make_profile(cfg, name, gguf, mmproj, gpu, free_port(profiles))
    register_profile(state_path, name, profile)
    files = [str(p) for p in shards(gguf)] + ([str(mmproj)] if mmproj else [])
    data = _read(state_path)
    data.setdefault("local_models", {})[name] = {"files": files, "downloaded": downloaded, "repo": repo}
    _write(state_path, data)
    return name


def _read(state_path: Path) -> dict:
    try:
        return json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _write(state_path: Path, data: dict) -> None:
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(data, indent=1), encoding="utf-8")


def local_record(state_path: Path, name: str) -> dict | None:
    rec = (_read(state_path).get("local_models") or {}).get(name)
    return rec if isinstance(rec, dict) else None


def forget(cfg, state_path: Path, name: str, delete_files: bool = True) -> int:
    """Eintrag entfernen; selbst geladene Dateien löschen (nur innerhalb von models_dir) → freigegebene Bytes.
    Dateien, die der Nutzer selbst nach ~/models gelegt hat, bleiben."""
    data = _read(state_path)
    rec = (data.get("local_models") or {}).pop(name, None)
    _write(state_path, data)
    if not rec or not rec.get("downloaded") or not delete_files:
        return 0
    base = models_dir(cfg).resolve()
    still_used = {f for r in (data.get("local_models") or {}).values() for f in r.get("files", [])}
    freed = 0
    for f in rec.get("files", []):
        p = Path(f)
        try:
            inside = p.resolve().is_relative_to(base)
        except OSError:
            continue
        if inside and f not in still_used and p.is_file():
            freed += p.stat().st_size
            p.unlink()
            if p.parent != base and not any(p.parent.iterdir()):
                p.parent.rmdir()
    return freed


# ---------------------------------------------------------------- Vorhandene Dateien (~/models)
def find_mmproj(directory: Path) -> Path | None:
    found = sorted(p for p in directory.glob("*.gguf") if "mmproj" in p.name.lower())
    for pref in ("f16", "bf16", "f32"):
        hit = next((p for p in found if re.search(rf"[-_.]{pref}\.gguf$", p.name, re.I)), None)
        if hit:
            return hit
    return found[0] if found else None


def scan(cfg, profiles: dict, vram_gb: float = 0.0, depth: int = 3) -> list[dict]:
    """GGUF-Dateien in models_dir: je Modell eine Zeile (geteilte Dateien zusammengefasst, Bildmodul dazu), mit dem
    Profil, das sie schon nutzt."""
    from .models import Preset, fit, need_gb
    base = models_dir(cfg)
    if not base.is_dir():
        return []
    used = {path: name for name, p in profiles.items() for path in profile_paths(p)}
    out = []
    for path in sorted(base.rglob("*.gguf")):
        rel = path.relative_to(base)
        if len(rel.parts) > depth or "mmproj" in path.name.lower():
            continue
        m = SPLIT_RE.search(path.name)
        if m and int(m.group(1)) != 1:
            continue
        parts = shards(path)
        if not all(p.exists() for p in parts):
            continue  # geteilte Datei unvollständig
        size = sum(p.stat().st_size for p in parts) / 1e9
        mmproj = find_mmproj(path.parent)
        extra = mmproj.stat().st_size / 1e9 if mmproj else 0
        p = Preset(str(path), label_for(path), round(size, 1), need_gb(size + extra), "", "")
        out.append({"path": str(path), "rel": str(rel), "label": label_for(path), "size_gb": round(size, 1),
                    "vram_gb": p.vram_gb, "fit": fit(p, vram_gb), "mmproj": str(mmproj) if mmproj else None,
                    "parts": len(parts), "profile": used.get(path.resolve())})
    return out

