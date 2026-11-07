"""WebSocket /ws: Chat, Audio, Bestätigungen und Rückfragen der Oberfläche."""

from __future__ import annotations

import asyncio
import contextlib
import json
import re

from fastapi import WebSocket, WebSocketDisconnect

from ...config import wake_phrase
from ...hub import Client
from ...voice.listen import AudioSession
from ..security import check_host


def register(app, ctx) -> None:
    # gemeinsame Objekte und Helfer anderer Teile (siehe webapp/context.py)
    agent = ctx.agent
    auto_file = ctx.auto_file
    boot_id = ctx.boot_id
    broker = ctx.broker
    cfg = ctx.cfg
    chat_switched = ctx.chat_switched
    from_jail = ctx.from_jail
    hub = ctx.hub
    llm = ctx.llm
    memory = ctx.memory
    mode_info = ctx.mode_info
    model_resting = ctx.model_resting
    settings_info = ctx.settings_info
    startup = ctx.startup
    stt = ctx.stt
    wake = ctx.wake
    wake_model = ctx.wake_model
    waking = ctx.waking
    from ... import web_auth
    web_token = ctx.web_token

    @app.websocket("/ws")
    async def ws_endpoint(ws: WebSocket):
        origin = ws.headers.get("origin", "")
        host = re.sub(r"^https?://", "", origin)
        # Schutz vor Cross-Site-WebSocket-Hijacking: fremde Webseiten dürfen keine Befehle senden
        if not check_host(host, cfg.port) or not check_host(ws.headers.get("host"), cfg.port) \
                or await asyncio.to_thread(from_jail, ws):  # kostet nur, solange ein Käfig-Prozess lebt
            await ws.close(code=4403)
            return
        if not web_auth.valid(web_auth.given(ws.headers, ws.cookies), web_token):
            await ws.close(code=4401)  # Oberfläche lädt neu und zeigt die Anmeldung
            return
        await ws.accept()
        client = Client(ws)
        client.audio = AudioSession(cfg.voice, stt, wake, client.send, lambda t: hub.submit(t, "voice"))
        hub.clients.add(client)
        await client.send({"type": "hello", "busy": agent.busy(), "boot": boot_id,
                           "name": cfg.assistant_name, "wake_phrase": wake_phrase(cfg.voice.wakeword_model),
                           "pending": [cid for cid in hub.pending], "context": agent.last_context,
                           **mode_info(), **settings_info()})
        await client.send(startup.snapshot())
        await client.send({"type": "sudo_cached", "until": broker.cached_until()})
        for qid, (_fut, questions) in list(hub.questions.items()):  # offene Rückfrage auch im neuen Fenster
            await client.send({"type": "question_request", "id": qid, "questions": questions})
        if not getattr(llm, "switching", None):
            hub.prewarm_soon()  # Oberfläche offen → den Chat schon einlesen, bevor die erste Frage kommt
        if hub.undelivered:
            events, hub.undelivered = hub.undelivered, []
            for event in events:
                await client.send(event)
                hub.speaker.say(event["spoken"])
        try:
            while True:
                msg = await ws.receive()
                if msg["type"] == "websocket.disconnect":
                    break
                if msg.get("bytes"):
                    await client.audio.feed(msg["bytes"])
                    continue
                if not msg.get("text"):
                    continue
                data = json.loads(msg["text"])
                t = data.get("type")
                if t == "user_message":
                    from ... import attachments
                    names = [str(n) for n in (data.get("images") or [])[:attachments.MAX_PER_MESSAGE]
                             if attachments.path_of(cfg, str(n))]
                    await hub.submit(str(data.get("text", "")), images=names)
                elif t == "confirm":
                    changes = data.get("args") if isinstance(data.get("args"), dict) else None
                    if data.get("trust") and data.get("approved") and str(data.get("id")) in hub.pending:
                        hub.agent.trust_chat()  # fremde Inhalte bis hierher: wieder der eingestellte Modus
                    hub.resolve(str(data.get("id")), bool(data.get("approved")), changes)
                elif t == "question_answer":  # Antwort auf question (None/leer = abgebrochen)
                    answers = data.get("answers") if isinstance(data.get("answers"), list) else None
                    hub.answer(str(data.get("id")), [a if isinstance(a, (str, list)) else str(a)
                                                     for a in answers] if answers else None)
                elif t == "stop":
                    await hub.stop()
                elif t == "tts":
                    client.tts = bool(data.get("enabled"))
                elif t == "password":
                    # Passwort nur an den wartenden sudo weiterreichen – nie loggen oder speichern
                    broker.answer(str(data.get("id", "")), data.get("password") or None,
                                  remember=bool(data.get("remember", True)), user=str(data.get("user") or "")[:100])
                elif t == "password_forget":
                    broker.forget()
                elif t == "password_cancel":
                    broker.answer(str(data.get("id", "")), None)
                elif t == "think":
                    level = str(data.get("level") or "")
                    # Stufe (low/medium/high) bei Modellen mit Stufen, sonst nur an/aus
                    # Stufe des Modells (bei Modellen mit Stufen), sonst nur an/aus
                    think = (level if re.fullmatch(r"[a-z][a-z-]{1,15}", level) and level not in ("on", "off")
                             else True) if data.get("enabled") else False
                    if think != hub.think:  # die Vorlage rendert den Prompt mit anderem Denkmodus anders → neu vorwärmen
                        hub.think = think
                        agent._cache_owner = None
                        hub.prewarm_soon()
                    await hub.broadcast({"type": "settings_sync", **settings_info()})
                elif t == "prewarm":  # die Oberfläche merkt: gleich kommt eine Frage (Tippen, Sprechen)
                    if model_resting() and not any(not w.done() for w in waking):
                        waking[:] = [asyncio.create_task(wake_model())]  # entladen → schon beim Tippen laden
                    else:
                        hub.prewarm_soon()
                elif t == "compact":  # Knopf an der Kontext-Kachel
                    task = asyncio.create_task(hub.compact(str(data.get("focus", ""))[:500]))
                    hub.tasks.add(task)
                    task.add_done_callback(hub.tasks.discard)
                elif t == "auto_mode":
                    mode = str(data.get("mode", "read"))
                    hub.agent.auto_mode = mode if mode in ("off", "read", "files", "auto") else "read"
                    with contextlib.suppress(OSError):
                        auto_file.write_text(hub.agent.auto_mode, encoding="utf-8")
                    await hub.broadcast({"type": "settings_sync", **settings_info()})
                elif t == "plan_mode":
                    hub.plan_mode = bool(data.get("enabled"))
                elif t in ("plan_accept", "plan_revise", "plan_discard"):
                    await hub.plan_decision(str(data.get("id", "")), t.split("_", 1)[1],
                                            str(data.get("text", ""))[:4000])
                elif t == "wake":
                    await client.audio.set_wake(bool(data.get("enabled")))
                elif t == "ptt_start":
                    hub.speaker.stop()
                    await client.send({"type": "audio_stop"})
                    await client.audio.start_recording(ptt=True)
                elif t == "ptt_stop":
                    await client.audio.stop_recording()
                elif t == "listen":
                    await client.audio.start_recording(ptt=False)
                elif t == "cancel_listen":
                    await client.audio.cancel()
                elif t == "speech_interrupt":
                    hub.speaker.stop()
                elif t == "reset_conversation":
                    # NEU-Knopf: neuer Chat (der bisherige bleibt in der Historie)
                    if not agent.busy():
                        memory.new_chat()
                        await hub.broadcast({"type": "conversation_reset"})
                        await chat_switched()
        except WebSocketDisconnect:
            pass
        finally:
            hub.clients.discard(client)
