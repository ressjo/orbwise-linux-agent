"""Aufgabenliste für Aufgaben mit mehreren Schritten (wie Claude Codes To-do-Liste).

Kleine Modelle verlieren bei langen Aufgaben leicht den Faden: Die Liste steht im Verlauf, übersteht jede
Komprimierung (der Code hängt sie an die Zusammenfassung) und erscheint live in der Antwort."""

from __future__ import annotations

import re
from typing import Annotated

from .registry import ToolContext, tool

_LINE = re.compile(r"^\s*(?:[-*]\s*)?\[([ xX~>])\]\s*(.+?)\s*$")
_STATE = {" ": "open", "x": "done", "X": "done", "~": "active", ">": "active"}
MARK = {"open": "[ ]", "active": "[~]", "done": "[x]"}


def parse_todos(text: str) -> list[dict]:
    """„- [ ] Aufgabe“ / „- [~] läuft“ / „- [x] erledigt“, eine pro Zeile; Zeilen ohne Kästchen gelten als offen."""
    items = []
    for raw in (text or "").splitlines():
        if not raw.strip():
            continue
        m = _LINE.match(raw)
        if m:
            items.append({"text": m.group(2)[:200], "state": _STATE[m.group(1)]})
        else:
            items.append({"text": raw.strip().lstrip("-*• ").strip()[:200], "state": "open"})
    return items[:30]


def format_todos(items: list[dict]) -> str:
    return "\n".join(f"- {MARK.get(i['state'], '[ ]')} {i['text']}" for i in items)


@tool("Aufgabenliste für Aufgaben ab 3 Schritten: anlegen, nach jedem Schritt die ganze Liste neu schicken, genau "
      "ein Punkt läuft.")
async def todo_write(
    ctx: ToolContext,
    todos: Annotated[str, "Je Zeile '- [ ] offen', '- [~] läuft', '- [x] erledigt'"],
) -> str:
    items = parse_todos(todos)
    if not items:
        return "Leere Liste – eine Aufgabe pro Zeile angeben, z. B. '- [~] Tests ausführen'."
    conv = ctx.memory.conversation if ctx.memory else None
    if conv is not None:
        conv.meta["todos"] = items
    if ctx.emit:
        await ctx.emit({"type": "todos", "id": ctx.call_id, "items": items})
    done = sum(1 for i in items if i["state"] == "done")
    return f"Aufgabenliste ({done}/{len(items)} erledigt):\n{format_todos(items)}"
