"""Routinen und Hintergrund-Schleifen (Erinnerungen, Telemetrie, Tageszusammenfassungen, Sandbox-Leerlauf); ruht das
Sprachmodell?"""

from __future__ import annotations

import asyncio
import logging
import shutil
from datetime import datetime

from .. import askpass, metrics, prompts, usage
from ..llm_router import LLMRouter
from ..tools import proc

log = logging.getLogger(__name__)


def setup(ctx) -> None:
    # gemeinsame Objekte und Helfer anderer Teile (siehe webapp/context.py)
    agent = ctx.agent
    cfg = ctx.cfg
    heal_index_soon = ctx.heal_index_soon
    hub = ctx.hub
    images = ctx.images
    llm = ctx.llm
    memory = ctx.memory
    routines = ctx.routines

    # ---------- Routinen ----------
    def start_routine(rid: str) -> bool:
        """Startet eine Routine im Hintergrund (sie wartet ggf., bis eine laufende Anfrage fertig ist)."""
        r = routines.get(rid)
        if r is None or r.last_status == "running":
            return False
        routines.mark_started(rid, datetime.now())
        task = asyncio.create_task(run_routine(rid))
        hub.tasks.add(task)  # STOP bricht auch eine laufende Routine ab
        task.add_done_callback(hub.tasks.discard)
        return True

    agent.services["start_routine"] = start_routine

    async def run_routine(rid: str) -> None:
        r = routines.get(rid)
        if r is None:
            return
        denied: list[str] = []
        await hub.broadcast({"type": "routines_changed"})

        async def emit(ev: dict) -> None:
            # Nicht in den offenen Chat streamen und nicht vorlesen – nur Aktivität und Orb-Zustand zeigen
            if ev.get("type") in ("tool_call", "tool_result", "tool_output", "state"):
                await hub.broadcast({**ev, "routine": r.name})

        async def confirm(call_id: str, name: str, args: dict, reason: str) -> bool:
            ok = bool(hub.clients) and await hub.confirm(
                call_id, name, args, prompts.text(cfg, "routine_confirm").format(name=r.name, reason=reason or name))
            if not ok:
                denied.append(name)
            return ok

        now = datetime.now()
        when = now.strftime("%d.%m.%Y %H:%M") if cfg.language != "en" else now.strftime("%Y-%m-%d %H:%M")
        text = prompts.text(cfg, "routine_prompt").format(name=r.name, when=when, task=r.task)
        status, answer, chat_id = "ok", "", r.chat_id
        try:
            askpass.REMOTE.set(True)  # Routinen laufen unbeaufsichtigt – kein gemerktes sudo-Passwort
            answer, chat_id = await agent.run_in_chat(r.chat_id, f"⟳ {r.name}", text, emit, confirm)
            status = "denied" if denied else "ok"
        except asyncio.CancelledError:
            status, answer = "error", prompts.spoken(cfg, "routine_cancelled")
        except Exception as e:  # noqa: BLE001
            log.exception("Routine %s fehlgeschlagen", r.name)
            status, answer = "error", str(e)
        summary = " ".join(answer.split())[:300]
        routines.set_result(rid, status, summary, chat_id)
        await hub.broadcast({"type": "routine_done", "id": rid, "name": r.name, "chat_id": chat_id,
                             "status": status, "summary": summary})
        await hub.broadcast({"type": "chats_changed"})
        if not agent.busy():
            await hub.broadcast({"type": "state", "state": "idle"})
        if hub.clients:  # die Routine hat den Cache des Servers belegt – den offenen Chat im Leerlauf zurückholen
            hub.prewarm_soon()
        if shutil.which("notify-send"):
            await proc.launch(["notify-send", "--app-name=Orbwise", f"Orbwise – {r.name}",
                               summary[:200] or prompts.spoken(cfg, "routine_done")], wait=1)

    async def sandbox_idle_loop() -> None:
        """Sandbox-Container ohne Befehle seit sandbox.idle_minutes anhalten (beim nächsten Befehl startet er wieder)."""
        from .. import sandbox as sbx
        while True:
            await asyncio.sleep(60)
            for name in sbx.idle(cfg):
                try:
                    await asyncio.to_thread(sbx.stop, cfg, name)
                except Exception as e:  # noqa: BLE001
                    log.info("Sandbox %s nicht angehalten: %s", name, e)

    async def routine_loop() -> None:
        await asyncio.sleep(3)
        while True:
            try:
                for r in routines.due(datetime.now()):
                    start_routine(r.id)
                heal_index_soon()  # im Betrieb beschädigter Suchindex → bald neu aufbauen, nicht erst beim Neustart
            except Exception as e:  # noqa: BLE001
                log.warning("Routinen-Planer: %s", e)
            await asyncio.sleep(20)

    async def metrics_loop() -> None:
        while True:
            if hub.clients:
                try:
                    data = await asyncio.to_thread(metrics.collect)
                    await hub.broadcast({"type": "metrics", **data, "usage": usage.summary(cfg)})
                except Exception as e:  # noqa: BLE001
                    log.debug("Telemetrie fehlgeschlagen: %s", e)
            await asyncio.sleep(2)

    def llm_resting() -> bool:
        """Soll das Sprachmodell gerade aus bleiben? (Bild-Modus hat den Grafikspeicher bzw. von Hand entladen) – dann
        keine Wartung, die es laden würde (Tageszusammenfassungen, Suchindex mit Embeddings)."""
        return bool(getattr(llm, "gpu_borrowed", None) or getattr(llm, "unloaded", False) or images.view_open)

    app_state_resting = llm_resting  # für Tests: app.state.llm_resting

    waking: list[asyncio.Task] = []

    def model_resting() -> bool:
        """Sprachmodell entladen (von Hand oder für Bilder), aber die Bild-Ansicht ist zu – dann soll Tippen es laden."""
        return isinstance(llm, LLMRouter) and not images.view_open and bool(llm.gpu_borrowed or llm.unloaded)

    async def wake_model() -> None:
        """Beim Tippen in Tools/Coding: Sprachmodell im Hintergrund laden (Bildserver geht vorher aus), danach den
        Chat schon einlesen – die Frage muss dann nicht auf das Laden warten."""
        try:
            if llm.gpu_borrowed:
                await llm.gpu_borrowed()  # Bildmodell raus, Sprachmodell rein (ein laufendes Bild wird erst fertig)
            if llm.unloaded:
                await llm.ensure_loaded()  # zeigt „lädt …“ wie beim Absenden
        except Exception as e:  # noqa: BLE001 – dann lädt es eben beim Absenden (mit Fehlermeldung dort)
            log.info("Sprachmodell beim Tippen nicht geladen: %s", e)
            return
        hub.prewarm_soon()

    async def summary_loop() -> None:
        await asyncio.sleep(30)
        while True:
            if not agent.lock.locked() and not llm_resting():
                try:
                    done = await memory.summarize_pending()
                    if done:
                        log.info("Tageszusammenfassungen erstellt: %s", ", ".join(done))
                except Exception as e:  # noqa: BLE001
                    log.warning("Zusammenfassungen fehlgeschlagen: %s", e)
                if getattr(memory, "summary_ran", False):  # auch ohne Ergebnis: der Server hatte andere Texte im Cache
                    memory.summary_ran = False
                    agent._cache_owner = None
                    if hub.clients:
                        hub.prewarm_soon()
            await asyncio.sleep(300)

    # für andere Teile der App
    ctx.app_state_resting = app_state_resting
    ctx.llm_resting = llm_resting
    ctx.metrics_loop = metrics_loop
    ctx.model_resting = model_resting
    ctx.routine_loop = routine_loop
    ctx.sandbox_idle_loop = sandbox_idle_loop
    ctx.start_routine = start_routine
    ctx.summary_loop = summary_loop
    ctx.wake_model = wake_model
    ctx.waking = waking
