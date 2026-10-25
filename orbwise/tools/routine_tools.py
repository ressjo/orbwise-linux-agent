"""Tools für Routinen („jeden Werktag um 8 nach Linux-News suchen“) – anlegen, ansehen, ändern, löschen, starten."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any

from ..lang import T
from ..routines import Routine, RoutineStore, days_label, parse_days, parse_time, when_label
from .registry import CONFIRM, SAFE, ToolContext, tool


def store_for(ctx: ToolContext) -> RoutineStore:
    store = ctx.services.get("routines")
    if store is None:
        store = RoutineStore(ctx.cfg.memory.dir.parent / "routines.json")
        ctx.services["routines"] = store
    return store


def _line(r: Routine, now: datetime) -> str:
    state = "" if r.enabled else " (pausiert)"
    last = f" · zuletzt {r.last_run[:16].replace('T', ' ')} ({r.last_status})" if r.last_run else ""
    return (f"[{r.id}] {r.name}{state} – {r.schedule()} · nächste: {when_label(r.next_run(now), now)}{last}\n"
            f"    Aufgabe: {r.task}")


def _plan_text(args: dict) -> str:
    """Lesbarer Zeitplan für den Bestätigungsdialog (ohne Fehler zu werfen)."""
    try:
        at = parse_time(args.get("time", ""))
    except ValueError:
        at = str(args.get("time", "?"))
    if str(args.get("date") or "").strip():
        return f"einmalig am {args['date']} um {at}"
    try:
        days = days_label(parse_days(args.get("days") or ""))
    except ValueError:
        days = str(args.get("days"))
    return f"{days} um {at}"


def _create_risk(ctx: ToolContext, args: dict) -> tuple[str, str]:
    name = args.get("name") or args.get("task", "")[:40]
    return CONFIRM, T(f"Neue Routine „{name}“", f"New routine “{name}”") + f" – {_plan_text(args)}:\n{args.get('task', '')}"


def _find_one(ctx: ToolContext, which: str) -> Routine:
    hits = store_for(ctx).find(which)
    if not hits:
        raise ValueError(f"Keine Routine „{which}“ gefunden (routine_list zeigt alle).")
    if len(hits) > 1:
        raise ValueError("Mehrdeutig: " + ", ".join(f"[{r.id}] {r.name}" for r in hits) + " – bitte die ID angeben.")
    return hits[0]


ROUTINE_ACTIONS_EN = {"ändern": "change", "löschen": "delete", "jetzt ausführen": "run now"}


def _named_risk(action: str):
    def risk(ctx: ToolContext, args: dict) -> tuple[str, str]:
        try:
            r = _find_one(ctx, str(args.get("which", "")))
            label = f"„{r.name}“ ({r.schedule()})"
        except ValueError:
            label = f"„{args.get('which', '')}“"
        changes = ", ".join(f"{k} → {v}" for k, v in args.items() if k != "which" and v not in (None, ""))
        text = T(f"Routine {label} {action}", f"{ROUTINE_ACTIONS_EN.get(action, action)} routine {label}")
        return CONFIRM, text + (f": {changes}" if changes else "")
    return risk


@tool("Legt eine Routine an: eine Aufgabe automatisch zur festen Uhrzeit (täglich, an Wochentagen oder einmalig), "
      "Ergebnis in eigenem Chat.",
      risk=_create_risk)
async def routine_create(
    ctx: ToolContext,
    task: Annotated[str, "Klarer Auftrag, z. B. 'Suche die Linux-News von heute und fasse sie zusammen'"],
    time: Annotated[str, "HH:MM"],
    name: Annotated[str, "Kurzer Name"] = "",
    days: Annotated[str, "Leer = täglich, 'werktags', 'Wochenende' oder 'Mo, Mi, Fr'"] = "",
    date: Annotated[str, "Nur einmalig: Datum oder 'nächsten Freitag'"] = "",
) -> str:
    try:
        r = store_for(ctx).add(name, task, time, days, date)
    except ValueError as e:
        return f"Routine nicht angelegt: {e}"
    now = datetime.now()
    return f"✔ Routine angelegt: „{r.name}“ – {r.schedule()}, nächste Ausführung {when_label(r.next_run(now), now)}."


@tool("Listet Routinen mit Zeitplan, nächster Ausführung und letztem Ergebnis.", risk=SAFE)
async def routine_list(ctx: ToolContext) -> str:
    items = store_for(ctx).items
    if not items:
        return "Es sind keine Routinen angelegt."
    now = datetime.now()
    return "\n".join(_line(r, now) for r in items)


@tool("Ändert eine Routine; leere Felder bleiben unverändert.",
      risk=_named_risk("ändern"))
async def routine_update(
    ctx: ToolContext,
    which: Annotated[str, "ID oder Name"],
    task: str = "",
    time: Annotated[str, "HH:MM"] = "",
    days: Annotated[str, "'täglich' = jeden Tag"] = "",
    name: str = "",
    enabled: Annotated[str, "'ja' = aktiv, 'nein' = pausiert"] = "",
) -> str:
    try:
        r = _find_one(ctx, which)
        on: Any = None
        if enabled.strip():
            on = enabled.strip().lower() in ("ja", "yes", "true", "1", "an", "on", "aktiv")
        r = store_for(ctx).update(r.id, task=task or None, time=time or None, days=days or None, name=name or None,
                                  enabled=on)
    except ValueError as e:
        return f"Nicht geändert: {e}"
    now = datetime.now()
    return f"✔ Routine „{r.name}“: {r.schedule()}{'' if r.enabled else ' (pausiert)'}, nächste: {when_label(r.next_run(now), now)}."


@tool("Löscht eine Routine.", risk=_named_risk("löschen"))
async def routine_delete(ctx: ToolContext, which: Annotated[str, "ID oder Name"]) -> str:
    try:
        r = _find_one(ctx, which)
    except ValueError as e:
        return str(e)
    store_for(ctx).delete(r.id)
    return f"✔ Routine „{r.name}“ gelöscht (ihr Chat bleibt im Verlauf)."


@tool("Startet eine Routine sofort (direkt nach dieser Antwort).", risk=SAFE)
async def routine_run_now(ctx: ToolContext, which: Annotated[str, "ID oder Name"]) -> str:
    try:
        r = _find_one(ctx, which)
    except ValueError as e:
        return str(e)
    start = ctx.services.get("start_routine")
    if not start:
        return "Routinen können nur laufen, während der Orbwise-Server läuft."
    if not start(r.id):
        return f"Die Routine „{r.name}“ läuft bereits."
    return f"✔ Routine „{r.name}“ startet gleich – das Ergebnis erscheint in ihrem Chat im VERLAUF."
