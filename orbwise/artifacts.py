"""Artefakte: was der Agent nebenbei erzeugt (Diagramme, Grafiken, Exporte, Hilfsskripte), ohne dass der Nutzer einen
Ort genannt hat – liegt unter <data>/artifacts, einem Temp-Ordner. Die Einstellungen zeigen seinen Platz und leeren
ihn. Bilder, die im Chat erscheinen, liegen als Kopie bei den Anhängen (attachments.py) und bleiben im Verlauf
sichtbar, auch wenn der Ordner geleert wird."""

from __future__ import annotations

import os
import shutil
import time
from pathlib import Path

# Bilder, die der Chat zeigen kann (SVG nicht: eine SVG-Datei kann Skripte enthalten)
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".gif"}
RECENT_SECONDS = 15 * 60  # „gerade erzeugt“: open_file zeigt solche Bilder im Chat statt im Bildbetrachter


def folder(cfg) -> Path:
    return Path(cfg.memory.dir).expanduser().parent / "artifacts"


def ensure(cfg) -> Path:
    path = folder(cfg)
    path.mkdir(parents=True, exist_ok=True)
    return path


def is_image(path: Path) -> bool:
    return path.suffix.lower() in IMAGE_SUFFIXES


def inside(cfg, path: Path) -> bool:
    try:
        return Path(os.path.realpath(path)).is_relative_to(os.path.realpath(folder(cfg)))
    except (OSError, ValueError):
        return False


def fresh(cfg, path: Path, now: float | None = None) -> bool:
    """Im Artefakt-Ordner oder gerade erst entstanden – dann gehört das Bild in den Chat."""
    if inside(cfg, path):
        return True
    try:
        return (now or time.time()) - path.stat().st_mtime < RECENT_SECONDS
    except OSError:
        return False


def usage(cfg) -> dict:
    root = folder(cfg)
    size = files = 0
    if root.is_dir():
        for dirpath, _, names in os.walk(root):
            for name in names:
                try:
                    st = os.lstat(os.path.join(dirpath, name))
                except OSError:
                    continue
                size += st.st_size
                files += 1
    return {"path": str(root), "bytes": size, "files": files}


def clear(cfg) -> dict:
    """Leert den Ordner (er selbst bleibt). Liefert, was vorher drin war."""
    before = usage(cfg)
    root = folder(cfg)
    if root.is_dir() and not root.is_symlink():
        for entry in root.iterdir():
            try:
                if entry.is_dir() and not entry.is_symlink():
                    shutil.rmtree(entry)
                else:
                    entry.unlink()
            except OSError:
                pass
    return before
