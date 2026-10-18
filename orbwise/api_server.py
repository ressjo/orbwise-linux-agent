"""OpenAI-kompatible Schnittstelle unter /v1 – wie der Server-Modus von LM Studio: andere Programme (VS Code mit
Continue oder Cline, Open WebUI, eigene Skripte) nutzen das geladene Modell über Orbwise. Sie bekommen das Modell
„roh“ – ohne Orbwise-Systemprompt, Gedächtnis und Werkzeuge, also auch ohne Zugriff auf den Rechner.

Orbwise steht davor, statt den Port von llama-server freizugeben: Es startet den Modell-Server bei Bedarf, gibt den
Grafikspeicher aus dem Bild-Modus zurück, wechselt auf Wunsch das Modell und weiß, dass sein eigener Prompt-Cache
danach weg ist. Aus, bis es in der Oberfläche eingeschaltet wird; jede Anfrage braucht den eigenen API-Schlüssel.
Erreichbar nur auf diesem Rechner (Orbwise lauscht auf 127.0.0.1 und lehnt fremde Host-Namen ab)."""

from __future__ import annotations

import json
import logging
import secrets
import time
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from .lang import T
from .llm import LLMError

log = logging.getLogger(__name__)
STATE_KEY = "api_server"


# ---------------------------------------------------------------- Einstellungen (state.json)
def _read(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def settings(path: Path) -> dict:
    raw = _read(path).get(STATE_KEY) or {}
    return {"enabled": bool(raw.get("enabled")), "key": str(raw.get("key") or ""),
            "allow_switch": bool(raw.get("allow_switch"))}


def save_settings(path: Path, **changes: Any) -> dict:
    data = _read(path)
    current = settings(path)
    current.update({k: v for k, v in changes.items() if k in current})
    if not current["key"]:
        current["key"] = new_key()
    data[STATE_KEY] = current
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=1), encoding="utf-8")
    return current


def new_key() -> str:
    return "ow-" + secrets.token_urlsafe(24)


def openai_error(status: int, message: str, kind: str = "invalid_request_error") -> JSONResponse:
    return JSONResponse({"error": {"message": message, "type": kind, "code": status}}, status_code=status)


def resolve_model(profiles: dict, requested: str, active: str) -> str:
    """Profilname zur Modellangabe des Programms – Profilname, Anzeigename oder Modellname; sonst das geladene."""
    want = (requested or "").strip().lower()
    if want:
        for name, p in profiles.items():
            if want in (name.lower(), (p.label or "").lower(), (p.model or "").lower()):
                return name
    return active


def upstream(router, name: str) -> tuple[str, dict]:
    """(Basis-URL der OpenAI-Schnittstelle, Header) des Profils: eigener llama-server oder Ollamas /v1."""
    p = router.profiles[name]
    if p.backend == "openai":
        return p.base_url.rstrip("/"), ({"Authorization": f"Bearer {p.api_key}"} if p.api_key else {})
    return (p.base_url or router.cfg.base_url).rstrip("/") + "/v1", {}


