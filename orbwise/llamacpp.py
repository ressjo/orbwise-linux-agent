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
import time
import zipfile
from collections.abc import Awaitable, Callable
from pathlib import Path
from urllib.parse import urlparse

import httpx

from . import state
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
def flavors_for(gpu: dict, preferred: str | None = None) -> list[str]:
    """Varianten in der Reihenfolge, in der sie probiert werden; `preferred` (per Messung gewählt) zuerst."""
    vendor = gpu.get("vendor")
    if vendor == "amd":
        order = ["rocm", "vulkan", "cpu"]
    elif vendor == "nvidia":
        order = ["cuda", "vulkan", "cpu"]
    else:
        return ["cpu"]
    if preferred in order:
        order.remove(preferred)
        order.insert(0, preferred)
    return order


def other_flavor(gpu: dict, current: str | None) -> str | None:
    """Die andere GPU-Variante zum Vergleich (ROCm ↔ Vulkan, CUDA ↔ Vulkan) – None, wenn es keine gibt."""
    return next((f for f in flavors_for(gpu) if f not in (current, "cpu")), None)


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


def pick_assets(assets: list[dict], gpu: dict, preferred: str | None = None,
                rocm: str | None = None) -> list[tuple[str, dict]]:
    """(Variante, Asset) in der Reihenfolge, in der sie probiert werden. Gibt es mehrere ROCm-Builds (je ROCm-Version
    einer), zuerst der zur installierten ROCm-Version (`rocm`, z. B. "7.2.4") passende."""
    out = []
    for flavor in flavors_for(gpu, preferred):
        found = [a for a in assets if FLAVORS[flavor].search(a.get("name", ""))]
        if flavor == "rocm" and len(found) > 1:
            found.sort(key=lambda a: _rocm_match(asset_rocm(a.get("name", "")), rocm), reverse=True)
        if found:
            out.append((flavor, found[0]))
    return out


def asset_rocm(name: str) -> str | None:
    """ROCm-Version, mit der ein Build gebaut wurde (…-rocm-7.2-x64… → "7.2"), sonst None."""
    m = re.search(r"(?:rocm|hip)[-_]?(\d+(?:\.\d+)*)", name, re.I)
    return m.group(1) if m else None


def _version(text: str | None) -> tuple[int, ...]:
    return tuple(int(x) for x in re.findall(r"\d+", text or "")[:3])


def _rocm_match(build: str | None, system: str | None) -> tuple:
    """Rangfolge eines ROCm-Builds zur System-Version: gleiche Haupt- und Nebenversion > gleiche Hauptversion (die
    nicht neuer ist als das System) > Rest; innerhalb davon der neueste."""
    b, sv = _version(build), _version(system)
    if not b or not sv:
        return (0, b)
    same_major = b[0] == sv[0]
    return (2 if b[:2] == sv[:2] else 1 if same_major and b <= sv else 0, b)


def rocm_version(base: str | None = None) -> str | None:
    """Installierte ROCm-Version (aus <ROCm>/.info/version, z. B. "7.2.4") – None ohne ROCm."""
    for root_dir in filter(None, (base, os.environ.get("ROCM_PATH"), "/opt/rocm")):
        try:
            raw = (Path(root_dir) / ".info" / "version").read_text(encoding="utf-8").strip()
        except OSError:
            continue
        m = re.match(r"\d+(?:\.\d+)*", raw)
        if m:
            return m.group(0)
    return None


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


# ---------------------------------------------------------------- llama-server: Hilfetext, Prompt-Cache-Optionen
CACHE_CHECKPOINTS = 32
_HELP: dict[str, str] = {}


def server_help(binary: Path, env: dict | None = None, run: Runner = subprocess.run) -> str:
    """--help eines llama-server (einmal je Programm gemerkt) – leer, wenn er nicht startet."""
    key = str(binary)
    if key not in _HELP:
        try:
            result = run([str(binary), "--help"], capture_output=True, text=True, timeout=30,
                         env={**os.environ, **(env or {})})
            _HELP[key] = (result.stdout or "") + (result.stderr or "")
        except (OSError, subprocess.SubprocessError):
            _HELP[key] = ""
    return _HELP[key]


def _mem_mib() -> int:
    try:
        with open("/proc/meminfo", encoding="utf-8") as f:
            for line in f:
                if line.startswith("MemTotal:"):
                    return int(line.split()[1]) // 1024
    except (OSError, ValueError, IndexError):
        pass
    return 0


def _arg(command: str, *flags: str) -> str:
    """Wert der letzten Angabe einer Option im Startbefehl (auch „--opt=wert“ und in Anführungszeichen)."""
    names = "|".join(re.escape(f) for f in flags)
    found = re.findall(rf"(?:^|\s)(?:{names})(?:\s+|=)(\"[^\"]+\"|'[^']+'|\S+)", command)
    return found[-1].strip("\"'") if found else ""


