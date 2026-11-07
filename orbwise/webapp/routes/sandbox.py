"""Coding: Projektordner, Sandbox an/aus, Änderungen ansehen und übernehmen."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

from fastapi import HTTPException, Request

from ...lang import T


def register(app, ctx) -> None:
    # gemeinsame Objekte und Helfer anderer Teile (siehe webapp/context.py)
    agent = ctx.agent
    cfg = ctx.cfg
    chat_switched = ctx.chat_switched
    hub = ctx.hub
    memory = ctx.memory
    not_busy = ctx.not_busy

    # ---------------------------------------------------------------- Sandbox (Coding)
    def coding_chat() -> None:
        if memory.mode != "coding":
            raise HTTPException(409, T("Nur im Coding-Modus.", "Only in coding mode."))

    def sandbox_json() -> dict:
        from ... import sandbox as sbx
        from ... import workspace
        sb = agent.sandbox_state()
        out = {"enabled": bool(sb), **sbx.status(cfg, sb)}
        out["containers_blocked"] = sbx.containers_blocked(sbx.engine(cfg)[0])
        out["containers_allowed"] = not out["containers_blocked"]
        if sb:
            out.update(network=sb.get("network", True), path=sb["path"], project=sb.get("project", ""),
                       branch=workspace.head_label(sb["path"]) or sb.get("branch", ""),
                       changes=len(workspace.changes(sb)), containers=bool(sb.get("containers")),
                       containers_test=sb.get("containers_test"))
            from ... import github
            out["github"] = github.repo_of(github.sandbox_origin(sb))
            if sb.get("network", True) and cfg.host not in ("127.0.0.1", "localhost", "::1"):
                out["warning"] = T(f"Orbwise lauscht auf {cfg.host} – mit Netz erreicht der Container die Orbwise-API.",
                                   f"Orbwise listens on {cfg.host} – with network on, the container can reach the "
                                   "Orbwise API.")
        return out

    async def sandbox_progress(text: str) -> None:
        await hub.broadcast({"type": "sandbox_progress", "text": text})

    @app.get("/api/fs/dirs")
    async def fs_dirs(path: str = ""):
        """Ordner-Auswahl der Oberfläche (Projektordner): nur Ordnernamen eines Verzeichnisses, keine Dateien."""
        raw = os.path.expanduser(path.strip() or "~")
        p = Path(raw)
        if not p.is_absolute():
            p = Path.home() / p
        if not p.is_dir():  # halb getippt: den nächsten vorhandenen Ordner zeigen
            p = p.parent if p.parent.is_dir() else Path.home()
        try:
            names = sorted((e.name for e in p.iterdir() if e.is_dir()), key=lambda n: (n.startswith("."), n.lower()))
            error = ""
        except OSError as e:
            names, error = [], str(e)
        names = [n for n in names if not n.startswith(".")][:300]
        return {"path": str(p), "parent": str(p.parent) if p.parent != p else "", "dirs": names, "error": error}

    @app.get("/api/project")
    async def project_info():
        """Projekt des offenen Coding-Chats für den Chip oben: Pfad, GitHub-Repo und Branch (bei aktiver Sandbox die der
        Arbeitskopie)."""
        from ... import github
        path = agent.project_dir() if memory.mode == "coding" else None
        if not path:
            return {"path": "", "repo": None, "branch": None, "sandbox": False, "has_commits": False}

        sb = agent.sandbox_state()

        def read() -> tuple:
            if sb:  # Arbeitskopie: kein git in ihrem .git (siehe github.push_from_copy)
                from ... import workspace
                return github.repo_of(github.sandbox_origin(sb)), workspace.head_label(sb["path"])
            repo = github.remote_repo(path)
            try:
                branch = github.current_branch(path)
                if branch == "HEAD":  # kein Branch ausgecheckt: kurzer Commit-Hash
                    branch = github.git(["rev-parse", "--short", "HEAD"], cwd=path).strip()
            except (github.GitHubError, OSError):
                branch = None
            return repo, branch
        repo, branch = await asyncio.to_thread(read)
        real = agent.real_project() or path
        commits = await asyncio.to_thread(lambda: github.is_repo(real) and github.has_commits(real))
        return {"path": path, "repo": repo, "branch": branch or None, "sandbox": bool(sb), "has_commits": commits}

    @app.get("/api/sandbox")
    async def sandbox_get():
        coding_chat()
        return await asyncio.to_thread(sandbox_json)

    @app.post("/api/sandbox")
    async def sandbox_set(request: Request):
        """{enabled}: ein-/ausschalten (aus = Container und Arbeitskopie weg); {network}: Netz an/aus;
        {containers}: Docker im Container an/aus (nur rootless Podman, mit Selbsttest)."""
        from ... import sandbox as sbx
        from ... import workspace
        coding_chat()
        not_busy()
        data = await request.json()
        try:
            if "enabled" in data:
                if data["enabled"]:
                    await agent.sandbox_enable(sandbox_progress)
                else:
                    await agent.sandbox_disable()
            if "network" in data:
                await agent.sandbox_network(bool(data["network"]))
            if "containers" in data:
                await agent.sandbox_containers(bool(data["containers"]), sandbox_progress)
        except (sbx.SandboxError, workspace.WorkspaceError, OSError) as e:
            raise HTTPException(400, str(e)) from e
        await chat_switched()
        return await asyncio.to_thread(sandbox_json)

    @app.get("/api/sandbox/changes")
    async def sandbox_changes(path: str = ""):
        from ... import workspace
        coding_chat()
        sb = agent.sandbox_state()
        if not sb:
            raise HTTPException(409, T("Keine Sandbox aktiv.", "No sandbox active."))
        if path:
            return {"path": path, "diff": await asyncio.to_thread(workspace.diff, sb, path)}
        return {"files": await asyncio.to_thread(workspace.changes, sb)}

    @app.post("/api/sandbox/apply")
    async def sandbox_apply(request: Request):
        """„In Projekt übernehmen“: geänderte Dateien der Arbeitskopie ins Projekt kopieren (Konflikte melden)."""
        from ... import workspace
        coding_chat()
        not_busy()
        sb = agent.sandbox_state()
        if not sb:
            raise HTTPException(409, T("Keine Sandbox aktiv.", "No sandbox active."))
        data = await request.json()
        out = await asyncio.to_thread(workspace.apply, sb, bool(data.get("force")))
        memory.active.save()
        if out["applied"]:
            memory.active.meta.setdefault("pending_notes", []).append(T(
                f"(Hinweis: Der Nutzer hat die Änderungen der Sandbox ins Projekt übernommen: {', '.join(out['applied'])}.)",
                f"(Note: the user applied the sandbox changes to the project: {', '.join(out['applied'])}.)"))
            memory.active.save()
        return out

    # für andere Teile der App
    ctx.coding_chat = coding_chat
