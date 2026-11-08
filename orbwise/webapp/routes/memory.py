"""Gedächtnis-Ansicht (Tage, Fakten, löschen) und Erinnerungen."""

from __future__ import annotations

import asyncio

from fastapi import HTTPException, Request

from ...lang import T
from ...memory.files import valid_day


def register(app, ctx) -> None:
    # gemeinsame Objekte und Helfer anderer Teile (siehe webapp/context.py)
    agent = ctx.agent
    memory = ctx.memory
    reminders = ctx.reminders

    @app.get("/api/reminders")
    async def get_reminders():
        return [{"id": r.id, "text": r.text, "due": r.due, "kind": r.kind} for r in reminders.upcoming()]

    @app.delete("/api/reminders/{rid}")
    async def delete_reminder(rid: str):
        if not reminders.cancel(rid):
            raise HTTPException(404, "Erinnerung nicht gefunden")
        return {"ok": True}

    # Gedächtnis-Ansicht: das des offenen Chats – Tools oder das eigene des Coding-Projekts
    @app.get("/api/memory/scope")
    async def memory_scope():
        project = memory.project_of(memory.active)
        return {"mode": "tools" if project is None else "coding", "project": project or ""}

    @app.get("/api/memory/days")
    async def memory_days():
        coding = memory.scope_id(memory.active) is not None
        summaries = set() if coding else set(memory.summaries.days())
        return [{"day": d, "summary": d in summaries} for d in memory.scope_journal(memory.active).days()]

    @app.get("/api/memory/day/{day}")
    async def memory_day(day: str):
        if not valid_day(day):
            raise HTTPException(400, "ungültiges Datum")
        coding = memory.scope_id(memory.active) is not None
        return {"day": day, "journal": memory.scope_journal(memory.active).read(day),
                "summary": None if coding else memory.summaries.read(day)}

    @app.get("/api/memory/facts")
    async def memory_facts():
        return [{"fact": f, "day": d} for f, d in memory.scoped_facts(memory.active).list()]

    @app.post("/api/memory/facts/delete")
    async def memory_fact_delete(request: Request):
        """Einen Fakt aus dem Gedächtnis des offenen Chats löschen (genau diesen, nicht per Suchtext)."""
        fact = str((await request.json()).get("fact") or "")
        if not await memory.forget_fact(fact, memory.active):
            raise HTTPException(404, T("Diesen Fakt gibt es nicht (mehr).", "This fact does not exist (anymore)."))
        agent._cache_owner = None  # Systemprompt enthält die Fakten – beim nächsten Mal neu einlesen
        return {"ok": True}

    @app.delete("/api/memory/day/{day}")
    async def memory_day_delete(day: str):
        """Tagebuch eines Tages vergessen (Protokoll, Zusammenfassung, Suchindex) – Fakten und Chats bleiben."""
        if not valid_day(day):
            raise HTTPException(400, "ungültiges Datum")
        if not await asyncio.to_thread(memory.forget_day, day, memory.active):
            raise HTTPException(404, T("Für diesen Tag gibt es keine Einträge.", "There are no entries for this day."))
        return {"ok": True}
