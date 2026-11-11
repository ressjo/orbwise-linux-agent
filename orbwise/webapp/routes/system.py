"""Version und Update, Status, Passwortdialog (askpass), Artefakte und Anhänge, Telemetrie, Herunterfahren, Jobs."""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import subprocess
import time
from pathlib import Path

from fastapi import HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse

from ... import askpass, metrics, usage
from ...extras.calendar_tools import calendar_status
from ...extras.trilium import trilium_status
from ...lang import T

log = logging.getLogger(__name__)


def register(app, ctx) -> None:
    # gemeinsame Objekte und Helfer anderer Teile (siehe webapp/context.py)
    state_file = ctx.state_file
    agent = ctx.agent
    background = ctx.background
    broker = ctx.broker
    cfg = ctx.cfg
    hub = ctx.hub
    idle_if_free = ctx.idle_if_free
    images = ctx.images
    llm = ctx.llm
    memory = ctx.memory
    model_progress = ctx.model_progress
    startup = ctx.startup
    stt = ctx.stt
    telegram_bot = ctx.telegram_bot
    tts = ctx.tts
    wake = ctx.wake

    # ---------- Version und Update aus der Oberfläche ----------
    update_state: dict = {"checked": 0.0, "info": None, "running": False}

    @app.get("/api/update")
    async def update_info(check: bool = False, force: bool = False):
        """Installierter Stand; mit check=1 auch, ob es einen neueren gibt (git fetch höchstens alle 3 Stunden)."""
        from ... import update as upd
        if not check:
            return {**await asyncio.to_thread(upd.details), "running": update_state["running"]}
        fresh = time.monotonic() - update_state["checked"] < 3 * 3600
        if force or not fresh or update_state["info"] is None:
            update_state["info"] = await asyncio.to_thread(upd.check)
            update_state["checked"] = time.monotonic()
        return {**update_state["info"], "running": update_state["running"]}

    @app.post("/api/update")
    async def update_start():
        """Update laden (git pull, Abhängigkeiten) und Orbwise neu starten – Fortschritt per WebSocket
        (update_progress, update_done). Nicht, während Orbwise antwortet oder ein Bild rechnet."""
        from ... import update as upd
        if update_state["running"]:
            raise HTTPException(409, T("Das Update läuft schon.", "The update is already running."))
        if agent.busy() or images.lock.locked():
            raise HTTPException(409, T("Orbwise arbeitet gerade – erst abwarten, dann aktualisieren.",
                                       "Orbwise is busy – wait, then update."))
        update_state["running"] = True
        loop = asyncio.get_running_loop()
        lines: list[str] = []

        def out(line: str) -> None:
            lines.append(line)
            asyncio.run_coroutine_threadsafe(hub.broadcast({"type": "update_progress", "text": line}), loop)

        async def run() -> None:
            restart: list[Path] = []
            try:
                code = await asyncio.to_thread(upd.update, None, cfg.port, subprocess.run,
                                               lambda port, root: restart.append(root), out)
            except Exception as e:  # noqa: BLE001 – melden statt hängen lassen
                log.exception("Update fehlgeschlagen")
                code, lines[:] = 1, [*lines, str(e)]
            update_state["running"] = False
            update_state["info"] = None  # beim nächsten Blick neu prüfen
            await hub.broadcast({"type": "update_done", "ok": code == 0, "restart": bool(restart),
                                 "text": "\n".join(lines[-12:])})
            if restart:
                await asyncio.sleep(1)  # Meldung noch zustellen
                await asyncio.to_thread(upd.restart_self, cfg.port, restart[0])
        background.append(asyncio.create_task(run()))
        return {"started": True}

    @app.get("/api/startup")
    async def startup_state():
        return startup.snapshot()

    @app.get("/api/status")
    async def status():
        voice = {
            "stt": stt is not None,
            "tts": bool(tts and tts.available()),
            "tts_error": tts.error if tts else "Sprache deaktiviert",
            "wake": bool(wake and wake._ok),  # nicht hier laden (blockiert) – das macht warm_voice beim Start
            "wake_error": wake.error if wake else None,
        }
        return {
            "name": cfg.assistant_name, "user": cfg.user_name,
            "llm": await llm.status(),
            "voice": voice,
            "memory": {
                "days": len(memory.journal.days()),
                "facts": len(memory.scoped_facts(memory.active).list()),
                "chunks": memory.index.count(),
                "dir": str(cfg.memory.dir),
            },
            "trilium": await trilium_status(cfg),
            "calendar": await calendar_status(cfg),
            "busy": agent.busy(),
            "telegram": telegram_bot.status if telegram_bot is not None else {"running": False, "configured": False},
        }

    @app.post("/api/askpass")
    async def askpass_request(request: Request):
        """Vom Askpass-Helfer (sudo -A) aufgerufen – nur mit gültigem Einmal-Token."""
        token = request.headers.get("x-orbwise-askpass", "")
        try:
            body = await request.json()
            prompt, inner = str(body.get("prompt", ""))[:200], str(body.get("sudo_command", ""))[:300]
        except (ValueError, AttributeError):
            prompt = inner = ""
        client_port = request.client.port if request.client else None
        password = await broker.request(token, prompt, inner, caller=lambda: askpass.caller_is_sudo_helper(
            client_port, cfg.port, broker.helper))
        if password is None:
            return JSONResponse({"error": "abgelehnt"}, status_code=403)
        return JSONResponse({"password": password}, headers={"Cache-Control": "no-store"})

    # OpenAI-kompatible Schnittstelle (/v1) für andere Programme – aus, bis sie in der Oberfläche eingeschaltet wird
    from . import apiserver
    apiserver.register(app, cfg=cfg, llm=llm, agent=agent, hub=hub, state_file=state_file,
                        model_progress=model_progress, idle_if_free=idle_if_free)

    # ---------- Angehängte Bilder und PDFs im Chat ----------
    # ---------- Autostart beim Anmelden (Einstellungen → Allgemein), standardmäßig aus ----------
    @app.get("/api/autostart")
    async def autostart_get():
        from ... import autostart
        return {"enabled": autostart.enabled(), "path": str(autostart.path())}

    @app.post("/api/autostart")
    async def autostart_set(request: Request):
        from ... import autostart
        body = await request.json()
        try:
            on = await asyncio.to_thread(autostart.set_enabled, isinstance(body, dict) and body.get("enabled") is True)
        except OSError as e:
            raise HTTPException(500, str(e)) from e
        return {"enabled": on, "path": str(autostart.path())}

    # ---------- MCP-Server: verbunden? welche Werkzeuge? ----------
    @app.get("/api/mcp")
    async def mcp_status():
        return {"servers": ctx.mcp.status() if getattr(ctx, "mcp", None) else []}

    # ---------- Schritt-Limit je Antwort (Einstellungen → Allgemein), 0 = unbegrenzt ----------
    @app.get("/api/steps")
    async def steps_get():
        from ... import step_limit
        return step_limit.info(cfg)

    @app.post("/api/steps")
    async def steps_set(request: Request):
        from ... import step_limit
        body = await request.json()
        try:
            return await asyncio.to_thread(step_limit.set_limits, cfg, state_file, body if isinstance(body, dict) else {})
        except ValueError as e:
            raise HTTPException(400, T("Ungültiges Schritt-Limit.", "Invalid step limit.")) from e

    # ---------- Artefakte: Temp-Ordner für Diagramme, Exporte & Co. (siehe artifacts.py) ----------
    @app.get("/api/artifacts")
    async def artifacts_usage():
        from ... import artifacts
        return await asyncio.to_thread(artifacts.usage, cfg)

    @app.delete("/api/artifacts")
    async def artifacts_clear():
        from ... import artifacts
        before = await asyncio.to_thread(artifacts.clear, cfg)
        return {**await asyncio.to_thread(artifacts.usage, cfg), "freed": before["bytes"], "removed": before["files"]}

    @app.post("/api/attachments")
    async def attachment_upload(request: Request):
        from ... import attachments
        body = await request.json()
        title = Path(str(body.get("name") or "")).name[:120]
        try:  # PDFs werden dabei ausgelesen – kann bei großen Dateien einen Moment dauern
            name = await asyncio.to_thread(attachments.save_data_url, cfg, str(body.get("data") or ""), title)
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
        if attachments.is_doc(name):
            d = attachments.doc(cfg, name) or {"name": name, "title": title, "pages": 0, "text": ""}
            return {"name": name, "url": f"/api/attachments/{name}", "doc": attachments.doc_info(d)}
        vision = await agent.llm.supports_images() if hasattr(agent.llm, "supports_images") else False
        return {"name": name, "url": f"/api/attachments/{name}", "vision": vision}

    @app.get("/api/attachments/{name}")
    async def attachment_file(name: str):
        from ... import attachments
        path = attachments.path_of(cfg, name)
        if not path:
            raise HTTPException(404, "Datei nicht gefunden")
        return FileResponse(path, media_type=attachments.MIME.get(path.suffix, "image/png"),
                            headers={"Cache-Control": "private, max-age=86400", "X-Content-Type-Options": "nosniff"})

    @app.get("/api/metrics")
    async def get_metrics():
        return {**await asyncio.to_thread(metrics.collect), "usage": usage.summary(cfg)}

    @app.post("/api/shutdown")
    async def shutdown():
        """„Beenden“ unten in der Seitenleiste: wie Strg+C – uvicorn fährt herunter, lifespan räumt auf (Modell-Server,
        Sandbox-Container, Hintergrund-Jobs …). Hilft auch, wenn der Server nach einem Update im Hintergrund läuft."""
        await hub.broadcast({"type": "shutdown"})
        asyncio.get_running_loop().call_later(0.3, os.kill, os.getpid(), signal.SIGTERM)
        return {"ok": True}

    @app.post("/api/review/stop")
    async def review_stop():
        """„Stoppen“ am laufenden Code-Check-Schritt in der Aktivität."""
        return {"stopped": agent.stop_review()}

    @app.post("/api/jobs/{job_id}/stop")
    async def stop_job(job_id: str):
        from ...tools import jobs
        job = jobs.stop(job_id)
        if not job:
            raise HTTPException(404, T("Unbekannter Job.", "Unknown job."))
        return job.info()