def cache_ram_mib(command: str, env: dict | None = None, ram: int = 0) -> int:
    """Größe des Prompt-Caches im RAM: Platz für etwa zwei volle Chats (KV je Token aus der GGUF-Datei × Kontext,
    mit Zuschlag für rekurrenten Zustand und Checkpoints). llama-server legt einen Zustand, der größer als die Grenze
    ist, gar nicht erst ab – ein langer Chat fiele sonst nach jeder fremden Anfrage (Zusammenfassung, Telegram,
    anderer Chat) ganz heraus. Höchstens 25 % des Arbeitsspeichers, nie weniger als früher (4 GB bzw. 15 %); ohne
    lesbares Modell der llama.cpp-Standard (8 GB)."""
    from .llm_memory import cache_bytes, gguf_metadata, kv_per_token_from_info
    floor = min(4096, int(ram * 0.15)) if ram else 4096
    cap = int(ram * 0.25) if ram else 8192
    want = 8192
    model = os.path.expanduser(_arg(command, "-m", "--model"))
    try:
        meta = gguf_metadata(model) if model else {}
    except (OSError, ValueError, Exception):  # noqa: BLE001 – ungewöhnliche Datei: Standardgröße
        meta = {}
    per_token = kv_per_token_from_info(meta, cache_bytes(command, env or {})) if meta else None
    ctx = _arg(command, "-c", "--ctx-size")
    n_ctx = int(ctx) if ctx.isdigit() else 0
    n_ctx = n_ctx or next((v for k, v in meta.items() if k.endswith(".context_length")), 0)
    if per_token and n_ctx:
        want = int(2 * per_token * n_ctx * 1.2 / 2**20)
    return max(floor, min(want, cap))


def cache_flags(command: str, env: dict | None = None, run: Runner = subprocess.run, mem_mib: int | None = None) -> str:
    """Zusätzliche Argumente für einen direkt gestarteten llama-server: mehr Checkpoints (Hybrid-Modelle können einen
    früheren Prompt nur darüber weiterverwenden), ein Prompt-Cache im RAM (bemessen nach Modell und Kontext, siehe
    cache_ram_mib) und das Sichern ruhender Slots. Nur Optionen, die der vorhandene llama-server kennt, und nur,
    wenn der Befehl sie nicht schon selbst setzt – sonst leer."""
    direct = re.match(r"\s*(?:\S+=\S*\s+)*(\S*llama-server)(\s|$)", command)
    if not direct:
        return ""
    binary = Path(os.path.expanduser(direct.group(1)))
    if not binary.is_absolute():
        binary = Path(shutil.which(str(binary)) or binary)
    help_text = server_help(binary, env, run)
    if not help_text:
        return ""
    ram = mem_mib if mem_mib is not None else _mem_mib()
    wanted = [("--ctx-checkpoints", str(CACHE_CHECKPOINTS)),
              ("--cache-ram", str(cache_ram_mib(command, env, ram))),
              ("--cache-idle-slots", "")]
    parts = []
    for flag, value in wanted:
        if re.search(rf"(^|\s){re.escape(flag)}(\s|=|$)", command) or flag not in help_text:
            continue
        parts.append(f"{flag} {value}".strip())
    return " ".join(parts)


def missing_libs(binary: Path, lib_path: str = "", run: Runner = subprocess.run) -> list[str]:
    """Bibliotheken, die der Loader für `binary` nicht findet (leer = alles da oder ldd fehlt)."""
    env = dict(os.environ)
    if lib_path:
        env["LD_LIBRARY_PATH"] = lib_path + (":" + env["LD_LIBRARY_PATH"] if env.get("LD_LIBRARY_PATH") else "")
    try:
        result = run(["ldd", str(binary)], capture_output=True, text=True, env=env)
    except OSError:
        return []
    return sorted(set(re.findall(r"^\s*(\S+)\s+=>\s+not found", result.stdout or "", re.M)))


def _client() -> httpx.Client:
    return httpx.Client(follow_redirects=True, timeout=httpx.Timeout(60, read=300), headers={"User-Agent": "orbwise"})


