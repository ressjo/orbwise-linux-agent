"""Speicherplatz: was Orbwise auf der Platte belegt – Sprach- und Bildmodelle, Programme (llama.cpp, Stimmen, Browser),
Zwischenspeicher, Sicherungspunkte und die eigenen Daten. Die Einstellungen (Allgemein) zeigen das und löschen, was
gefahrlos weg kann: Zwischenspeicher, alte llama.cpp-Builds, den eigenen Browser, Sicherungspunkte. Modelle löscht
die Oberfläche einzeln über die vorhandenen Endpunkte (/api/models/…, /api/image/models/…), die dabei aufpassen,
dass kein laufendes Modell verschwindet."""

from __future__ import annotations

import json
import os
import shutil
import time
from pathlib import Path

from . import artifacts, llamacpp, snapshot, workspace
from .tools import browser

RECENT_SECONDS = 10 * 60  # gerade benutzte Dateien bleiben beim Leeren liegen (laufender Befehl, offene Ausgabe)
# bleibt beim Leeren des Zwischenspeichers: der eigene Messtext und der geladene (damit Messungen vergleichbar bleiben)
CACHE_KEEP = {("quality", "eigener-text.txt"), ("quality", "text.txt"), ("quality", "text.json")}


# ---------------------------------------------------------------- Orte
def data_dir(cfg) -> Path:
    return Path(cfg.memory.dir).expanduser().parent


def cache_dir() -> Path:
    return Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / "orbwise"


def size_of(path: Path, exclude: tuple[Path, ...] = ()) -> int:
    """Belegter Platz (Dateigröße, Links nicht verfolgt); Unterordner in exclude zählen nicht mit."""
    try:
        st = path.lstat()
    except OSError:
        return 0
    if not path.is_dir() or path.is_symlink():
        return st.st_size
    skip = {os.path.realpath(p) for p in exclude}
    total = 0
    for dirpath, dirnames, names in os.walk(path):
        dirnames[:] = [d for d in dirnames if os.path.realpath(os.path.join(dirpath, d)) not in skip]
        for name in names:
            try:
                total += os.lstat(os.path.join(dirpath, name)).st_size
            except OSError:
                continue
    return total


def _inside(path: Path, base: Path) -> bool:
    try:
        return Path(os.path.realpath(path)).is_relative_to(os.path.realpath(base))
    except (OSError, ValueError):
        return False


# ---------------------------------------------------------------- Programme
def llamacpp_parts(cfg) -> tuple[int, int]:
    """(aktueller Build, alles andere: vorige Version, andere Varianten, Reste) in Bytes."""
    base = llamacpp.root(cfg)
    current = llamacpp.manifest(cfg).get("dir")
    now = old = 0
    if base.is_dir():
        for p in base.iterdir():
            if p.is_symlink() or p.name == "manifest.json":
                continue
            if p.name == current:
                now += size_of(p)
            else:
                old += size_of(p)
    return now, old


def remove_old_llamacpp(cfg) -> int:
    """Alles außer dem laufenden Build löschen (vorige Version, andere Varianten, abgebrochene Downloads) → Bytes.
    Ein Update holt bei Bedarf neu; gerade erst geschriebene Ordner (ein Update läuft) bleiben."""
    base = llamacpp.root(cfg)
    data = llamacpp.manifest(cfg)
    current = data.get("dir")
    if not base.is_dir() or not current:
        return 0
    freed = 0
    now = time.time()
    for p in base.iterdir():
        if p.is_symlink() or p.name in ("manifest.json", current):
            continue
        try:
            if now - p.stat().st_mtime < RECENT_SECONDS:
                continue
        except OSError:
            continue
        size = size_of(p)
        if p.is_dir():
            shutil.rmtree(p, ignore_errors=True)
        else:
            p.unlink(missing_ok=True)
        freed += size - (size_of(p) if p.exists() else 0)
    if data.get("previous") and not (base / data["previous"]).exists():
        data["previous"] = None
        (base / "manifest.json").write_text(json.dumps(data, indent=1), encoding="utf-8")
    return freed


