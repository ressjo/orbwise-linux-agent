"""Verbindung zur Oberfläche: Hub (WebSocket-Clients, Anfragen an den Agenten, Bestätigungen, Rückfragen, Sprachausgabe) und die Texte für Bestätigungen."""

from __future__ import annotations

import asyncio
import json
import logging
import re
from pathlib import Path

from fastapi import WebSocket

from . import prompts
from .agent import Agent
from .config import Config
from .lang import T
from .tools.registry import get_tool
from .voice.listen import AudioSession, WakeWordFactory, WhisperSTT
from .voice.tts import TTS, Speaker

log = logging.getLogger(__name__)

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

    async def goal_command(self, arg: str) -> str:
        """„/goal <Ziel>“ startet den Ziel-Modus, „/goal stop“ beendet ihn, „/goal weiter“ setzt nach einer Grenze fort,
        „/goal“ allein zeigt das aktive Ziel. Liefert den Text, der als Nutzernachricht weiterläuft (leer = nichts)."""
        goal = self.agent.goal_state()
        low = arg.lower()
        if low in ("stop", "stopp", "aus", "off", "ende", "end"):
            if goal:
                self.agent.set_goal(None)
                await self.broadcast({"type": "goal", "state": "stopped", "text": goal["text"],
                                      "steps": goal.get("steps", 0)})
            return ""
        if not arg or low in ("weiter", "continue", "fortsetzen"):
            if not goal:
                await self.broadcast({"type": "toast", "text": prompts.text(self.cfg, "goal_none")})
                return ""
            if not arg:
                await self.broadcast({"type": "goal", "state": "active", "text": goal["text"],
                                      "steps": goal.get("steps", 0)})
                return ""
            goal = self.agent.set_goal(goal["text"])  # Zähler und Uhr neu
            await self.broadcast({"type": "goal", "state": "started", "text": goal["text"], "steps": 0})
            return prompts.text(self.cfg, "goal_resume")
        goal = self.agent.set_goal(arg)
        await self.broadcast({"type": "goal", "state": "started", "text": goal["text"], "steps": 0})
        return arg

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
        """Im Coding-Modus spricht Orbwise nicht (keine Sprachausgabe, keine Spracheingabe)."""
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
        # Rückfrage nur wegen fremder Inhalte im Chat? Dann bietet die Karte „in diesem Chat nicht mehr fragen“ an
        trustable = prompts.text(self.cfg, "tainted_confirm") in (reason or "")
        await self.broadcast({"type": "confirm_request", "id": call_id, "name": name, "args": args,
                              "reason": reason, "summary": describe_call(name, args, self.cfg), "editable": editable,
                              **({"trustable": True} if trustable else {})})
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
        goal_cmd = re.match(r"^/(goal|ziel)\b\s*(.*)$", text, re.I | re.S)
        if goal_cmd and source != "voice":
            text = await self.goal_command(goal_cmd.group(2).strip())
            if not text:
                return
        review = re.match(r"^/(review|check|prüfen|pruefen)\b\s*(.*)$", text, re.I | re.S)
        if review and source != "voice":  # 🛡 Code-Check: prüfen, berichten, beheben, erneut prüfen
            full = review.group(2).strip().lower() in ("all", "alles", "ganz", "projekt", "full")
            task = asyncio.create_task(self.review(full))
            self.tasks.add(task)
            task.add_done_callback(self.tasks.discard)
            return
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

    async def review(self, full: bool = False) -> None:
        """Code-Check: Bericht, danach bestätigte Funde und fehlschlagende Prüfungen beheben lassen und erneut prüfen
        – höchstens coding.review_rounds Runden (siehe agent_review.py)."""
        label = T("🛡 Code-Check", "🛡 Code check") + (T(" (ganzes Projekt)", " (whole project)") if full else "")
        await self.broadcast({"type": "user", "text": label, "source": "review", "images": [], "files": []})
        try:
            result = await self.agent.code_review(self.emit, self.confirm, full, think=self.think)
            rounds = max(0, self.cfg.coding.review_rounds)
            for round_ in range(1, rounds + 1):
                if not result.get("fix") or self.agent.review_stopped():
                    break
                await self.broadcast({"type": "user", "text": result["fix"], "source": "review", "images": [],
                                      "files": []})
                await self.agent.run(result["fix"], self.emit, self.confirm, think=self.think)
                result = await self.agent.code_recheck(self.emit, self.confirm, result, round_, round_ == rounds)
        except asyncio.CancelledError:
            pass
        except Exception as e:  # noqa: BLE001
            log.exception("Code-Check fehlgeschlagen")
            await self.emit({"type": "error", "text": T(f"Code-Check fehlgeschlagen: {e}", f"Code check failed: {e}")})
        finally:
            if not self.agent.busy():
                await self.broadcast({"type": "state", "state": "idle"})

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
