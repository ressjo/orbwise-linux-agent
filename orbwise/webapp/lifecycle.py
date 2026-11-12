"""Start und Ende des Servers: Suchindex heilen, Stimme/Spracherkennung/Wake-Word vorladen, Piper einrichten, Modell
starten, lifespan."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import time

from fastapi import FastAPI

from .. import retire
from ..config import env, wake_phrase
from ..lang import T
from ..llm import LLMError
from ..llm_router import LLMRouter
from ..voice import piper, pocket

log = logging.getLogger(__name__)


def setup(ctx) -> None:
    # gemeinsame Objekte und Helfer anderer Teile (siehe webapp/context.py)
    agent = ctx.agent
    background = ctx.background
    bg_jobs = ctx.bg_jobs
    cfg = ctx.cfg
    en = ctx.en
    hub = ctx.hub
    images = ctx.images
    llm = ctx.llm
    memory = ctx.memory
    retired = ctx.retired
    startup = ctx.startup
    stt = ctx.stt
    tts = ctx.tts
    wake = ctx.wake

    async def heal_index() -> None:
        """Beschädigter Suchindex wurde leer neu angelegt → im Hintergrund aus den Gedächtnis-Dateien füllen."""
        if not memory.index.needs_rebuild:
            return
        await hub.broadcast({"type": "memory", "text": "Search index was damaged – rebuilding it from the memory files."
                             if en else "Suchindex war beschädigt – wird aus den Gedächtnis-Dateien neu aufgebaut."})
        if not startup.ready:
            await startup.set("memory", "running", T("Suchindex wird neu aufgebaut …", "rebuilding the search index …"))
        try:
            if await memory.heal_index_if_needed():
                await hub.broadcast({"type": "memory", "text": "Search index rebuilt." if en
                                     else "Suchindex neu aufgebaut."})
            await startup.set("memory", "ok", memory_summary())
        except Exception as e:  # noqa: BLE001
            heal_failed_at[0] = time.monotonic()
            log.warning("Neuaufbau des Suchindex fehlgeschlagen: %s", e)
            await startup.set("memory", "warn", T("Suchindex nicht neu aufgebaut: ", "search index not rebuilt: ") + str(e))

    healing: list[asyncio.Task] = []
    heal_failed_at = [-1e9]

    def heal_index_soon() -> None:
        if (memory.index.needs_rebuild and not any(not t.done() for t in healing) and not ctx.llm_resting()
                and time.monotonic() - heal_failed_at[0] > 600):  # nach einem Fehlschlag erst in 10 min wieder
            healing[:] = [asyncio.create_task(heal_index())]  # nicht an STOP gebunden

    side_tasks: set[asyncio.Task] = set()

    def keep(task: asyncio.Task) -> None:
        """Referenz halten, bis die Aufgabe fertig ist (sonst kann sie der Garbage Collector abräumen)."""
        side_tasks.add(task)
        task.add_done_callback(side_tasks.discard)

    def memory_summary() -> str:
        return T(f"{len(memory.journal.days())} Tage · {len(memory.facts.list())} Fakten",
                 f"{len(memory.journal.days())} days · {len(memory.facts.list())} facts")

    piper_jobs: dict = {"voice": "", "stop": None}  # laufender Download einer Piper-Stimme

    async def setup_piper(voice_id: str, progress=None) -> bool:
        """Piper einrichten (eigene Umgebung) und die Stimme laden – beim Start, wenn Piper gewählt ist, oder aus der
        Oberfläche. Fortschritt per WebSocket (piper_progress)."""
        import threading
        loop = asyncio.get_running_loop()

        def out(line: str) -> None:
            log.info("Piper: %s", line.strip())
            m = re.search(r"\((\d+) %\)", line)
            asyncio.run_coroutine_threadsafe(hub.broadcast({"type": "piper_progress", "voice": voice_id,
                                                            "text": line.strip(), "pct": int(m.group(1)) if m else None}),
                                             loop)
            if progress:
                asyncio.run_coroutine_threadsafe(progress(line.strip()), loop)
        if not tts.piper.installed() and not await asyncio.to_thread(piper.install, tts.piper.data_dir, out):
            return False
        if piper.has_voice(tts.piper.data_dir, voice_id):
            return True
        stop = threading.Event()
        piper_jobs.update(voice=voice_id, stop=stop)
        try:
            return await asyncio.to_thread(piper.download_voice, tts.piper.data_dir, voice_id, out, None, stop)
        except Exception as e:  # noqa: BLE001 – abgebrochen oder Netz
            out(f"✘ {e}" if str(e) else "✘ abgebrochen")
            return False
        finally:
            piper_jobs.update(voice="", stop=None)

    async def warm_tts() -> None:
        if tts is None:
            await startup.set("tts", "warn", T("Browser-Stimme", "browser voice"))
            return
        if tts.is_piper(tts.voice) and not await asyncio.to_thread(tts.available):  # Piper gewählt, fehlt noch
            async def say(text: str) -> None:
                await startup.set("tts", "running", T("richte Piper ein: ", "setting up Piper: ") + text)
            await startup.set("tts", "running", T("richte Piper ein …", "setting up Piper …"))
            await setup_piper(tts.piper_id(tts.voice), say)
        if not await asyncio.to_thread(tts.available):
            await startup.set("tts", "warn", T("Browser-Stimme", "browser voice") + (f" – {tts.error}" if tts.error else ""))
            return
        label = tts.label_of(tts.voice)
        await startup.set("tts", "running", T("lade Stimme ", "loading voice ") + label + " …")
        try:
            await asyncio.to_thread(tts.warmup)
            speaks = pocket.language_name(tts.reply_language, cfg.language == "en")
            await startup.set("tts", "ok", f"{label} · {speaks}")
        except Exception as e:  # noqa: BLE001
            await startup.set("tts", "warn", T("Browser-Stimme – ", "browser voice – ") + str(e))

    async def warm_voice() -> None:
        """Stimme, Spracherkennung und Wake-Word im Hintergrund laden – mit Anzeige, was gerade lädt."""
        if not cfg.voice.enabled:
            off = T("aus (voice.enabled)", "off (voice.enabled)")
            for key in ("stt", "tts", "wake"):
                await startup.set(key, "off", off)
            return
        await warm_tts()
        if wake is None:
            await startup.set("wake", "off")
        else:
            await startup.set("wake", "running", T("lade Wake-Word-Modell …", "loading the wake word model …"))
            ok = await asyncio.to_thread(wake.available)
            await startup.set("wake", "ok" if ok else "warn", wake_phrase(cfg.voice.wakeword_model) if ok else (wake.error or ""))
        if stt is None:
            await startup.set("stt", "error", T("faster-whisper ist nicht installiert", "faster-whisper is not installed"))
        elif env("SKIP_WARMUP") == "1":
            await startup.set("stt", "ok", "Whisper " + cfg.voice.stt_model)
        else:
            await startup.set("stt", "running", T("lade Whisper „", "loading Whisper “") + cfg.voice.stt_model
                              + T("“ …", "” …"))
            try:
                await asyncio.to_thread(stt.warmup)
                await startup.set("stt", "ok", "Whisper " + cfg.voice.stt_model)
            except Exception as e:  # noqa: BLE001
                await startup.set("stt", "error", str(e))

    async def watch_telegram() -> None:
        if ctx.telegram_bot is None:
            await startup.set("telegram", "off", T("nicht eingerichtet", "not set up"))
            return
        await startup.set("telegram", "running", T("verbinde …", "connecting …"))
        for _ in range(240):
            st = ctx.telegram_bot.status
            if st.get("running"):
                await startup.set("telegram", "ok", st.get("bot") or T("verbunden", "connected"))
                return
            if st.get("error"):
                await startup.set("telegram", "error", st["error"])
                return
            await asyncio.sleep(0.25)
        await startup.set("telegram", "warn", T("noch keine Verbindung", "not connected yet"))

    async def remove_piper_voices() -> None:
        """Stimmen der früheren Piper-Einbindung (<data>/voices) löschen (einmalig) – Piper liegt jetzt in
        <data>/piper und richtet sich beim Start selbst ein."""
        freed = await asyncio.to_thread(retire.remove_piper_voices, cfg.memory.dir.parent / "voices")
        if freed:
            log.info("Alte Piper-Stimmen entfernt (%.0f MB frei)", freed / 1e6)

    async def remove_retired(found: dict) -> None:
        """Strata ist aus Orbwise entfernt: laufenden Server stoppen, Modell und Engine löschen (einmalig)."""
        freed = await asyncio.to_thread(retire.remove_strata_files, found["strata_dirs"])
        log.info("Strata entfernt (Profile: %s, %.1f GB frei)", ", ".join(found["strata"]), freed / 1e9)
        await hub.broadcast({"type": "models_changed"})

    from ..mcp import McpManager
    ctx.mcp = McpManager(cfg)

    async def connect_mcp() -> None:
        """MCP-Server im Hintergrund verbinden (npx lädt beim ersten Mal erst herunter) – danach kennt der Agent
        ihre Werkzeuge."""
        try:
            if await ctx.mcp.start():
                hub.agent.reload_tools()
        except Exception as e:  # noqa: BLE001 – MCP ist Zubehör, der Start hängt nicht daran
            log.warning("MCP-Server nicht verbunden: %s", e)

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI):
        hub.speaker.start()
        await startup.set("memory", "ok", memory_summary())
        heal_index_soon()
        if isinstance(llm, LLMRouter):
            background.append(asyncio.create_task(start_model()))
            if retired and retired["strata"]:
                background.append(asyncio.create_task(remove_retired(retired)))
        else:
            await startup.set("model", "ok", T("Demo-Modus", "demo mode"))
        background.append(asyncio.create_task(warm_voice()))
        background.append(asyncio.create_task(connect_mcp()))
        background.append(asyncio.create_task(remove_piper_voices()))
        background.append(asyncio.create_task(watch_telegram()))
        background.append(asyncio.create_task(ctx.summary_loop()))
        background.append(asyncio.create_task(ctx.metrics_loop()))
        background.append(asyncio.create_task(ctx.reminder_loop()))
        background.append(asyncio.create_task(ctx.routine_loop()))
        background.append(asyncio.create_task(ctx.sandbox_idle_loop()))
        if ctx.telegram_bot is not None:
            background.append(asyncio.create_task(ctx.telegram_bot.serve()))
        yield
        for t in [*background, *healing, *side_tasks]:
            t.cancel()
        bg_jobs.stop_all()  # Dev-Server & Co. nicht verwaist weiterlaufen lassen
        await ctx.mcp.stop()  # gestartete MCP-Server beenden
        from .. import sandbox as sbx
        for name in list(sbx.last_used):  # Sandbox-Container anhalten (nicht löschen – die Arbeit bleibt)
            await asyncio.to_thread(sbx.stop, cfg, name)
        bg_jobs.set_broadcast(None)
        from ..tools.ssh import close_all as close_ssh
        await close_ssh()  # offene SSH-Verbindungen schließen (sonst bleiben sie bis zum Zeitlimit angemeldet)
        if images.server is not None:
            await images.server.stop()  # Bildserver nicht verwaist laufen lassen
        await hub.speaker.close()
        if tts:
            await asyncio.to_thread(tts.stop)
        await llm.close()
        memory.close()

    async def model_progress(text: str) -> None:
        await hub.broadcast({"type": "model_progress", "text": text})
        if startup.steps["model"]["state"] == "running":
            await startup.set("model", "running", text)

    async def model_reloading(text: str) -> None:
        """Eine Frage holt ein von Hand entladenes Modell zurück: Oberfläche zeigt „lädt …“."""
        if text and isinstance(llm, LLMRouter):
            await hub.broadcast({"type": "model_switching", "name": llm.active, "label": llm.profile.label})
        await model_progress(text)

    async def model_reloaded() -> None:
        await hub.broadcast({"type": "model_active", "name": llm.active})
        await hub.broadcast({"type": "models_changed"})

    if isinstance(llm, LLMRouter):
        llm.load_progress = model_reloading
        llm.load_done = model_reloaded

    async def idle_if_free() -> None:
        """Oberfläche auf „bereit“ setzen – nur, wenn gerade keine Anfrage läuft."""
        if not agent.busy():
            await hub.broadcast({"type": "state", "state": "idle"})

    async def start_model() -> None:
        # Beim Start das aktive Profil vorbereiten (z. B. llama-server starten); Chats warten so lange
        name = llm.active
        label = llm.profile.label or llm.profile.model
        if llm.unloaded:  # beim Beenden entladen → bleibt so, bis jemand fragt
            llm.apply_mode(ctx.mode_info()["mode"])
            await startup.set("model", "ok", label + T(" · entladen – lädt bei der ersten Frage",
                                                       " · unloaded – loads with the first question"))
            return
        llm.switching = name if llm.server_for(name) else None
        await startup.set("model", "running", T("starte ", "starting ") + label + " …")
        try:
            async with agent.lock:
                llm.apply_mode(ctx.mode_info()["mode"])  # Coding kann ein eigenes Kontextfenster haben
                await llm.start(model_progress)
                await startup.set("model", "running", T(f"lade {llm.profile.model} in den Speicher …",
                                                        f"loading {llm.profile.model} into memory …"))
            await hub.broadcast({"type": "model_active", "name": name})
            await startup.set("model", "ok", label)
            hub.prewarm_soon()  # leerer Chat: Systemprompt + Werkzeuge in den Cache – auch ohne offene Oberfläche
        except LLMError as e:
            log.warning("Modell-Start fehlgeschlagen: %s", e)
            await hub.broadcast({"type": "model_error", "name": name, "text": str(e)})
            await startup.set("model", "error", str(e))
        finally:
            llm.switching = None
            await idle_if_free()  # wer sich während des Ladens verbunden hat, sah „denke nach“

    # für andere Teile der App
    ctx.heal_index_soon = heal_index_soon
    ctx.idle_if_free = idle_if_free
    ctx.keep = keep
    ctx.lifespan = lifespan
    ctx.model_progress = model_progress
    ctx.piper_jobs = piper_jobs
    ctx.setup_piper = setup_piper
