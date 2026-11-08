"""Skills einrichten, testen, an-/abschalten."""

from __future__ import annotations

import asyncio

from fastapi import HTTPException, Request

from ... import skills as skill_store
from ...lang import T


def register(app, ctx) -> None:
    # gemeinsame Objekte und Helfer anderer Teile (siehe webapp/context.py)
    agent = ctx.agent
    cfg = ctx.cfg
    hub = ctx.hub

    # ---------- Skills: einrichten, an-/abschalten (Werte in state.json, Geheimnisse im Schlüsselbund) ----------
    def skills_changed() -> None:
        agent.reload_tools()
        hub.prewarm_soon()

    @app.get("/api/skills")
    async def skills_get():
        st = await asyncio.to_thread(skill_store.status, cfg)
        st["plaintext"] = skill_store.plaintext_left(cfg)
        return st

    @app.post("/api/skills/{skill_id}")
    async def skills_save(skill_id: str, request: Request):
        body = await request.json()
        if skill_id not in skill_store.BY_ID or not isinstance(body, dict):
            raise HTTPException(404 if skill_id not in skill_store.BY_ID else 400, T("Unbekannter Skill", "Unknown skill"))
        values = body.get("values") if isinstance(body.get("values"), dict) else None
        enabled = body.get("enabled") if isinstance(body.get("enabled"), bool) else None
        try:
            await asyncio.to_thread(skill_store.save, cfg, skill_id, enabled, values)
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
        skills_changed()
        await hub.broadcast({"type": "skills_changed", "id": skill_id})
        return {"ok": True}

    @app.post("/api/skills/{skill_id}/test")
    async def skills_test(skill_id: str, request: Request):
        if skill_id not in skill_store.BY_ID:
            raise HTTPException(404, T("Unbekannter Skill", "Unknown skill"))
        body = await request.json()
        values = body.get("values") if isinstance(body, dict) and isinstance(body.get("values"), dict) else None
        try:
            return await skill_store.test(cfg, skill_id, values)
        except ValueError as e:
            return {"ok": False, "message": str(e)}

    @app.delete("/api/skills/{skill_id}")
    async def skills_remove(skill_id: str):
        if skill_id not in skill_store.BY_ID:
            raise HTTPException(404, T("Unbekannter Skill", "Unknown skill"))
        await asyncio.to_thread(skill_store.remove, cfg, skill_id)
        skills_changed()
        await hub.broadcast({"type": "skills_changed", "id": skill_id})
        return {"ok": True}
