"""Telegram-Bot anbinden (eigener Chat „📱 Telegram“) und fällige Erinnerungen ausliefern."""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
from datetime import datetime

from .. import askpass, prompts
from ..hub import describe_call
from ..tools import proc
from ..tools.registry import get_tool

log = logging.getLogger(__name__)


def setup(ctx) -> None:
    # gemeinsame Objekte und Helfer anderer Teile (siehe webapp/context.py)
    agent = ctx.agent
    cfg = ctx.cfg
    hub = ctx.hub
    idle_if_free = ctx.idle_if_free
    images = ctx.images
    keep = ctx.keep
    memory = ctx.memory
    reminders = ctx.reminders
    stt = ctx.stt

    # ---------- Telegram ----------
    telegram_bot = None
    if cfg.telegram.secret:
        from ..extras.telegram import CHAT_TITLE, TelegramBot, chat_state_file, transcribe_voice
        tg_state = chat_state_file(cfg)

        async def run_telegram(text: str, emit, confirm, plan: bool = False, approved_plan: str = "") -> str:
            """Anfrage vom Handy: eigener Chat „📱 Telegram“ – der offene Chat im Dashboard bleibt unberührt."""
            askpass.REMOTE.set(True)  # gemerktes sudo-Passwort gilt nicht für Aufträge vom Handy
            try:
                chat_id = json.loads(tg_state.read_text(encoding="utf-8")).get("chat_id", "")
            except (OSError, ValueError):
                chat_id = ""

            async def emit_all(ev: dict) -> None:
                await emit(ev)
                if ev.get("type") in ("tool_call", "tool_result", "tool_output", "state"):
                    await hub.broadcast({**ev, "routine": "Telegram"})  # Aktivität/Orb im Dashboard

            answer, chat_id = await agent.run_in_chat(chat_id, CHAT_TITLE, text, emit_all, confirm, plan=plan,
                                                    think=hub.think, approved_plan=approved_plan)  # Denken-Knopf gilt auch vom Handy
            tg_state.parent.mkdir(parents=True, exist_ok=True)
            tg_state.write_text(json.dumps({"chat_id": chat_id}), encoding="utf-8")
            await hub.broadcast({"type": "chats_changed"})
            if hub.clients:  # der Server hatte eben den Telegram-Chat im Cache – den offenen Chat wieder einlesen
                hub.prewarm_soon()
            await idle_if_free()
            return answer

        def tool_editable(name: str) -> bool:
            spec = get_tool(name)
            return bool(spec and spec.editable)

        telegram_bot = TelegramBot(
            cfg, run_telegram, lambda name, args: describe_call(name, args, cfg), tool_editable,
            transcribe=(lambda data: transcribe_voice(stt, data)) if stt else None,
            on_stop=hub.stop)  # /stop vom Handy stoppt auch, was am PC läuft
        agent.services["telegram"] = telegram_bot  # für telegram_send_file
        telegram_bot.images = images  # /bild
        hub.cancellers.append(telegram_bot.cancel_current)  # STOP am PC bricht auch Anfragen vom Handy ab

    async def fire_reminder(r, now) -> None:
        late = (now - r.due_dt).total_seconds() > 120
        kind = prompts.spoken(cfg, "kind_timer" if r.kind == "timer" else "kind_reminder")
        spoken = (prompts.spoken(cfg, "missed", kind=kind, time=r.due_dt.strftime("%H:%M"), text=r.text) if late
                  else prompts.spoken(cfg, "timer" if r.kind == "timer" else "reminder", text=r.text))
        reminders.mark_done(r.id)
        memory.journal.append("Erinnerung", spoken)
        event = {"type": "reminder", "id": r.id, "text": r.text, "kind": r.kind,
                 "due": r.due, "late": late, "spoken": spoken}
        if hub.clients:
            await hub.broadcast(event)
            hub.speaker.say(spoken)
        else:
            hub.undelivered.append(event)
        if telegram_bot is not None:  # immer zusätzlich aufs Handy – im Hintergrund, Telegram darf nichts aufhalten
            keep(asyncio.create_task(telegram_bot.notify(("⏰ " if r.kind != "timer" else "⏱ ") + spoken)))
        if shutil.which("notify-send"):
            await proc.launch(["notify-send", "--app-name=Orbwise", "--urgency=critical",
                               f"Orbwise – {kind}", r.text], wait=1)

    async def reminder_loop() -> None:
        while True:
            try:
                now = datetime.now()
                for r in reminders.due(now):
                    await fire_reminder(r, now)
            except Exception as e:  # noqa: BLE001
                log.warning("Erinnerung fehlgeschlagen: %s", e)
            await asyncio.sleep(1)

    # für andere Teile der App
    ctx.reminder_loop = reminder_loop
    ctx.telegram_bot = telegram_bot
