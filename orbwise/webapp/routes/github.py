"""GitHub: Anmeldung, Repos, Branches, klonen, pushen, leeres Repo verbinden."""

from __future__ import annotations

import asyncio
import logging

from fastapi import HTTPException, Request

from ...lang import T

log = logging.getLogger(__name__)


def register(app, ctx) -> None:
    # gemeinsame Objekte und Helfer anderer Teile (siehe webapp/context.py)
    agent = ctx.agent
    cfg = ctx.cfg
    chat_switched = ctx.chat_switched
    coding_chat = ctx.coding_chat
    hub = ctx.hub
    memory = ctx.memory
    not_busy = ctx.not_busy

    # ---------------------------------------------------------------- GitHub
    @app.get("/api/github/status")
    async def github_status():
        from ... import github
        tok, source = await asyncio.to_thread(github.token_info, cfg)
        if not tok:
            return {"connected": False, "source": "", "clone_dir": str(github.clone_dir(cfg))}
        try:
            user = await github.user(cfg)
        except Exception as e:  # noqa: BLE001
            return {"connected": False, "source": source, "error": str(e)}
        return {"connected": True, "source": source, "login": user["login"], "clone_dir": str(github.clone_dir(cfg))}

    @app.get("/api/github/repos")
    async def github_repos(q: str = ""):
        from ... import github
        try:
            return {"repos": await github.repos(cfg, q)}
        except github.GitHubError as e:
            raise HTTPException(400, str(e)) from e

    @app.get("/api/github/branches")
    async def github_branches(repo: str = ""):
        from ... import github
        try:
            return {"branches": await github.branches(cfg, repo)}
        except github.GitHubError as e:
            raise HTTPException(400, str(e)) from e

    @app.post("/api/github/clone")
    async def github_clone(request: Request):
        """Repo klonen und zum Projekt des offenen Coding-Chats machen."""
        from ... import github
        coding_chat()
        not_busy()
        data = await request.json()
        repo, branch = str(data.get("repo", "")).strip(), str(data.get("branch") or "").strip()
        try:
            path = await asyncio.to_thread(github.clone, cfg, repo, None, branch)
            await agent.switch_project(path)
        except (github.GitHubError, OSError) as e:
            raise HTTPException(400, str(e)) from e
        await chat_switched()
        await hub.broadcast({"type": "chats_changed"})
        return {"project": path, "branch": branch}

    @app.post("/api/github/push")
    async def github_push():
        """Knopf „Zu GitHub pushen“ (der Klick ist die Freigabe): aktuellen Branch der Arbeitskopie bzw. des Projekts."""
        from ... import github
        coding_chat()
        not_busy()
        path = agent.project_dir()
        if not path:
            raise HTTPException(409, T("Kein Projektordner gewählt.", "No project folder chosen."))
        sb = agent.sandbox_state()
        from ... import sandbox as sbx
        try:
            if sb:  # offene Änderungen der Arbeitskopie zuerst committen – sonst käme nur ein leerer Branch an
                await sbx.commit_all(cfg, sb, str(memory.active.meta.get("title") or ""))
                return await asyncio.to_thread(github.push_from_copy, cfg, sb["path"], github.sandbox_origin(sb), "",
                                               sb.get("project") or "")
            return await asyncio.to_thread(github.push, cfg, path)
        except (github.GitHubError, sbx.SandboxError, OSError) as e:
            raise HTTPException(400, str(e)) from e

    @app.post("/api/github/connect")
    async def github_connect(request: Request):
        """„Zu GitHub pushen“ ohne origin: Projekt mit einem (leeren) Repo verbinden – notfalls git init und erster
        Commit – und hochladen. Der Klick im Dialog ist die Freigabe."""
        from ... import github
        coding_chat()
        not_busy()
        project = agent.real_project()
        if not project:
            raise HTTPException(409, T("Kein Projektordner gewählt.", "No project folder chosen."))
        data = await request.json()
        sb = agent.sandbox_state()
        identity = None
        try:
            identity = await github.user(cfg)
        except Exception as e:  # noqa: BLE001 – dann die git-Identität des Rechners
            log.info("GitHub-Konto nicht abrufbar: %s", e)
        from ... import sandbox as sbx
        try:
            from ... import workspace
            if sb and workspace._git_dir(sb["path"]) is not None:
                await sbx.commit_all(cfg, sb, str(memory.active.meta.get("title") or data.get("message") or ""))
            out = await asyncio.to_thread(github.connect_and_push, cfg, project, str(data.get("repo", "")), sb,
                                          str(data.get("message") or ""), identity)
        except (github.GitHubError, sbx.SandboxError, OSError) as e:
            raise HTTPException(400, str(e)) from e
        finally:
            if sb:
                memory.conversation.save()  # gemerktes origin der Arbeitskopie
        return out
