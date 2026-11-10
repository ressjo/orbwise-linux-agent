"""Coding-Modus nach dem Vorbild von OpenCode 2: Umgebung und AGENTS.md im Systemprompt, Schnappschüsse je Antwort
(Rückgängig), Notizen fertiger Hintergrund-Jobs und Unteragenten (explore/general) mit eigenem Kontext.

Mixin für Agent – nutzt dessen Zustand (self.cfg, self.memory, self._execute, self._call_llm …)."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import uuid
from pathlib import Path

from . import prompts, snapshot, workspace
from . import sandbox as sbx
from .llm import ContextOverflow, LLMError, strip_think
from .memory import est_tokens
from .tools import jobs
from .tools.proc import clip_saved
from .tools.registry import SAFE, coerce_args, get_tool

log = logging.getLogger(__name__)

EXPLORE_TOOLS = {"read", "grep", "glob", "map", "shell", "job_output", "websearch", "webfetch", "recall"}
GENERAL_EXCLUDED = {"subagent", "question"}
SNAPSHOTS_KEEP = 60
SUB_STUB = 300  # so viele Zeichen bleiben von einem älteren Werkzeug-Ergebnis des Unteragenten, wenn Platz fehlt


def _sub_tokens(messages: list[dict]) -> int:
    return est_tokens(json.dumps(messages, ensure_ascii=False))


def _sub_fit(messages: list[dict], room: int) -> None:
    """Passt der Prompt des Unteragenten nicht in room (geschätzte Token), ältere Werkzeug-Ergebnisse auf ihren Anfang
    kürzen – das älteste zuerst, das letzte nur im Notfall. Auftrag und Antworten des Modells bleiben."""
    tools = [m for m in messages if m["role"] == "tool"]
    for m in tools:
        if _sub_tokens(messages) <= room:
            return
        text = m["content"]
        if len(text) > SUB_STUB * 2:
            m["content"] = (text[:SUB_STUB] + f"\n[… gekürzt, um Platz zu schaffen ({len(text)} Zeichen) – bei Bedarf "
                                              "erneut abrufen]")


class CodingMixin:
    # ---------- Prompt ----------
    def coding_env(self) -> str:
        project = self.project_dir()
        git = bool(project) and any((Path(project) / ".git").exists() or (p / ".git").exists()
                                    for p in Path(project).resolve().parents)
        shell = os.path.basename(os.environ.get("SHELL") or "/bin/bash")
        text = prompts.coding_env(self.cfg, git, shell)
        sb = self.sandbox_state()
        if sb:
            text += " " + prompts.sandbox_note(self.cfg, engine=sb.get("engine") or "podman", image=sb.get("image", ""),
                                               network=sb.get("network", True), project=sb.get("project", ""),
                                               branch=sb.get("branch") or "-", containers=bool(sb.get("containers")))
        elif project and self.tool_allowed("github_push") and self.tools["github_push"].is_enabled(self.cfg):
            text += " " + prompts.github_note(self.cfg)
        return text

    # ---------- Sandbox (Container + Arbeitskopie, siehe sandbox.py / workspace.py) ----------
    def real_project(self) -> str | None:
        """Der vom Nutzer gewählte Projektordner (nicht die Arbeitskopie)."""
        if self.mode != "coding":
            return None
        raw = self.memory.conversation.meta.get("project") or ""
        path = Path(os.path.expanduser(raw)) if raw else None
        return str(path) if path and path.is_dir() else None

    def sandbox_state(self) -> dict | None:
        if self.mode != "coding":
            return None
        sb = self.memory.conversation.meta.get("sandbox")
        if not sb or not sb.get("enabled") or not Path(sb.get("path", "")).is_dir():
            return None
        project = self.memory.conversation.meta.get("project") or ""
        if project and Path(os.path.expanduser(project)).resolve() != Path(sb.get("project", "")).resolve():
            return None  # Projekt gewechselt – diese Arbeitskopie gilt nicht mehr
        return sb

    async def sandbox_enable(self, progress=None) -> dict:
        """Sandbox für den offenen Coding-Chat einschalten: Arbeitskopie anlegen (der Container startet beim ersten
        Befehl). Liefert den Zustand; Fehler als SandboxError/WorkspaceError."""
        conv = self.memory.conversation
        project = self.real_project()
        if not project:
            raise workspace.WorkspaceError("Erst einen Projektordner wählen.")
        binary, error = await asyncio.to_thread(sbx.engine, self.cfg)
        if not binary:
            raise sbx.SandboxError(error)
        identity = None
        from . import github
        if await asyncio.to_thread(github.token, self.cfg):
            try:
                identity = await github.user(self.cfg)
            except Exception as e:  # noqa: BLE001 – ohne GitHub-Identität nimmt die Kopie die des Projekts
                log.info("GitHub-Konto nicht abrufbar: %s", e)
        if progress:
            await progress("Lege die Arbeitskopie an …")
        old = conv.meta.get("sandbox") or {}
        state = await asyncio.to_thread(workspace.create, conv.chat_id, project, conv.meta.get("title", ""), identity,
                                        str(conv.meta.get("sandbox_branch") or ""))
        if state.get("branch"):
            conv.meta["sandbox_branch"] = state["branch"]  # bleibt nach „Sandbox beenden“ – ein Branch je Chat
        sb = {**state, "enabled": True, "network": old.get("network", self.cfg.sandbox.network),
              "name": sbx.container_name(conv.chat_id), "image": sbx.image(self.cfg),
              "engine": os.path.basename(binary)}
        conv.meta["sandbox"] = sb
        conv.meta.pop("last_sandbox", None)
        conv.meta.setdefault("pending_notes", []).append(prompts.text(self.cfg, "sandbox_on_note").format(
            path=sb["path"], project=sb.get("project", project)))
        conv.save()
        return sb

    async def sandbox_disable(self, discard: bool = True) -> None:
        """Ausschalten: Container weg, Arbeitskopie weg (discard) – nicht übernommene Änderungen gehen verloren."""
        conv = self.memory.conversation
        sb = conv.meta.pop("sandbox", None)
        if sb:
            conv.meta["last_sandbox"] = {"path": sb.get("path", ""), "project": sb.get("project", "")}
            conv.meta.setdefault("pending_notes", []).append(prompts.text(self.cfg, "sandbox_off_note").format(
                project=sb.get("project", "")))
        conv.save()
        if not sb:
            return
        await asyncio.to_thread(sbx.remove, self.cfg, sb.get("name") or sbx.container_name(conv.chat_id))
        if discard:
            await asyncio.to_thread(workspace.discard, sb)

    async def switch_project(self, path: str) -> None:
        """Projektordner des offenen Coding-Chats wechseln (z. B. nach github_clone). Eine Sandbox gehört zum alten
        Projekt – sie wird für das neue frisch angelegt."""
        had = bool(self.memory.conversation.meta.get("sandbox"))
        if had:
            await self.sandbox_disable()
        self.memory.set_project(path)
        self.memory.conversation.epoch["instructions"] = None  # AGENTS.md des neuen Projekts
        if had and path:
            await self.sandbox_enable()

    async def sandbox_network(self, on: bool) -> dict | None:
        """Netz an/aus: der Container wird mit der neuen Einstellung neu angelegt (die Arbeitskopie bleibt)."""
        sb = self.sandbox_state()
        if not sb or sb.get("network", True) == on:
            return sb
        sb["network"] = on
        self.memory.conversation.save()
        await asyncio.to_thread(sbx.remove, self.cfg, sb["name"])
        return sb

    async def sandbox_containers(self, on: bool, progress=None) -> dict | None:
        """„Docker im Container“ an/aus (nur rootless Podman): Container mit den nötigen Rechten neu anlegen, danach
        ein kurzer Selbsttest. Verhindert das Standard-seccomp-Profil verschachtelte Container, einmal mit dem eigenen."""
        sb = self.sandbox_state()
        if not sb:
            return None
        blocked = sbx.containers_blocked((await asyncio.to_thread(sbx.engine, self.cfg))[0]) if on else ""
        if blocked:
            raise sbx.SandboxError(f"Docker im Container: {blocked} Die nötigen Rechte wären sonst zu weitreichend.")
        conv = self.memory.conversation
        if on or sb.get("containers"):  # an: immer frisch anlegen und testen
            sb["containers"] = on
            sb.pop("containers_seccomp", None)
            sb.pop("containers_test", None)
            if str(sb.get("image", "")).startswith("orbwise-sandbox:"):
                sb["image"] = sbx.image(self.cfg)  # ältere Sandboxen: neues Image mit Podman
            conv.save()
            await asyncio.to_thread(sbx.remove, self.cfg, sb["name"])
        if on and not sb.get("network", True):
            sb["containers_test"] = {"ok": None, "message": "Netz ist aus – Images lassen sich nicht laden. Netz "
                                                            "einschalten, dann klappt docker pull/build."}
        elif on:
            if progress:
                await progress("Teste Docker im Container …")
            res = await sbx.containers_test(self.cfg, sb, progress)
            if not res["ok"] and sbx.needs_seccomp(res["message"]):
                sb["containers_seccomp"] = True
                conv.save()
                await asyncio.to_thread(sbx.remove, self.cfg, sb["name"])
                res = await sbx.containers_test(self.cfg, sb, progress)
            sb["containers_test"] = res
        conv.save()
        return sb

    def project_instructions(self) -> str:
        """AGENTS.md (bzw. die erste vorhandene Datei aus coding.instructions) des Projekts – je Epoche einmal gelesen,
        damit der Systemprompt bis zur nächsten Komprimierung gleich bleibt. Höchstens ~4 % des Fensters."""
        ep = self.memory.conversation.epoch
        if ep.get("instructions") is not None:
            return ep["instructions"]
        text = ""
        project = self.project_dir()
        if project:
            for name in self.cfg.coding.instructions:
                f = Path(project) / name
                if f.is_file():
                    try:
                        body = f.read_text(encoding="utf-8", errors="replace").strip()
                    except OSError:
                        continue
                    room = max(1500, int(self.context_budget() * 0.04 * 3))
                    if len(body) > room:
                        body = body[:room] + "\n…"
                    text = prompts.project_instructions(self.cfg, name, body) if body else ""
                    break
        ep["instructions"] = text
        return text

    # ---------- Notizen für das Modell ----------
    def coding_notes(self) -> str:
        """Fertige Hintergrund-Jobs und zurückgesetzte Antworten – das Modell erfährt davon beim nächsten Schritt."""
        if self.mode != "coding":
            return ""
        parts = []
        pending = self.memory.conversation.meta.pop("pending_notes", None)
        if pending:
            parts += list(pending)
        done = jobs.take_notes()
        if done:
            parts.append(done)
        return "\n\n".join(parts)

    # ---------- Schnappschüsse (Rückgängig je Antwort) ----------
    def _snap_project(self) -> str | None:
        if self.mode != "coding" or self._plan or not self.cfg.coding.snapshots:
            return None
        return self.project_dir()

    async def snapshot_begin(self) -> None:
        project = self._snap_project()
        self._snap = (project, await asyncio.to_thread(snapshot.capture, project)) if project else None

    async def snapshot_end(self, emit, msg_id: str) -> None:
        snap, self._snap = getattr(self, "_snap", None), None
        if not snap or not snap[1]:
            return
        project, before = snap
        after = await asyncio.to_thread(snapshot.capture, project)
        files = await asyncio.to_thread(snapshot.changed, project, before, after) if after else []
        if not files:
            return
        conv = self.memory.conversation
        turn = sum(1 for m in conv.history if m["role"] == "user" and not m.get("goal_nudge")
                   and not m.get("verify_nudge"))
        snaps = conv.meta.setdefault("snapshots", {})
        snaps[str(turn)] = {"project": project, "before": before, "after": after, "files": files, "msg": msg_id}
        for key in list(snaps)[:-SNAPSHOTS_KEEP]:
            snaps.pop(key, None)
        await emit({"type": "changes", "id": msg_id, "turn": turn, "files": files})

    # ---------- Prüf-Schranke (siehe verify.py) ----------
    async def verify_gate(self, emit, confirm, state: dict, final: bool = False) -> str | None:
        """Vor „fertig“: hat diese Antwort Dateien geändert, die Prüfungen des Projekts ausführen.
        → Hinweis für das Modell (rot, Runden übrig) oder None (fertig). Das Ergebnis für unter die Antwort steht dann
        in state["status"]. state lebt eine Antwort lang: rounds, after, results, off. final: die Antwort endet hier
        ohnehin (Schritt-Limit, Schleife) – nur prüfen und melden, nichts zurückgeben."""
        from . import verify
        if self.mode != "coding" or self._plan or self.cfg.coding.verify == "off" or state.get("off"):
            return None
        project = self.project_dir()
        if not project:
            return None
        snap = getattr(self, "_snap", None)
        after = await asyncio.to_thread(snapshot.capture, project) if snap and snap[1] else None
        if snap and snap[1] and after:
            files = [f["path"] for f in await asyncio.to_thread(snapshot.changed, project, snap[1], after)
                     if f.get("status") != "deleted"]
            if not files and not state.get("results"):
                return None  # nichts geändert – nichts zu prüfen
            if after == state.get("after") and state.get("results"):  # seit der letzten Prüfung nichts geändert
                state["status"] = verify.summary(state["results"], self.cfg.language == "en")
                return None
        elif state.get("edited"):  # ohne Schnappschüsse: edit/write in dieser Antwort benutzt
            files = list(state.get("paths") or [])
        else:
            return None
        checks = verify.checks_for(self.cfg, project, files)
        if not checks:
            return None
        timeout = max(30, int(self.cfg.coding.verify_timeout))
        call = {"function": {"name": "shell", "arguments": {
            "command": verify.script(checks, timeout), "timeout": timeout * len(checks) + 30}}}
        await emit({"type": "segment_end", "id": state.get("msg_id", "")})
        _name, result, note = await self._execute(call, emit, confirm)
        if note.endswith("abgelehnt") or result.startswith("BLOCKIERT") or result.startswith("Unbekanntes Tool"):
            state["off"] = True  # vom Nutzer abgelehnt bzw. nicht möglich – in dieser Antwort nicht erneut fragen
            return None
        results = verify.parse(result)
        if not results:
            return None
        state["results"], state["after"] = results, after
        en = self.cfg.language == "en"
        if not final and any(r["status"] == "fail" for r in results) \
                and state.get("rounds", 0) < self.cfg.coding.verify_rounds:
            state["rounds"] = state.get("rounds", 0) + 1
            return verify.nudge(results, en)
        state["status"] = verify.summary(results, en)
        return None

    # ---------- Unteragenten ----------
    async def run_subagent(self, ctx, kind: str, description: str, task: str) -> str:
        """Eigene Nachrichtenliste, eigener Systemprompt, eingeschränkte Werkzeuge; nur die Antwort geht zurück.
        Läuft innerhalb des Werkzeug-Aufrufs (die Sperre des Agenten hält schon der Hauptlauf)."""
        parent, emit, confirm = ctx.call_id, ctx.emit, ctx.confirm
        allowed = EXPLORE_TOOLS if kind == "explore" else \
            {s["function"]["name"] for s in self._mode_schemas()[0]} - GENERAL_EXCLUDED
        schemas = [s for s in self._mode_schemas()[0] if s["function"]["name"] in allowed]
        system = prompts.subagent_prompt(self.cfg, kind, project=self.project_dir() or "~", env=self.coding_env())
        extra = self.project_instructions()
        messages = [{"role": "system", "content": system + ("\n\n" + extra if extra else "")},
                    {"role": "user", "content": task}]

        async def sub_emit(ev: dict) -> None:
            if not emit:
                return
            if ev.get("type") in ("tool_call", "tool_result", "tool_output", "job"):
                await emit({**ev, "sub": parent})
            # Zustände und Tokens des Unteragenten nicht als Antwort des Hauptagenten zeigen

        steps = self.cfg.coding.subagent_steps
        # Platz: das Budget des Fensters abzüglich der Werkzeugbeschreibungen, die mit jedem Schritt mitgehen
        room = max(1500, self.context_budget() - est_tokens(json.dumps(schemas, ensure_ascii=False)))
        prev, self._prompt_prev = self._prompt_prev, None  # eigener Prompt – nicht als „neu eingelesen“ verbuchen
        from .agent import changed_code
        answer, notes = "", []
        seen: dict[str, int] = {}
        final = squeezed = False
        try:
            for step in range(steps + 1):
                final = final or step == steps or _sub_tokens(messages) > 0.75 * room
                if final:
                    messages.append({"role": "user", "content": prompts.subagent_text(self.cfg, "final")})
                _sub_fit(messages, room)  # mehrere große Ergebnisse in einem Schritt: ältere kürzen statt überlaufen
                try:
                    message = await self._sub_step(messages, None if final else schemas)
                except ContextOverflow:
                    if squeezed:
                        raise
                    # der Server zählt mehr als geschätzt: auf die Hälfte des jetzigen Prompts kürzen und ohne
                    # Werkzeuge zusammenfassen lassen
                    squeezed = True
                    _sub_fit(messages, min(room, _sub_tokens(messages)) // 2)
                    if not final:
                        final = True
                        messages.append({"role": "user", "content": prompts.subagent_text(self.cfg, "final")})
                    message = await self._sub_step(messages, None)
                content = strip_think(message.get("content") or "")
                calls = [] if final else message.get("tool_calls") or []
                entry = {"role": "assistant", "content": content}
                if calls:
                    entry["tool_calls"] = calls
                messages.append(entry)
                if not calls:
                    answer = content
                    break
                if content.strip():
                    notes.append(content.strip())
                done, looping = self._repeats(seen, calls)
                for i, call in enumerate(calls):
                    name, result = done[i][:2] if i in done else \
                        await self._sub_execute(call, kind, allowed, sub_emit, confirm)
                    if changed_code(name, result):
                        seen.clear()
                    messages.append({"role": "tool", "content": result, "tool_name": name})
                final = final or looping  # dreht sich im Kreis: nächster Schritt ohne Werkzeuge
        except LLMError as e:
            # nicht alles verlieren: was der Unteragent unterwegs festgehalten hat, geht mit zurück
            answer = "\n".join(notes[-3:]) + f"\n(Unteragent abgebrochen: {e})"
        finally:
            self._prompt_prev = prev
            self._cache_owner = None  # der Server hat jetzt den Prompt des Unteragenten im Cache
        answer = answer.strip() or "(Der Unteragent hat kein Ergebnis geliefert.)"
        return clip_saved(f"[{kind}: {description}]\n{answer}", ctx.limit(), "subagent")

    async def _sub_step(self, messages: list[dict], tools: list[dict] | None) -> dict:
        kwargs = {} if self._think is None else {"think": self._think}
        result: dict = {}
        async for ev in self._call_llm(messages, tools, **kwargs):
            if ev.get("type") == "done":
                result = ev.get("message") or {}
        return result

    async def _sub_execute(self, call: dict, kind: str, allowed: set[str], emit, confirm) -> tuple[str, str]:
        fn = call.get("function", {})
        name = fn.get("name", "")
        if name not in allowed:
            return name, f"Werkzeug '{name}' ist für diesen Unteragenten nicht verfügbar. Verfügbar: " \
                         f"{', '.join(sorted(allowed))}"
        if kind == "explore" and name == "shell":
            spec = get_tool(name)
            args = coerce_args(spec, fn.get("arguments")) if spec else {}
            from .tools.registry import ToolContext
            ctx = ToolContext(cfg=self.cfg, memory=self.memory, cwd=self.project_dir())
            if args.get("background") or (spec and spec.assess(ctx, args)[0] != SAFE):
                return name, "Explore darf nur lesende Shell-Befehle ausführen (ls, git log, cat …)."
        call = {**call, "id": call.get("id") or uuid.uuid4().hex[:8]}
        name, result, _note = await self._execute(call, emit, confirm)
        return name, result
