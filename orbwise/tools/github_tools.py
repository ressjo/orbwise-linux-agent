"""GitHub im Coding-Modus: Repo klonen (wird Projekt des Chats) und den aktuellen Branch pushen.
Beides fragt vorher; Pushen auch im Auto-Modus „Auto“ (siehe agent_safety.ALWAYS_ASK)."""

from __future__ import annotations

import asyncio
from typing import Annotated

from .. import github
from .registry import CONFIRM, ToolContext, tool


def _connected(cfg) -> bool:
    return bool(github.token(cfg))


def _clone_risk(ctx: ToolContext, args: dict) -> tuple[str, str]:
    return CONFIRM, f"klont {args.get('repo', '')} nach {github.clone_dir(ctx.cfg)} und macht es zum Projekt"


@tool("Klont ein GitHub-Repo (owner/name) in den Projekte-Ordner und macht es zum Projekt dieses Chats. Gibt es den "
      "Ordner schon, wird er aktualisiert (fetch).", risk=_clone_risk, enabled=_connected)
async def github_clone(ctx: ToolContext, repo: Annotated[str, "Repo als owner/name"]) -> str:
    try:
        path = await asyncio.to_thread(github.clone, ctx.cfg, repo)
    except (github.GitHubError, OSError) as e:
        return f"Klonen fehlgeschlagen: {e}"
    if ctx.agent is not None:
        await ctx.agent.switch_project(path)
    return f"Geklont nach {path} – das ist jetzt der Projektordner (relative Pfade beziehen sich darauf)."


def _push_risk(ctx: ToolContext, args: dict) -> tuple[str, str]:
    path = ctx.cwd or ""
    repo = github.remote_repo(path) if path else None
    branch = args.get("branch") or "den aktuellen Branch"
    return CONFIRM, f"lädt {branch} nach github.com/{repo or '?'} hoch"


@tool("Lädt den aktuellen Branch des Projekts (in der Sandbox: der Arbeitskopie) zu GitHub hoch (git push -u origin). "
      "Vorher mit git committen – oder message angeben, dann werden alle Änderungen vorher committet. Liefert den "
      "Link, um einen Pull Request zu erstellen.", risk=_push_risk, enabled=_connected)
async def github_push(
    ctx: ToolContext,
    branch: Annotated[str, "Optional: Branch (Standard: der aktuelle)"] = "",
    message: Annotated[str, "Optional: Commit-Nachricht – committet vorher alle Änderungen"] = "",
) -> str:
    path = ctx.cwd
    if not path:
        return "Kein Projektordner gewählt."
    try:
        if message.strip():
            status = await asyncio.to_thread(github.git, ["status", "--porcelain"], path)
            if status.strip():
                await asyncio.to_thread(github.git, ["add", "-A"], path)
                await asyncio.to_thread(github.git, ["commit", "-q", "-m", message.strip()], path)
        out = await asyncio.to_thread(github.push, ctx.cfg, path, branch)
    except (github.GitHubError, OSError) as e:
        return f"Push fehlgeschlagen: {e}"
    if out.get("compare"):
        return f"Branch {out['branch']} ist auf GitHub: {out['url']}\nPull Request erstellen: {out['compare']}"
    return f"Branch {out['branch']} wurde gepusht."
