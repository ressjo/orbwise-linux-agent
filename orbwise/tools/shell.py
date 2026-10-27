from __future__ import annotations

import os
from typing import Annotated, Any
from urllib.parse import urlparse

from ..lang import T
from . import proc
from .registry import BLOCKED, ToolContext, tool
from .safety import apply_privilege, classify_command

# Eingerichtete Dienste haben eigene Werkzeuge – per curl/wget an ihnen vorbei geht nur schief (erfundene API,
# fehlender Token) und kann Zugangsdaten auslesen
SERVICE_TOOLS = (("paperless", "Paperless", "paperless_search, paperless_ask, paperless_read, paperless_review_next"),
                 ("trilium", "Trilium", "trilium_search, trilium_read, trilium_create_note"),
                 ("homeassistant", "Home Assistant", "ha_find, ha_state, ha_control"),
                 ("calendar", "den Kalender", "calendar_events, calendar_free, calendar_add"),
                 ("portainer", "Portainer", "portainer_containers, portainer_update, portainer_container_action"))
_LOCAL = {"localhost", "127.0.0.1", "0.0.0.0", "::1", "[::1]"}


def service_targets(cfg: Any) -> list[tuple[str, list[str], str]]:
    """(Name, Erkennungsmuster, Werkzeuge) je eingerichtetem Dienst – Muster wie „nas:8000“ bzw. „//paperless.example“;
    läuft ein Dienst lokal, zählen alle lokalen Adressen mit diesem Port."""
    out = []
    for key, label, tools in SERVICE_TOOLS:
        url = str(getattr(getattr(cfg, key, None), "url", "") or "").strip()
        if not url:
            continue
        parsed = urlparse(url if "://" in url else "http://" + url)
        host = (parsed.hostname or "").lower()
        if not host:
            continue
        try:
            port = parsed.port
        except ValueError:
            port = None
        if port:
            hosts = (_LOCAL | {host}) if host in _LOCAL else {host}
            patterns = [f"{h}:{port}" for h in sorted(hosts)]
        else:
            patterns = [f"//{host}"]
        out.append((label, patterns, tools))
    return out


def _risk(ctx: ToolContext, args: dict) -> tuple[str, str]:
    command = str(args.get("command", ""))
    low = command.lower()
    for label, patterns, tools in service_targets(ctx.cfg):
        if any(p in low for p in patterns):
            return BLOCKED, T(f"für {label} gibt es eigene Werkzeuge ({tools} …) – nutze diese statt run_shell/curl",
                              f"{label} has its own tools ({tools} …) – use them instead of run_shell/curl")
    return classify_command(command, ctx.cwd)


@tool(
    "Führt einen Bash-Befehl aus und liefert die Ausgabe. Root: 'sudo' voranstellen. Keine interaktiven Programme "
    "(vim, htop, less).",
    risk=_risk,
)
async def run_shell(
    ctx: ToolContext,
    command: str,
    timeout_seconds: Annotated[int, "Standard 120"] = 0,
) -> str:
    timeout = timeout_seconds if timeout_seconds > 0 else ctx.cfg.tools.shell_timeout
    timeout = min(timeout, ctx.cfg.tools.update_timeout)
    if ctx.jail:  # Auto ohne Rückfrage: im Käfig (siehe jail.py)
        from .. import jail
        rc, out = await jail.run(ctx, command, ctx.cwd or os.path.expanduser("~"), timeout, "run_shell")
        return proc.format_result(rc, out, ctx.limit())
    command = apply_privilege(command, ctx.cfg.tools.privilege_cmd)
    rc, out = await proc.run(ctx, command, timeout=timeout, cwd=ctx.cwd)
    return proc.format_result(rc, out, ctx.limit())
