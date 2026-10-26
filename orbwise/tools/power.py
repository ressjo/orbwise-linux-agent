"""Herunterfahren, Neustart, Standby, Ruhezustand, Sperren – über systemd/logind, meist ganz ohne Root."""

from __future__ import annotations

import asyncio
from typing import Annotated

from ..lang import T
from . import proc
from .registry import CONFIRM, SAFE, ToolContext, tool

PASSIVE = {"poweroff": "heruntergefahren", "reboot": "neu gestartet"}
ACTIONS = {
    "poweroff": ("herunterfahren", ["systemctl", "poweroff"], "-h"),
    "reboot": ("neu starten", ["systemctl", "reboot"], "-r"),
    "suspend": ("in den Standby versetzen", ["systemctl", "suspend"], None),
    "hibernate": ("in den Ruhezustand versetzen", ["systemctl", "hibernate"], None),
    "lock": ("den Bildschirm sperren", ["loginctl", "lock-session"], None),
    "cancel": ("geplantes Herunterfahren abbrechen", ["shutdown", "-c"], None),
}
ALIASES = {"shutdown": "poweroff", "aus": "poweroff", "ausschalten": "poweroff", "restart": "reboot",
           "neustart": "reboot", "standby": "suspend", "sleep": "suspend", "ruhezustand": "hibernate",
           "sperren": "lock", "abbrechen": "cancel"}
ACTIONS_EN = {"poweroff": "shut down", "reboot": "restart", "suspend": "suspend", "hibernate": "hibernate"}
START_DELAY = 5  # Sekunden – damit Orbwise die Antwort noch aussprechen kann

# Für Tests austauschbar
_tasks: set[asyncio.Task] = set()


def _action(name: str) -> str:
    key = (name or "").strip().lower()
    return ALIASES.get(key, key)


def _risk(ctx: ToolContext, args: dict) -> tuple[str, str]:
    action = _action(args.get("action", ""))
    if action in ("lock", "cancel") or action not in ACTIONS:
        return SAFE, T("harmlos", "harmless")
    delay = int(args.get("delay_minutes") or 0)
    return CONFIRM, T(f"Rechner {ACTIONS[action][0]}" + (f" in {delay} Minuten" if delay > 0 else ""),
                      f"{ACTIONS_EN[action]} the computer" + (f" in {delay} minutes" if delay > 0 else ""))


async def _run_with_fallback(ctx: ToolContext, argv: list[str]) -> tuple[int | None, str]:
    """Erst ohne Root (logind erlaubt das der aktiven Sitzung), sonst mit Passwort über sudo."""
    rc, out = await proc.run(ctx, argv, timeout=30, stream=False)
    if rc != 0 and any(w in out.lower() for w in ("access denied", "interactive authentication", "not authorized",
                                                   "permission denied", "must be root", "failed to")):
        from .packages import privileged
        rc, out = await proc.run(ctx, privileged(ctx, argv), timeout=180, stream=False)
    return rc, out


@tool("Herunterfahren, Neustart, Standby/Ruhezustand, Sperren oder geplantes Herunterfahren abbrechen. IMMER "
      "dieses Tool statt run_shell/sudo.",
      risk=_risk)
async def power(
    ctx: ToolContext,
    action: Annotated[str, "poweroff | reboot | suspend | hibernate | lock | cancel"],
    delay_minutes: Annotated[int, "Erst in N Minuten (poweroff/reboot)"] = 0,
) -> str:
    act = _action(action)
    if act not in ACTIONS:
        return f"Unbekannte Aktion '{action}'. Möglich: {', '.join(ACTIONS)}."
    label, argv, shutdown_flag = ACTIONS[act]
    delay = max(0, int(delay_minutes or 0))
    if delay and shutdown_flag:
        rc, out = await _run_with_fallback(ctx, ["shutdown", shutdown_flag, f"+{delay}"])
        if rc != 0:
            return proc.format_result(rc, out, 1000)
        return f"Geplant: Der Rechner wird in {delay} Minuten {PASSIVE[act]}. Abbrechen mit power(action='cancel')."
    if act in ("poweroff", "reboot"):
        async def later() -> None:
            await asyncio.sleep(START_DELAY)
            await _run_with_fallback(ctx, argv)

        task = asyncio.create_task(later())
        _tasks.add(task)
        task.add_done_callback(_tasks.discard)
        return f"Wird ausgeführt: Der Rechner wird in {START_DELAY} Sekunden {PASSIVE[act]}."
    rc, out = await _run_with_fallback(ctx, argv)
    if rc != 0:
        return proc.format_result(rc, out, 1000)
    return f"Erledigt: {label}."
