"""Routinen (Planer) und Briefing-Einstellungen."""

from __future__ import annotations

from datetime import datetime

from fastapi import HTTPException, Request

from ...config import BRIEFING_SECTIONS, BriefingConfig
from ...tools import briefing
from ...tools.registry import ToolContext


def register(app, ctx) -> None:
    # gemeinsame Objekte und Helfer anderer Teile (siehe webapp/context.py)
    agent = ctx.agent
    cfg = ctx.cfg
    memory = ctx.memory
    routines = ctx.routines
    start_routine = ctx.start_routine

    def routine_json(r) -> dict:
        return routines.to_dict(r, datetime.now(), cfg.language == "en")

    async def routine_body(request: Request) -> dict:
        body = await request.json()
        if not isinstance(body, dict):
            raise HTTPException(400, "JSON-Objekt erwartet")
        return body

    @app.get("/api/routines")
    async def routines_list():
        return [routine_json(r) for r in routines.items]

    @app.post("/api/routines")
    async def routines_add(request: Request):
        b = await routine_body(request)
        try:
            r = routines.add(str(b.get("name", "")), str(b.get("task", "")), str(b.get("time", "")),
                             b.get("days") or [], str(b.get("date") or ""))
        except (ValueError, TypeError) as e:
            raise HTTPException(400, str(e)) from e
        return routine_json(r)

    @app.put("/api/routines/{rid}")
    async def routines_update(rid: str, request: Request):
        b = await routine_body(request)
        try:
            r = routines.update(rid, **{k: b[k] for k in ("name", "task", "time", "days", "date", "enabled") if k in b})
        except KeyError as e:
            raise HTTPException(404, "Unbekannte Routine") from e
        except (ValueError, TypeError) as e:
            raise HTTPException(400, str(e)) from e
        return routine_json(r)

    @app.delete("/api/routines/{rid}")
    async def routines_delete(rid: str):
        if not routines.delete(rid):
            raise HTTPException(404, "Unbekannte Routine")
        return {"ok": True}

    @app.post("/api/routines/{rid}/run")
    async def routines_run(rid: str):
        if routines.get(rid) is None:
            raise HTTPException(404, "Unbekannte Routine")
        return {"ok": start_routine(rid)}

    @app.get("/api/briefing")
    async def briefing_get():
        s, why = briefing.settings(cfg), briefing.availability(cfg)
        en = cfg.language == "en"
        return {"settings": s.model_dump(), "customized": s != cfg.briefing,
                "sections": [{"id": k, "label": briefing.LABELS[k][1 if en else 0], "note": why[k]}
                             for k in BRIEFING_SECTIONS]}

    @app.put("/api/briefing")
    async def briefing_put(request: Request):
        body = await request.json()
        if not isinstance(body, dict):
            raise HTTPException(400, "JSON-Objekt erwartet")
        allowed = set(BriefingConfig.model_fields)
        try:
            s = briefing.save_settings(cfg, {k: v for k, v in body.items() if k in allowed})
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
        return {"ok": True, "settings": s.model_dump()}

    @app.delete("/api/briefing")
    async def briefing_reset():
        return {"ok": True, "settings": briefing.save_settings(cfg, None).model_dump()}

    @app.post("/api/briefing/preview")
    async def briefing_preview():
        ctx = ToolContext(cfg=cfg, memory=memory, services=agent.services)
        return {"text": await briefing.build_briefing(ctx)}
