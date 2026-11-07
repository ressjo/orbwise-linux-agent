"""Oberflächen-Dateien ausliefern: Inhalts-Hash in den Verweisen, immer neu prüfen."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

from fastapi.staticfiles import StaticFiles

WEB_DIR = Path(__file__).parent.parent / "web"

def versioned_assets(html: str) -> str:
    """/static/app.js → /static/app.js?v=<Inhalts-Hash>: Nach einem Update lädt der Browser jede geänderte Datei
    sofort neu. Sonst mischt er neue und alte Dateien aus dem Cache (z. B. neues app.js mit altem orb.js) –
    dann bricht die Oberfläche an fehlenden Funktionen ab (keine Werkzeug-Anzeige, „denke nach“ bleibt stehen)."""
    def stamp(m: re.Match) -> str:
        path = WEB_DIR / m.group(2)
        if not path.is_file():
            return m.group(0)
        digest = hashlib.sha1(path.read_bytes()).hexdigest()[:10]
        return f"{m.group(1)}/static/{m.group(2)}?v={digest}{m.group(3)}"
    return re.sub(r'((?:src|href)=")/static/([\w./-]+)(")', stamp, html)


class FreshStaticFiles(StaticFiles):
    """Oberflächen-Dateien immer neu prüfen (ETag → meist 304): nach einem Update läuft sonst stundenlang
    das alte app.js aus dem Browser-Cache weiter."""

    def file_response(self, *args, **kwargs):
        resp = super().file_response(*args, **kwargs)
        resp.headers["Cache-Control"] = "no-cache"
        return resp
