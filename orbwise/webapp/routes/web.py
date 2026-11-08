"""Zugang zur Oberfläche: nur localhost, Käfig-Sperre, Anmeldung mit Zugangs-Token, index.html."""

from __future__ import annotations

import asyncio
import json
import re
import secrets
import time

from fastapi import Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from ...lang import T
from ...web_i18n import translate_index
from ..security import check_host
from ..static import WEB_DIR, versioned_assets


def register(app, ctx) -> None:
    # gemeinsame Objekte und Helfer anderer Teile (siehe webapp/context.py)
    cfg = ctx.cfg
    from ... import web_auth
    web_token = ctx.web_token

    def from_jail(conn) -> bool:
        """Kommt die Verbindung aus dem Käfig des Auto-Modus? Dann darf sie Orbwise nicht bedienen (z. B. sich eine
        Rückfrage selbst bestätigen) – das Netz ist im Käfig an, localhost also erreichbar."""
        from ... import jail
        client = conn.client
        return bool(client) and jail.active() and jail.peer_is_jailed(client.port, cfg.port)

    @app.middleware("http")
    async def only_local(request: Request, call_next):
        # Schutz vor DNS-Rebinding: nur Anfragen an localhost zulassen
        if not check_host(request.headers.get("host"), cfg.port):
            return JSONResponse({"error": "forbidden host"}, status_code=403)
        from ... import jail
        if jail.active() and await asyncio.to_thread(from_jail, request):
            return JSONResponse({"error": "forbidden: isolated command"}, status_code=403)
        # Schreibende Anfragen nur von der eigenen Oberfläche (Schutz vor CSRF)
        origin = request.headers.get("origin")
        if request.method not in ("GET", "HEAD") and origin and \
                not check_host(re.sub(r"^https?://", "", origin), cfg.port):
            return JSONResponse({"error": "forbidden origin"}, status_code=403)
        # Zugangs-Token (web_auth.py): sonst könnte jedes Programm auf dem Rechner Orbwise bedienen
        path = request.url.path
        if not web_auth.is_public(path) and path != "/" \
                and not web_auth.valid(web_auth.given(request.headers, request.cookies), web_token):
            return JSONResponse({"error": "unauthorized", "detail": T(
                "Nicht angemeldet – Orbwise über das Startmenü oder mit „orbwise open“ öffnen.",
                "Not signed in – open Orbwise from the app menu or with “orbwise open”.")}, status_code=401)
        return await call_next(request)

    def signed_in(response, token: str):
        response.set_cookie(web_auth.COOKIE, token, max_age=web_auth.MAX_AGE, httponly=True, samesite="strict",
                            path="/")
        return response

    open_codes: dict[str, float] = {}  # Einmal-Codes für den Starter: Code → gültig bis

    def new_open_url(seconds: float = 60) -> str:
        now = time.monotonic()
        for c, until in list(open_codes.items()):
            if until < now:
                open_codes.pop(c, None)
        code = secrets.token_urlsafe(18)
        open_codes[code] = now + seconds
        return f"http://localhost:{cfg.port}/?code={code}"
    app.state.open_url = new_open_url  # orbwise serve --open

    @app.post("/api/open-code")
    async def open_code():
        """Für den Starter / „orbwise open“ (mit Token-Header): ein Einmal-Code für die Adresszeile – so landet das
        Token selbst nie im Browser-Verlauf."""
        return {"url": new_open_url()}

    @app.get("/")
    async def index(request: Request, code: str = ""):
        if code:  # Link aus dem Starter: Code einlösen (nur einmal), Cookie setzen, Code aus der Adresszeile nehmen
            until = open_codes.pop(code, 0)
            if until < time.monotonic():
                return HTMLResponse(web_auth.login_page(cfg.language, expired=True), status_code=401,
                                    headers={"Cache-Control": "no-store"})
            return signed_in(RedirectResponse("/", status_code=303), web_token)
        if not web_auth.valid(web_auth.given(request.headers, request.cookies), web_token):
            return HTMLResponse(web_auth.login_page(cfg.language), status_code=401, headers={"Cache-Control": "no-store"})
        html = translate_index((WEB_DIR / "index.html").read_text(encoding="utf-8"), cfg.language)
        return HTMLResponse(versioned_assets(html), headers={"Cache-Control": "no-cache"})

    @app.post("/api/login")
    async def login(request: Request):
        """Anmeldeseite: Token einfügen (Formular oder JSON) → Cookie."""
        raw = (await request.body())[:2000].decode(errors="replace")
        if "json" in request.headers.get("content-type", ""):
            try:
                given = str(json.loads(raw).get("token") or "")
            except (ValueError, AttributeError):
                given = ""
        else:
            from urllib.parse import parse_qs
            given = (parse_qs(raw).get("token") or [""])[0]
        if not web_auth.valid(given.strip(), web_token):
            await asyncio.sleep(1)  # Raten bremsen
            return HTMLResponse(web_auth.login_page(cfg.language, wrong=True), status_code=401,
                                headers={"Cache-Control": "no-store"})
        return signed_in(RedirectResponse("/", status_code=303), web_token)

    @app.get("/api/alive")
    async def alive():
        """Lebenszeichen ohne Token – für den Starter (orbwise-open), bevor er die Oberfläche öffnet."""
        return {"ok": True}

    # für andere Teile der App
    ctx.from_jail = from_jail
