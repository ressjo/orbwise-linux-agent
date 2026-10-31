"""Zugangsschutz für Oberfläche und API: ein geheimes Token, das nur der eigene Benutzer lesen kann.

Ohne Token könnte jedes Programm auf dem Rechner – auch das eines anderen Benutzers – Orbwise über localhost
bedienen und damit Befehle in deinem Namen ausführen. Das Token liegt in ~/.config/orbwise/web-token (nur für dich
lesbar, 0600) und bleibt über Neustarts gleich.

- Browser: der Starter, `orbwise open` und `serve --open` holen sich mit dem Token einen Einmal-Code (60 s) und öffnen
  http://localhost:<port>/?code=… – der Server setzt ein HttpOnly-Cookie und leitet auf / um; danach reichen
  Lesezeichen. Das Token selbst landet so nie im Browser-Verlauf.
- Programme (orbwise-CLI, Skripte): Header X-Orbwise-Token.
- Ohne Token: Anmeldeseite (Token einfügen) bzw. 401.

Gegen Programme, die als derselbe Benutzer laufen, schützt das nur begrenzt – sie könnten die Datei lesen.
"""

from __future__ import annotations

import os
import secrets
from pathlib import Path

from .config import CONFIG_DIR, env

COOKIE = "orbwise_token"
HEADER = "x-orbwise-token"
MAX_AGE = 400 * 24 * 3600  # so lange, wie Browser ein Cookie höchstens halten


def token_path() -> Path:
    return Path(env("WEB_TOKEN_FILE") or CONFIG_DIR / "web-token").expanduser()


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def load_or_create(path: Path | None = None) -> str:
    """Token lesen – beim ersten Start anlegen (0600, Ordner 0700). Zu weit offene Rechte werden enger gestellt."""
    path = path or token_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path.parent, 0o700)
    except OSError:
        pass
    token = _read(path)
    if len(token) >= 32:
        try:
            if path.stat().st_mode & 0o077:
                os.chmod(path, 0o600)
        except OSError:
            pass
        return token
    token = secrets.token_urlsafe(32)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(token + "\n")
    os.replace(tmp, path)
    return token


def read_token() -> str:
    """Token für Programme, die mit einem laufenden Orbwise sprechen (leer, wenn es noch keins gibt)."""
    return _read(token_path())


def valid(given: str | None, token: str) -> bool:
    return bool(given and token) and secrets.compare_digest(given.encode(), token.encode())


def given(headers, cookies) -> str:
    """Token aus Header oder Cookie einer Anfrage bzw. WebSocket-Verbindung."""
    return headers.get(HEADER) or cookies.get(COOKIE) or ""


def headers(port: int) -> dict:
    """Header für die orbwise-CLI (Host wegen des Schutzes vor DNS-Rebinding, Token wegen dieses Moduls)."""
    out = {"Host": f"localhost:{port}"}
    tok = read_token()
    if tok:
        out["X-Orbwise-Token"] = tok
    return out


def open_url(port: int) -> str:
    """Adresse zum Öffnen im Browser: mit Einmal-Code vom laufenden Server, sonst die blanke Adresse
    (dann erscheint die Anmeldeseite)."""
    import httpx
    try:
        r = httpx.post(f"http://127.0.0.1:{port}/api/open-code", headers=headers(port), timeout=5)
        if r.status_code == 200:
            return r.json()["url"]
    except (httpx.HTTPError, ValueError, KeyError):
        pass
    return f"http://localhost:{port}/"


# Pfade ohne Token: eigene Schlüssel (/v1: API-Schlüssel, /api/askpass: Einmal-Token), Dateien der Oberfläche,
# Lebenszeichen für den Starter, Anmeldung
PUBLIC_PREFIXES = ("/v1/", "/static/")
PUBLIC_PATHS = {"/api/askpass", "/api/alive", "/api/login", "/favicon.ico"}


def is_public(path: str) -> bool:
    return path in PUBLIC_PATHS or path.startswith(PUBLIC_PREFIXES)


def login_page(lang: str = "de", wrong: bool = False, expired: bool = False) -> str:
    de = lang != "en"
    title = "Orbwise ist geschützt" if de else "Orbwise is protected"
    text = ("Öffne Orbwise über das Startmenü oder führe im Terminal <code>orbwise open</code> aus. "
            "Alternativ das Token aus <code>~/.config/orbwise/web-token</code> hier einfügen:") if de else (
           "Open Orbwise from the app menu or run <code>orbwise open</code> in a terminal. "
           "Or paste the token from <code>~/.config/orbwise/web-token</code> here:")
    error = (f'<p class="err">{"Falsches Token." if de else "Wrong token."}</p>' if wrong else "") + (
        f'<p class="err">{"Der Link ist abgelaufen – bitte neu öffnen." if de else "The link has expired – please open again."}</p>'
        if expired else "")
    button = "Anmelden" if de else "Sign in"
    return f"""<!doctype html><html lang="{'de' if de else 'en'}"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Orbwise</title>
<style>
:root {{ color-scheme: light dark; --bg:#f4f1ea; --fg:#2a2a28; --muted:#6b6862; --line:#d8d3c8; --accent:#2b6b6b; }}
@media (prefers-color-scheme: dark) {{ :root {{ --bg:#1d1d1b; --fg:#ecebe6; --muted:#a19e96; --line:#3a3935; --accent:#5aa5a5; }} }}
body {{ margin:0; min-height:100vh; display:grid; place-items:center; background:var(--bg); color:var(--fg);
  font:16px/1.5 system-ui, sans-serif; padding:16px; box-sizing:border-box; }}
main {{ max-width:460px; width:100%; }} h1 {{ font-size:22px; margin:0 0 10px; }} p {{ color:var(--muted); }}
code {{ font-size:14px; }} .err {{ color:#c0392b; }}
form {{ display:flex; gap:8px; margin-top:14px; }}
input {{ flex:1; min-width:0; padding:10px 12px; border:1px solid var(--line); border-radius:10px; background:transparent;
  color:inherit; font:inherit; }}
button {{ padding:10px 16px; border:0; border-radius:10px; background:var(--accent); color:#fff; font:inherit; cursor:pointer; }}
</style></head><body><main><h1>{title}</h1><p>{text}</p>{error}
<form method="post" action="/api/login"><input name="token" type="password" autocomplete="off" autofocus
 aria-label="Token"><button type="submit">{button}</button></form></main></body></html>"""
