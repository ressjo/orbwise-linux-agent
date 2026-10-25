"""FastAPI-Server: Weboberfläche, WebSocket für Chat/Audio, REST-Endpunkte für Status und Gedächtnis."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import json
import logging
import os
import re
import shutil
import time
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from . import agent_safety, askpass, metrics, prompts, retire, usage
from . import skills as skill_store
from .agent import Agent
from .config import BRIEFING_SECTIONS, BriefingConfig, Config, env
from .lang import T, set_lang
from .llm import FakeLLM, LLMError
from .llm_router import LLMRouter
from .memory import Memory
from .memory.files import valid_day
from .reminders import ReminderStore
from .routines import RoutineStore
from .tools import briefing, proc
from .tools.calendar_tools import calendar_status
from .tools.registry import ToolContext, get_tool
from .tools.trilium import trilium_status
from .voice import pocket
from .voice.listen import AudioSession, WakeWordFactory, WhisperSTT
from .voice.tts import TTS, Speaker
from .web_i18n import translate_index

log = logging.getLogger(__name__)
WEB_DIR = Path(__file__).parent / "web"

YES = re.compile(r"\b(ja|jawohl|jep|jo|okay|ok|klar|mach(\s+es|'s)?|los|bestätig\w*|ausführen|genehmigt|positiv|yes|yeah|yep|sure|go\s+ahead|do\s+it|confirm\w*|proceed)\b", re.I)
NO = re.compile(r"\b(nein|nee|nö|stopp?|abbrechen|abbruch|nicht|lass\s+es|negativ|no|nope|cancel|don'?t|abort)\b", re.I)


def parse_yes_no(text: str) -> bool | None:
    no, yes = bool(NO.search(text)), bool(YES.search(text))
    if no:
        return False
    if yes:
        return True
    return None


CALL_TEXTS = {"run_shell": ("call_shell", "command"), "install_package": ("call_install", "names"),
              "remove_package": ("call_remove", "names"), "system_update": ("call_update", ""),
              "calendar_update": ("call_cal_update", "query"), "calendar_delete": ("call_cal_delete", "query"),
              "trilium_update_note": ("call_trilium", "note"), "write_file": ("call_write", "path"),
              "edit_file": ("call_edit", "path"), "shell": ("call_shell", "command"),
              "write": ("call_write", "path"), "edit": ("call_edit", "path"),
              "mail_send": ("call_mail", "to")}


def spoken_confirm(name: str, args: dict, cfg=None) -> str:
    """Gesprochene Rückfrage: Befehle werden nicht vorgelesen (sie stehen im Dialog), Pfade nur als Dateiname."""
    if name in ("run_shell", "shell"):
        return prompts.spoken(cfg, "confirm_shell")
    if name == "mail_send":
        return prompts.spoken(cfg, "confirm_mail", v=str(args.get("to", "")))
    if name in ("write_file", "edit_file", "write", "edit") and args.get("path"):
        args = {**args, "path": Path(str(args["path"])).name}
    return prompts.spoken(cfg, "confirm", what=describe_call(name, args, cfg))


def describe_call(name: str, args: dict, cfg=None) -> str:
    key, field = CALL_TEXTS.get(name, ("call_other", ""))
    value = str(args.get(field, "")) if field else name
    return prompts.spoken(cfg, key, v=value)


class Client:
    def __init__(self, ws: WebSocket):
        self.ws = ws
        self.tts = True
        self.audio: AudioSession | None = None
        self.lock = asyncio.Lock()

    async def send(self, event: dict) -> None:
        async with self.lock:
            try:
                await self.ws.send_text(json.dumps(event, ensure_ascii=False))
            except Exception:  # noqa: BLE001 – Verbindung weg
                pass


class Hub:
    def __init__(self, cfg: Config, agent: Agent, speaker_tts: TTS | None,
                 stt: WhisperSTT | None, wake: WakeWordFactory | None):
        self.cfg = cfg
        self.agent = agent
        self.askpass = None  # AskpassBroker (Passwortfeld für sudo -A)
        self.cancellers: list = []  # weitere Abbrecher für STOP (z. B. laufende Telegram-Anfrage) → int
        self.think: bool | str | None = None  # Denken-Knopf: False/Stufe low|medium|high (None = Profil)
        self.plan_mode = False  # PLAN-Knopf: erst einen Plan vorlegen, ausführen nach Freigabe
        self.plan_pending: str | None = None  # ID des Plans, der auf Ausführen/Ändern/Verwerfen wartet
        self.plan_text = ""  # sein Text – wird beim Ausführen an die Anfrage geheftet
        self._plan_msgs: set[str] = set()  # Antworten, die ein Plan sind – werden nicht vorgelesen
        self.clients: set[Client] = set()
        # Erinnerungen, die fällig wurden, als keine Oberfläche offen war – werden beim Verbinden zugestellt
        self.undelivered: list[dict] = []
        self.pending: dict[str, asyncio.Future] = {}
        self.editable_pending: set[str] = set()
        # offene Rückfragen des Werkzeugs question: ID → (Future, Fragen)
        self.questions: dict[str, tuple[asyncio.Future, list[dict]]] = {}
        self.tasks: set[asyncio.Task] = set()
        self._prewarm: asyncio.Task | None = None
        self._prewarm_again = False
        self.stt = stt
        self.wake = wake
        self.speaker = Speaker(speaker_tts, self.broadcast_tts, lambda: any(c.tts for c in self.clients))

    def prewarm_soon(self) -> None:
        """Den Prompt-Anfang des offenen Chats im Leerlauf einlesen lassen (nach Chatwechsel, Telegram/Routine,
        beim Tippen …) – die nächste Frage liest dann nur noch ihren neuen Teil ein. Siehe Agent.prewarm."""
        if self._prewarm and not self._prewarm.done():
            self._prewarm_again = True  # läuft noch (vielleicht für den vorigen Chat) – danach noch einmal prüfen
            return
        if self.agent.lock.locked() or not self.agent.cache_cold():
            return
        self._prewarm = asyncio.create_task(self._prewarm_loop())

    async def _prewarm_loop(self) -> None:
        try:
            while True:
                self._prewarm_again = False
                await self.agent.prewarm(self.broadcast, think=self.think)
                if not self._prewarm_again:
                    return
        except Exception as e:  # noqa: BLE001 – Vorwärmen ist nur eine Beschleunigung
            log.info("Vorwärmen fehlgeschlagen: %s", e)

    async def compact(self, focus: str = "") -> None:
        """„/compact [Fokus]“ oder der Knopf an der Kontext-Kachel: den offenen Chat jetzt zusammenfassen."""
        try:
            done = await self.agent.compact_now(self.emit, focus)
        except Exception as e:  # noqa: BLE001
            log.warning("Komprimieren fehlgeschlagen: %s", e)
            done = False
        if not done:
            await self.broadcast({"type": "compact_skipped"})

    async def broadcast(self, event: dict) -> None:
        await asyncio.gather(*(c.send(event) for c in list(self.clients)))

    async def broadcast_tts(self, event: dict) -> None:
        await asyncio.gather(*(c.send(event) for c in list(self.clients) if c.tts))

    def coding(self) -> bool:
        """Im Coding-Modus spricht Jarvis nicht (keine Sprachausgabe, keine Spracheingabe)."""
        return self.agent.memory.active.meta.get("mode") == "coding"

    async def emit(self, event: dict) -> None:
        t = event.get("type")
        if self.coding() and t in ("token", "segment_end", "assistant_end", "error", "plan"):
            if t == "plan":
                self.plan_pending, self.plan_text = event["id"], event.get("text") or ""
            await self.broadcast(event)
            return
        if t == "assistant_start" and event.get("plan"):
            self._plan_msgs = {event["id"]}
        elif t == "token":
            if event["id"] not in self._plan_msgs:  # einen ganzen Plan vorzulesen wäre zu lang – kurze Ansage
                self.speaker.feed(event["id"], event["text"])
        elif t in ("segment_end", "assistant_end"):
            self.speaker.end(event["id"])
        elif t == "error":
            self.speaker.say(event["text"])
        elif t == "plan":
            self.plan_pending, self.plan_text = event["id"], event.get("text") or ""
            steps = int(event.get("steps") or 0)
            self.speaker.say(prompts.spoken(self.cfg, "plan_ready", n=steps) if steps
                             else prompts.spoken(self.cfg, "plan_ready_short"))
        await self.broadcast(event)

    async def close_plan(self, outcome: str) -> None:
        """Offenen Plan abschließen: accepted · revised · discarded · replaced."""
        if self.plan_pending:
            plan_id, self.plan_pending, self.plan_text = self.plan_pending, None, ""
            await self.broadcast({"type": "plan_closed", "id": plan_id, "outcome": outcome})

    async def plan_decision(self, plan_id: str, action: str, feedback: str = "") -> None:
        """Ausführen / Ändern (mit Wunsch) / Verwerfen eines vorgelegten Plans."""
        if not self.plan_pending or plan_id != self.plan_pending:
            return
        if action == "accept":
            approved = self.plan_text
            await self.close_plan("accepted")
            if self.plan_mode:  # Plan angenommen → Planmodus geht aus, ausgeführt wird normal
                self.plan_mode = False
                await self.broadcast({"type": "plan_mode", "enabled": False})
            await self.submit(prompts.text(self.cfg, "plan_execute"), plan=False, approved_plan=approved)
        elif action == "revise" and feedback.strip():
            await self.close_plan("revised")
            await self.submit(prompts.text(self.cfg, "plan_revise").format(feedback=feedback.strip()), plan=True)
        elif action == "discard":
            await self.close_plan("discarded")

    async def confirm(self, call_id: str, name: str, args: dict, reason: str) -> bool | tuple[bool, dict]:
        """Wartet auf Ja/Nein. Bei Tools mit bearbeitbaren Feldern (z. B. mail_send) liefert eine Bestätigung
        (True, geänderte Felder) – die übernimmt der Agent."""
        fut = asyncio.get_running_loop().create_future()
        self.pending[call_id] = fut
        spec = get_tool(name)
        editable = list(spec.editable) if spec else []
        if editable:
            self.editable_pending.add(call_id)
        await self.broadcast({"type": "confirm_request", "id": call_id, "name": name, "args": args,
                              "reason": reason, "summary": describe_call(name, args, self.cfg), "editable": editable})
        self.speaker.say(spoken_confirm(name, args, self.cfg))
        try:
            # zum Bearbeiten (z. B. einer Mail) mehr Zeit lassen
            return await asyncio.wait_for(fut, timeout=900 if editable else 180)
        except asyncio.TimeoutError:
            return False
        finally:
            self.pending.pop(call_id, None)
            self.editable_pending.discard(call_id)
            await self.broadcast({"type": "confirm_done", "id": call_id})

    async def ask(self, call_id: str, questions: list[dict]) -> list | None:
        """Werkzeug question: Fragen mit Antwortmöglichkeiten im Gespräch zeigen und auf die Antwort warten.
        Liefert je Frage die gewählten Antworten (Liste) bzw. den freien Text – None, wenn nicht geantwortet."""
        fut = asyncio.get_running_loop().create_future()
        self.questions[call_id] = (fut, questions)
        await self.broadcast({"type": "question_request", "id": call_id, "questions": questions})
        await self.broadcast({"type": "state", "state": "confirm"})
        try:
            return await asyncio.wait_for(fut, timeout=900)
        except asyncio.TimeoutError:
            return None
        finally:
            self.questions.pop(call_id, None)
            await self.broadcast({"type": "question_done", "id": call_id})

    def answer(self, call_id: str, answers: list | None) -> None:
        entry = self.questions.get(call_id)
        if entry and not entry[0].done():
            entry[0].set_result(answers or None)

    def resolve(self, call_id: str, approved: bool, changes: dict | None = None) -> None:
        fut = self.pending.get(call_id)
        if fut and not fut.done():
            fut.set_result((True, changes) if approved and changes and call_id in self.editable_pending else approved)

    async def submit(self, text: str, source: str = "text", plan: bool | None = None,
                     approved_plan: str = "", images: list[str] | None = None) -> None:
        """plan: None = wie der PLAN-Knopf steht; True/False erzwingt (Ausführen/Überarbeiten eines Plans).
        images: Namen angehängter Bilder (siehe attachments.py)."""
        text = text.strip()
        if images and not text:
            text = prompts.text(self.cfg, "image_only")
        if not text or (source == "voice" and self.coding()):
            return  # im Coding-Modus gibt es keine Spracheingabe
        command = re.match(r"^/(compact|komprimieren)\b\s*(.*)$", text, re.I | re.S)
        if command and source != "voice":  # wie in Claude Code: /compact [worauf es ankommt]
            task = asyncio.create_task(self.compact(command.group(2)))
            self.tasks.add(task)
            task.add_done_callback(self.tasks.discard)
            return
        if self.plan_pending and plan is None and not self.pending:
            # Kurzes „ja/ausführen“ bzw. „nein“ entscheidet über den offenen Plan; alles andere ersetzt ihn
            decision = parse_yes_no(text) if len(text.split()) <= 4 else None
            if decision is not None:
                await self.plan_decision(self.plan_pending, "accept" if decision else "discard")
                return
            await self.close_plan("replaced")
        # Wartet eine Bestätigung, wird gesprochener Text als Ja/Nein interpretiert
        if self.pending:
            decision = parse_yes_no(text)
            if decision is not None:
                for cid in list(self.pending):
                    # Fenster mit bearbeitbaren Feldern (Mail) nur per Klick bestätigen – „Nein“ bricht aber ab
                    if decision and cid in self.editable_pending:
                        continue
                    self.resolve(cid, decision)
                return
            if source == "voice":
                self.speaker.say(prompts.spoken(self.cfg, "yes_no"))
                return
            # Neue getippte Anfrage statt Antwort → offene Aktion ablehnen
            for cid in list(self.pending):
                self.resolve(cid, False)
        self.speaker.stop()
        await self.broadcast({"type": "audio_stop"})
        from . import attachments
        docs = [d for d in (attachments.doc(self.cfg, n) for n in images or [] if attachments.is_doc(n)) if d]
        await self.broadcast({"type": "user", "text": text, "source": source,
                              "images": [n for n in images or [] if not attachments.is_doc(n)],
                              "files": [attachments.doc_info(d) for d in docs]})
        task = asyncio.create_task(self._run(text, self.plan_mode if plan is None else plan, approved_plan, images))
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    async def _run(self, text: str, plan: bool = False, approved_plan: str = "",
                   images: list[str] | None = None) -> None:
        try:
            await self.agent.run(text, self.emit, self.confirm, think=self.think, plan=plan,
                                 approved_plan=approved_plan, images=images)
        except asyncio.CancelledError:
            pass
        except Exception as e:  # noqa: BLE001
            log.exception("Agent-Fehler")
            await self.emit({"type": "error", "text": f"Interner Fehler: {e}"})
        finally:
            if not self.agent.busy():
                await self.broadcast({"type": "state", "state": "idle"})

    async def stop(self) -> int:
        """Alles Laufende abbrechen (STOP im Dashboard, /stop per Telegram). Liefert, wie viel gestoppt wurde."""
        stopped = 0
        if self.askpass:
            self.askpass.cancel_all()
        for fut in self.pending.values():
            if not fut.done():
                fut.set_result(False)
                stopped += 1
        for fut, _questions in self.questions.values():
            if not fut.done():
                fut.set_result(None)
                stopped += 1
        for task in list(self.tasks):
            if not task.done():
                task.cancel()
                stopped += 1
        for cancel in self.cancellers:
            stopped += cancel()
        self.speaker.stop()
        await self.broadcast({"type": "audio_stop"})
        return stopped


STARTUP_STEPS = [("memory", "Gedächtnis", "Memory"), ("model", "Sprachmodell", "Language model"),
                 ("stt", "Spracherkennung", "Speech recognition"), ("tts", "Sprachausgabe", "Speech output"),
                 ("wake", "Wake-Word", "Wake word"), ("telegram", "Telegram", "Telegram")]


class Startup:
    """Was beim Start von Orbwise noch lädt – für die Startanzeige der Oberfläche (live per WebSocket).
    Zustände: pending · running · ok · warn · error · off."""

    def __init__(self, cfg: Config, notify):
        en = cfg.language == "en"
        self.notify = notify
        self.steps = {k: {"key": k, "label": e if en else d, "state": "pending", "text": ""} for k, d, e in STARTUP_STEPS}

    @property
    def ready(self) -> bool:
        return all(s["state"] not in ("pending", "running") for s in self.steps.values())

    def snapshot(self) -> dict:
        return {"type": "startup", "steps": list(self.steps.values()), "ready": self.ready}

    async def set(self, key: str, state: str, text: str = "") -> None:
        self.steps[key].update(state=state, text=text)
        await self.notify(self.snapshot())


def is_loopback(host: str) -> bool:
    import ipaddress
    if host in ("localhost", "ip6-localhost"):
        return True
    try:
        return ipaddress.ip_address(host.strip("[]")).is_loopback
    except ValueError:
        return False


def remote_bind_warning(cfg: Config) -> str:
    """Leer bei 127.0.0.1. Sonst ist das Dashboard – ohne Anmeldung, mit Befehlsausführung – im Netz erreichbar;
    die Host-Prüfung hält Browser-Angriffe ab, aber kein Gerät im LAN, das den Host-Header selbst setzt."""
    if is_loopback(cfg.host):
        return ""
    return T(f"host: {cfg.host} macht das Dashboard ohne Anmeldung im Netzwerk erreichbar – jedes Gerät dort könnte "
             "Befehle auf diesem PC auslösen. Für unterwegs lieber Telegram, VPN (z. B. WireGuard/Tailscale) oder "
             "einen SSH-Tunnel (ssh -L 8765:localhost:8765 pc) nutzen.",
             f"host: {cfg.host} makes the dashboard reachable on the network without a login – any device there could "
             "run commands on this PC. For remote use prefer Telegram, a VPN (e.g. WireGuard/Tailscale) or an SSH "
             "tunnel (ssh -L 8765:localhost:8765 pc).")


def check_host(host: str | None, port: int) -> bool:
    if not host:
        return False
    name = host.rsplit(":", 1)[0] if not host.startswith("[") else host.split("]")[0] + "]"
    return name in ("localhost", "127.0.0.1", "[::1]")


def versioned_assets(html: str) -> str:
    """/static/app.js → /static/app.js?v=<Inhalts-Hash>: Nach einem Update lädt der Browser jede geänderte Datei
    sofort neu. Sonst mischt er neue und alte Dateien aus dem Cache (z. B. neues app.js mit altem orb.js) –
    dann bricht die Oberfläche an fehlenden Funktionen ab (keine Werkzeug-Anzeige, „denke nach“ bleibt stehen)."""
    def stamp(m: re.Match) -> str:
        path = WEB_DIR / m.group(2)
        if not path.is_file():
            return m.group(0)
        digest = hashlib.sha1(path.read_bytes()).hexdigest()[:10]
        return f"{m.group(1)}/static/{m.group(2)}?v={digest}{m.group(3)}"
    return re.sub(r'((?:src|href)=")/static/([\w./-]+)(")', stamp, html)


class FreshStaticFiles(StaticFiles):
    """Oberflächen-Dateien immer neu prüfen (ETag → meist 304): nach einem Update läuft sonst stundenlang
    das alte app.js aus dem Browser-Cache weiter."""

    def file_response(self, *args, **kwargs):
        resp = super().file_response(*args, **kwargs)
        resp.headers["Cache-Control"] = "no-cache"
        return resp


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
        if (memory.index.needs_rebuild and not any(not t.done() for t in healing)
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

    async def warm_tts() -> None:
        if tts is None or not await asyncio.to_thread(tts.available):
            await startup.set("tts", "warn", T("Browser-Stimme", "browser voice") + (f" – {tts.error}" if tts and tts.error else ""))
            return
        label = pocket.BY_NAME[tts.voice].label
        await startup.set("tts", "running", T("lade Stimme ", "loading voice ") + label + " …")
        try:
            await asyncio.to_thread(tts.warmup)
            speaks = pocket.language_name(tts.reply_language, cfg.language == "en")
            await startup.set("tts", "ok", f"Pocket TTS · {label} · {speaks}")
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
            await startup.set("wake", "ok" if ok else "warn", "„Hey Jarvis“" if ok else (wake.error or ""))
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
        if telegram_bot is None:
            await startup.set("telegram", "off", T("nicht eingerichtet", "not set up"))
            return
        await startup.set("telegram", "running", T("verbinde …", "connecting …"))
        for _ in range(240):
            st = telegram_bot.status
            if st.get("running"):
                await startup.set("telegram", "ok", st.get("bot") or T("verbunden", "connected"))
                return
            if st.get("error"):
                await startup.set("telegram", "error", st["error"])
                return
            await asyncio.sleep(0.25)
        await startup.set("telegram", "warn", T("noch keine Verbindung", "not connected yet"))

    async def remove_piper_voices() -> None:
        """Piper ist aus Orbwise entfernt: die heruntergeladenen Piper-Stimmen löschen (einmalig) und – wer bisher Piper
        hörte – gleich Pocket TTS einrichten, damit nicht plötzlich der Browser spricht."""
        freed = await asyncio.to_thread(retire.remove_piper_voices, cfg.memory.dir.parent / "voices")
        if not freed:
            return
        log.info("Piper-Stimmen entfernt (%.0f MB frei)", freed / 1e6)
        if tts and not tts.pocket.installed() and pocket.cpu_supported():
            await startup.set("tts", "running", T("richte Pocket TTS ein …", "setting up Pocket TTS …"))
            if await asyncio.to_thread(pocket.install, tts.pocket.data_dir, log.info):
                await warm_tts()
            else:
                await startup.set("tts", "warn", T("Browser-Stimme – Pocket TTS unter Einstellungen → Stimme",
                                                   "browser voice – Pocket TTS under Settings → Voice"))

    async def remove_retired(found: dict) -> None:
        """Strata ist aus Orbwise entfernt: laufenden Server stoppen, Modell und Engine löschen (einmalig)."""
        freed = await asyncio.to_thread(retire.remove_strata_files, found["strata_dirs"])
        log.info("Strata entfernt (Profile: %s, %.1f GB frei)", ", ".join(found["strata"]), freed / 1e9)
        await hub.broadcast({"type": "models_changed"})

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
        background.append(asyncio.create_task(remove_piper_voices()))
        background.append(asyncio.create_task(watch_telegram()))
        background.append(asyncio.create_task(summary_loop()))
        background.append(asyncio.create_task(metrics_loop()))
        background.append(asyncio.create_task(reminder_loop()))
        background.append(asyncio.create_task(routine_loop()))
        background.append(asyncio.create_task(sandbox_idle_loop()))
        if telegram_bot is not None:
            background.append(asyncio.create_task(telegram_bot.serve()))
        yield
        for t in [*background, *healing, *side_tasks]:
            t.cancel()
        bg_jobs.stop_all()  # Dev-Server & Co. nicht verwaist weiterlaufen lassen
        from . import sandbox as sbx
        for name in list(sbx.last_used):  # Sandbox-Container anhalten (nicht löschen – die Arbeit bleibt)
            await asyncio.to_thread(sbx.stop, cfg, name)
        bg_jobs.set_broadcast(None)
        from .tools.ssh import close_all as close_ssh
        await close_ssh()
        if images.server is not None:
            await images.server.stop()  # Bildserver nicht verwaist laufen lassen  # offene SSH-Verbindungen schließen (sonst bleiben sie bis zum Zeitlimit angemeldet)
        await hub.speaker.close()
        if tts:
            await asyncio.to_thread(tts.pocket.stop)
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
            llm.apply_mode(mode_info()["mode"])
            await startup.set("model", "ok", label + T(" · entladen – lädt bei der ersten Frage",
                                                       " · unloaded – loads with the first question"))
            return
        llm.switching = name if llm.server_for(name) else None
        await startup.set("model", "running", T("starte ", "starting ") + label + " …")
        try:
            async with agent.lock:
                llm.apply_mode(mode_info()["mode"])  # Coding kann ein eigenes Kontextfenster haben
                await llm.start(model_progress)
                await startup.set("model", "running", T(f"lade {llm.profile.model} in den Speicher …",
                                                        f"loading {llm.profile.model} into memory …"))
                try:  # sonst wartet die erste Frage, bis Ollama das Modell geladen hat
                    await llm.warm()
                    warm_error = ""
                except LLMError as e:  # Profil ist trotzdem aktiv – nur vorladen ging nicht (z. B. Ollama aus)
                    warm_error = str(e)
            await hub.broadcast({"type": "model_active", "name": name})
            await startup.set("model", "warn" if warm_error else "ok", warm_error or label)
        except LLMError as e:
            log.warning("Modell-Start fehlgeschlagen: %s", e)
            await hub.broadcast({"type": "model_error", "name": name, "text": str(e)})
            await startup.set("model", "error", str(e))
        finally:
            llm.switching = None
            await idle_if_free()  # wer sich während des Ladens verbunden hat, sah „denke nach“

    # ---------- Telegram ----------
    telegram_bot = None
    if cfg.telegram.secret:
        from .telegram import CHAT_TITLE, TelegramBot, chat_state_file, transcribe_voice
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
        from . import sandbox as sbx
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

    async def summary_loop() -> None:
        await asyncio.sleep(30)
        while True:
            if not agent.lock.locked():
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

    app = FastAPI(title="Orbwise", lifespan=lifespan)
    app.state.hub = hub
    app.state.telegram = telegram_bot
    app.state.memory = memory

    @app.middleware("http")
    async def only_local(request: Request, call_next):
        # Schutz vor DNS-Rebinding: nur Anfragen an localhost zulassen
        if not check_host(request.headers.get("host"), cfg.port):
            return JSONResponse({"error": "forbidden host"}, status_code=403)
        # Schreibende Anfragen nur von der eigenen Oberfläche (Schutz vor CSRF)
        origin = request.headers.get("origin")
        if request.method not in ("GET", "HEAD") and origin and \
                not check_host(re.sub(r"^https?://", "", origin), cfg.port):
            return JSONResponse({"error": "forbidden origin"}, status_code=403)
        return await call_next(request)

    @app.get("/")
    async def index():
        html = translate_index((WEB_DIR / "index.html").read_text(encoding="utf-8"), cfg.language)
        return HTMLResponse(versioned_assets(html), headers={"Cache-Control": "no-cache"})

    @app.get("/api/startup")
    async def startup_state():
        return startup.snapshot()

    @app.get("/api/status")
    async def status():
        voice = {
            "stt": stt is not None,
            "tts": bool(tts and tts.available()),
            "tts_error": tts.error if tts else "Sprache deaktiviert",
            "wake": bool(wake and wake._ok),  # nicht hier laden (blockiert) – das macht warm_voice beim Start
            "wake_error": wake.error if wake else None,
        }
        return {
            "name": cfg.assistant_name, "user": cfg.user_name,
            "llm": await llm.status(),
            "voice": voice,
            "memory": {
                "days": len(memory.journal.days()),
                "facts": len(memory.facts.list()),
                "chunks": memory.index.count(),
                "dir": str(cfg.memory.dir),
            },
            "trilium": await trilium_status(cfg),
            "calendar": await calendar_status(cfg),
            "busy": agent.busy(),
            "telegram": telegram_bot.status if telegram_bot is not None else {"running": False, "configured": False},
        }

    # ---------- Modelle (Liste, Hinzufügen, Laden/Entladen, Einstellungen je Modell) – siehe api_models.py ----------
    state_file = cfg.memory.dir.parent / "state.json"
    from . import api_models
    models_api = api_models.register(
        app, cfg=cfg, llm=llm, agent=agent, memory=memory, hub=hub, state_file=state_file, background=background,
        model_progress=model_progress, idle_if_free=idle_if_free, mode_info=lambda: mode_info(),
        mode_models=lambda: mode_models(), remember_mode_model=lambda mode, name: remember_mode_model(mode, name))

    @app.post("/api/askpass")
    async def askpass_request(request: Request):
        """Vom Askpass-Helfer (sudo -A) aufgerufen – nur mit gültigem Einmal-Token."""
        token = request.headers.get("x-orbwise-askpass", "")
        try:
            prompt = str((await request.json()).get("prompt", ""))[:200]
        except ValueError:
            prompt = ""
        password = await broker.request(token, prompt)
        if password is None:
            return JSONResponse({"error": "abgelehnt"}, status_code=403)
        return JSONResponse({"password": password}, headers={"Cache-Control": "no-store"})

    # OpenAI-kompatible Schnittstelle (/v1) für andere Programme – aus, bis sie in der Oberfläche eingeschaltet wird
    from . import api_server
    api_server.register(app, cfg=cfg, llm=llm, agent=agent, hub=hub, state_file=state_file,
                        model_progress=model_progress, idle_if_free=idle_if_free)

    # ---------- Angehängte Bilder und PDFs im Chat ----------
    @app.post("/api/attachments")
    async def attachment_upload(request: Request):
        from . import attachments
        body = await request.json()
        title = Path(str(body.get("name") or "")).name[:120]
        try:  # PDFs werden dabei ausgelesen – kann bei großen Dateien einen Moment dauern
            name = await asyncio.to_thread(attachments.save_data_url, cfg, str(body.get("data") or ""), title)
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
        if attachments.is_doc(name):
            d = attachments.doc(cfg, name) or {"name": name, "title": title, "pages": 0, "text": ""}
            return {"name": name, "url": f"/api/attachments/{name}", "doc": attachments.doc_info(d)}
        vision = await agent.llm.supports_images() if hasattr(agent.llm, "supports_images") else False
        return {"name": name, "url": f"/api/attachments/{name}", "vision": vision}

    @app.get("/api/attachments/{name}")
    async def attachment_file(name: str):
        from . import attachments
        path = attachments.path_of(cfg, name)
        if not path:
            raise HTTPException(404, "Datei nicht gefunden")
        return FileResponse(path, media_type=attachments.MIME.get(path.suffix, "image/png"),
                            headers={"Cache-Control": "private, max-age=86400", "X-Content-Type-Options": "nosniff"})

    # ---------- Bild-Modus (Qwen-Image-2.1 über stable-diffusion.cpp, siehe imagegen.py) ----------
    image_task: list[asyncio.Task] = []

    @app.get("/api/image/status")
    async def image_status():
        return {**images.status(), "telegram": bool(telegram_bot is not None and cfg.telegram.chat_id)}

    @app.get("/api/image/list")
    async def image_list(limit: int = 60):
        return {"images": images.list(limit)}

    @app.get("/api/image/file/{name}")
    async def image_file(name: str):
        path = images.path_of(name)
        if not path:
            raise HTTPException(404, "Bild nicht gefunden")
        return FileResponse(path, media_type="image/png", headers={"Cache-Control": "private, max-age=86400"})

    @app.delete("/api/image/file/{name}")
    async def image_delete(name: str):
        if not images.delete(name):
            raise HTTPException(404, "Bild nicht gefunden")
        return {"ok": True}

    @app.post("/api/image/generate")
    async def image_generate(request: Request):
        """Startet ein Bild im Hintergrund; Fortschritt und Ergebnis kommen per WebSocket (image_progress/_done)."""
        data = await request.json()
        if images.lock.locked():
            raise HTTPException(409, "Es wird gerade ein Bild erzeugt – bitte warten oder abbrechen.")
        if agent.busy():
            raise HTTPException(409, "Jarvis arbeitet gerade – bitte kurz warten.")
        # Vorlagen in Reihenfolge (Bild 1, 2, 3): {"file": Galerie-Name} oder {"data": data:-URL eines hochgeladenen Bilds}
        sources = list(data.get("refs") or [])
        sources += [{"file": n} for n in data.get("ref_files") or []] + [{"data": d} for d in data.get("ref_images") or []]
        refs: list[bytes] = []
        for src in sources:
            if not isinstance(src, dict):
                continue
            if src.get("file"):
                path = images.path_of(str(src["file"]))
                if not path:
                    raise HTTPException(400, f"Bild nicht gefunden: {src['file']}")
                refs.append(path.read_bytes())
            elif src.get("data"):
                try:
                    refs.append(base64.b64decode(str(src["data"]).split(",", 1)[-1]))
                except ValueError:
                    raise HTTPException(400, "Bild nicht lesbar") from None
        from .imagegen import MAX_REFS
        if len(refs) > MAX_REFS or sum(len(r) for r in refs) > 40_000_000:
            raise HTTPException(400, f"Höchstens {MAX_REFS} Vorlagen bis zusammen 40 MB")

        async def progress(state: dict) -> None:
            await hub.broadcast({"type": "image_progress", **state})

        async def run() -> None:
            try:
                saved = await images.generate(
                    str(data.get("prompt") or ""), negative=str(data.get("negative") or ""),
                    size=str(data.get("size") or ""), steps=int(data.get("steps") or 0),
                    cfg_scale=float(data.get("cfg_scale") or 0), seed=int(data.get("seed", -1) or -1),
                    count=int(data.get("count") or 1), ref_images=refs, progress=progress,
                    megapixels=float(data.get("megapixels") or 0))
                await hub.broadcast({"type": "image_done", "images": saved})
            except ImageError as e:
                await hub.broadcast({"type": "image_error", "text": str(e)})
            except Exception as e:  # noqa: BLE001
                log.exception("Bild fehlgeschlagen")
                await hub.broadcast({"type": "image_error", "text": str(e)})
        image_task[:] = [asyncio.create_task(run())]
        return JSONResponse({"started": True}, status_code=202)

    @app.post("/api/image/cancel")
    async def image_cancel():
        return {"ok": await images.cancel()}

    @app.post("/api/image/release")
    async def image_release():
        """Bild-Modus verlassen: Bildmodell entladen, Sprachmodell zurück (im Hintergrund)."""
        if images.loaded() and not images.lock.locked():
            background.append(asyncio.create_task(images.release()))
        return {"ok": True}

    @app.post("/api/image/telegram/{name}")
    async def image_telegram(name: str):
        path = images.path_of(name)
        if not path:
            raise HTTPException(404, "Bild nicht gefunden")
        bot = agent.services.get("telegram")
        if bot is None:
            raise HTTPException(400, "Telegram ist nicht eingerichtet")
        try:
            await bot.send_file(path.read_bytes(), path.name)
        except Exception as e:  # noqa: BLE001
            raise HTTPException(502, f"Senden fehlgeschlagen: {bot.redact(e)}") from e
        return {"ok": True}

    @app.get("/api/reminders")
    async def get_reminders():
        return [{"id": r.id, "text": r.text, "due": r.due, "kind": r.kind} for r in reminders.upcoming()]

    @app.delete("/api/reminders/{rid}")
    async def delete_reminder(rid: str):
        if not reminders.cancel(rid):
            raise HTTPException(404, "Erinnerung nicht gefunden")
        return {"ok": True}

    @app.get("/api/metrics")
    async def get_metrics():
        return {**await asyncio.to_thread(metrics.collect), "usage": usage.summary(cfg)}

    @app.get("/api/memory/days")
    async def memory_days():
        summaries = set(memory.summaries.days())
        return [{"day": d, "summary": d in summaries} for d in memory.journal.days()]

    @app.get("/api/memory/day/{day}")
    async def memory_day(day: str):
        if not valid_day(day):
            raise HTTPException(400, "ungültiges Datum")
        return {"day": day, "journal": memory.journal.read(day), "summary": memory.summaries.read(day)}

    @app.get("/api/memory/facts")
    async def memory_facts():
        return [{"fact": f, "day": d} for f, d in memory.facts.list()]

    @app.get("/api/history")
    async def history():
        """Der ganze sichtbare Chat (die letzten 40 Nachrichten) – auch komprimierte Teile; dazu, wo eine
        Komprimierung stattfand (Trenner mit aufklappbarer Zusammenfassung)."""
        from . import attachments
        conv = memory.active
        shown = [(i, m) for i, m in enumerate(conv.history)
                 if m["role"] in ("user", "assistant")
                 and (m.get("content") or m.get("attachments") or m.get("docs"))][-40:]
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
                              "editable": i >= start,
                              **({"images": m["attachments"]} if m.get("attachments") else {}),
                              **({"files": [attachments.doc_info(d) for d in m["docs"]]} if m.get("docs") else {})}
                             for i, m in shown],
                "turns": max(turns.values(), default=0), "epochs": epochs}

    @app.post("/api/chats/active/revert")
    async def revert_turn(request: Request):
        """Coding-Modus: die Dateiänderungen einer Antwort zurücksetzen (oder mit redo wiederherstellen) – wie /undo
        bei OpenCode. Betrifft nur die Dateien, die sich in dieser Antwort geändert haben."""
        from . import snapshot
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

    # ---------------------------------------------------------------- Sandbox (Coding)
    def coding_chat() -> None:
        if memory.mode != "coding":
            raise HTTPException(409, T("Nur im Coding-Modus.", "Only in coding mode."))

    def sandbox_json() -> dict:
        from . import sandbox as sbx
        from . import workspace
        sb = agent.sandbox_state()
        out = {"enabled": bool(sb), **sbx.status(cfg, sb)}
        if sb:
            out.update(network=sb.get("network", True), path=sb["path"], project=sb.get("project", ""),
                       branch=workspace.head_label(sb["path"]) or sb.get("branch", ""),
                       changes=len(workspace.changes(sb)))
            from . import github
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
        from . import github
        path = agent.project_dir() if memory.mode == "coding" else None
        if not path:
            return {"path": "", "repo": None, "branch": None, "sandbox": False}

        sb = agent.sandbox_state()

        def read() -> tuple:
            if sb:  # Arbeitskopie: kein git in ihrem .git (siehe github.push_from_copy)
                from . import workspace
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
        return {"path": path, "repo": repo, "branch": branch or None, "sandbox": bool(sb)}

    @app.get("/api/sandbox")
    async def sandbox_get():
        coding_chat()
        return await asyncio.to_thread(sandbox_json)

    @app.post("/api/sandbox")
    async def sandbox_set(request: Request):
        """{enabled}: ein-/ausschalten (aus = Container und Arbeitskopie weg); {network}: Netz an/aus."""
        from . import sandbox as sbx
        from . import workspace
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
        except (sbx.SandboxError, workspace.WorkspaceError, OSError) as e:
            raise HTTPException(400, str(e)) from e
        await chat_switched()
        return await asyncio.to_thread(sandbox_json)

    @app.get("/api/sandbox/changes")
    async def sandbox_changes(path: str = ""):
        from . import workspace
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
        from . import workspace
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

    # ---------------------------------------------------------------- GitHub
    @app.get("/api/github/status")
    async def github_status():
        from . import github
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
        from . import github
        try:
            return {"repos": await github.repos(cfg, q)}
        except github.GitHubError as e:
            raise HTTPException(400, str(e)) from e

    @app.post("/api/github/clone")
    async def github_clone(request: Request):
        """Repo klonen und zum Projekt des offenen Coding-Chats machen."""
        from . import github
        coding_chat()
        not_busy()
        repo = str((await request.json()).get("repo", "")).strip()
        try:
            path = await asyncio.to_thread(github.clone, cfg, repo)
            await agent.switch_project(path)
        except (github.GitHubError, OSError) as e:
            raise HTTPException(400, str(e)) from e
        await chat_switched()
        await hub.broadcast({"type": "chats_changed"})
        return {"project": path}

    @app.post("/api/github/push")
    async def github_push():
        """Knopf „Zu GitHub pushen“ (der Klick ist die Freigabe): aktuellen Branch der Arbeitskopie bzw. des Projekts."""
        from . import github
        coding_chat()
        not_busy()
        path = agent.project_dir()
        if not path:
            raise HTTPException(409, T("Kein Projektordner gewählt.", "No project folder chosen."))
        sb = agent.sandbox_state()
        try:
            if sb:
                return await asyncio.to_thread(github.push_from_copy, cfg, sb["path"], github.sandbox_origin(sb))
            return await asyncio.to_thread(github.push, cfg, path)
        except (github.GitHubError, OSError) as e:
            raise HTTPException(400, str(e)) from e

    @app.post("/api/jobs/{job_id}/stop")
    async def stop_job(job_id: str):
        from .tools import jobs
        job = jobs.stop(job_id)
        if not job:
            raise HTTPException(404, T("Unbekannter Job.", "Unknown job."))
        return job.info()

    def user_turns(conv) -> dict[int, int]:
        """Index im Verlauf → Nummer der Nutzerfrage (1, 2, …), zu der die Nachricht gehört (Antworten zählen zur
        Frage davor) – so kann die Oberfläche „Frage 3 bearbeiten / neu generieren“ sagen."""
        out, n = {}, 0
        for i, m in enumerate(conv.history):
            if m["role"] == "user":
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
        from . import attachments
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
            raise HTTPException(409, "Jarvis arbeitet gerade – bitte kurz warten oder STOP drücken")

    def settings_info() -> dict:
        """Was der Server gerade anwendet – alle Fenster zeigen dasselbe (Handy und PC, Telegram nutzt es auch).
        think: None, solange noch keine Oberfläche es gesetzt hat; root: Auto-Erleichterungen sind dann aus."""
        think = hub.think
        return {"auto_mode": agent.auto_mode, "root": agent_safety.running_as_root(),
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
        return {"mode": conv.meta.get("mode") or "tools", "project": conv.meta.get("project") or "",
                "chat_id": conv.chat_id,
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

    # ---------- Modi: Tools (Assistent) und Coding ----------
    mode_models_file = memory.chats.dir / "mode-models.json"

    def mode_models() -> dict:
        try:
            return json.loads(mode_models_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def remember_mode_model(mode: str, name: str) -> None:
        data = mode_models()
        data[mode] = name
        mode_models_file.write_text(json.dumps(data), encoding="utf-8")

    @app.post("/api/mode")
    async def set_mode(request: Request):
        mode = str((await request.json()).get("mode", ""))
        if mode not in ("tools", "coding"):
            raise HTTPException(400, "Unbekannter Modus")
        not_busy()
        old = mode_info()["mode"]
        if isinstance(llm, LLMRouter) and old != mode and old not in mode_models():
            remember_mode_model(old, llm.active)  # damit der Rückweg wieder das bisherige Modell nimmt
        memory.switch_mode(mode)
        await chat_switched()
        await hub.broadcast({"type": "chats_changed"})
        # Eigenes Modell für diesen Modus gewählt? Dann im Hintergrund umschalten
        target = mode_models().get(mode)
        if isinstance(llm, LLMRouter) and target and target != llm.active and target in llm.profiles:
            await llm.set_mode(mode, restart=False)  # das neue Modell startet gleich mit dem Fenster des Modus
            background.append(asyncio.create_task(switch_model_quietly(target)))
        elif isinstance(llm, LLMRouter):
            background.append(asyncio.create_task(switch_context_quietly(mode)))
        return mode_info()

    async def switch_context_quietly(mode: str) -> None:
        """Eigenes Kontextfenster des Modus: Server des aktiven Modells damit neu starten (wenn es abweicht)."""
        wanted = llm.wanted_context(llm.active, mode)
        if not wanted or wanted == llm.profile.num_ctx or llm.profile.backend == "ollama" and not llm.profile.server:
            await llm.set_mode(mode)  # Ollama: neues num_ctx gilt ab der nächsten Anfrage, kein Neustart nötig
            return
        name = llm.active
        await hub.broadcast({"type": "model_switching", "name": name, "label": llm.profile.label})
        llm.switching = name
        try:
            async with agent.lock:
                await llm.set_mode(mode, model_progress)
        except LLMError as e:
            await hub.broadcast({"type": "model_error", "name": name, "text": str(e)})
            return
        finally:
            llm.switching = None
            await idle_if_free()
        await hub.broadcast({"type": "model_active", "name": name})
        agent._cache_owner = None
        agent.build_messages()
        await hub.broadcast({"type": "context", **(agent.last_context or {})})
        await hub.broadcast({"type": "models_changed"})
        hub.prewarm_soon()

    async def switch_model_quietly(name: str) -> None:
        with contextlib.suppress(HTTPException):
            await models_api.activate_model(name)

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
        not_busy()
        memory.new_chat()
        await chat_switched()
        return {"id": memory.active.chat_id}

    @app.post("/api/chats/{chat_id}/activate")
    async def chat_activate(chat_id: str):
        chat_or_404(chat_id)
        not_busy()
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
            not_busy()  # weder den offenen Chat noch den einer laufenden Routine/Telegram-Anfrage
        from . import sandbox as sbx
        from . import workspace
        await asyncio.to_thread(sbx.remove, cfg, sbx.container_name(chat_id))  # Sandbox des Chats wegräumen
        await asyncio.to_thread(workspace.discard_chat, chat_id)
        days = memory.forget_chat(chat_id)
        heal_index_soon()
        if was_active:
            await chat_switched()
        await hub.broadcast({"type": "chats_changed"})
        return {"ok": True, "days": days}

    def routine_json(r) -> dict:
        return routines.to_dict(r, datetime.now(), cfg.language == "en")

    async def routine_body(request: Request) -> dict:
        body = await request.json()
        if not isinstance(body, dict):
            raise HTTPException(400, "JSON-Objekt erwartet")
        return body

    @app.get("/api/routines")
    async def routines_list():
        return [routine_json(r) for r in routines.items]

    @app.post("/api/routines")
    async def routines_add(request: Request):
        b = await routine_body(request)
        try:
            r = routines.add(str(b.get("name", "")), str(b.get("task", "")), str(b.get("time", "")),
                             b.get("days") or [], str(b.get("date") or ""))
        except (ValueError, TypeError) as e:
            raise HTTPException(400, str(e)) from e
        return routine_json(r)

    @app.put("/api/routines/{rid}")
    async def routines_update(rid: str, request: Request):
        b = await routine_body(request)
        try:
            r = routines.update(rid, **{k: b[k] for k in ("name", "task", "time", "days", "date", "enabled") if k in b})
        except KeyError as e:
            raise HTTPException(404, "Unbekannte Routine") from e
        except (ValueError, TypeError) as e:
            raise HTTPException(400, str(e)) from e
        return routine_json(r)

    @app.delete("/api/routines/{rid}")
    async def routines_delete(rid: str):
        if not routines.delete(rid):
            raise HTTPException(404, "Unbekannte Routine")
        return {"ok": True}

    @app.post("/api/routines/{rid}/run")
    async def routines_run(rid: str):
        if routines.get(rid) is None:
            raise HTTPException(404, "Unbekannte Routine")
        return {"ok": start_routine(rid)}

    # ---------- Skills: einrichten, an-/abschalten (Werte in state.json, Geheimnisse im Schlüsselbund) ----------
    def skills_changed() -> None:
        agent.reload_tools()
        hub.prewarm_soon()

    @app.get("/api/skills")
    async def skills_get():
        st = await asyncio.to_thread(skill_store.status, cfg)
        st["plaintext"] = skill_store.plaintext_left(cfg)
        return st

    @app.post("/api/skills/{skill_id}")
    async def skills_save(skill_id: str, request: Request):
        body = await request.json()
        if skill_id not in skill_store.BY_ID or not isinstance(body, dict):
            raise HTTPException(404 if skill_id not in skill_store.BY_ID else 400, T("Unbekannter Skill", "Unknown skill"))
        values = body.get("values") if isinstance(body.get("values"), dict) else None
        enabled = body.get("enabled") if isinstance(body.get("enabled"), bool) else None
        try:
            await asyncio.to_thread(skill_store.save, cfg, skill_id, enabled, values)
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
        skills_changed()
        await hub.broadcast({"type": "skills_changed", "id": skill_id})
        return {"ok": True}

    @app.post("/api/skills/{skill_id}/test")
    async def skills_test(skill_id: str, request: Request):
        if skill_id not in skill_store.BY_ID:
            raise HTTPException(404, T("Unbekannter Skill", "Unknown skill"))
        body = await request.json()
        values = body.get("values") if isinstance(body, dict) and isinstance(body.get("values"), dict) else None
        try:
            return await skill_store.test(cfg, skill_id, values)
        except ValueError as e:
            return {"ok": False, "message": str(e)}

    @app.delete("/api/skills/{skill_id}")
    async def skills_remove(skill_id: str):
        if skill_id not in skill_store.BY_ID:
            raise HTTPException(404, T("Unbekannter Skill", "Unknown skill"))
        await asyncio.to_thread(skill_store.remove, cfg, skill_id)
        skills_changed()
        await hub.broadcast({"type": "skills_changed", "id": skill_id})
        return {"ok": True}

    @app.get("/api/briefing")
    async def briefing_get():
        s, why = briefing.settings(cfg), briefing.availability(cfg)
        en = cfg.language == "en"
        return {"settings": s.model_dump(), "customized": s != cfg.briefing,
                "sections": [{"id": k, "label": briefing.LABELS[k][1 if en else 0], "note": why[k]}
                             for k in BRIEFING_SECTIONS]}

    @app.put("/api/briefing")
    async def briefing_put(request: Request):
        body = await request.json()
        if not isinstance(body, dict):
            raise HTTPException(400, "JSON-Objekt erwartet")
        allowed = set(BriefingConfig.model_fields)
        try:
            s = briefing.save_settings(cfg, {k: v for k, v in body.items() if k in allowed})
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
        return {"ok": True, "settings": s.model_dump()}

    @app.delete("/api/briefing")
    async def briefing_reset():
        return {"ok": True, "settings": briefing.save_settings(cfg, None).model_dump()}

    @app.post("/api/briefing/preview")
    async def briefing_preview():
        ctx = ToolContext(cfg=cfg, memory=memory, services=agent.services)
        return {"text": await briefing.build_briefing(ctx)}

    @app.get("/api/voices")
    async def voices():
        if not tts:
            return {"available": False, "voices": [], "languages": []}
        return {"available": True, "current": tts.voice, "language": tts.reply_language,
                "voices": pocket.voice_list(cfg.language, tts.voice, tts.languages),
                "languages": pocket.language_list(cfg.language == "en"), "pocket": tts.pocket.describe()}

    @app.post("/api/pocket/install")
    async def install_pocket():
        """Pocket TTS installieren (Programm von GitHub), danach im Hintergrund starten – das lädt beim ersten Mal
        Modell und Stimme von Hugging Face."""
        if not tts:
            raise HTTPException(400, "Sprachausgabe ist deaktiviert")
        lines: list[str] = []
        if not await asyncio.to_thread(pocket.install, tts.pocket.data_dir, lines.append):
            raise HTTPException(502, (lines[-1] if lines else "Installation fehlgeschlagen").lstrip("✘ "))
        tts.pocket.warm(tts.voice)
        await startup.set("tts", "ok", f"Pocket TTS · {pocket.BY_NAME[tts.voice].label}")
        return {"ok": True, "pocket": tts.pocket.describe()}

    @app.delete("/api/pocket")
    async def remove_pocket():
        if not tts:
            raise HTTPException(400, "Sprachausgabe ist deaktiviert")
        await asyncio.to_thread(tts.pocket.stop)
        freed = await asyncio.to_thread(pocket.remove, tts.pocket.data_dir)
        await startup.set("tts", "warn", T("Browser-Stimme", "browser voice"))
        return {"ok": True, "freed_mb": round(freed / 1e6)}

    @app.post("/api/voices/select")
    async def select_voice(request: Request):
        """Stimme wählen, optional mit ihrer Sprache – die gilt dann auch für die Antworten."""
        data = await request.json()
        if not tts:
            raise HTTPException(400, "Sprachausgabe ist deaktiviert")
        voice, language = str(data.get("voice") or ""), (str(data["language"]) if data.get("language") else None)
        if not await asyncio.to_thread(tts.select, voice, language):
            raise HTTPException(400, "Unbekannte Stimme oder Sprache")
        apply_reply_language()
        await hub.broadcast({"type": "voice_changed", "voice": tts.voice, "language": tts.reply_language})
        return {"ok": True, "current": tts.voice, "language": tts.reply_language}

    @app.get("/api/voices/{name}/preview")
    async def preview_voice(name: str, text: str = ""):
        if not tts or name not in pocket.BY_NAME or not tts.available():
            raise HTTPException(404, "Stimme nicht verfügbar")
        lang = tts.language_of(name)
        if lang != tts.reply_language:  # der Server hat nur das Modell einer Sprache geladen
            en = cfg.language == "en"
            raise HTTPException(409, T(
                f"{pocket.BY_NAME[name].label} spricht {pocket.language_name(lang, en)}, geladen ist gerade "
                f"{pocket.language_name(tts.reply_language, en)} – zum Anhören die Stimme wählen.",
                f"{pocket.BY_NAME[name].label} speaks {pocket.language_name(lang, en)}, but "
                f"{pocket.language_name(tts.reply_language, en)} is loaded – select the voice to hear it."))
        sample = text.strip()[:200] or pocket.SAMPLES.get(lang, pocket.SAMPLES["en"])
        try:
            wav = await asyncio.to_thread(tts.synth, sample, name)
        except Exception as e:  # noqa: BLE001
            raise HTTPException(502, f"Probe fehlgeschlagen: {e}") from e
        return Response(wav, media_type="audio/wav")

    @app.websocket("/ws")
    async def ws_endpoint(ws: WebSocket):
        origin = ws.headers.get("origin", "")
        host = re.sub(r"^https?://", "", origin)
        # Schutz vor Cross-Site-WebSocket-Hijacking: fremde Webseiten dürfen keine Befehle senden
        if not check_host(host, cfg.port) or not check_host(ws.headers.get("host"), cfg.port):
            await ws.close(code=4403)
            return
        await ws.accept()
        client = Client(ws)
        client.audio = AudioSession(cfg.voice, stt, wake, client.send, lambda t: hub.submit(t, "voice"))
        hub.clients.add(client)
        await client.send({"type": "hello", "busy": agent.busy(),
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
                    from . import attachments
                    names = [str(n) for n in (data.get("images") or [])[:attachments.MAX_PER_MESSAGE]
                             if attachments.path_of(cfg, str(n))]
                    await hub.submit(str(data.get("text", "")), images=names)
                elif t == "confirm":
                    changes = data.get("args") if isinstance(data.get("args"), dict) else None
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

    app.mount("/static", FreshStaticFiles(directory=WEB_DIR), name="static")
    return app