# ---------------------------------------------------------------- Routen
def register(app: FastAPI, *, cfg, llm, agent, hub, state_file: Path, model_progress, idle_if_free) -> None:
    from .llm_router import LLMRouter

    stats = {"requests": 0, "running": 0, "last": None, "last_client": "", "last_model": "", "last_error": ""}

    def api_url() -> str:
        return f"http://127.0.0.1:{cfg.port}/v1"

    def check_key(request: Request) -> JSONResponse | None:
        st = settings(state_file)
        if not st["enabled"]:
            return openai_error(503, T("Der API-Server von Orbwise ist aus – einschalten unter Modelle → API.",
                                       "The Orbwise API server is off – turn it on under Models → API."), "unavailable")
        auth = request.headers.get("authorization", "")
        given = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
        if not given or not secrets.compare_digest(given.encode(), st["key"].encode()):
            return openai_error(401, T("Falscher oder fehlender API-Schlüssel (Authorization: Bearer …).",
                                       "Wrong or missing API key (Authorization: Bearer …)."), "invalid_api_key")
        return None

    def router_or_error() -> JSONResponse | None:
        if not isinstance(llm, LLMRouter):
            return openai_error(503, T("Im Demo-Modus gibt es kein echtes Modell.", "There is no real model in demo mode."),
                                "unavailable")
        return None

    async def broadcast() -> None:
        await hub.broadcast({"type": "api_activity", **public_stats()})

    def public_stats() -> dict:
        return {k: stats[k] for k in ("requests", "running", "last", "last_client", "last_model", "last_error")}

    async def switch_to(name: str) -> None:
        """Auf Wunsch eines Programms das Modell wechseln – wie der Laden-Knopf, wartet auf eine laufende Antwort."""
        await hub.broadcast({"type": "model_switching", "name": name, "label": llm.profiles[name].label})
        llm.switching = name
        try:
            async with agent.lock:
                await llm.activate(name, model_progress)
        except LLMError as e:
            await hub.broadcast({"type": "model_error", "name": name, "text": str(e)})
            raise
        finally:
            llm.switching = None
            await idle_if_free()
        await hub.broadcast({"type": "model_active", "name": name})
        agent._cache_owner = None

    async def prepare(body: dict, request: Request) -> tuple[str, str, dict] | JSONResponse:
        """Modell bestimmen und bereit machen → (Profil, URL-Basis, Header)."""
        st = settings(state_file)
        name = resolve_model(llm.profiles, str(body.get("model") or ""), llm.active)
        if name != llm.active and not st["allow_switch"]:
            name = llm.active  # Programme nutzen das geladene Modell, egal welches sie nennen
        if llm.switching:
            return openai_error(503, T("Orbwise wechselt gerade das Modell – gleich noch einmal versuchen.",
                                       "Orbwise is switching models – try again in a moment."), "unavailable")
        try:
            if name != llm.active:
                await switch_to(name)
            if llm.gpu_borrowed:  # Bild-Modus hat den Grafikspeicher – zurückgeben, Sprachmodell starten
                await llm.gpu_borrowed()
            server = llm.server_for(name)
            if server is not None:
                await server.ensure_running(model_progress)
        except LLMError as e:
            return openai_error(502, str(e), "server_error")
        stats.update(requests=stats["requests"] + 1, running=stats["running"] + 1, last=time.time(),
                     last_client=request.headers.get("user-agent", "")[:80], last_model=llm.profiles[name].label or name,
                     last_error="")
        await broadcast()
        base, headers = upstream(llm, name)
        return name, base, headers

    async def finish(error: str = "") -> None:
        stats["running"] = max(0, stats["running"] - 1)
        if error:
            stats["last_error"] = error[:300]
        # Der Modell-Server hat jetzt den Prompt des Programms im Cache – Orbwise liest seinen Chat beim nächsten Mal neu
        agent._cache_owner = None
        agent.api_used = True
        await broadcast()

    async def forward(request: Request, path: str) -> Response:
        if (err := check_key(request)) or (err := router_or_error()):
            return err
        try:
            body = await request.json()
        except ValueError:
            return openai_error(400, T("Kein gültiges JSON.", "Invalid JSON."))
        if not isinstance(body, dict):
            return openai_error(400, T("Kein gültiges JSON-Objekt.", "Not a JSON object."))
        if path == "/chat/completions" and not isinstance(body.get("messages"), list):
            return openai_error(400, T("„messages“ fehlt.", "“messages” is missing."))
        ready = await prepare(body, request)
        if isinstance(ready, JSONResponse):
            return ready
        name, base, headers = ready
        payload = {**body, "model": llm.profiles[name].model or name}
        timeout = httpx.Timeout(cfg.llm.request_timeout, connect=10)
        client = httpx.AsyncClient(base_url=base, headers=headers, timeout=timeout)
        try:
            resp = await client.send(client.build_request("POST", path, json=payload), stream=True)
        except httpx.HTTPError as e:
            await client.aclose()
            await finish(str(e))
            return openai_error(502, T(f"Modell-Server nicht erreichbar: {e}", f"Model server not reachable: {e}"),
                                "server_error")
        if resp.status_code != 200 or not payload.get("stream"):
            raw = await resp.aread()
            await resp.aclose()
            await client.aclose()
            await finish("" if resp.status_code == 200 else f"{resp.status_code}: {raw[:200].decode(errors='replace')}")
            if resp.status_code == 200:  # dem Programm den Namen zeigen, den es aus /v1/models kennt
                try:
                    data = json.loads(raw)
                    data["model"] = name
                    raw = json.dumps(data).encode()
                except (ValueError, TypeError):
                    pass
            return Response(raw, status_code=resp.status_code,
                            media_type=resp.headers.get("content-type", "application/json"))

        async def relay():
            error = ""
            try:
                async for chunk in resp.aiter_raw():
                    yield chunk
            except httpx.HTTPError as e:
                error = str(e)
            finally:
                await resp.aclose()
                await client.aclose()
                await finish(error)
        return StreamingResponse(relay(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})

    @app.get("/v1/models")
    async def v1_models(request: Request):
        if (err := check_key(request)) or (err := router_or_error()):
            return err
        names = list(llm.profiles) if settings(state_file)["allow_switch"] else [llm.active]
        names.sort(key=lambda n: n != llm.active)
        return {"object": "list", "data": [{"id": n, "object": "model", "created": 0, "owned_by": "orbwise",
                                            "name": llm.profiles[n].label or n, "loaded": n == llm.active}
                                           for n in names]}

    @app.post("/v1/chat/completions")
    async def v1_chat(request: Request):
        return await forward(request, "/chat/completions")

    @app.post("/v1/completions")
    async def v1_completions(request: Request):
        return await forward(request, "/completions")

    # ---------- Einstellungen für die Oberfläche ----------
    @app.get("/api/apiserver")
    async def apiserver_get():
        st = settings(state_file)
        return {**st, "url": api_url(), "stats": public_stats(), "available": isinstance(llm, LLMRouter)}

    @app.post("/api/apiserver")
    async def apiserver_set(request: Request):
        body = await request.json()
        if not isinstance(body, dict):
            raise HTTPException(400, "JSON-Objekt erwartet")
        changes = {k: bool(body[k]) for k in ("enabled", "allow_switch") if k in body}
        if body.get("new_key"):
            changes["key"] = new_key()
        st = save_settings(state_file, **changes)
        log.info("API-Server %s", "an" if st["enabled"] else "aus")
        return {**st, "url": api_url(), "stats": public_stats(), "available": isinstance(llm, LLMRouter)}
