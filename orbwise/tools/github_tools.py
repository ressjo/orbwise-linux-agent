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
    branch = f" (Branch {args['branch']})" if args.get("branch") else ""
    return CONFIRM, f"klont {args.get('repo', '')}{branch} nach {github.clone_dir(ctx.cfg)} und macht es zum Projekt"


@tool("Klont ein GitHub-Repo (owner/name) in den Projekte-Ordner und macht es zum Projekt dieses Chats. Gibt es den "
      "Ordner schon, wird er aktualisiert (fetch).", risk=_clone_risk, enabled=_connected)
async def github_clone(ctx: ToolContext, repo: Annotated[str, "Repo als owner/name"],
                       branch: Annotated[str, "Optional: Branch (Standard: der des Repos)"] = "") -> str:
    try:
        path = await asyncio.to_thread(github.clone, ctx.cfg, repo, None, branch)
    except (github.GitHubError, OSError) as e:
        return f"Klonen fehlgeschlagen: {e}"
    if ctx.agent is not None:
        await ctx.agent.switch_project(path)
    return f"Geklont nach {path} – das ist jetzt der Projektordner (relative Pfade beziehen sich darauf)."


def _push_risk(ctx: ToolContext, args: dict) -> tuple[str, str]:
    path = ctx.cwd or ""
    if ctx.sandbox:  # nie git im .git der Arbeitskopie aufrufen (siehe github.push_from_copy)
        repo = github.repo_of(github.sandbox_origin(ctx.sandbox))
    else:
        repo = github.remote_repo(path) if path else None
    if not repo and args.get("repo"):
        return CONFIRM, f"verbindet das Projekt mit github.com/{args['repo']} (origin) und lädt es hoch"
    branch = args.get("branch") or "den aktuellen Branch"
    return CONFIRM, f"lädt {branch} nach github.com/{repo or '?'} hoch"


@tool("Lädt den aktuellen Branch des Projekts (in der Sandbox: der Arbeitskopie) zu GitHub hoch (git push -u origin). "
      "Vorher mit git committen – oder message angeben, dann werden alle Änderungen vorher committet (in der Sandbox "
      "werden offene Änderungen immer vorher committet). Liefert den "
      "Link, um einen Pull Request zu erstellen. Hat das Projekt noch kein GitHub-Repo (kein origin), repo angeben: "
      "dann wird es damit verbunden (notfalls git init und erster Commit) und hochgeladen.",
      risk=_push_risk, enabled=_connected)
async def github_push(
    ctx: ToolContext,
    branch: Annotated[str, "Optional: Branch (Standard: der aktuelle)"] = "",
    message: Annotated[str, "Optional: Commit-Nachricht – committet vorher alle Änderungen"] = "",
    repo: Annotated[str, "Optional: owner/name eines (leeren) Repos, falls das Projekt noch keins hat"] = "",
) -> str:
    path = ctx.cwd
    if not path:
        return "Kein Projektordner gewählt."
    if repo.strip() and not (github.sandbox_origin(ctx.sandbox) if ctx.sandbox else github.remote_repo(path)):
        return await _connect(ctx, repo, message)
    if ctx.sandbox:
        return await _push_sandbox(ctx, ctx.sandbox, branch, message)
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


async def _push_sandbox(ctx: ToolContext, sb: dict, branch: str, message: str) -> str:
    """Sandbox: offene Änderungen im Container committen, pushen auf dem Host aus einem frischen Repo (Hooks der Kopie
    laufen nie)."""
    from .. import sandbox
    try:
        await sandbox.commit_all(ctx.cfg, sb, message)
        res = await asyncio.to_thread(github.push_from_copy, ctx.cfg, sb["path"], github.sandbox_origin(sb), branch,
                                      sb.get("project") or "")
    except (github.GitHubError, sandbox.SandboxError, OSError) as e:
        return f"Push fehlgeschlagen: {e}"
    if res.get("compare"):
        return f"Branch {res['branch']} ist auf GitHub: {res['url']}\nPull Request erstellen: {res['compare']}"
    return f"Branch {res['branch']} wurde gepusht."


async def _connect(ctx: ToolContext, repo: str, message: str) -> str:
    """Projekt ohne origin mit einem Repo verbinden und hochladen (siehe github.connect_and_push)."""
    agent = ctx.agent
    project = (agent.real_project() if agent is not None else None) or (ctx.sandbox or {}).get("project") or ctx.cwd
    identity = None
    try:
        identity = await github.user(ctx.cfg)
    except Exception:  # noqa: BLE001 – dann die git-Identität des Rechners
        pass
    try:
        if ctx.sandbox:
            from .. import sandbox
            await sandbox.commit_all(ctx.cfg, ctx.sandbox, message)
        out = await asyncio.to_thread(github.connect_and_push, ctx.cfg, project, repo, ctx.sandbox, message, identity)
    except Exception as e:  # noqa: BLE001 – GitHubError, SandboxError, OSError
        return f"Verbinden/Push fehlgeschlagen: {e}"
    finally:
        if ctx.sandbox and agent is not None:
            agent.memory.conversation.save()
    text = f"Projekt ist mit github.com/{out.get('repo') or repo} verbunden, Branch {out['branch']} ist hochgeladen"
    if out.get("files"):
        text += f" (erster Commit mit {out['files']} Dateien)"
    if out.get("skipped"):
        text += ". Nicht committet, weil Zugangsdaten: " + ", ".join(out["skipped"])
    return text + (f"\n{out['url']}" if out.get("url") else "")

