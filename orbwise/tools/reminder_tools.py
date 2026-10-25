"""Tools für Erinnerungen und Timer."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from ..reminders import ReminderStore, parse_when
from .registry import ToolContext, tool


def store_for(ctx: ToolContext) -> ReminderStore:
    store = ctx.services.get("reminders")
    if store is None:
        store = ReminderStore(ctx.cfg.memory.dir.parent / "reminders.json")
        ctx.services["reminders"] = store
    return store


@tool("Setzt eine Erinnerung oder einen Timer: in_minutes relativ, at fest. „Auf der Arbeit“, „morgen früh“: "
      "Uhrzeit aus den Fakten nehmen oder nachfragen.")
async def set_reminder(
    ctx: ToolContext,
    text: Annotated[str, "Woran erinnert wird"],
    in_minutes: Annotated[float, "z. B. 20; 0.5 = 30 s"] = 0,
    at: Annotated[str, "'YYYY-MM-DD HH:MM', 'HH:MM' oder wie gesagt"] = "",
    timer: Annotated[bool, "true = Timer"] = False,
) -> str:
    try:
        due = parse_when(in_minutes, at)
    except ValueError as e:
        return str(e)
    if due < datetime.now():
        return "Dieser Zeitpunkt liegt in der Vergangenheit."
    r = store_for(ctx).add(text, due, "timer" if timer else "reminder")
    return f"Gespeichert: {r.label()}"


@tool("Listet alle anstehenden Erinnerungen und Timer auf.")
async def list_reminders(ctx: ToolContext) -> str:
    items = store_for(ctx).upcoming()
    if not items:
        return "Es gibt keine anstehenden Erinnerungen."
    return "\n".join(r.label() for r in items)


@tool("Löscht Erinnerungen/Timer per ID, Stichwort oder 'alle'.")
async def cancel_reminder(ctx: ToolContext, which: Annotated[str, "ID, Stichwort oder 'alle'"]) -> str:
    removed = store_for(ctx).cancel(which)
    if not removed:
        return f"Keine anstehende Erinnerung passt zu '{which}'."
    return "Gelöscht: " + "; ".join(r.text for r in removed)