def remove_browser() -> int:
    path = browser.own_dir()
    freed = size_of(path)
    shutil.rmtree(path, ignore_errors=True)
    return freed - size_of(path)


# ---------------------------------------------------------------- Zwischenspeicher, Sicherungspunkte
def clear_cache(busy: set[str] = frozenset(), now: float | None = None) -> int:
    """~/.cache/orbwise leeren → freigegebene Bytes. Bleibt: was gerade benutzt wird (Protokolle laufender
    Hintergrund-Aufgaben, kürzlich geänderte Dateien), der Messtext für „Qualität messen“."""
    base = cache_dir()
    if not base.is_dir():
        return 0
    now = now or time.time()
    busy_real = {os.path.realpath(p) for p in busy}
    freed = 0
    for dirpath, dirnames, names in os.walk(base, topdown=False):
        rel = Path(dirpath).relative_to(base).parts
        for name in names:
            path = os.path.join(dirpath, name)
            if (*rel, name) in CACHE_KEEP or os.path.realpath(path) in busy_real:
                continue
            try:
                st = os.lstat(path)
                if now - st.st_mtime < RECENT_SECONDS:
                    continue
                os.unlink(path)
                freed += st.st_size
            except OSError:
                continue
        for name in dirnames:  # leere Unterordner weg (sie entstehen bei Bedarf neu)
            path = os.path.join(dirpath, name)
            if not os.path.islink(path):
                try:
                    os.rmdir(path)
                except OSError:
                    pass
    return freed


def clear_snapshots() -> int:
    path = snapshot.base_dir()
    freed = size_of(path)
    shutil.rmtree(path, ignore_errors=True)
    return freed - size_of(path)


# ---------------------------------------------------------------- Überblick
def _group(key: str, de: str, en: str, items: list[dict], *, path: Path | None = None, clear: str = "",
           note: tuple[str, str] = ("", "")) -> dict:
    return {"key": key, "label": de, "label_en": en, "path": str(path) if path else "", "clear": clear,
            "note": note[0], "note_en": note[1], "items": items, "bytes": sum(i["bytes"] for i in items)}


def _item(label: str, size: int, *, label_en: str = "", delete: str = "", open_: str = "", note: str = "",
          note_en: str = "", path: Path | None = None, active: bool = False) -> dict:
    return {"label": label, "label_en": label_en or label, "bytes": size, "delete": delete, "open": open_,
            "note": note, "note_en": note_en or note, "path": str(path) if path else "", "active": active}


