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

from . import prompts, snapshot
from .llm import LLMError, strip_think
from .memory import est_tokens
from .tools import jobs
from .tools.proc import clip_saved
from .tools.registry import SAFE, coerce_args, get_tool

log = logging.getLogger(__name__)

EXPLORE_TOOLS = {"read", "grep", "glob", "shell", "job_output", "websearch", "webfetch", "recall"}
GENERAL_EXCLUDED = {"subagent", "question"}
SNAPSHOTS_KEEP = 60


class CodingMixin:
    # ---------- Prompt ----------
    def coding_env(self) -> str:
        project = self.project_dir()
        git = bool(project) and any((Path(project) / ".git").exists() or (p / ".git").exists()
                                    for p in Path(project).resolve().parents)
        shell = os.path.basename(os.environ.get("SHELL") or "/bin/bash")
        return prompts.coding_env(self.cfg, git, shell)

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
        turn = sum(1 for m in conv.history if m["role"] == "user")
        snaps = conv.meta.setdefault("snapshots", {})
        snaps[str(turn)] = {"project": project, "before": before, "after": after, "files": files, "msg": msg_id}
        for key in list(snaps)[:-SNAPSHOTS_KEEP]:
            snaps.pop(key, None)
        await emit({"type": "changes", "id": msg_id, "turn": turn, "files": files})

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
        budget = self.context_budget()
        prev, self._prompt_prev = self._prompt_prev, None  # eigener Prompt – nicht als „neu eingelesen“ verbuchen
        answer = ""
        try:
            for step in range(steps + 1):
                final = step == steps or est_tokens(json.dumps(messages, ensure_ascii=False)) > 0.75 * budget
                if final:
                    messages.append({"role": "user", "content": prompts.subagent_text(self.cfg, "final")})
                message = await self._sub_step(messages, None if final else schemas)
                content = strip_think(message.get("content") or "")
                calls = [] if final else message.get("tool_calls") or []
                entry = {"role": "assistant", "content": content}
                if calls:
                    entry["tool_calls"] = calls
                messages.append(entry)
                if not calls:
                    answer = content
                    break
                for call in calls:
                    name, result = await self._sub_execute(call, kind, allowed, sub_emit, confirm)
                    messages.append({"role": "tool", "content": result, "tool_name": name})
        except LLMError as e:
            answer = (answer or "") + f"\n(Unteragent abgebrochen: {e})"
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
