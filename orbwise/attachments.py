"""Bilder, die der Nutzer im Chat anhängt (Büroklammer, Einfügen, Ziehen): liegen unter <data>/attachments und
stehen als Dateiname an der Nutzernachricht ("attachments"). Kann das aktive Modell Bilder sehen, bekommt es sie
direkt (OpenAI-Format image_url bzw. Ollama images); sonst steht der Pfad in der Nachricht und das Modell nutzt
look_at_image (eigenes Vision-Modell)."""

from __future__ import annotations

import base64
import hashlib
import re
from pathlib import Path

MAX_BYTES = 20_000_000
MAX_PER_MESSAGE = 4
TOKENS_PER_IMAGE = 800  # grobe Schätzung für die Kontext-Anzeige (je nach Modell und Auflösung 256–1.600)
MIME = {".png": "image/png", ".jpg": "image/jpeg", ".webp": "image/webp", ".gif": "image/gif"}


def folder(cfg) -> Path:
    return Path(cfg.memory.dir).expanduser().parent / "attachments"


def kind(data: bytes) -> str | None:
    """Dateiendung nach Inhalt (nicht nach Name) – nur echte Bilder."""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if data.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return ".gif"
    return None


def save(cfg, data: bytes) -> str:
    if len(data) > MAX_BYTES:
        raise ValueError("Bild zu groß (höchstens 20 MB)")
    ext = kind(data)
    if not ext:
        raise ValueError("Kein unterstütztes Bild (PNG, JPEG, WebP, GIF)")
    name = hashlib.sha256(data).hexdigest()[:20] + ext
    target = folder(cfg)
    target.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = target / name
    if not path.exists():
        path.write_bytes(data)
    return name


def save_data_url(cfg, raw: str) -> str:
    try:
        data = base64.b64decode(raw.split(",", 1)[-1], validate=False)
    except ValueError as e:
        raise ValueError("Bild nicht lesbar") from e
    return save(cfg, data)


def path_of(cfg, name: str) -> Path | None:
    if not re.fullmatch(r"[0-9a-f]{20}\.(png|jpg|webp|gif)", name or ""):
        return None
    path = folder(cfg) / name
    return path if path.is_file() else None


def data_url(path: Path) -> str:
    mime = MIME.get(path.suffix.lower(), "image/png")
    return f"data:{mime};base64," + base64.b64encode(path.read_bytes()).decode()
