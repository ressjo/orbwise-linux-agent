"""Tools für das Langzeitgedächtnis."""

from __future__ import annotations

from typing import Annotated

from ..memory.files import valid_day
from . import proc
from .registry import ToolContext, tool


@tool("Merkt sich dauerhaft einen Fakt über den Nutzer, seine Vorlieben, sein System oder Projekte – als "
      "vollständigen Satz.")
async def remember(ctx: ToolContext, fact: str) -> str:
    added, replaced = await ctx.memory.remember_fact(fact)
    if replaced:
        return f"Aktualisiert (vorher: {replaced})."
    return "Gespeichert." if added else "Das war bereits bekannt."


@tool("Löscht gespeicherte Fakten, die den Suchtext enthalten.")
async def forget(ctx: ToolContext, query: str) -> str:
    removed = await ctx.memory.forget(query)
    return ("Gelöscht: " + "; ".join(removed)) if removed else "Kein passender Fakt gefunden."


@tool("Durchsucht das Gedächtnis (frühere Gespräche, Tageszusammenfassungen, Fakten). Mit date: Protokoll dieses "
      "Tages; relative Angaben wie 'letzten Dienstag' direkt übergeben.")
async def recall(
    ctx: ToolContext,
    query: str = "",
    date: Annotated[str, "YYYY-MM-DD oder wie gesagt"] = "",
) -> str:
    mem = ctx.memory
    parts = []
    if date and not valid_day(date):
        from ..dates import resolve
        when = resolve(date, future=False)  # Rückblick: „Dienstag“ = der letzte Dienstag
        if when is None:
            return "Datum nicht verstanden – bitte YYYY-MM-DD oder z. B. 'letzten Dienstag'."
        date = when.day.isoformat()
    if date:
        summary = mem.summaries.read(date)
        journal = mem.journal.read(date)
        if summary:
            parts.append(summary)
        if journal and (not summary or query):
            parts.append("Protokoll:\n" + proc.clip(journal, 5000))
        if not parts:
            parts.append(f"Für {date} gibt es keine Einträge. Vorhandene Tage: {', '.join(mem.journal.days()[:15])}")
    if query:
        hits = await mem.index.search(query, k=8)
        parts.append(mem.format_hits(hits) if hits else "Keine passenden Erinnerungen gefunden.")
    if not parts:
        days = mem.journal.days()
        parts.append("Tage mit Einträgen: " + (", ".join(days[:30]) or "keine"))
    return "\n\n".join(parts)
