"""Bilder und PDFs, die der Nutzer im Chat anhängt (Büroklammer, Einfügen, Ziehen): liegen unter <data>/attachments.

Bilder stehen als Dateiname an der Nutzernachricht ("attachments"). Kann das aktive Modell Bilder sehen, bekommt es
sie direkt (OpenAI-Format image_url bzw. Ollama images); sonst steht der Pfad in der Nachricht und das Modell nutzt
look_at_image (eigenes Vision-Modell).

PDFs werden lokal ausgelesen (pypdf, nichts verlässt den Rechner). Der Text steht an der Nachricht ("docs") und geht
mit ihr ans Modell – bei großen Dateien gekürzt auf einen Teil des Kontextfensters, mit Hinweis. Gescannte PDFs ohne
Textebene haben keinen Text; das sagt die Oberfläche gleich beim Anhängen."""

from __future__ import annotations

import base64
import hashlib
import json
import re
from pathlib import Path

from .lang import T

MAX_BYTES = 20_000_000
MAX_PDF_BYTES = 50_000_000
MAX_PDF_CHARS = 2_000_000  # mehr Text hebt Orbwise nicht auf (passt ohnehin in kein Kontextfenster)
MAX_PER_MESSAGE = 4
MIME = {".png": "image/png", ".jpg": "image/jpeg", ".webp": "image/webp", ".gif": "image/gif", ".pdf": "application/pdf"}


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
    if data[:5] == b"%PDF-":
        return ".pdf"
    return None


def is_doc(name: str) -> bool:
    return name.endswith(".pdf")


def save(cfg, data: bytes, title: str = "") -> str:
    ext = kind(data)
    if not ext:
        raise ValueError(T("Nicht unterstützt – Bilder (PNG, JPEG, WebP, GIF) oder PDF",
                           "Not supported – images (PNG, JPEG, WebP, GIF) or PDF"))
    if len(data) > (MAX_PDF_BYTES if ext == ".pdf" else MAX_BYTES):
        raise ValueError(T("PDF zu groß (höchstens 50 MB)", "PDF too large (50 MB max)") if ext == ".pdf"
                         else T("Bild zu groß (höchstens 20 MB)", "Image too large (20 MB max)"))
    name = hashlib.sha256(data).hexdigest()[:20] + ext
    target = folder(cfg)
    target.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = target / name
    if not path.exists():
        path.write_bytes(data)
    if ext == ".pdf":
        _write_doc(path, title, data)
    return name


def save_data_url(cfg, raw: str, title: str = "") -> str:
    try:
        data = base64.b64decode(raw.split(",", 1)[-1], validate=False)
    except ValueError as e:
        raise ValueError(T("Datei nicht lesbar", "File not readable")) from e
    return save(cfg, data, title)


def path_of(cfg, name: str) -> Path | None:
    if not re.fullmatch(r"[0-9a-f]{20}\.(png|jpg|webp|gif|pdf)", name or ""):
        return None
    path = folder(cfg) / name
    return path if path.is_file() else None


# ---------------------------------------------------------------- PDFs
def pdf_text(data: bytes) -> tuple[str, int]:
    """(Text mit Seitenmarken, Seitenzahl). Passwortgeschützte oder kaputte Dateien → ValueError."""
    import io

    from pypdf import PdfReader
    from pypdf.errors import PdfReadError
    try:
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted and not reader.decrypt(""):
            raise ValueError(T("Das PDF ist passwortgeschützt.", "The PDF is password-protected."))
        parts, size = [], 0
        for i, page in enumerate(reader.pages, 1):
            try:
                text = (page.extract_text() or "").strip()
            except Exception:  # noqa: BLE001 – eine kaputte Seite soll nicht das ganze Dokument kosten
                text = ""
            if text:
                parts.append(f"--- {T('Seite', 'Page')} {i} ---\n{text}")
                size += len(parts[-1])
            if size > MAX_PDF_CHARS:
                break
        return "\n\n".join(parts)[:MAX_PDF_CHARS], len(reader.pages)
    except (PdfReadError, OSError, KeyError, TypeError, AttributeError) as e:
        raise ValueError(T("PDF nicht lesbar: ", "PDF not readable: ") + str(e)[:120]) from e


def _write_doc(path: Path, title: str, data: bytes) -> None:
    meta = path.with_suffix(".json")
    if meta.exists():
        old = json.loads(meta.read_text(encoding="utf-8"))
        if title and old.get("title") != title:  # gleiche Datei unter neuem Namen
            old["title"] = title
            meta.write_text(json.dumps(old, ensure_ascii=False), encoding="utf-8")
        return
    text, pages = pdf_text(data)
    meta.write_text(json.dumps({"title": title or path.name, "pages": pages, "text": text}, ensure_ascii=False),
                    encoding="utf-8")


def doc(cfg, name: str) -> dict | None:
    """Ausgelesenes PDF: {name, title, pages, text}."""
    path = path_of(cfg, name)
    if not path or not is_doc(name):
        return None
    try:
        data = json.loads(path.with_suffix(".json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return {"name": name, "title": str(data.get("title") or name), "pages": int(data.get("pages") or 0),
            "text": str(data.get("text") or "")}


def doc_info(d: dict) -> dict:
    """Was die Oberfläche von einem Dokument braucht (ohne den Text)."""
    return {"name": d["name"], "title": d["title"], "pages": d["pages"], "chars": d.get("chars", len(d.get("text", ""))),
            "truncated": bool(d.get("truncated")), "scanned": not d.get("text") and not d.get("chars")}


def fit_docs(docs: list[dict], max_chars: int) -> list[dict]:
    """Texte auf zusammen höchstens max_chars kürzen (gleichmäßig verteilt) – die Nachricht soll das Kontextfenster
    nicht sprengen. Merkt sich die Originallänge, damit das Modell vom Kürzen weiß."""
    share = max(2000, max_chars // max(1, len(docs)))
    out = []
    for d in docs:
        text = d["text"]
        out.append({**d, "text": text[:share], "chars": len(text), "truncated": len(text) > share})
    return out


def pages_text(n: int) -> str:
    return T(f"{n} Seite" if n == 1 else f"{n} Seiten", f"{n} page" if n == 1 else f"{n} pages")


def doc_block(d: dict) -> str:
    """Dokument für das Modell: Kopf mit Titel und Seiten, der Text, ein klares Ende."""
    head = T(f"[Angehängtes PDF „{d['title']}“, ", f"[Attached PDF “{d['title']}”, ") + pages_text(d["pages"])
    if not d["text"]:
        return head + T(" – kein Text auslesbar (vermutlich gescannt). Sag das dem Nutzer.]",
                        " – no text could be extracted (probably scanned). Tell the user.]")
    if d.get("truncated"):
        shown, total = f"{len(d['text']):,}", f"{d['chars']:,}"
        head += T(f" – gekürzt: die ersten {shown.replace(',', '.')} von {total.replace(',', '.')} Zeichen; sag dem "
                  "Nutzer, dass nur der Anfang gelesen wurde",
                  f" – shortened: the first {shown} of {total} characters; tell the user only the beginning was read")
    return f"{head}]\n{d['text']}\n[{T('Ende des PDFs', 'End of the PDF')}]"


def data_url(path: Path) -> str:
    mime = MIME.get(path.suffix.lower(), "image/png")
    return f"data:{mime};base64," + base64.b64encode(path.read_bytes()).decode()