def install(cfg, gpu: dict, out: Out = print, update: bool = False, client: httpx.Client | None = None,
            run: Runner = subprocess.run, release_api: str | None = None) -> dict | None:
    """Neuesten fertigen Build laden (oder nichts tun, wenn schon einer da ist und update=False). Startet die
    GPU-Variante nicht (fehlende ROCm-/CUDA-Bibliotheken), wird die nächste probiert. → Manifest oder None."""
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
        rocm = rocm_version()
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
        preferred = old.get("preferred_flavor")
        choices = pick_assets(assets, gpu, preferred, rocm)
        for flavor, asset in choices:
            target = base / f"{tag}-{flavor}"
            why = _fetch_build(base, assets, flavor, asset, tag, client, out, run)
            if why:
                out(T(f"  {flavor}-Build läuft hier nicht ({why}) – versuche den nächsten …",
                      f"  {flavor} build does not run here ({why}) – trying the next one …"))
                continue
            _link_current(base, target)
            data = {"tag": tag, "flavor": flavor, "asset": asset["name"], "dir": target.name,
                    "previous": old.get("dir") if old.get("dir") != target.name else old.get("previous")}
            if preferred:
                data["preferred_flavor"] = preferred
            if flavor == "rocm" and asset_rocm(asset["name"]):
                data["rocm_build"] = asset_rocm(asset["name"])
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


ROCM_RELEASE_API = "https://api.github.com/repos/ROCm/ROCm/releases/latest"
ROCM_GUIDE = "https://rocm.docs.amd.com/projects/install-on-linux/en/latest/"
_CHECKS: dict[str, tuple[float, str | None]] = {}
CHECK_TTL = 3 * 3600  # GitHub ohne Anmeldung erlaubt 60 Abfragen pro Stunde – höchstens alle 3 h nachsehen


def _cached(key: str, fetch: Callable[[], str | None], force: bool = False) -> str | None:
    hit = _CHECKS.get(key)
    if hit and not force and time.monotonic() - hit[0] < CHECK_TTL:
        return hit[1]
    try:
        value = fetch()
    except (httpx.HTTPError, ValueError, KeyError, TypeError):
        return hit[1] if hit else None  # offline: der letzte bekannte Stand
    _CHECKS[key] = (time.monotonic(), value)
    return value


def _get_json(url: str, client: httpx.Client | None):
    c = client or httpx.Client(follow_redirects=True, timeout=15, headers={"User-Agent": "orbwise"})
    try:
        r = c.get(url)
        r.raise_for_status()
        return r.json()
    finally:
        if client is None:
            c.close()


def latest_tag(gpu: dict, client: httpx.Client | None = None, force: bool = False) -> str | None:
    """Neueste llama.cpp-Version mit einem Build für diese GPU (gemerkt) – None, wenn nicht erreichbar."""
    def fetch() -> str | None:
        rel, _seen = pick_release(_get_json(RELEASE_API, client), gpu)
        return str(rel.get("tag_name")) if rel else None
    return _cached(f"llamacpp:{gpu.get('vendor')}", fetch, force)


def latest_rocm(client: httpx.Client | None = None, force: bool = False) -> str | None:
    """Neueste ROCm-Version laut AMDs Releases auf GitHub (rocm-7.2.4 → "7.2.4") – nur zur Anzeige."""
    def fetch() -> str | None:
        m = re.search(r"\d+(?:\.\d+)*", str(_get_json(ROCM_RELEASE_API, client).get("tag_name") or ""))
        return m.group(0) if m else None
    return _cached("rocm", fetch, force)


def rocm_status(cfg, latest: str | None = None) -> dict:
    """Installiertes ROCm, womit der llama.cpp-Build gebaut ist und ob die Hauptversionen zusammenpassen."""
    system, m = rocm_version(), manifest(cfg)
    build = m.get("rocm_build") if m.get("flavor") == "rocm" else None
    out = {"installed": system, "build": build, "latest": latest, "guide": ROCM_GUIDE,
           "newer": bool(system and latest and _version(latest) > _version(system))}
    out["mismatch"] = bool(system and build and _version(system)[:1] != _version(build)[:1])
    return out


def _fetch_build(base: Path, assets: list[dict], flavor: str, asset: dict, tag: str, client: httpx.Client, out: Out,
                 run: Runner) -> str:
    """Build laden und nach base/<tag>-<flavor> entpacken → '' wenn er hier läuft, sonst der Grund (Ordner weg)."""
    from .imagegen_setup import download
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
        shutil.rmtree(target, ignore_errors=True)
    return why


def _link_current(base: Path, target: Path) -> None:
    link = base / "current.tmp"
    if link.is_symlink() or link.exists():
        link.unlink()
    os.symlink(target.name, link)
    os.replace(link, base / "current")  # atomar: ein laufender Server nutzt weiter den alten Build


def flavor_dir(cfg, flavor: str) -> Path:
    """Ordner des Builds dieser Variante zur installierten Version (für den Vergleich beim Messen)."""
    return root(cfg) / f"{manifest(cfg).get('tag')}-{flavor}"


