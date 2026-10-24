"""state.json: gemeinsamer Speicher für Profile und Einstellungen (Router, Modellseite, Agent, API-Server …).

Alle lesen und schreiben über dieses Modul: `update` hält ein prozessweites Lock über Lesen – Ändern – Schreiben, und
geschrieben wird atomar (Temp-Datei im selben Ordner, fsync, rename). Ein Absturz beim Speichern hinterlässt so nie
eine halbe Datei, und zwei gleichzeitige Änderungen (z. B. Download-Job und Modellseite) überschreiben sich nicht.
"""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

_LOCK = threading.RLock()


def load(path: Path | None) -> dict:
    """Inhalt der Datei – leer, wenn sie fehlt oder kaputt ist."""
    if not path:
        return {}
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=1)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def save(path: Path, data: dict) -> None:
    """Ganze Datei ersetzen (atomar)."""
    with _LOCK:
        _write(Path(path), data)


def update(path: Path | None, change: Callable[[dict], Any]) -> Any:
    """Lesen, `change(data)` ändern lassen, atomar schreiben – unter einem Lock. → Rückgabe von `change`."""
    if not path:
        return change({})
    with _LOCK:
        data = load(path)
        result = change(data)
        _write(Path(path), data)
        return result
