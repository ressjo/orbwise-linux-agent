"""FastAPI-Server: Weboberfläche, WebSocket für Chat/Audio, REST-Endpunkte für Status und Gedächtnis."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import uuid

from fastapi import FastAPI

from . import askpass, metrics, prompts, retire
from . import skills as skill_store
from .agent import Agent
from .config import Config, env
from .hub import Hub
from .lang import set_lang
from .llm import FakeLLM
from .llm_router import LLMRouter
from .memory import Memory
from .reminders import ReminderStore
from .routines import RoutineStore
from .voice.listen import WakeWordFactory, WhisperSTT
from .voice.tts import TTS
from .webapp import background as background_tasks
from .webapp import lifecycle, telegram_link
from .webapp.context import AppContext
from .webapp.routes import chats, github, models, modes, sandbox, skills, system, voices, web, websocket
from .webapp.routes import images as images_routes
from .webapp.routes import memory as memory_routes
from .webapp.routes import routines as routines_routes
from .webapp.security import check_host, is_loopback, remote_bind_warning  # noqa: F401 – für cli und Tests
from .webapp.startup import Startup
from .webapp.static import WEB_DIR, FreshStaticFiles, versioned_assets  # noqa: F401 – für Tests

log = logging.getLogger(__name__)


def create_app(cfg: Config) -> FastAPI:
    set_lang(cfg.language)  # Meldungen außerhalb der Prompts (z. B. Modell-Server-Fehler)
    skill_store.apply(cfg)  # eingerichtete Skills (load_config tut das schon – Configs aus Tests/Code ohne Datei auch)
    fake = env("FAKE_LLM") == "1"
    # Profile entfernter Engines (Strata, Bonsai) aus state.json nehmen, bevor der Router sie liest – Dateien beim Start
    retired = None if fake else retire.clean_state(cfg.memory.dir.parent / "state.json")
    with contextlib.suppress(OSError):  # „Aus Erfahrung lernen“ gibt es nicht mehr – gelernte Lektionen wegräumen
        if retire.remove_lessons(cfg.memory.dir):
            log.info("Gelernte Lektionen entfernt (Funktion „Aus Erfahrung lernen“ gibt es nicht mehr)")
    llm = FakeLLM(language=cfg.language) if fake else LLMRouter(cfg.llm, state_path=cfg.memory.dir.parent / "state.json")
    memory = Memory(cfg.memory, llm)
    memory.start_fresh()  # nach dem Start immer ein leerer Chat im Tools-Modus – vorgeladen werden nur Modell + Werkzeuge
    boot_id = uuid.uuid4().hex[:8]  # je Serverstart: die Oberfläche stellt den Bildmodus nur innerhalb eines Laufs wieder her
    agent = Agent(cfg, llm, memory)
    # Auto-Knopf überlebt einen Neustart – sonst liefen Telegram/Routinen bis zum Öffnen des Dashboards mit „Nur lesen“
    auto_file = cfg.memory.dir.parent / "auto_mode"
    with contextlib.suppress(OSError):
        if (saved := auto_file.read_text(encoding="utf-8").strip()) in ("off", "read", "files", "auto"):
            agent.auto_mode = saved
    reminders = ReminderStore(cfg.memory.dir.parent / "reminders.json")
    agent.services["reminders"] = reminders
    routines = RoutineStore(cfg.memory.dir.parent / "routines.json")
    routines.reset_running()
    agent.services["routines"] = routines

    stt = wake = tts = None
    if cfg.voice.enabled:
        stt = WhisperSTT(cfg.voice)
        stt = stt if stt.available() else None
        wake = WakeWordFactory(cfg.voice)
        tts = TTS(cfg.voice, cfg.memory.dir.parent, cfg.memory.dir.parent / "state.json")
    hub = Hub(cfg, agent, tts, stt, wake)
    agent.services["ask"] = hub.ask  # Werkzeug question (Coding-Modus)
    from .tools import jobs as bg_jobs
    bg_jobs.set_broadcast(hub.broadcast)  # Hintergrund-Jobs melden sich auch nach dem Ende der Antwort

    def apply_reply_language() -> None:
        """Die Sprache der gewählten Stimme gilt auch für die Antworten; spricht man eine andere Sprache als die der
        Oberfläche, erkennt Whisper die Sprache selbst."""
        reply = tts.reply_language if tts else cfg.language
        agent.reply_language = reply if reply != cfg.language else None
        prompts.set_spoken_language(agent.reply_language)
        if stt:
            stt.language = None if agent.reply_language else (cfg.voice.language or cfg.language)
    apply_reply_language()
    from .imagegen import ImageEngine, ImageError
    images = ImageEngine(cfg, llm if isinstance(llm, LLMRouter) else None,
                         gpu_info=lambda: (metrics.collect() or {}).get("gpu"))
    agent.services["images"] = images
    startup = Startup(cfg, hub.broadcast)
    en = cfg.language == "en"
    # Root-Rechte per Passwortfeld in der Oberfläche (sudo -A)
    helper = None
    if cfg.tools.privilege_cmd == "dashboard":
        try:
            helper = askpass.write_helper()
        except OSError as e:
            log.warning("Askpass-Helfer konnte nicht angelegt werden: %s", e)
    broker = askpass.AskpassBroker(cfg.port, notify=hub.broadcast, helper=helper,
                                   has_ui=lambda: bool(hub.clients), say=hub.speaker.say,
                                   say_text=prompts.spoken(cfg, "password"),
                                   remember_seconds=cfg.tools.sudo_remember_minutes * 60)
    askpass.BROKER = broker
    hub.askpass = broker
    background: list[asyncio.Task] = []
    state_file = cfg.memory.dir.parent / "state.json"
    ctx = AppContext(cfg=cfg, llm=llm, memory=memory, agent=agent, hub=hub, images=images, startup=startup,
                     broker=broker, reminders=reminders, routines=routines, stt=stt, tts=tts, wake=wake,
                     background=background, state_file=state_file, boot_id=boot_id, auto_file=auto_file, en=en,
                     retired=retired, bg_jobs=bg_jobs, apply_reply_language=apply_reply_language,
                     ImageError=ImageError)
    lifecycle.setup(ctx)  # Start/Ende, Modell, Stimme, Suchindex
    telegram_link.setup(ctx)  # Telegram-Bot, Erinnerungen
    background_tasks.setup(ctx)  # Routinen, Hintergrund-Schleifen

    app = FastAPI(title="Orbwise", lifespan=ctx.lifespan)
    from . import web_auth
    ctx.web_token = web_auth.load_or_create()
    ctx.app = app
    app.state.web_token = ctx.web_token
    app.state.hub = hub
    app.state.telegram = ctx.telegram_bot
    app.state.memory = memory
    app.state.llm_resting = ctx.app_state_resting
    app.state.images = images

    # Endpunkte – die Reihenfolge ist die, in der FastAPI Pfade abgleicht
    web.register(app, ctx)
    system.register(app, ctx)
    ctx.models_api = models.register(
        app, cfg=cfg, llm=llm, agent=agent, memory=memory, hub=hub, state_file=state_file, background=background,
        model_progress=ctx.model_progress, idle_if_free=ctx.idle_if_free, mode_info=lambda: ctx.mode_info(),
        mode_models=lambda: ctx.mode_models(), remember_mode_model=lambda mode, name: ctx.remember_mode_model(mode, name))
    images_routes.register(app, ctx)
    memory_routes.register(app, ctx)
    chats.register(app, ctx)
    sandbox.register(app, ctx)
    github.register(app, ctx)
    modes.register(app, ctx)
    routines_routes.register(app, ctx)
    skills.register(app, ctx)
    voices.register(app, ctx)
    websocket.register(app, ctx)

    app.mount("/static", FreshStaticFiles(directory=WEB_DIR), name="static")
    return app