def fetch_flavor(cfg, gpu: dict, flavor: str, out: Out = print, client: httpx.Client | None = None,
                 run: Runner = subprocess.run, release_api: str | None = None) -> Path:
    """Dieselbe llama.cpp-Version in einer anderen Variante daneben legen (z. B. Vulkan neben ROCm) – zum Messen;
    `current` bleibt, bis use_flavor umstellt. → Ordner des Builds; ValueError, wenn es ihn nicht gibt/nicht läuft."""
    m = manifest(cfg)
    tag = m.get("tag")
    if not tag:
        raise ValueError(T("llama.cpp ist noch nicht eingerichtet.", "llama.cpp is not set up yet."))
    target = flavor_dir(cfg, flavor)
    if flavor == m.get("flavor") or ((target / "llama-server").exists() and not check_build(target, flavor, run)):
        return target
    own = client is None
    client = client or _client()
    try:
        r = client.get(release_api or RELEASE_API)
        r.raise_for_status()
        releases = r.json()
        rel = next((x for x in (releases if isinstance(releases, list) else [releases])
                    if str(x.get("tag_name")) == tag), None)
        assets = (rel or {}).get("assets") or []
        asset = next((a for a in assets if FLAVORS[flavor].search(a.get("name", ""))), None)
        if asset is None:
            raise ValueError(T(f"Für llama.cpp {tag} gibt es keinen {flavor}-Build.",
                               f"There is no {flavor} build for llama.cpp {tag}."))
        why = _fetch_build(root(cfg), assets, flavor, asset, tag, client, out, run)
        if why:
            raise ValueError(T(f"Der {flavor}-Build läuft hier nicht: {why}", f"The {flavor} build does not run here: {why}"))
        (target / "asset.txt").write_text(asset["name"], encoding="utf-8")
        return target
    finally:
        if own:
            client.close()


def use_flavor(cfg, flavor: str) -> dict:
    """Auf die (mit fetch_flavor geholte) Variante umstellen – gilt für alle llama.cpp-Modelle ab dem nächsten Start
    und bleibt bei Updates die erste Wahl. Der bisherige Build bleibt als Rückfall liegen."""
    base = root(cfg)
    old = manifest(cfg)
    target = flavor_dir(cfg, flavor)
    if not (target / "llama-server").exists():
        raise ValueError(T(f"Der {flavor}-Build ist nicht geladen – erst messen.",
                           f"The {flavor} build is not downloaded – measure first."))
    _link_current(base, target)
    asset = (target / "asset.txt").read_text(encoding="utf-8").strip() if (target / "asset.txt").exists() else ""
    data = {**old, "flavor": flavor, "asset": asset or old.get("asset", ""), "dir": target.name,
            "previous": old.get("dir") if old.get("dir") != target.name else old.get("previous"),
            "preferred_flavor": flavor}
    (base / "manifest.json").write_text(json.dumps(data, indent=1), encoding="utf-8")
    return data


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
    if item.get("mtp"):  # MTP-Kopf für spekulatives Decoding (klein; einschalten auf der Modellseite)
        parts.append((item["mtp"]["path"], int(item["mtp"].get("size") or 0)))
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
    from .models import is_sidecar, register_profile
    gguf = gguf.expanduser().resolve()
    if not gguf.is_file() or not gguf.name.lower().endswith(".gguf") or is_sidecar(gguf.name):
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

    def remember(data: dict) -> None:
        data.setdefault("local_models", {})[name] = {"files": files, "downloaded": downloaded, "repo": repo}
    state.update(state_path, remember)
    return name


def local_record(state_path: Path, name: str) -> dict | None:
    rec = (state.load(state_path).get("local_models") or {}).get(name)
    return rec if isinstance(rec, dict) else None


def forget(cfg, state_path: Path, name: str, delete_files: bool = True) -> int:
    """Eintrag entfernen; selbst geladene Dateien löschen (nur innerhalb von models_dir) → freigegebene Bytes.
    Dateien, die der Nutzer selbst nach ~/models gelegt hat, bleiben."""
    def drop(data: dict) -> tuple[dict | None, dict]:
        return (data.get("local_models") or {}).pop(name, None), dict(data.get("local_models") or {})
    rec, remaining = state.update(state_path, drop)
    if not rec or not rec.get("downloaded") or not delete_files:
        return 0
    base = models_dir(cfg).resolve()
    still_used = {f for r in remaining.values() for f in r.get("files", [])}
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
    from .models import Preset, fit, is_sidecar, need_gb
    base = models_dir(cfg)
    if not base.is_dir():
        return []
    used = {path: name for name, p in profiles.items() for path in profile_paths(p)}
    out = []
    for path in sorted(base.rglob("*.gguf")):
        rel = path.relative_to(base)
        if len(rel.parts) > depth or is_sidecar(path.name):
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

