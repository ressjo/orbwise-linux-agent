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
import tarfile
import time
import zipfile
from datetime import datetime
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from . import askpass, metrics, prompts
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
from .voice import catalog
from .voice.listen import AudioSession, WakeWordFactory, WhisperSTT
from .voice.tts import PiperTTS, Speaker
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
              "edit_file": ("call_edit", "path"),
              "mail_send": ("call_mail", "to")}


def spoken_confirm(name: str, args: dict, cfg=None) -> str:
    """Gesprochene Rückfrage: Befehle werden nicht vorgelesen (sie stehen im Dialog), Pfade nur als Dateiname."""
    if name == "run_shell":
        return prompts.spoken(cfg, "confirm_shell")
    if name == "mail_send":
        return prompts.spoken(cfg, "confirm_mail", v=str(args.get("to", "")))
    if name in ("write_file", "edit_file") and args.get("path"):
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
    def __init__(self, cfg: Config, agent: Agent, speaker_tts: PiperTTS | None,
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
                await self.agent.prewarm(self.broadcast)
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
        await self.broadcast({"type": "user", "text": text, "source": source, "images": images or []})
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
    fake = env("FAKE_LLM") == "1"
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
        tts = PiperTTS(cfg.voice)
    hub = Hub(cfg, agent, tts, stt, wake)
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

    async def warm_voice() -> None:
        """Stimme, Spracherkennung und Wake-Word im Hintergrund laden – mit Anzeige, was gerade lädt."""
        if not cfg.voice.enabled:
            off = T("aus (voice.enabled)", "off (voice.enabled)")
            for key in ("stt", "tts", "wake"):
                await startup.set(key, "off", off)
            return
        if tts is None or not await asyncio.to_thread(tts.available):
            await startup.set("tts", "warn", T("Browser-Stimme", "browser voice") + (f" – {tts.error}" if tts and tts.error else ""))
        else:
            await startup.set("tts", "running", T("lade Stimme ", "loading voice ") + tts.current + " …")
            try:
                await asyncio.to_thread(tts._load, tts.current)
                await startup.set("tts", "ok", "Piper · " + tts.current)
            except Exception as e:  # noqa: BLE001
                await startup.set("tts", "warn", T("Browser-Stimme – ", "browser voice – ") + str(e))
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

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI):
        hub.speaker.start()
        await startup.set("memory", "ok", memory_summary())
        heal_index_soon()
        if isinstance(llm, LLMRouter):
            background.append(asyncio.create_task(start_model()))
        else:
            await startup.set("model", "ok", T("Demo-Modus", "demo mode"))
        background.append(asyncio.create_task(warm_voice()))
        background.append(asyncio.create_task(watch_telegram()))
        background.append(asyncio.create_task(summary_loop()))
        background.append(asyncio.create_task(metrics_loop()))
        background.append(asyncio.create_task(reminder_loop()))
        background.append(asyncio.create_task(routine_loop()))
        if telegram_bot is not None:
            background.append(asyncio.create_task(telegram_bot.serve()))
        yield
        for t in [*background, *healing, *side_tasks]:
            t.cancel()
        from .tools.ssh import close_all as close_ssh
        await close_ssh()
        if images.server is not None:
            await images.server.stop()  # Bildserver nicht verwaist laufen lassen  # offene SSH-Verbindungen schließen (sonst bleiben sie bis zum Zeitlimit angemeldet)
        await hub.speaker.close()
        await llm.close()
        memory.close()

    async def model_progress(text: str) -> None:
        await hub.broadcast({"type": "model_progress", "text": text})
        if startup.steps["model"]["state"] == "running":
            await startup.set("model", "running", text)

    async def idle_if_free() -> None:
        """Oberfläche auf „bereit“ setzen – nur, wenn gerade keine Anfrage läuft."""
        if not agent.busy():
            await hub.broadcast({"type": "state", "state": "idle"})

    async def start_model() -> None:
        # Beim Start das aktive Profil vorbereiten (z. B. llama-server starten); Chats warten so lange
        name = llm.active
        label = llm.profile.label or llm.profile.model
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
                    await hub.broadcast({"type": "metrics", **data})
                except Exception as e:  # noqa: BLE001
                    log.debug("Telemetrie fehlgeschlagen: %s", e)
            await asyncio.sleep(2)

    async def summary_loop() -> None:
        await asyncio.sleep(30)
        while True:
            idle = time.time() - memory.last_activity >= min(10, cfg.memory.summarize_idle_minutes) * 60
            if (idle and cfg.memory.learning and not agent.lock.locked() and not images.loaded()
                    and not getattr(llm, "switching", None) and memory.lessons.pending()):
                try:  # im Leerlauf über Fehler, Korrekturen und 👎 nachdenken (kurze Lektionen)
                    async with agent.lock:
                        learned = await agent.reflect_lessons()
                    if learned:
                        log.info("Aus Erfahrung gelernt: %d Lektion(en)", learned)
                        await hub.broadcast({"type": "lessons_changed"})
                        if hub.clients:
                            hub.prewarm_soon()
                except Exception as e:  # noqa: BLE001
                    log.warning("Nachdenken über Fehler fehlgeschlagen: %s", e)
            if not agent.lock.locked():
                try:
                    done = await memory.summarize_pending()
                    if done:
                        log.info("Tageszusammenfassungen erstellt: %s", ", ".join(done))
                        agent._cache_owner = None  # der Server hatte dafür andere Texte im Cache
                        if hub.clients:
                            hub.prewarm_soon()
                except Exception as e:  # noqa: BLE001
                    log.warning("Zusammenfassungen fehlgeschlagen: %s", e)
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

    @app.get("/api/models")
    async def get_models():
        if not isinstance(llm, LLMRouter):
            return {"active": "demo", "switching": None,
                    "profiles": [{"name": "demo", "label": "Demo (Fake-LLM)", "backend": "fake", "model": "fake",
                                  "base_url": "", "managed": False, "active": True}]}
        from . import bonsai as bonsai_mod
        from . import llamacpp as llamacpp_mod
        from . import models as mdl
        profiles = llm.describe()
        sizes: dict[str, int] = {}
        # Größen nur von Ollama-Servern holen, die ein Profil wirklich nutzt (llama-server kennt /api/tags nicht)
        for base in {p.get("base_url") or cfg.llm.base_url for p in profiles if p["backend"] == "ollama"}:
            try:
                async with httpx.AsyncClient(base_url=base, timeout=2) as client:
                    for m in (await client.get("/api/tags")).json().get("models", []):
                        sizes.setdefault(m.get("name", ""), int(m.get("size") or 0))
            except (httpx.HTTPError, ValueError):
                pass
        added = mdl.added_profiles(state_file)  # state.json nur einmal lesen
        added_slugs = {mdl.slug(t) for t in mdl.added_models(state_file)}
        for p in profiles:
            p["deletable"] = p["name"] in added or p["name"] in added_slugs
            size = sizes.get(p["model"]) or sizes.get(f"{p['model']}:latest") if p["backend"] == "ollama" else None
            if p["name"] in bonsai_mod.VARIANTS:
                size = sum(f.stat().st_size for f in bonsai_mod.model_files(variant=p["name"]))
            elif (local := llamacpp_mod.local_record(state_file, p["name"])) is not None:
                size = sum(Path(f).stat().st_size for f in local.get("files", []) if Path(f).is_file())
                p["engine"] = "llama.cpp"
                p["keeps_files"] = not local.get("downloaded")
            p["size_gb"] = round(size / 1e9, 1) if size else None
            if p["backend"] == "ollama" and mdl.is_hf(p["model"]):  # alter Weg → auf llama.cpp umstellen anbieten
                p["convert"] = p["model"]
        return {"active": llm.active, "switching": llm.switching, "profiles": profiles,
                "pulls": [{"tag": t, **pull_state.get(t, {})} for t in sorted(pulls)]}

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

    # ---------- Modelle hinzufügen (Vorauswahl + ollama pull mit Fortschritt) ----------
    state_file = cfg.memory.dir.parent / "state.json"
    pulls: dict[str, asyncio.Task] = {}
    pull_state: dict[str, dict] = {}  # letzter Fortschritt je Download (für das Modell-Menü)

    @app.get("/api/models/presets")
    async def model_presets():
        from . import models as mdl
        gpu = await asyncio.to_thread(mdl.detect_gpu)
        installed = await asyncio.to_thread(mdl.installed_models, cfg.llm.base_url)
        return {"gpu": gpu, "presets": mdl.preset_list(gpu["vram_gb"], installed),
                "pulling": sorted(pulls)}

    @app.get("/api/models/hf/search")
    async def hf_search(q: str = ""):
        from . import models as mdl
        if not q.strip():
            return {"results": []}
        tag = mdl.normalize_tag(q)
        if tag and mdl.is_hf(tag):  # Link eingefügt → gleich dieses Repo
            return {"results": [{"repo": tag[6:].split(":", 1)[0], "tag": tag if ":" in tag else None}]}
        try:
            return {"results": await asyncio.to_thread(mdl.hf_search, q)}
        except (httpx.HTTPError, ValueError) as e:
            raise HTTPException(502, f"Hugging Face nicht erreichbar: {e}") from e

    @app.get("/api/models/hf/quants")
    async def hf_quants(repo: str):
        from . import llamacpp
        from . import models as mdl
        gpu = await asyncio.to_thread(mdl.detect_gpu)
        try:
            items = await asyncio.to_thread(mdl.hf_quants, repo, gpu["vram_gb"], llamacpp.models_dir(cfg))
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
        except httpx.HTTPStatusError as e:
            raise HTTPException(404 if e.response.status_code in (401, 404) else 502,
                                "Repo nicht gefunden oder nicht öffentlich") from e
        except httpx.HTTPError as e:
            raise HTTPException(502, f"Hugging Face nicht erreichbar: {e}") from e
        return {"repo": repo, "gpu": gpu, "quants": items, "pulling": sorted(pulls)}

    # ---------- Eigene Modelle über llama.cpp (Hugging Face, ~/models) ----------
    llamacpp_lock = asyncio.Lock()
    add_lock = asyncio.Lock()  # Profile nacheinander anlegen – sonst bekämen zwei fertige Downloads denselben Port

    def hf_base() -> str:
        from . import models as mdl
        return mdl.HF_API[:-4] if mdl.HF_API.endswith("/api") else "https://huggingface.co"

    async def pull_event(tag: str, **data) -> None:
        pull_state[tag] = data
        await hub.broadcast({"type": "model_pull", "tag": tag, **data})

    async def ensure_llamacpp(tag: str, gpu: dict, update: bool = False) -> dict:
        """llama.cpp einrichten (einmalig) bzw. aktualisieren – Statuszeilen gehen als model_pull-Fortschritt raus."""
        from . import llamacpp
        async with llamacpp_lock:
            if llamacpp.installed(cfg) and not update:
                return llamacpp.manifest(cfg)
            loop = asyncio.get_running_loop()
            lines: list[str] = []

            def out(line: str) -> None:
                lines.append(line)
                asyncio.run_coroutine_threadsafe(pull_event(tag, status=line.strip()), loop)
            await pull_event(tag, status="llama.cpp wird eingerichtet …")
            try:
                result = await asyncio.to_thread(llamacpp.install, cfg, gpu, out, update)
            except (httpx.HTTPError, OSError, ValueError, zipfile.BadZipFile, tarfile.TarError) as e:
                raise LLMError(f"llama.cpp konnte nicht geladen werden: {e}") from e
            if not result:
                raise LLMError("llama.cpp konnte nicht eingerichtet werden" + (f": {lines[-1].strip()}" if lines else ""))
            return result

    async def run_job(tag: str, job) -> None:
        """Download/Einrichtung als Hintergrund-Aufgabe mit model_pull-Events (Fortschritt, fertig, Fehler, Abbruch)."""
        try:
            result = await job()
            await hub.broadcast({"type": "model_pull", "tag": tag, "done": True, **result})
        except asyncio.CancelledError:
            await hub.broadcast({"type": "model_pull", "tag": tag, "cancelled": True})
            raise
        except (httpx.HTTPError, LLMError, ValueError, OSError) as e:
            text = str(e)
            if isinstance(e, httpx.HTTPStatusError) and e.response.status_code in (401, 403):
                text = "Hugging Face verlangt eine Anmeldung für dieses Modell (HF_TOKEN setzen)"
            await hub.broadcast({"type": "model_pull", "tag": tag, "error": text or type(e).__name__})
        except Exception as e:  # noqa: BLE001 – unerwartet: trotzdem melden, sonst hinge die Anzeige
            log.exception("Modell-Aufgabe %s fehlgeschlagen", tag)
            await hub.broadcast({"type": "model_pull", "tag": tag, "error": f"{type(e).__name__}: {e}"})
        finally:
            pulls.pop(tag, None)
            pull_state.pop(tag, None)

    async def replace_profile(old: str, new: str) -> str:
        """Altes Ollama-Profil (hf.co/…) durch das neue llama.cpp-Profil ersetzen: war es aktiv, das neue aktivieren;
        dann das alte entfernen und seine Ollama-Kopie löschen → Hinweis für die Meldung (leer = alles gut)."""
        from . import models as mdl
        if old not in llm.profiles or not mdl.removable(state_file, old):
            return ""
        note = ""
        if llm.active == old:
            try:
                await activate_model(new)
            except HTTPException as e:
                return f"Neues Modell angelegt, startet aber nicht: {e.detail}"
        model = llm.profiles[old].model
        await llm.remove_profile(old)
        mdl.unregister_model(state_file, old)
        if not any(p.backend == "ollama" and p.model == model for p in llm.profiles.values()):
            try:
                async with httpx.AsyncClient(base_url=cfg.llm.base_url, timeout=30) as client:
                    await client.request("DELETE", "/api/delete", json={"model": model})
            except httpx.HTTPError as e:
                note = f"Ollama-Kopie nicht gelöscht: {e}"
        await hub.broadcast({"type": "models_changed"})
        return note

    async def pull_gguf(tag: str, replace: str = "") -> dict:
        """hf.co/<repo>:<quant> → Dateien nach ~/models laden, llama.cpp bereitstellen, Profil anlegen. replace: altes
        Ollama-Profil desselben Modells, das danach wegfällt."""
        from . import llamacpp
        from . import models as mdl
        repo, _, quant = tag[6:].partition(":")
        gpu = await asyncio.to_thread(mdl.detect_gpu)
        items = await asyncio.to_thread(mdl.hf_quants, repo, gpu["vram_gb"], llamacpp.models_dir(cfg))
        item = next((x for x in items if x["quant"].lower() == quant.lower()), None) if quant else \
            next((x for x in items if x.get("recommended")), items[0] if items else None)
        if not item:
            raise LLMError(f"Keine GGUF-Variante {quant} in {repo} gefunden".replace("  ", " "))
        await ensure_llamacpp(tag, gpu)
        last = 0.0

        async def progress(done: int, total: int) -> None:
            nonlocal last
            now = time.monotonic()
            if now - last > 0.5 or done >= total:
                last = now
                await pull_event(tag, status="downloading", completed=done, total=total)
        first, mmproj, _ = await llamacpp.download_quant(cfg, repo, item, progress, hf_base())
        async with add_lock:
            name = await asyncio.to_thread(llamacpp.add_model, cfg, state_file, first, mmproj, gpu, llm.profiles,
                                           True, repo)
            llm.add_downloaded_models()
        if replace:
            note = await replace_profile(replace, name)
            return {"profile": name, "replaced": replace, **({"note": note} if note else {})}
        return {"profile": name}

    async def add_local(tag: str, path: Path) -> dict:
        from . import llamacpp
        from . import models as mdl
        gpu = await asyncio.to_thread(mdl.detect_gpu)
        await ensure_llamacpp(tag, gpu)
        async with add_lock:
            name = await asyncio.to_thread(llamacpp.add_model, cfg, state_file, path, None, gpu, llm.profiles, False)
            llm.add_downloaded_models()
        return {"profile": name}

    def start_job(tag: str, job) -> str:
        existing = next((k for k in pulls if k.lower() == tag.lower()), None)
        if existing:
            return existing
        pulls[tag] = asyncio.create_task(run_job(tag, job))
        return tag

    @app.get("/api/models/local")
    async def models_local():
        """GGUF-Dateien in ~/models (llm.models_dir) – mit dem Profil, das sie schon nutzt."""
        from . import llamacpp
        from . import models as mdl
        gpu = await asyncio.to_thread(mdl.detect_gpu)
        profiles = llm.profiles if isinstance(llm, LLMRouter) else {}
        items = await asyncio.to_thread(llamacpp.scan, cfg, profiles, gpu["vram_gb"])
        return {"dir": llamacpp._short(llamacpp.models_dir(cfg)), "items": items, "gpu": gpu, "pulling": sorted(pulls)}

    @app.post("/api/models/local")
    async def models_local_add(request: Request):
        from . import llamacpp
        if not isinstance(llm, LLMRouter):
            raise HTTPException(400, "Im Demo-Modus nicht verfügbar")
        raw = str((await request.json()).get("path", "")).strip()
        path = Path(os.path.expanduser(raw))
        if not raw or not path.is_file() or not path.name.lower().endswith(".gguf") or "mmproj" in path.name.lower():
            raise HTTPException(400, "Keine GGUF-Modelldatei gefunden: " + raw[:200])
        tag = start_job(llamacpp.label_for(path), lambda: add_local(llamacpp.label_for(path), path))
        return {"ok": True, "tag": tag}

    @app.get("/api/llamacpp")
    async def llamacpp_status():
        from . import llamacpp
        m = llamacpp.manifest(cfg)
        return {"installed": llamacpp.installed(cfg), "tag": m.get("tag"), "flavor": m.get("flavor"),
                "updating": "llama.cpp" in pulls}

    @app.post("/api/llamacpp/update")
    async def llamacpp_update():
        """Neuesten llama.cpp-Build laden – laufende Modelle nutzen ihn ab dem nächsten Start."""
        from . import models as mdl

        async def job() -> dict:
            gpu = await asyncio.to_thread(mdl.detect_gpu)
            m = await ensure_llamacpp("llama.cpp", gpu, update=True)
            return {"kind": "llamacpp", "version": m.get("tag"), "flavor": m.get("flavor")}
        start_job("llama.cpp", job)
        return {"ok": True}

    async def pull_model(tag: str) -> None:
        from . import models as mdl
        last = 0.0
        try:
            async with httpx.AsyncClient(base_url=cfg.llm.base_url, timeout=None) as client:
                async with client.stream("POST", "/api/pull", json={"model": tag, "stream": True}) as resp:
                    if resp.status_code != 200:
                        raise LLMError(f"Ollama {resp.status_code}: {(await resp.aread()).decode(errors='replace')[:200]}")
                    async for line in resp.aiter_lines():
                        if not line.strip():
                            continue
                        ev = json.loads(line)
                        if ev.get("error"):
                            raise LLMError(ev["error"])
                        now = time.monotonic()
                        if now - last > 0.5 or ev.get("status") == "success":
                            last = now
                            pull_state[tag] = {"status": ev.get("status", ""), "completed": ev.get("completed"),
                                               "total": ev.get("total")}
                            await hub.broadcast({"type": "model_pull", "tag": tag, **pull_state[tag]})
            name = mdl.register_model(state_file, tag)
            if isinstance(llm, LLMRouter):
                llm.add_downloaded_models()
            await hub.broadcast({"type": "model_pull", "tag": tag, "done": True, "profile": name})
        except asyncio.CancelledError:
            # Stream geschlossen → Ollama bricht ab; Teilstücke bleiben im Ollama-Cache (erneuter Start setzt fort)
            await hub.broadcast({"type": "model_pull", "tag": tag, "cancelled": True})
            raise
        except (httpx.HTTPError, LLMError, ValueError) as e:
            text = str(e) if not isinstance(e, httpx.ConnectError) else "Ollama ist nicht erreichbar"
            await hub.broadcast({"type": "model_pull", "tag": tag, "error": text})
        finally:
            pulls.pop(tag, None)
            pull_state.pop(tag, None)

    @app.post("/api/models/pull")
    async def model_pull(request: Request):
        from . import models as mdl
        if not isinstance(llm, LLMRouter):
            raise HTTPException(400, "Im Demo-Modus nicht verfügbar")
        body = await request.json()
        tag = mdl.normalize_tag(str(body.get("tag", "")))
        if not tag:
            raise HTTPException(400, "Ungültiger Modellname oder Hugging-Face-Link (geteilte GGUF-Dateien gehen nicht)")
        if tag in mdl.BY_TAG and mdl.BY_TAG[tag].kind != "ollama":
            raise HTTPException(400, prompts.spoken(cfg, "setup_terminal", cmd=f"orbwise model add {tag}"))
        if mdl.is_hf(tag):  # Hugging Face: Orbwise lädt die GGUF-Datei selbst und startet sie mit llama.cpp
            replace = str(body.get("replace") or "")  # altes hf.co-Ollama-Profil umstellen
            return {"ok": True, "tag": start_job(tag, lambda: pull_gguf(tag, replace))}
        if tag not in pulls:
            pulls[tag] = asyncio.create_task(pull_model(tag))
        return {"ok": True, "tag": tag}

    @app.delete("/api/models/pull/{tag:path}")
    async def model_pull_cancel(tag: str):
        task = pulls.get(tag) or next((t for k, t in pulls.items() if k.lower() == tag.lower()), None)
        if task is None:
            raise HTTPException(404, "Kein laufender Download")
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        return {"ok": True}

    @app.delete("/api/models/{name}")
    async def model_delete(name: str):
        """Per Oberfläche/CLI hinzugefügtes Modell entfernen und seine Dateien löschen (gibt Speicher frei)."""
        from . import bonsai as bonsai_mod
        from . import llamacpp as llamacpp_mod
        from . import models as mdl
        if not isinstance(llm, LLMRouter):
            raise HTTPException(400, "Im Demo-Modus nicht verfügbar")
        if name not in llm.profiles:
            raise HTTPException(404, "Unbekanntes Modell")
        if not mdl.removable(state_file, name):
            raise HTTPException(400, "Dieses Modell ist in der config.yaml eingetragen – dort entfernen.")
        profile = llm.profiles[name]
        if name == llm.active or llm.switching:
            raise HTTPException(409, "Das aktive Modell kann nicht gelöscht werden – erst ein anderes wählen.")
        if profile.backend == "ollama" and profile.model in pulls:
            raise HTTPException(409, "Das Modell wird gerade geladen – erst den Download abbrechen.")
        await llm.remove_profile(name)
        mdl.unregister_model(state_file, name)
        freed = 0
        if name in bonsai_mod.VARIANTS:
            freed = await asyncio.to_thread(bonsai_mod.remove_model_files, None, name)
        elif llamacpp_mod.local_record(state_file, name) is not None:  # nur selbst geladene Dateien löschen
            freed = await asyncio.to_thread(llamacpp_mod.forget, cfg, state_file, name)
        elif profile.backend == "ollama" and not any(
                p.backend == "ollama" and p.model == profile.model for p in llm.profiles.values()):
            try:  # nur löschen, wenn kein anderes Profil dasselbe Ollama-Modell nutzt
                async with httpx.AsyncClient(base_url=cfg.llm.base_url, timeout=30) as client:
                    await client.request("DELETE", "/api/delete", json={"model": profile.model})
            except httpx.HTTPError as e:
                log.warning("Ollama-Modell %s nicht gelöscht: %s", profile.model, e)
        await hub.broadcast({"type": "models_changed"})
        return {"ok": True, "freed_gb": round(freed / 1e9, 1)}

    @app.post("/api/models/reload")
    async def models_reload():
        added = llm.add_downloaded_models() if isinstance(llm, LLMRouter) else []
        return {"added": added}

    @app.post("/api/models/{name}/activate")
    async def activate_model(name: str):
        if not isinstance(llm, LLMRouter):
            raise HTTPException(400, "Im Demo-Modus nicht verfügbar")
        if name not in llm.profiles:
            raise HTTPException(404, "Unbekanntes Profil")
        await hub.broadcast({"type": "model_switching", "name": name, "label": llm.profiles[name].label})
        llm.switching = name
        try:
            async with agent.lock:  # wartet, bis eine laufende Antwort fertig ist
                await llm.activate(name, model_progress)
        except LLMError as e:
            await hub.broadcast({"type": "model_error", "name": name, "text": str(e)})
            raise HTTPException(502, str(e)) from e
        finally:
            llm.switching = None
            await idle_if_free()
        await hub.broadcast({"type": "model_active", "name": name})
        remember_mode_model(mode_info()["mode"], name)  # gilt ab jetzt für diesen Modus
        agent._cache_owner = None  # neues Modell = leerer Cache → offenen Chat gleich einlesen
        hub.prewarm_soon()
        return {"ok": True, "active": llm.active}

    @app.get("/api/llm/memory")
    async def llm_memory():
        """Wo liegt der Kontext (VRAM/RAM), und wie groß darf das Kontextfenster werden?"""
        from . import llm_memory
        gpu = (await asyncio.to_thread(metrics.collect)).get("gpu")
        if not isinstance(llm, LLMRouter):
            return {"available": False}
        info, reason = await llm.memory_info()
        if not info:
            return {"available": False, "profile": llm.active, "ctx": llm.context_size, "reason": reason,
                    "managed": llm.profile.server is not None, "backend": llm.profile.backend}
        return {"available": True, "profile": llm.active, "backend": llm.profile.backend,
                **llm_memory.summary(info), "recommend": llm_memory.recommend(info, gpu), "gpu": gpu}

    # ---------- Erfahrungen (gelernte Lektionen) ----------
    @app.get("/api/lessons")
    async def lessons_list():
        store = memory.lessons
        items = sorted(store.lessons, key=lambda x: max(x.last_used, x.created), reverse=True)
        return {"enabled": cfg.memory.learning, "lessons": [x.public() for x in items],
                "pending": len(store.pending()), "max": store.max_count}

    @app.post("/api/lessons")
    async def lessons_add(request: Request):
        data = await request.json()
        try:
            lesson, merged = await memory.lessons.add(str(data.get("text") or ""),
                                                      [str(x) for x in data.get("scope") or []], "manual")
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
        return {"lesson": lesson.public(), "merged": merged}

    @app.patch("/api/lessons/{lesson_id}")
    async def lessons_update(lesson_id: str, request: Request):
        data = await request.json()
        scope = data.get("scope")
        lesson = await memory.lessons.update(lesson_id, data.get("text"),
                                             [str(x) for x in scope] if isinstance(scope, list) else None)
        if not lesson:
            raise HTTPException(404, "Lektion nicht gefunden")
        return {"lesson": lesson.public()}

    @app.delete("/api/lessons/{lesson_id}")
    async def lessons_delete(lesson_id: str):
        if not memory.lessons.delete(lesson_id):
            raise HTTPException(404, "Lektion nicht gefunden")
        return {"ok": True}

    # ---------- Angehängte Bilder im Chat ----------
    @app.post("/api/attachments")
    async def attachment_upload(request: Request):
        from . import attachments
        try:
            name = attachments.save_data_url(cfg, str((await request.json()).get("data") or ""))
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
        vision = await agent.llm.supports_images() if hasattr(agent.llm, "supports_images") else False
        return {"name": name, "url": f"/api/attachments/{name}", "vision": vision}

    @app.get("/api/attachments/{name}")
    async def attachment_file(name: str):
        from . import attachments
        path = attachments.path_of(cfg, name)
        if not path:
            raise HTTPException(404, "Bild nicht gefunden")
        return FileResponse(path, media_type=attachments.MIME.get(path.suffix, "image/png"),
                            headers={"Cache-Control": "private, max-age=86400"})

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

    @app.post("/api/context/test")
    async def context_test(quick: bool = False):
        """Kontext-Selbsttest gegen das aktive Modell (siehe context_test.py) – der offene Chat bleibt unverändert."""
        from .context_test import run_context_test
        if not isinstance(llm, LLMRouter):
            raise HTTPException(400, "Im Demo-Modus nicht verfügbar")
        if agent.busy() or llm.switching:
            raise HTTPException(409, "Gerade läuft eine Anfrage oder ein Modellwechsel – bitte gleich noch einmal.")
        gpu = (await asyncio.to_thread(metrics.collect)).get("gpu")
        async with agent.lock:
            result = await run_context_test(agent, llm, quick=quick, gpu=gpu)
        agent._cache_owner = None  # der Test hat den Cache des Servers belegt → offenen Chat neu einlesen
        hub.prewarm_soon()
        return result

    @app.post("/api/models/{name}/context")
    async def set_model_context(name: str, request: Request):
        if not isinstance(llm, LLMRouter):
            raise HTTPException(400, "Im Demo-Modus nicht verfügbar")
        if name not in llm.profiles:
            raise HTTPException(404, "Unbekanntes Profil")
        body = await request.json()
        try:
            ctx = int(body.get("ctx", 0))
        except (TypeError, ValueError):
            raise HTTPException(400, "ctx fehlt") from None
        mode = str(body.get("mode") or llm.mode)
        if mode not in ("tools", "coding"):
            raise HTTPException(400, "Unbekannter Modus")
        if ctx == 0 and mode != "coding":
            raise HTTPException(400, "ctx fehlt")
        active = name == llm.active and mode == llm.mode
        if active:
            await hub.broadcast({"type": "model_switching", "name": name, "label": llm.profiles[name].label})
            llm.switching = name
        try:
            async with agent.lock:  # wartet, bis eine laufende Antwort fertig ist
                await llm.set_context(name, ctx, model_progress, mode=mode)
                if active:
                    await llm.warm()  # Ollama: gleich mit der neuen Größe laden
        except LLMError as e:
            await hub.broadcast({"type": "model_error", "name": name, "text": str(e)})
            raise HTTPException(400 if "zwischen" in str(e) else 502, str(e)) from e
        finally:
            llm.switching = None
            await idle_if_free()
        if active:
            await hub.broadcast({"type": "model_active", "name": name})
            agent._cache_owner = None  # neu geladen = leerer Cache
            agent.build_messages()  # Kontext-Kachel: neue Fenstergröße und Komprimierungspunkt sofort zeigen
            await hub.broadcast({"type": "context", **(agent.last_context or {})})
            hub.prewarm_soon()
        await hub.broadcast({"type": "models_changed"})
        return {"ok": True, "ctx": llm.context_size if active else ctx}

    @app.get("/api/models/{name}/sampling")
    async def get_model_sampling(name: str):
        from .llm import SAMPLING_LIMITS, SAMPLING_PRESETS
        if not isinstance(llm, LLMRouter):
            raise HTTPException(400, "Im Demo-Modus nicht verfügbar")
        if name not in llm.profiles:
            raise HTTPException(404, "Unbekanntes Profil")
        p = llm.profiles[name]
        return {"think": p.sampling.get("think") or {}, "fast": p.sampling.get("fast") or {},
                "temperature": p.temperature, "presets": SAMPLING_PRESETS,
                "limits": {k: [lo, hi] for k, (lo, hi, _) in SAMPLING_LIMITS.items()}}

    @app.post("/api/models/{name}/sampling")
    async def set_model_sampling(name: str, request: Request):
        """Sampling-Werte je Modus (mit/ohne Denken) – gilt ab der nächsten Antwort."""
        if not isinstance(llm, LLMRouter):
            raise HTTPException(400, "Im Demo-Modus nicht verfügbar")
        if name not in llm.profiles:
            raise HTTPException(404, "Unbekanntes Profil")
        body = await request.json()
        try:
            sets = await llm.set_sampling(name, body.get("think"), body.get("fast"))
        except LLMError as e:
            raise HTTPException(400, str(e)) from e
        await hub.broadcast({"type": "models_changed"})
        return {"ok": True, **sets}

    @app.post("/api/models/{name}/kv")
    async def set_model_kv(name: str, request: Request):
        """KV-Cache-Stufe (f16/q8_0/q4_0) eines eigenen llama-server – kleinerer Cache = mehr Kontext im VRAM."""
        from .llm_router import KV_TYPES
        if not isinstance(llm, LLMRouter):
            raise HTTPException(400, "Im Demo-Modus nicht verfügbar")
        if name not in llm.profiles:
            raise HTTPException(404, "Unbekanntes Profil")
        kv = str((await request.json()).get("kv", ""))
        if kv not in KV_TYPES:
            raise HTTPException(400, "KV-Cache-Stufe bitte f16, q8_0 oder q4_0")
        if not llm.profiles[name].server:
            try:
                await llm.set_kv(name, kv)
            except LLMError as e:
                raise HTTPException(400, str(e)) from e
        active = name == llm.active
        if active:
            await hub.broadcast({"type": "model_switching", "name": name, "label": llm.profiles[name].label})
            llm.switching = name
        try:
            async with agent.lock:  # wartet, bis eine laufende Antwort fertig ist
                await llm.set_kv(name, kv, model_progress)
        except LLMError as e:
            await hub.broadcast({"type": "model_error", "name": name, "text": str(e)})
            raise HTTPException(502, str(e)) from e
        finally:
            llm.switching = None
            await idle_if_free()
        if active:
            await hub.broadcast({"type": "model_active", "name": name})
            agent._cache_owner = None  # neu gestartet = leerer Cache
            agent.build_messages()
            await hub.broadcast({"type": "context", **(agent.last_context or {})})
            hub.prewarm_soon()
        await hub.broadcast({"type": "models_changed"})
        return {"ok": True, "kv": kv}

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
        return await asyncio.to_thread(metrics.collect)

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
        conv = memory.active
        shown = [(i, m) for i, m in enumerate(conv.history)
                 if m["role"] in ("user", "assistant") and m.get("content")][-40:]
        epochs = []
        for ep in conv.epochs[1:]:
            at = next((pos for pos, (i, _) in enumerate(shown) if i >= ep.get("start", 0)), None)
            if at is not None and ep.get("summary"):
                epochs.append({"at": at, "summary": ep["summary"], "ts": ep.get("ts"), "reason": ep.get("reason", "")})
        first = conv.epochs[0].get("summary", "") if conv.epochs else ""  # Zusammenfassung aus älteren Versionen
        return {"summary": first, "chat": {"id": conv.chat_id, "title": conv.meta.get("title", "")},
                "messages": [{"role": m["role"], "content": m["content"],
                              **({"images": m["attachments"]} if m.get("attachments") else {})} for _, m in shown],
                "epochs": epochs}

    # ---------- Chat-Historie ----------
    def chat_or_404(chat_id: str) -> None:
        if not memory.chats.exists(chat_id):
            raise HTTPException(404, "Chat nicht gefunden")

    def not_busy() -> None:
        if agent.busy():
            raise HTTPException(409, "Jarvis arbeitet gerade – bitte kurz warten oder STOP drücken")

    def mode_info() -> dict:
        conv = memory.active
        return {"mode": conv.meta.get("mode") or "tools", "project": conv.meta.get("project") or "",
                "chat_id": conv.chat_id}

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
            await activate_model(name)

    @app.post("/api/chats/{chat_id}/project")
    async def chat_project(chat_id: str, request: Request):
        chat_or_404(chat_id)
        raw = str((await request.json()).get("path", "")).strip()
        path = Path(os.path.expanduser(raw)) if raw else None
        if path is not None and not path.is_dir():
            raise HTTPException(400, f"Ordner nicht gefunden: {raw}")
        value = str(path) if path else ""
        conv = memory._loaded(chat_id)
        if conv is not None:
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
            return {"available": False, "voices": []}
        tts.available()
        extra = await catalog.fetch_catalog(tts.voices_dir)
        voices = catalog.voice_list(tts.voices_dir, tts.current, cfg.language, extra)
        for v in voices:
            f = tts.path(v["name"])
            v["size_mb"] = round(f.stat().st_size / 1e6) if v["installed"] and f.exists() else None
        return {"available": True, "current": tts.current, "rate": tts.rate, "voices": voices}

    @app.delete("/api/voices/{name}")
    async def delete_voice(name: str):
        if not tts or name not in tts.installed():  # nur echte installierte Namen – kein Pfad von außen
            raise HTTPException(404, "Stimme nicht installiert")
        if name == tts.current:
            raise HTTPException(409, "Die aktive Stimme kann nicht gelöscht werden – erst eine andere wählen.")
        tts.remove(name)
        return {"ok": True}

    @app.post("/api/voices/{name}/install")
    async def install_voice(name: str):
        if not tts:
            raise HTTPException(400, "Sprachausgabe ist deaktiviert")
        extra = {} if name in catalog.BY_NAME else await catalog.fetch_catalog(tts.voices_dir)
        if not catalog.installable(name, extra):  # nur Auswahl + offizieller Piper-Katalog
            raise HTTPException(404, "Unbekannte Stimme")
        try:
            await catalog.install_voice(name, tts.voices_dir, extra=extra)
        except Exception as e:  # noqa: BLE001
            raise HTTPException(502, f"Download fehlgeschlagen: {e}") from e
        return {"ok": True}

    @app.put("/api/voices/upload/{name}/{kind}")
    async def upload_voice(name: str, kind: str, request: Request):
        """Eigene Piper-Stimme hochladen: erst kind=config (die .onnx.json), dann kind=model (die .onnx).
        Roher Datenstrom statt Formular – dafür braucht es keine zusätzliche Bibliothek."""
        if not tts:
            raise HTTPException(400, "Sprachausgabe ist deaktiviert")
        if kind not in ("config", "model") or catalog.clean_voice_name(name) != name:
            raise HTTPException(400, "Ungültiger Stimmenname")
        model, config = tts.path(name), tts.voices_dir / f"{name}.onnx.json"
        if model.exists():
            raise HTTPException(409, f"Die Stimme „{name}“ gibt es schon – erst löschen.")
        if kind == "model" and not config.exists():
            raise HTTPException(400, "Zuerst die Konfiguration (.onnx.json) hochladen.")
        limit = catalog.UPLOAD_MAX_CONFIG if kind == "config" else catalog.UPLOAD_MAX_MODEL
        target = config if kind == "config" else model
        tts.voices_dir.mkdir(parents=True, exist_ok=True)
        part = target.with_name(target.name + ".part")
        size, head = 0, b""
        try:
            with part.open("wb") as f:
                async for chunk in request.stream():
                    size += len(chunk)
                    if size > limit:
                        raise HTTPException(413, "Datei zu groß")
                    if len(head) < 16:
                        head += chunk[:16]
                    f.write(chunk)
            if kind == "config":
                if not catalog.is_piper_config(part.read_bytes()):
                    raise HTTPException(400, "Das ist keine Piper-Konfiguration (.onnx.json mit „audio.sample_rate“).")
            elif size < catalog.UPLOAD_MIN_MODEL or not head.startswith(b"\x08"):
                raise HTTPException(400, "Das ist kein Piper-Stimmenmodell (.onnx).")
            part.replace(target)
        except BaseException:
            part.unlink(missing_ok=True)
            if kind == "model":
                config.unlink(missing_ok=True)  # keine halbe Stimme zurücklassen
            raise
        if kind == "model":
            tts.forget(name)
        return {"ok": True, "name": name, "size_mb": round(size / 1e6)}

    @app.get("/api/voices/{name}/preview")
    async def preview_voice(name: str, text: str = ""):
        if not tts or name not in tts.installed() or not tts.available():
            raise HTTPException(404, "Stimme nicht installiert")
        sample = text.strip()[:200] or "Guten Abend. Alle Systeme sind einsatzbereit. Womit kann ich dienen?"
        wav = await asyncio.to_thread(tts.synth, sample, name)
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
                           **mode_info()})
        await client.send(startup.snapshot())
        await client.send({"type": "sudo_cached", "until": broker.cached_until()})
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
                elif t == "feedback":  # 👍/👎 an einer Antwort (Lernen aus Erfahrung)
                    agent.feedback(str(data.get("id", "")), bool(data.get("good")), str(data.get("text") or "")[:500])
                elif t == "confirm":
                    changes = data.get("args") if isinstance(data.get("args"), dict) else None
                    hub.resolve(str(data.get("id")), bool(data.get("approved")), changes)
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
                    # Stufe (low/medium/high) oder nur an/aus von älteren Oberflächen
                    hub.think = (level if level in ("low", "medium", "high") else True) if data.get("enabled") else False
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
                elif t == "plan_mode":
                    hub.plan_mode = bool(data.get("enabled"))
                elif t in ("plan_accept", "plan_revise", "plan_discard"):
                    await hub.plan_decision(str(data.get("id", "")), t.split("_", 1)[1],
                                            str(data.get("text", ""))[:4000])
                elif t == "voice_settings" and tts:
                    if data.get("voice"):
                        tts.select(str(data["voice"]))
                    if data.get("rate"):
                        tts.set_rate(float(data["rate"]))
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
