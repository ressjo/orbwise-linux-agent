"""Chats: Verlauf, Liste, anlegen, umbenennen, löschen, exportieren, Projekt zuordnen, Schritte zurücknehmen."""

from __future__ import annotations

import asyncio
import os
import re
import time
from datetime import datetime
from pathlib import Path

from fastapi import HTTPException, Request
from fastapi.responses import Response

from ... import agent_safety
from ...lang import T


def register(app, ctx) -> None:
    # gemeinsame Objekte und Helfer anderer Teile (siehe webapp/context.py)
    agent = ctx.agent
    cfg = ctx.cfg
    heal_index_soon = ctx.heal_index_soon
    hub = ctx.hub
    llm = ctx.llm
    memory = ctx.memory

    @app.get("/api/history")
    async def history():
        """Der ganze sichtbare Chat – alle Nachrichten, auch komprimierte Teile; dazu, wo eine
        Komprimierung stattfand (Trenner mit aufklappbarer Zusammenfassung)."""
        from ... import attachments
        conv = memory.active
        shown = [(i, m) for i, m in enumerate(conv.history)
                 if m["role"] in ("user", "assistant")
                 and (m.get("content") or m.get("attachments") or m.get("docs") or m.get("shots"))]
        epochs = []
        for ep in conv.epochs[1:]:
            at = next((pos for pos, (i, _) in enumerate(shown) if i >= ep.get("start", 0)), None)
            if at is not None and ep.get("summary"):
                epochs.append({"at": at, "summary": ep["summary"], "ts": ep.get("ts"), "reason": ep.get("reason", "")})
        first = conv.epochs[0].get("summary", "") if conv.epochs else ""  # Zusammenfassung aus älteren Versionen
        turns = user_turns(conv)
        start = conv.epoch["start"]
        snaps = conv.meta.get("snapshots") or {}
        last_of_turn = {turns.get(i, 0): i for i, m in shown if m["role"] == "assistant"}

        def changes(i: int, m: dict) -> dict:
            snap = snaps.get(str(turns.get(i, 0))) if m["role"] == "assistant" else None
            if not snap or last_of_turn.get(turns.get(i, 0)) != i:
                return {}
            return {"changes": {"files": snap["files"], "undone": bool(snap.get("undone"))}}
        return {"summary": first, "chat": {"id": conv.chat_id, "title": conv.meta.get("title", "")},
                "messages": [{"role": m["role"], "content": m["content"], "turn": turns.get(i, 0), **changes(i, m),
                              **({"goal_nudge": True} if m.get("goal_nudge") else {}),
                              "editable": i >= start,
                              **({"images": m["attachments"]} if m.get("attachments") else {}),
                              **({"images": m["shots"]} if m.get("shots") else {}),
                              **({"files": [attachments.doc_info(d) for d in m["docs"]]} if m.get("docs") else {})}
                             for i, m in shown],
                "turns": max(turns.values(), default=0), "epochs": epochs, "usage": agent.chat_usage(conv)}

    @app.post("/api/chats/active/revert")
    async def revert_turn(request: Request):
        """Coding-Modus: die Dateiänderungen einer Antwort zurücksetzen (oder mit redo wiederherstellen) – wie /undo
        bei OpenCode. Betrifft nur die Dateien, die sich in dieser Antwort geändert haben."""
        from ... import snapshot
        data = await request.json()
        conv = memory.active
        key = str(int(data.get("turn") or 0))
        snap = (conv.meta.get("snapshots") or {}).get(key)
        if not snap:
            raise HTTPException(404, T("Zu dieser Antwort gibt es keinen Schnappschuss.", "No snapshot for this answer."))
        if agent.busy():
            raise HTTPException(409, T("Erst die laufende Antwort abwarten.", "Wait for the running answer first."))
        paths = [f["path"] for f in snap["files"]]
        project = snap["project"]
        if data.get("redo"):
            if not snap.get("undone"):
                raise HTTPException(409, T("Nichts wiederherzustellen.", "Nothing to restore."))
            done = await asyncio.to_thread(snapshot.restore, project, snap["undone"], paths)
            snap.pop("undone", None)
            note = T(f"(Hinweis: Der Nutzer hat die zurückgesetzten Dateiänderungen der Antwort auf Frage {key} "
                     f"wiederhergestellt: {', '.join(done)}. Vor weiteren Änderungen neu lesen.)",
                     f"(Note: the user restored the reverted file changes of the answer to question {key}: "
                     f"{', '.join(done)}. Read them again before changing them.)")
        else:
            current = await asyncio.to_thread(snapshot.capture, project)
            done = await asyncio.to_thread(snapshot.restore, project, snap["before"], paths)
            if current:
                snap["undone"] = current
            note = T(f"(Hinweis: Der Nutzer hat die Dateiänderungen der Antwort auf Frage {key} rückgängig gemacht: "
                     f"{', '.join(done)}. Diese Dateien sind wieder auf dem Stand davor – vor weiteren Änderungen "
                     "neu lesen.)",
                     f"(Note: the user undid the file changes of the answer to question {key}: {', '.join(done)}. "
                     "These files are back to their earlier state – read them again before changing them.)")
        conv.meta.setdefault("pending_notes", []).append(note)
        conv.save()
        event = {"type": "reverted", "turn": int(key), "undone": bool(snap.get("undone")), "files": done}
        await hub.broadcast(event)
        return event

    def user_turns(conv) -> dict[int, int]:
        """Index im Verlauf → Nummer der Nutzerfrage (1, 2, …), zu der die Nachricht gehört (Antworten zählen zur
        Frage davor) – so kann die Oberfläche „Frage 3 bearbeiten / neu generieren“ sagen."""
        out, n = {}, 0
        for i, m in enumerate(conv.history):
            if m["role"] == "user" and not m.get("goal_nudge"):  # „weiter am Ziel“ ist keine eigene Frage
                n += 1
            out[i] = n
        return out

    @app.post("/api/chats/active/rewind")
    async def rewind(request: Request):
        """Frage `turn` neu stellen: mit neuem Text (Bearbeiten) oder unverändert (Neu generieren). Alles ab dieser
        Frage wird verworfen – nur innerhalb des noch nicht zusammengefassten Teils des Chats."""
        not_busy()
        body = await request.json()
        try:
            turn = int(body.get("turn", 0))
        except (TypeError, ValueError):
            raise HTTPException(400, "turn fehlt") from None
        conv = memory.active
        idx = next((i for i, t in user_turns(conv).items()
                    if t == turn and conv.history[i]["role"] == "user"), None)
        if idx is None:
            raise HTTPException(404, "Diese Nachricht gibt es nicht (mehr)")
        old = conv.history[idx]
        text = str(body.get("text") if body.get("text") is not None else old.get("content") or "").strip()
        # angehängte Bilder und PDFs bleiben – beim Neu-Generieren und beim Bearbeiten der Frage
        images = list(old.get("attachments") or []) + [d["name"] for d in old.get("docs") or []]
        if not text and not images:
            raise HTTPException(400, "Leere Nachricht")
        try:
            conv.truncate(idx)
        except ValueError as e:
            raise HTTPException(409, str(e)) from e
        conv.save()
        agent._cache_owner = None  # Ende des Chats geändert – der Cache-Anfang passt aber meist noch
        await hub.broadcast({"type": "chat_reload", "turn": turn})  # Oberfläche schneidet ab dieser Frage ab
        await hub.submit(text, images=images or None)
        return {"ok": True, "turn": turn}

    @app.get("/api/chats/{chat_id}/export")
    async def export_chat(chat_id: str):
        """Chat als Markdown-Datei: Fragen, Antworten, Werkzeug-Aufrufe als kurze Zeile, Zusammenfassungen als Zitat."""
        from ... import attachments
        chat_or_404(chat_id)
        conv = memory.active if memory.active.chat_id == chat_id else memory._loaded(chat_id) \
            or memory.chats.open(chat_id)
        title = conv.meta.get("title") or T("Chat", "Chat")
        when = datetime.fromtimestamp(conv.meta.get("created") or time.time()).strftime("%d.%m.%Y %H:%M")
        lines = [f"# {title}", "", f"*{when} · Orbwise*", ""]
        summaries = {ep.get("start", 0): ep.get("summary") for ep in conv.epochs if ep.get("summary")}
        for i, m in enumerate(conv.history):
            if summaries.get(i):
                lines += ["> " + line for line in str(summaries[i]).splitlines()] + [""]
            files = [f"📎 {d['title']} ({attachments.pages_text(d['pages'])})" for d in m.get("docs") or []]
            if m["role"] == "user" and (m.get("content") or files):
                lines += [f"## {T('Du', 'You')}", "", *files, *([""] if files else []), str(m.get("content") or ""), ""]
            elif m["role"] == "assistant":
                for call in m.get("tool_calls") or []:
                    fn = call.get("function", {})
                    args = fn.get("arguments")
                    args = args if isinstance(args, dict) else {}
                    first = next((str(v) for v in args.values() if isinstance(v, (str, int, float))), "")
                    lines.append(f"🔧 {fn.get('name', '?')}" + (f": `{first[:160]}`" if first else ""))
                if m.get("content"):
                    lines += ["", f"## {cfg.assistant_name or 'Orbwise'}", "", str(m["content"]), ""]
        safe = re.sub(r"[^\w.-]+", "_", title)[:60].strip("_") or "chat"
        return Response("\n".join(lines).rstrip() + "\n", media_type="text/markdown; charset=utf-8",
                        headers={"Content-Disposition": f'attachment; filename="{safe}.md"'})

    # ---------- Chat-Historie ----------
    def chat_or_404(chat_id: str) -> None:
        if not memory.chats.exists(chat_id):
            raise HTTPException(404, "Chat nicht gefunden")

    def not_busy() -> None:
        if agent.busy():
            raise HTTPException(409, "Orbwise arbeitet gerade – bitte kurz warten oder STOP drücken")

    async def settle(timeout: float = 90) -> None:
        """Vor einem Chatwechsel: läuft eine Antwort, sofort ablehnen. Hält nur das Nachbereiten der letzten Antwort
        (Tagebuch, Suchindex, Platz schaffen) oder ein Modellvorgang den Agenten, darauf warten – die Oberfläche
        meldet dann schon „bereit“, ein Klick auf „Neuer Chat“ soll nicht ins Leere gehen."""
        if getattr(agent, "_turn_open", False):
            raise HTTPException(409, T("Orbwise antwortet gerade – erst abwarten oder STOP drücken.",
                                       "Orbwise is answering – wait for it or press STOP."))
        deadline = time.monotonic() + timeout
        while agent.busy():
            if getattr(agent, "_turn_open", False) or time.monotonic() > deadline:
                raise HTTPException(409, T("Orbwise arbeitet gerade – bitte kurz warten oder STOP drücken.",
                                           "Orbwise is busy – please wait a moment or press STOP."))
            await asyncio.sleep(0.1)

    def settings_info() -> dict:
        """Was der Server gerade anwendet – alle Fenster zeigen dasselbe (Handy und PC, Telegram nutzt es auch).
        think: None, solange noch keine Oberfläche es gesetzt hat; root: Auto-Erleichterungen sind dann aus."""
        think = hub.think
        from ... import jail
        ok, why = jail.available()
        return {"auto_mode": agent.auto_mode, "root": agent_safety.running_as_root(), "jail": {"ok": ok, "reason": why},
                "think": None if think is None else "off" if think is False else "on" if think is True else think}

    @app.get("/api/think")
    async def think_caps():
        """Denkfähigkeit des aktiven Modells: levels | toggle | none, null = unbekannt (Oberfläche: an/aus).
        levels/default: nur bei Modellen mit Stufen – die, die das Modell selbst vorgibt, und seine Voreinstellung."""
        caps = await llm.thinking_caps() if hasattr(llm, "thinking_caps") else None
        levels, default = await llm.thinking_levels() if caps == "levels" and hasattr(llm, "thinking_levels") \
            else ([], None)
        if caps == "levels" and not levels:
            caps = "toggle"  # Stufen nicht lesbar – dann nur an/aus statt erfundener Stufen
        evidence = await llm.thinking_evidence() if caps == "levels" and hasattr(llm, "thinking_evidence") else []
        return {"caps": caps, "levels": levels, "default": default, "evidence": evidence}

    def mode_info() -> dict:
        conv = memory.active
        sb = agent.sandbox_state() if memory.mode == "coding" else None
        goal = conv.meta.get("goal") if isinstance(conv.meta.get("goal"), dict) else None
        return {"mode": conv.meta.get("mode") or "tools", "project": conv.meta.get("project") or "",
                "chat_id": conv.chat_id, "goal": {"text": goal["text"], "steps": goal.get("steps", 0)} if goal else None,
                "sandbox": {"enabled": True, "network": sb.get("network", True), "branch": sb.get("branch", ""),
                            "engine": sb.get("engine", ""), "path": sb.get("path", "")} if sb else None}

    async def chat_switched() -> None:
        conv = memory.active
        await hub.broadcast({"type": "chat_switched", "id": conv.chat_id, "title": conv.meta.get("title", ""),
                             **mode_info()})
        hub.prewarm_soon()  # anderer Chat → seinen Anfang schon einlesen, bevor die erste Frage kommt

    @app.get("/api/chats")
    async def chats(q: str = "", mode: str = ""):
        memory.active.save()
        if memory.conversation is not memory.active:
            memory.conversation.save()
        return memory.chats.list(q, mode=mode or mode_info()["mode"])

    @app.post("/api/chats/{chat_id}/project")
    async def chat_project(chat_id: str, request: Request):
        chat_or_404(chat_id)
        raw = str((await request.json()).get("path", "")).strip()
        path = Path(os.path.expanduser(raw)) if raw else None
        if path is not None and not path.is_dir():
            raise HTTPException(400, f"Ordner nicht gefunden: {raw}")
        value = str(path) if path else ""
        conv = memory._loaded(chat_id)
        if conv is not None and conv is memory.active and conv.meta.get("sandbox") \
                and (conv.meta.get("project") or "") != value:
            not_busy()
            await agent.switch_project(value)  # die Sandbox gehört zum alten Projekt – neu anlegen
        elif conv is not None:
            conv.meta["project"] = value
            conv.save()
        else:
            memory.chats._update_meta(chat_id, project=value)
        if chat_id == memory.active.chat_id:
            await chat_switched()
        await hub.broadcast({"type": "chats_changed"})
        return {"project": value}

    @app.post("/api/chats")
    async def chat_new():
        await settle()
        memory.new_chat()
        await chat_switched()
        return {"id": memory.active.chat_id}

    @app.post("/api/chats/{chat_id}/activate")
    async def chat_activate(chat_id: str):
        chat_or_404(chat_id)
        await settle()
        memory.switch_chat(chat_id)
        await chat_switched()
        return {"id": chat_id}

    @app.post("/api/chats/{chat_id}/star")
    async def chat_star(chat_id: str, request: Request):
        chat_or_404(chat_id)
        body = await request.json()
        memory.star_chat(chat_id, bool(body.get("starred")))
        await hub.broadcast({"type": "chats_changed"})
        return {"ok": True}

    @app.patch("/api/chats/{chat_id}")
    async def chat_rename(chat_id: str, request: Request):
        chat_or_404(chat_id)
        title = str((await request.json()).get("title", "")).strip()
        if not title:
            raise HTTPException(400, "Titel fehlt")
        memory.rename_chat(chat_id, title)
        await hub.broadcast({"type": "chats_changed"})
        return {"ok": True}

    @app.delete("/api/chats/{chat_id}")
    async def chat_delete(chat_id: str):
        chat_or_404(chat_id)
        was_active = chat_id == memory.active.chat_id
        if was_active or chat_id == memory.conversation.chat_id:
            await settle()  # weder den offenen Chat noch den einer laufenden Routine/Telegram-Anfrage
        from ... import sandbox as sbx
        from ... import workspace
        await asyncio.to_thread(sbx.remove, cfg, sbx.container_name(chat_id))  # Sandbox des Chats wegräumen
        await asyncio.to_thread(workspace.discard_chat, chat_id)
        days = memory.forget_chat(chat_id)
        heal_index_soon()
        if was_active:
            await chat_switched()
        await hub.broadcast({"type": "chats_changed"})
        return {"ok": True, "days": days}

    # für andere Teile der App
    ctx.chat_switched = chat_switched
    ctx.mode_info = mode_info
    ctx.not_busy = not_busy
    ctx.settings_info = settings_info
    ctx.settle = settle