def overview(cfg, *, models: list[dict], images: list[dict], image_dir: Path | None) -> dict:
    """Alle Gruppen mit Größe. models/images kommen vom Aufrufer (er kennt Profile und Bildmodelle) als fertige
    Einträge; hier kommt dazu, was an Platz im Modellordner sonst noch liegt, die Programme und die Daten."""
    from .voice import piper, pocket
    data = data_dir(cfg)
    mdir = llamacpp.models_dir(cfg)
    counted = sum(m["bytes"] for m in models if m.get("in_models_dir"))
    rest = size_of(mdir) - counted if mdir.is_dir() else 0
    model_items = [{k: v for k, v in m.items() if k != "in_models_dir"} for m in models]
    if rest > 1024 * 1024:
        model_items.append(_item("Weitere Dateien im Modellordner", rest, label_en="Other files in the model folder",
                                 note="Nicht von Orbwise geladen – bleiben liegen.",
                                 note_en="Not downloaded by Orbwise – left alone.", path=mdir))

    image_items = list(images)
    if image_dir and image_dir.is_dir():
        prog = size_of(image_dir, exclude=(image_dir / "models",))
        loose = size_of(image_dir / "models") - sum(i["bytes"] for i in images)
        if loose > 1024 * 1024:
            image_items.append(_item("Reste (abgebrochene Downloads)", loose, label_en="Leftovers (cancelled downloads)",
                                     path=image_dir / "models"))
    else:
        prog = 0

    now, old = llamacpp_parts(cfg)
    programs = [_item("llama.cpp", now, note="aktuelle Version", note_en="current version",
                      path=llamacpp.root(cfg))] if now else []
    if old:
        programs.append(_item("llama.cpp – ältere Builds", old, label_en="llama.cpp – older builds",
                              delete="/api/storage/llamacpp-old",
                              note="Vorige Version und andere Varianten – lädt ein Update bei Bedarf neu.",
                              note_en="Previous version and other variants – an update fetches them again if needed."))
    if prog:
        programs.append(_item("Bilderzeugung (sd-server)", prog, label_en="Image generation (sd-server)", path=image_dir))
    tts_size = size_of(piper.home(data)) + size_of(data / "voices")
    if tts_size:
        programs.append(_item("Piper-Stimmen", tts_size, label_en="Piper voices", open_="voice", path=piper.home(data)))
    pocket_size = sum(size_of(p) for p in (pocket.home(data), *pocket.hf_cache_dirs()))
    if pocket_size:
        programs.append(_item("Pocket TTS", pocket_size, open_="voice", path=pocket.home(data)))
    chromium = size_of(browser.own_dir())
    if chromium:
        programs.append(_item("Browser (Chromium)", chromium, delete="/api/storage/browser", path=browser.own_dir(),
                              note="Lädt sich beim nächsten Gebrauch neu.", note_en="Downloads again when next needed."))

    snaps = size_of(snapshot.base_dir())
    work = size_of(workspace.base_dir())
    arts = artifacts.usage(cfg)["bytes"]
    known = (llamacpp.root(cfg), piper.home(data), pocket.home(data), data / "voices", artifacts.folder(cfg),
             browser.own_dir(), snapshot.base_dir(), workspace.base_dir(), mdir, cache_dir())
    if image_dir:
        known += (image_dir,)
    if os.path.realpath(data) == os.path.realpath(Path.home()):  # memory.dir direkt im Home: nicht das ganze Home zählen
        own = size_of(Path(cfg.memory.dir).expanduser()) + size_of(data / "state.json")
    else:
        own = size_of(data, exclude=tuple(p for p in known if _inside(p, data)))
    mine = [_item("Chats, Gedächtnis, Anhänge, Einstellungen", own, label_en="Chats, memory, attachments, settings",
                  path=data, note="Chats löschst du in der Seitenleiste.", note_en="Delete chats in the sidebar.")]
    if work:
        mine.append(_item("Arbeitskopien von Projekten", work, label_en="Project working copies",
                          path=workspace.base_dir()))

    groups = [
        _group("models", "Sprachmodelle", "Language models", model_items, path=mdir),
        _group("images", "Bildmodelle", "Image models", image_items, path=image_dir),
        _group("programs", "Programme", "Programs", programs),
        _group("cache", "Zwischenspeicher", "Cache", [_item("Zwischenspeicher", size_of(cache_dir()),
                                                            label_en="Cache", path=cache_dir())],
               path=cache_dir(), clear="/api/storage/cache",
               note=("Ausgaben von Befehlen, Protokolle, Downloads von Paperless – entsteht bei Bedarf neu.",
                     "Command output, logs, Paperless downloads – rebuilt when needed.")),
        _group("artifacts", "Artefakte", "Artifacts", [_item("Artefakte", arts, label_en="Artifacts",
                                                             path=artifacts.folder(cfg))],
               path=artifacts.folder(cfg), clear="/api/artifacts",
               note=("Diagramme, Exporte und Hilfsdateien, die Orbwise nebenbei erzeugt. Bilder im Chat bleiben.",
                     "Charts, exports and helper files Orbwise creates along the way. Images in the chat stay.")),
        _group("snapshots", "Sicherungspunkte", "Restore points", [_item("Sicherungspunkte", snaps,
                                                                         label_en="Restore points",
                                                                         path=snapshot.base_dir())],
               path=snapshot.base_dir(), clear="/api/storage/snapshots",
               note=("Stände deiner Projekte vor Änderungen des Agenten – zum Rückgängigmachen.",
                     "States of your projects before the agent changed them – for undo.")),
        _group("data", "Deine Daten", "Your data", mine),
    ]
    groups = [g for g in groups if g["bytes"] or g["key"] in ("models", "cache")]
    return {"total": sum(g["bytes"] for g in groups), "groups": groups}
