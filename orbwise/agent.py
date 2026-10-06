"""Der Agent: baut den Kontext (Gedächtnis + Verlauf), führt die Tool-Schleife aus und
meldet alles als Events an die Oberfläche.

Kontextfenster (Vorbild: Claude Code): Innerhalb einer Epoche wird der Prompt nur hinten verlängert – System-Prompt,
Fakten und Werkzeuge sind ein fester Schnappschuss, die Kontext-Notiz jeder Nutzernachricht wird beim ersten Senden
eingefroren. Der Modell-Server liest so nur Neues ein. Erst kurz vor dem Limit fasst das Modell den Chat einmal
strukturiert zusammen (Anfrage = der Prompt, den der Server schon kennt, plus eine Anweisung); danach beginnt eine
neue Epoche mit dieser Zusammenfassung und den letzten Schritten – mitten in einer Aufgabe geht es automatisch weiter.
"""

from __future__ import annotations

import asyncio
import getpass
import inspect
import json
import logging
import os
import platform
import re
import time
import uuid
from collections.abc import Awaitable, Callable
from datetime import datetime
from pathlib import Path

from . import prompts, toolselect
from .config import Config
from .context_plan import SUMMARY_MIN, THINK_LEVELS, ContextPlan, plan_for
from .llm import ContextOverflow, LLMError, strip_think
from .memory import Memory, est_tokens
from .memory.context import TRIM_NOTE, msg_tokens, render_transcript, shrink_tool_results
from .memory.index import shared_words
from .tools.proc import clip, clip_saved
from .tools.registry import (
    BLOCKED,
    CONFIRM,
    SAFE,
    ToolContext,
    coerce_args,
    get_tool,
    load_all_tools,
    missing_args,
    tool_schemas,
)
from .tools.system import _os_name

log = logging.getLogger(__name__)

# Nach dem Lesen fremder Mailinhalte brauchen auch sonst sichere Tools dieser Gruppen eine Bestätigung –
# eine Mail könnte versteckte Anweisungen enthalten (z. B. Daten per fetch_url nach außen schicken).
# Lernen aus Erfahrung (memory/lessons.py): Werkzeug-Ergebnisse, die nach Fehlschlag aussehen …
FAILED_RESULT = re.compile(r"^(?:\[[^\]]*\] )?(?:Fehler|Error|BLOCKIERT|Unbekanntes Tool|Fehlende Parameter|"
                           r"Exit-Code [1-9]|Zeitüberschreitung|Datei nicht gefunden|Programm '.*' ist nicht installiert)")
# … und Nutzer-Korrekturen der vorigen Antwort („nein, das ist falsch …“, „nimm lieber …“)
CORRECTION = re.compile(r"^\W*(?:nein\b|falsch|stimmt nicht|das (?:ist|war) (?:falsch|nicht (?:richtig|korrekt|das))|"
                        r"nicht so\b|doch nicht|nimm (?:lieber|stattdessen|doch)|du hast (?:das |es )?(?:falsch|vergessen)|"
                        r"eigentlich (?:meinte|wollte)|ich meinte|no\b|wrong|that'?s (?:wrong|not)|not what i|i meant)",
                        re.I)

# Alle Werkzeuge gehen nur mit, wenn ihre Beschreibungen in höchstens so vielen Sekunden eingelesen sind
ALL_TOOLS_SECONDS = 10.0

TAINT_SOURCES = {"mail_list", "mail_search", "mail_read", "mail_ask", "daily_briefing",
                 "look_at_screen", "look_at_image"}  # auch Bildschirm/Bild: eine Webseite kann Anweisungen zeigen
TAINT_GUARDED = {"shell", "ssh", "portainer", "web", "files", "apps", "obsidian", "trilium", "calendar_tools",
                 "homeassistant", "memory_tools", "reminder_tools", "power", "telegram_tools"}


def is_taint_source(name: str, result: str) -> bool:
    """Bringt dieses Tool-Ergebnis fremden Text (Mails) in den Verlauf?"""
    return name in TAINT_SOURCES and (name != "daily_briefing" or "E-Mail:" in result)


def plan_steps(text: str) -> int:
    """Anzahl nummerierter Schritte in einem Plan (für „Mein Plan hat n Schritte“)."""
    return len(re.findall(r"^\s*\d+[.)]\s", text, re.M))


def prompt_size(stats: dict, estimated: int) -> int:
    """Echte Prompt-Größe laut Server – 0, wenn sie unbrauchbar ist: Ollama zählt bei einem Cache-Treffer nur die
    neu verarbeiteten Token (prompt_eval_count); so ein Wert weit unter der Schätzung würde das Budget aufblähen."""
    total = int(stats.get("prompt_total") or 0)
    if stats.get("prompt_cached") is not None:
        return total  # llama-server nennt den Cache-Anteil getrennt – dort ist total die volle Größe
    return 0 if total and total < 0.6 * estimated else total


# Coding-Modus: nur was man zum Programmieren braucht – mehr Kontext bleibt für den Code frei
CODING_GROUPS = {"files", "shell", "web", "memory_tools", "todo_tools"}
__all__ = ["Agent", "CODING_GROUPS", "SUMMARY_MIN", "THINK_LEVELS", "ThinkFilter", "is_taint_source", "prompt_size"]
# Werkzeug-Aufruf als Text: llama-server gibt ihn bei tool_choice "none" als Inhalt zurück
RAW_TOOL_CALL = re.compile(r'\s*(<tool_call>|<function=|\[TOOL_CALLS\]|\{\s*"name"\s*:)')
Emit = Callable[[dict], Awaitable[None]]
Confirm = Callable[[str, str, dict, str], Awaitable[bool]]


class ThinkFilter:
    """Entfernt <think>…</think>-Blöcke aus einem Token-Stream (falls das Modell trotzdem 'denkt')."""

    OPEN, CLOSE = "<think>", "</think>"

    def __init__(self):
        self.buf = ""
        self.inside = False
        self.thought: list[str] = []  # Text innerhalb von <think>, abholbar mit take_thought()

    def take_thought(self) -> str:
        text, self.thought = "".join(self.thought), []
        return text

    def feed(self, text: str) -> str:
        self.buf += text
        out = []
        while True:
            tag = self.CLOSE if self.inside else self.OPEN
            idx = self.buf.find(tag)
            if idx >= 0:
                (self.thought if self.inside else out).append(self.buf[:idx])
                self.buf = self.buf[idx + len(tag):]
                self.inside = not self.inside
                continue
            # Möglichen Tag-Anfang am Ende zurückhalten
            keep = 0
            for k in range(1, len(tag)):
                if self.buf.endswith(tag[:k]):
                    keep = k
            (self.thought if self.inside else out).append(self.buf[: len(self.buf) - keep])
            self.buf = self.buf[len(self.buf) - keep:] if keep else ""
            return "".join(out)

    def flush(self) -> str:
        rest = "" if self.inside else self.buf
        self.buf = ""
        return rest


class Agent:
    def __init__(self, cfg: Config, llm, memory: Memory):
        self.cfg = cfg
        self.llm = llm
        self.memory = memory
        self._budget_scale = 1.0  # < 1, wenn der Server „Kontext zu klein“ gemeldet hat
        # Verhältnis echte/geschätzte Prompt-Token je Modellprofil (lernt aus den Zahlen des Servers)
        self._token_ratio: dict[str, float] = {}
        self._turn_time = ""  # Uhrzeit der aktuellen Anfrage
        self._turn_stamp = ""  # Datum + Uhrzeit für die Kontext-Notiz der aktuellen Anfrage
        self._turn_memories = ""  # zur aktuellen Anfrage gefundene Erinnerungen
        self._turn_lessons = ""  # … und passende gelernte Erfahrungen (Text für die Kontext-Notiz)
        self._turn_lesson_ids: list[str] = []
        self._turns: dict[str, dict] = {}  # letzte Runden (für 👍/👎): msg_id → Auszug
        self._last_turn: dict | None = None
        self._turn_open = False  # läuft gerade eine Anfrage (deren Notiz noch eingefroren werden darf)?
        self._think: bool | None = None
        self._think_level: str | None = None  # low | medium | high, wenn gedacht wird
        self._plan = False  # Planmodus: nur lesen, am Ende einen Plan vorlegen
        # gelernte Einlese-Geschwindigkeit je Modell (Token/s) – in state.json gemerkt, damit sie nach einem Neustart
        # gleich bekannt ist (entscheidet mit, ob alle Werkzeuge mitgehen)
        self._state_path = cfg.memory.dir.parent / "state.json"
        self._prefill_tps: dict[str, float] = self._load_prefill()
        self._saved_tps: dict[str, float] = dict(self._prefill_tps)
        self._last_prompt: dict[str, int] = {}  # Prompt-Größe des letzten Schritts (für „neu einzulesen“)
        self._approved_plan = ""  # beim Ausführen: der freigegebene Plan (hängt an der aktuellen Nachricht)
        self._full_prompt = 0  # ungekürzte Größe des nächsten Prompts (echte Token, geschätzt)
        # Wessen Prompt der Modell-Server gerade im Cache hat: (Profil, Chat, Epoche) – fürs Vorwärmen
        self._cache_owner: tuple | None = None
        self._prewarming = False
        self._compacted_at: tuple | None = None  # (Chat, Epochen, Nachrichten) der letzten Komprimierung
        # Auto-Knopf: "off" = jeder Shell-Befehl fragt, "read" = erkannte lesende Befehle laufen ohne Rückfrage,
        # "files" = zusätzlich Dateien im eigenen Home anlegen/schreiben/kopieren/verschieben (ohne Root, ohne Löschen),
        # "auto" = Shell-Befehle und Dateien ohne Root (Löschen, Ausschalten, Senden ins Netz, Startdateien fragen)
        self.auto_mode = "read"
        self.last_context: dict | None = None  # letzter Prompt-Aufbau (für die Kontext-Anzeige)
        self._vision = False  # sieht das aktive Modell angehängte Bilder selbst? (siehe _check_vision)
        self.tools = load_all_tools()
        self.all_schemas = tool_schemas(cfg)
        self._coding_schemas: tuple[list[dict], int] | None = None
        self.groups_of = {name: spec.group for name, spec in self.tools.items()}
        self.schemas = self.all_schemas
        self.schema_tokens = est_tokens(json.dumps(self.schemas, ensure_ascii=False))
        self.all_schema_tokens = self.schema_tokens
        self._tools_dropped: set[str] = set()  # beim letzten Wechsel weggefallene Werkzeug-Einheiten
        self._turn_units: set[str] = set()  # Einheiten, die die laufende Runde braucht (für „zuletzt gebraucht“)
        self._turn_used: set[str] = set()  # Einheiten der in dieser Runde aufgerufenen Werkzeuge
        self.lock = asyncio.Lock()
        self.services: dict = {}

    # ---------- Prompt ----------
    @property
    def mode(self) -> str:
        return self.memory.mode

    def project_dir(self) -> str | None:
        """Projektordner des Coding-Chats (falls gesetzt und vorhanden)."""
        if self.mode != "coding":
            return None
        project = self.memory.conversation.meta.get("project") or ""
        path = Path(os.path.expanduser(project)) if project else None
        return str(path) if path and path.is_dir() else None

    def tool_allowed(self, name: str) -> bool:
        return self.mode != "coding" or self.groups_of.get(name, "") in CODING_GROUPS

    def _mode_schemas(self) -> tuple[list[dict], int]:
        if self.mode != "coding":
            return self.all_schemas, self.all_schema_tokens
        if self._coding_schemas is None:
            schemas = [s for s in self.all_schemas if self.tool_allowed(s["function"]["name"])]
            self._coding_schemas = (schemas, est_tokens(json.dumps(schemas, ensure_ascii=False)))
        return self._coding_schemas

    def system_prompt(self, tools: set[str] | None = None) -> str:
        """Feste Anweisungen – ohne Datum/Uhrzeit (die stehen in der Kontext-Notiz), damit sich der Anfang des
        Prompts nicht ändert. tools: Namen der geladenen Werkzeuge – Hinweise nur für diese (None = alle)."""
        c = self.cfg
        if self.mode == "coding":
            return (prompts.coding_prompt(
                c, name=c.assistant_name, os=_os_name(), host=platform.node(),
                user=c.user_name or getpass.getuser(), home=Path.home(),
                project=self.project_dir() or prompts.text(c, "no_project")) + c.persona_extra).strip()
        base = prompts.base_prompt(
            c, name=c.assistant_name, os=_os_name(), host=platform.node(),
            user=c.user_name or getpass.getuser(), home=Path.home(),
            nas=", ".join(map(str, c.tools.nas_paths)))
        names = self._all_tool_names() if tools is None else tools
        names = {n for n in names if n in self.tools and self.tools[n].is_enabled(c)}  # Config kann sich ändern
        return (base + prompts.hints(c, names) + c.persona_extra).strip()

    def _all_tool_names(self) -> set[str]:
        """Werkzeuge, wenn alle ins Fenster passen (ohne load_tools – es gibt nichts nachzuladen)."""
        return {s["function"]["name"] for s in self._mode_schemas()[0]} - {"load_tools"}

    def epoch_system(self) -> str:
        """System-Prompt der Epoche: Anweisungen + Fakten-Schnappschuss. Bleibt bis zur nächsten Komprimierung
        gleich – ein neuer Fakt (remember) steht bis dahin im Werkzeug-Ergebnis. Die Zusammenfassung des Früheren
        steht nicht hier, sondern vor der ersten Nachricht (siehe summary_block): So bleiben Systemprompt und
        Werkzeuge über eine Komprimierung hinweg gleich und im Cache des Servers (wie bei Claude Code)."""
        ep = self.memory.conversation.epoch
        if ep.get("facts") is None:
            ep["facts"] = self.memory.facts_text(min(self.cfg.memory.facts_max_tokens, self.plan().facts))
        sections = [self.system_prompt(self._epoch_tools())]
        if ep["facts"]:
            sections.append(prompts.section(self.cfg, "facts") + "\n" + ep["facts"])
        return "\n\n".join(sections)

    def summary_block(self) -> str:
        """Zusammenfassung der früheren Epochen (und frisch angehängte Dateien) – steht im [Kontext]-Block der ersten
        Nutzernachricht; innerhalb der Epoche unverändert (append-only)."""
        ep = self.memory.conversation.epoch
        return prompts.summary_section(self.cfg, ep.get("summary") or "", ep.get("files") or "")

    def _epoch_tools(self) -> set[str] | None:
        """Werkzeuge der Epoche (None = alle gehen mit, großes Fenster)."""
        tools = self.memory.conversation.epoch.get("tools")
        return set(tools) if tools is not None else None

    def _freeze_note(self, hits=None) -> None:
        """Kontext-Notiz (Datum/Uhrzeit, Erinnerungen, Planmodus) der aktuellen Nutzernachricht beim ersten Senden
        an der Nachricht speichern – danach geht sie wörtlich mit, auch in späteren Runden (append-only)."""
        conv = self.memory.conversation
        if not (self._turn_open or hits is not None):
            return
        users = [i for i in conv.epoch_indices() if conv.history[i]["role"] == "user"]
        if not users or "note" in conv.history[users[-1]]:
            return
        memories = self.memory.format_hits(hits) if hits is not None else self._turn_memories
        stamp = self._turn_stamp or prompts.note_stamp(self.cfg, datetime.now())
        msg = conv.history[users[-1]]
        # Hinweis „kurz/zügig denken“ nur für diese Stufen („gründlich“ wird im kleinen Fenster nur begrenzt)
        hint = prompts.text(self.cfg, f"think_{self._think_level}") \
            if self._think and self._think_level in ("low", "medium") else ""
        msg["note"] = prompts.context_note(self.cfg, stamp, memories, plan=self._plan,
                                           approved_plan=self._approved_plan, extra=hint, lessons=self._turn_lessons)
        msg["note_meta"] = {"time": stamp, "plan": self._plan, "approved": self._approved_plan, "hint": hint,
                            "lessons": self._turn_lessons}

    def _prompt_message(self, m: dict) -> dict:
        out = {k: v for k, v in m.items() if k in ("role", "content", "tool_calls", "tool_name")}
        if m.get("note"):
            out["content"] = m["note"] + (m.get("content") or "")
        if m.get("attachments"):
            from . import attachments
            paths = [p for p in (attachments.path_of(self.cfg, n) for n in m["attachments"]) if p]
            if paths and self._vision:  # das Modell sieht die Bilder selbst
                out["images"] = [str(p) for p in paths]
            elif paths:  # sonst: Pfad nennen – look_at_image fragt das Vision-Modell
                out["content"] = (out.get("content") or "") + prompts.attachment_note(
                    self.cfg, [str(p) for p in paths], self.tool_allowed("look_at_image")
                    and self.tools["look_at_image"].is_enabled(self.cfg))
        return out

    async def _check_vision(self) -> None:
        """Sieht das aktive Modell Bilder? (bestimmt, wie angehängte Bilder in den Prompt kommen)"""
        fn = getattr(self.llm, "supports_images", None)
        try:
            self._vision = bool(await fn()) if fn else False
        except Exception:  # noqa: BLE001
            self._vision = False

    def build_messages(self, hits=None, display: bool = True, trim: bool = True) -> list[dict]:
        """Prompt für den nächsten Schritt. display=False: nur abschätzen (Komprimierung, Vorwärmen) – die Anzeige
        „Kontext“ behält dann die Zahlen des letzten echten Schritts. trim=False: ohne Notbremse (die
        Komprimierungs-Anfrage muss genau dem Anfang entsprechen, den der Server im Cache hat)."""
        conv = self.memory.conversation
        system = self.epoch_system()
        summary = self.summary_block()
        self._freeze_note(hits)
        total_budget = self.context_budget()
        summary_t = est_tokens(summary) if summary else 0
        budget = total_budget - est_tokens(system) - self.schema_tokens - summary_t
        if trim:  # Notbremse – normalerweise kommt vorher das Ausblenden bzw. die Komprimierung
            history = conv.trimmed_history(max(budget, 1000))
        else:
            history = [{k: v for k, v in m.items() if k in ("role", "content", "tool_calls", "tool_name", "note", "attachments")}
                       for m in conv.epoch_messages()]
        messages = [{"role": "system", "content": system}, *(self._prompt_message(m) for m in history)]
        if summary:  # vor die erste Nutzernachricht – in deren [Kontext]-Block
            first = next((i for i, m in enumerate(messages) if m["role"] == "user"), None)
            if first is None:
                messages.insert(1, {"role": "user", "content": prompts.with_summary(self.cfg, "", summary)})
            else:
                note = history[first - 1].get("note") or ""
                body = (messages[first]["content"] or "")[len(note):]
                messages[first]["content"] = prompts.with_summary(self.cfg, note, summary) + body
        # Für die Anzeige „Kontext“ (Schätzung; echte Server-Token kommen nach dem Schritt)
        base_t, system_t = est_tokens(self.system_prompt(self._epoch_tools())), est_tokens(system)
        history_t = sum(msg_tokens(m) for m in history) + summary_t
        notes_t = sum(est_tokens(m.get("note") or "") for m in history) + summary_t
        ratio = self.token_ratio()
        self._full_prompt = int((system_t + self.schema_tokens + conv.history_tokens() + summary_t) * ratio)
        trimmed = len(history) < len(conv.epoch_indices()) or any(
            m["role"] == "tool" and (m.get("content") or "").endswith(TRIM_NOTE) for m in history)
        used = system_t + self.schema_tokens + history_t
        if not display:
            return messages
        plan = self.plan()
        self.last_context = {
            "window": self.model_window(), "budget": total_budget, "used": used,
            "parts": {"system": base_t, "tools": self.schema_tokens, "memory": system_t - base_t + notes_t,
                      "history": history_t - notes_t},
            "trimmed": trimmed, "summarized": bool(conv.running_summary),
            # in echten Token: feste Grenze = Kontextfenster; ab compact_at wird Platz geschaffen
            "tokens": int(used * ratio), "reserve": self.answer_reserve(), "compact_at": self.compact_limit(),
            "epoch": len(conv.epochs), "small": plan.small, "cleared": conv.epoch.get("cleared", 0),
            "capped": self.cfg.memory.context_budget_tokens or None, "ratio": ratio,
        }
        return messages

    def model_window(self) -> int:
        """Kontextfenster des aktiven Modells (vom Server gemeldet, sonst aus der Config)."""
        num_ctx = getattr(self.llm, "context_size", None)
        if not num_ctx:
            profile = getattr(self.llm, "profile", None)
            num_ctx = getattr(profile, "num_ctx", None) or self.cfg.llm.num_ctx
        return int(num_ctx)

    def plan(self) -> ContextPlan:
        """Kontext-Stufe (unter 16k sparsam, siehe context_plan.py) – nach dem Fenster, das der Prompt nutzen darf."""
        window = self.model_window()
        if self.cfg.memory.context_budget_tokens:  # optionale Obergrenze für den Prompt
            window = min(window, self.cfg.memory.context_budget_tokens + 1500)
        return plan_for(window)

    def context_budget(self) -> int:
        """Prompt-Budget: Kontextfenster des aktiven Modells (optional begrenzt durch context_budget_tokens)
        (abzüglich Platz für die Antwort) – z. B. Bonsai mit 8192 Token."""
        budget = self.model_window() - self.answer_reserve()
        if self.cfg.memory.context_budget_tokens:
            budget = min(budget, self.cfg.memory.context_budget_tokens)
        return max(2000, int(budget * self._budget_scale / self.token_ratio()))

    def _thinking(self) -> bool:
        think = self._think
        if think is None:
            think = bool(getattr(getattr(self.llm, "profile", None), "think", False))
        return bool(think)

    def answer_reserve(self) -> int:
        """Platz für Antwort bzw. Denkkette – im kleinen Fenster knapper, Denken dort begrenzt."""
        return self.plan().reserve(self._thinking(), self._think_level or "high")

    def think_budget(self) -> int:
        """Höchstlänge der Denkkette in Token für diese Anfrage (0 = unbegrenzt bzw. kein Denken)."""
        return self.plan().think_budget(self._think_level) if self._think else 0

    def _set_think(self, think) -> None:
        """think: None (Profil), False, True (= normal) oder eine Stufe low/medium/high."""
        if isinstance(think, str):
            level = think if think in THINK_LEVELS else "medium"
            self._think, self._think_level = (think != "off"), (level if think != "off" else None)
        else:
            self._think = think
            self._think_level = "medium" if think else None

    def summary_budget(self) -> int:
        """Höchstlänge der Zusammenfassung bei einer Komprimierung (klein ≈ 8 %, groß ≈ 10 % des Fensters)."""
        return self.plan().summary_max

    def summary_floor(self) -> int:
        """Mindestlänge der Zusammenfassung – so viel muss beim Komprimieren noch frei sein."""
        return self.plan().summary_floor

    def compact_limit(self) -> int:
        """Ab dieser Prompt-Größe (echte Token) wird Platz geschaffen – so spät wie möglich: der nächste Schritt
        braucht Platz für Antwort bzw. Denkkette, die Zusammenfassung mindestens summary_floor (sie nimmt, was frei
        ist). 8k: ≈ 7,2k (88 %); 16k: ≈ 14,9k (91 %), mit Denken ≈ 13,4k; 32k: ≈ 30,4k (93 %)."""
        window = self.model_window()
        if self.cfg.memory.context_budget_tokens:  # optionale Obergrenze für den Prompt
            window = min(window, self.cfg.memory.context_budget_tokens + self.answer_reserve())
        plan = self.plan()
        return window - max(self.answer_reserve(), plan.summary_floor + plan.instruction_reserve)

    def _profile_key(self) -> str:
        return str(getattr(self.llm, "active", "") or "default")

    def token_ratio(self) -> float:
        return self._token_ratio.get(self._profile_key(), 1.0)

    def learn_tokens(self, estimated: int, real: int) -> None:
        """Schätzung an die echten Token des Servers angleichen (gleitend, begrenzt auf 0,5–3). Manche Modelle
        zählen deutlich mehr als „3 Zeichen pro Token“ (Chat-Vorlage, Werkzeugbeschreibungen, Tokenizer) – mit einer
        engeren Grenze zeigte die Anzeige z. B. 7k, obwohl der Server 21k las, und Komprimieren kam zu spät."""
        if estimated < 500 or not real:
            return
        key = self._profile_key()
        sample = max(0.5, min(3.0, real / estimated))
        old = self._token_ratio.get(key)
        self._token_ratio[key] = round(sample if old is None else 0.5 * old + 0.5 * sample, 3)
        if not 0.6 <= sample <= 1.5:
            log.info("Prompt laut Server %s Token, geschätzt %s – Faktor jetzt %s", real, estimated,
                     self._token_ratio[key])

    def choose_tools(self, used: set[str] | None = None) -> set[str]:
        """Werkzeuge für den nächsten Schritt (siehe toolselect.py). Passen alle gut ins Fenster, gehen immer alle mit.
        Sonst je Epoche eine Auswahl: Pflicht (Grundausstattung, passend zur Frage, in dieser Runde benutzt, per
        load_tools angeheftet, bei „ja“/„mach das“ der letzte Vorschlag) plus – solange es in die Werkzeug-Grenze
        passt – was zuletzt gebraucht wurde. Reicht die Auswahl, bleibt sie gleich (der Prompt-Anfang bleibt im
        Cache). Muss etwas dazu, ändert sich der Anfang ohnehin: im kleinen Fenster wird dann neu gepackt (Altes fällt
        weg), im großen wächst die Auswahl. used: Namen der in dieser Runde aufgerufenen Werkzeuge.
        Liefert die neu dazugekommenen Einheiten; die weggefallenen stehen in self._tools_dropped."""
        base, base_tokens = self._mode_schemas()
        conv = self.memory.conversation
        ep = conv.epoch
        plan = self.plan()
        self._tools_dropped = set()
        if base_tokens <= 0.3 * self.context_budget() and self._all_tools_quick(base_tokens):
            # alles passt: alle Werkzeuge, ohne load_tools (es gibt nichts nachzuladen)
            self._set_schemas([s for s in base if s["function"]["name"] != "load_tools"])
            ep["groups"] = ep["tools"] = None  # alle Werkzeuge – und alle Hinweise
            self._turn_units = set()
            return set()
        small = plan.small and self.mode != "coding"  # Coding: feste, kleine Auswahl (CODING_GROUPS)
        question = conv.history[conv.turn_start()].get("content", "") if conv.history else ""
        matched = toolselect.units_for([question], small)
        suggested = toolselect.units_for([self._previous_answer()], small) - matched
        pinned = set(ep.get("pinned") or [])
        if not small:  # ab 16k zählt immer die ganze Gruppe
            pinned = {u.split(":")[0] for u in pinned}
        recent = [set(r) for r in ep.get("recent") or []]
        self._turn_used = {self._unit(n, small) for n in used or ()} - {""}
        # was in den letzten Runden wirklich aufgerufen wurde, bleibt Pflicht – Folgefragen („und die vom Mai?“)
        # nennen die Gruppe meist nicht noch einmal
        used_before = set().union(*(set(u) for u in ep.get("used") or []))
        if not small:
            used_before = {u.split(":")[0] for u in used_before}
        must = matched | pinned | self._turn_used | used_before
        if conv.history and conv.history[conv.turn_start()].get("attachments") and not self._vision \
                and "look_at_image" in self.tools:
            must.add("vision")  # angehängtes Bild: ohne eigene Bildunterstützung über look_at_image
        if toolselect.is_follow_up(question):
            must |= suggested  # „ja“, „mach das“: gemeint ist der letzte Vorschlag des Modells
        if small and toolselect.wants_change([question]):  # z. B. „weiter“ im Paperless-Durchgang
            must |= toolselect.write_units({u for u in must.union(*recent) if ":" not in u})
        self._turn_units = set(must)
        if not small:
            must |= toolselect.CORE_GROUPS
        have = set(ep.get("groups") or [])
        if not have:  # erste Auswahl der Epoche: dazu, was die mitgenommenen Schritte benutzt haben (groß: und die
            # Auswahl der vorigen Epoche – dann bleibt der Prompt-Anfang über die Komprimierung hinweg gleich)
            carried = {self._unit(c.get("function", {}).get("name", ""), small)
                       for m in conv.epoch_messages() for c in m.get("tool_calls") or []}
            new = must | (carried - {""}) | set(ep.get("inherit") or [])
        elif must <= have:
            new = have  # nichts Neues nötig – der Prompt-Anfang bleibt im Cache
        elif small:
            new = must | (have & set().union(*recent, suggested))  # neu packen: nur, was zuletzt gebraucht wurde
        else:
            new = must | have
        if new != have:  # ändert sich der Anfang ohnehin: auf die Werkzeug-Grenze bringen
            new = self._fit_tools(base, new, must, recent, suggested, small)
        added = new - have if have else set()
        self._tools_dropped = have - new if have else set()
        ep["groups"] = sorted(new)
        self._set_schemas(self._with_loader(toolselect.select(base, self.groups_of, new, small)))
        ep["tools"] = sorted(s["function"]["name"] for s in self.schemas)
        return added

    def _all_tools_quick(self, tool_tokens: int) -> bool:
        """Dürfen alle Werkzeuge mitgehen? Nur, wenn ihr Einlesen kurz ist: Der Prompt-Anfang muss nach jedem Start,
        Modellwechsel und jeder Komprimierung neu eingelesen werden – bei ~140 Token/s (Bonsai auf 8 GB) kosten 16k
        Token Werkzeugbeschreibungen über 2 Minuten, auf einer schnellen Karte 5 s. Unbekannte Geschwindigkeit:
        alle (wird beim ersten Einlesen gelernt und gemerkt). Etwas Abstand zwischen An und Aus, damit die Auswahl
        nicht hin und her springt (jeder Wechsel kostet ein Neueinlesen)."""
        tps = self._prefill_tps.get(self._profile_key())
        if not tps:
            return True
        seconds = tool_tokens * self.token_ratio() / tps
        all_now = self.memory.conversation.epoch.get("tools", "") is None
        return seconds <= (ALL_TOOLS_SECONDS * 1.25 if all_now else ALL_TOOLS_SECONDS)

    def _load_prefill(self) -> dict[str, float]:
        try:
            data = json.loads(self._state_path.read_text(encoding="utf-8"))
            raw = data.get("prefill_tps") or {}
            return {str(k): float(v) for k, v in raw.items() if isinstance(v, (int, float)) and v > 0}
        except (OSError, ValueError, AttributeError):
            return {}

    def _save_prefill(self) -> None:
        """Gelernte Geschwindigkeit merken – nur bei spürbarer Änderung (state.json teilt sich das mit dem Router)."""
        key = self._profile_key()
        tps, saved = self._prefill_tps.get(key), self._saved_tps.get(key)
        if not tps or (saved and abs(tps - saved) / saved < 0.1):
            return
        try:
            data = json.loads(self._state_path.read_text(encoding="utf-8")) if self._state_path.exists() else {}
            data.setdefault("prefill_tps", {})[key] = tps
            self._state_path.parent.mkdir(parents=True, exist_ok=True)
            self._state_path.write_text(json.dumps(data, indent=1), encoding="utf-8")
            self._saved_tps[key] = tps
        except (OSError, ValueError) as e:
            log.info("Einlese-Geschwindigkeit nicht gespeichert: %s", e)

    def _unit(self, name: str, small: bool) -> str:
        """Auswahl-Einheit eines Werkzeugs – leer für die Grundausstattung des kleinen Fensters (immer geladen)."""
        if not name or (small and name in toolselect.SMALL_CORE):
            return ""
        return toolselect.unit_of(name, self.groups_of.get(name, ""), small)

    def _set_schemas(self, schemas: list[dict]) -> None:
        self.schemas = schemas
        self.schema_tokens = est_tokens(json.dumps(schemas, ensure_ascii=False))

    def _previous_answer(self) -> str:
        """Letzte Antwort des Modells vor der laufenden Frage (für „ja, mach das“)."""
        hist = self.memory.conversation.history
        for m in reversed(hist[:self.memory.conversation.turn_start()]):
            if m["role"] == "user":
                break
            if m["role"] == "assistant" and m.get("content"):
                return m["content"]
        return ""

    def _tool_cost(self, name: str) -> int:
        if not hasattr(self, "_costs"):
            self._costs = {s["function"]["name"]: est_tokens(json.dumps(s, ensure_ascii=False))
                           for s in self.all_schemas}
        return self._costs.get(name, 0)

    def _fit_tools(self, base: list[dict], units: set[str], must: set[str], recent: list[set[str]],
                   suggested: set[str], small: bool) -> set[str]:
        """Weiche Grenze für die Werkzeugbeschreibungen (klein 35 %, groß 50 % des Budgets): optionale Einheiten
        fallen weg – zuerst, was am längsten nicht gebraucht wurde. Pflicht-Einheiten bleiben immer."""
        cap = self.plan().tool_share * self.context_budget()

        def size(us: set[str]) -> int:
            return sum(self._tool_cost(s["function"]["name"]) for s in toolselect.select(base, self.groups_of, us, small))

        def keep_rank(u: str) -> int:
            if u in suggested:
                return 3
            if recent and u in recent[-1]:
                return 2
            return 1 if any(u in r for r in recent) else 0

        for u in sorted(units - must, key=keep_rank):
            if size(units) <= cap:
                break
            units = units - {u}
        return units

    def _with_loader(self, schemas: list[dict]) -> list[dict]:
        """load_tools nennt nur, was gerade fehlt – die Beschreibung ändert sich also nur mit der Auswahl."""
        names = {s["function"]["name"] for s in schemas}
        if "load_tools" not in names:
            return schemas
        from .tools import toolload
        missing: dict[str, bool] = {}
        for group in toolload.loadable_groups(self.cfg):
            tools = [n for n, g in self.groups_of.items()
                     if g == group and self.tools[n].is_enabled(self.cfg) and self.tool_allowed(n)]
            absent = [n for n in tools if n not in names]
            if absent:
                missing[group] = len(absent) < len(tools)  # nur ein Teil fehlt (klein: der ändernde)
        description = toolload.describe_missing(self.cfg, missing)
        return [{**s, "function": {**s["function"], "description": description}}
                if s["function"]["name"] == "load_tools" else s for s in schemas]

    def _note_recent(self) -> None:
        """Am Ende einer Runde: welche Einheiten sie gebraucht hat (die letzten zwei Runden bleiben beim Neupacken)."""
        ep = self.memory.conversation.epoch
        if ep.get("groups") is not None:
            ep["recent"] = [*(ep.get("recent") or [])[-1:], sorted(getattr(self, "_turn_units", set()))]
            ep["used"] = [*(ep.get("used") or [])[-1:], sorted(getattr(self, "_turn_used", set()))]

    def _call_llm(self, messages: list[dict], tools: list[dict] | None, **opts):
        """chat_stream mit Zusatzoptionen (think, max_tokens, tool_choice) – nur die, die das LLM-Objekt kennt."""
        fn = self.llm.chat_stream
        try:
            params = inspect.signature(fn).parameters
            if not any(p.kind == p.VAR_KEYWORD for p in params.values()):
                opts = {k: v for k, v in opts.items() if k in params}
        except (TypeError, ValueError):
            pass
        return fn(messages, tools, **{k: v for k, v in opts.items() if v is not None})

    # ---------- Ablauf ----------
    async def run(self, user_text: str, emit: Emit, confirm: Confirm, think: bool | None = None,
                  plan: bool = False, approved_plan: str = "", images: list[str] | None = None) -> str:
        """think: Denkmodus für diese Anfrage (None = Einstellung des Modell-Profils).
        plan: Planmodus – nur lesend nachsehen und einen Plan zur Freigabe vorlegen (gedacht wird nur, wenn der
        Denkmodus an ist). approved_plan: freigegebener Plan, der beim Ausführen angeheftet bleibt."""
        async with self.lock:
            self._plan, self._approved_plan = plan, approved_plan
            self._set_think(think)
            self._tainted = False
            try:
                return await self._run(user_text, emit, confirm, images)
            finally:
                self._think, self._think_level, self._plan, self._approved_plan = None, None, False, ""

    async def run_in_chat(self, chat_id: str, title: str, text: str, emit: Emit, confirm: Confirm,
                          plan: bool = False, think: bool | None = None,
                          approved_plan: str = "") -> tuple[str, str]:
        """Für Routinen und Telegram: Aufgabe in einem eigenen Chat erledigen – der aktive Chat des Nutzers bleibt
        unberührt. Liefert (Antwort, Chat-ID)."""
        async with self.lock:
            self._plan, self._approved_plan = plan, approved_plan
            self._set_think(think)
            self._tainted = False
            try:
                with self.memory.in_chat(chat_id, title) as conv:
                    answer = await self._run(text, emit, confirm)
                    return answer, conv.chat_id
            finally:
                self._think, self._think_level, self._plan, self._approved_plan = None, None, False, ""

    async def compact_now(self, emit: Emit, focus: str = "") -> bool:
        """„/compact [Fokus]“: den offenen Chat jetzt zusammenfassen (wie Claude Code)."""
        async with self.lock:
            return await self.compact_epoch(emit, reason="manual", focus=focus)

    def _relevant(self, question: str, hit) -> bool:
        """Passt die Erinnerung wirklich zur Frage? Fakten stehen ohnehin im Systemprompt."""
        if hit.kind == "fact":
            return False
        if hit.sim is not None:
            return hit.sim >= self.cfg.memory.retrieval_min_similarity
        return shared_words(question, hit.text) >= 2  # ohne Embeddings: mindestens zwei gleiche Wörter

    async def _memories_for(self, user_text: str) -> str:
        """Erinnerungen für die Kontext-Notiz: in der ersten Frage einer Epoche bis retrieval_max_tokens, danach ein
        Drittel und nur noch Neues (sie bleiben ja im Verlauf stehen); im Coding-Modus keine – dort holt recall."""
        if self.mode == "coding":
            return ""
        conv = self.memory.conversation
        hits = [h for h in await self.memory.retrieve(user_text, exclude_after=conv.window_start())
                if self._relevant(user_text, h)]
        ep = conv.epoch
        first = not any(conv.history[i]["role"] == "user" for i in conv.epoch_indices())
        most = min(self.cfg.memory.retrieval_max_tokens, self.plan().memories)  # kleines Fenster: weniger
        budget = most if first else most // 3
        shown = set(ep.get("shown") or [])
        taken: list[int] = []
        text = self.memory.format_hits([h for h in hits if h.id not in shown], budget, taken)
        ep["shown"] = sorted(shown | set(taken))
        return text

    # ---------- Lernen aus Erfahrung (siehe memory/lessons.py) ----------
    async def _lessons_for(self, user_text: str) -> tuple[str, list[str]]:
        """Passende Lektionen für die Kontext-Notiz – festes, kleines Budget (context_plan.lessons); was in dieser
        Epoche schon mitging, kommt nicht noch einmal. Meist: keine."""
        store = getattr(self.memory, "lessons", None)
        if not self.cfg.memory.learning or store is None or not store.lessons:
            return "", []
        plan = self.plan()
        ep = self.memory.conversation.epoch
        groups = {u.split(":")[0] for u in toolselect.units_for([user_text], False)}
        found = await store.relevant(user_text, groups, k=plan.lessons_count, skip=set(ep.get("lessons_shown") or []))
        lines, ids, used = [], [], 0
        for x in found:
            line = f"- {x.text}"
            if used + est_tokens(line) > plan.lessons:
                break
            lines.append(line)
            ids.append(x.id)
            used += est_tokens(line)
        if ids:
            ep["lessons_shown"] = sorted(set(ep.get("lessons_shown") or []) | set(ids))
            store.mark_used(ids)
        return "\n".join(lines), ids

    def _turn_record(self, user_text: str, notes: list[str], answer: str, groups: set[str]) -> dict:
        return {"user": user_text[:600], "steps": [n[:240] for n in notes[-12:]], "answer": answer[:800],
                "groups": sorted(groups), "lessons": list(self._turn_lesson_ids)}

    def feedback(self, msg_id: str, good: bool, text: str = "") -> bool:
        """👍/👎 zu einer Antwort: 👍 bestätigt die mitgegebenen Lektionen, 👎 wird zum Lern-Kandidaten."""
        store = getattr(self.memory, "lessons", None)
        turn = self._turns.get(msg_id)
        if store is None or turn is None:
            return False
        if good:
            store.mark_helped(turn["lessons"])
        else:
            store.mark_helped(turn["lessons"], -1)
            if self.cfg.memory.learning:
                store.add_candidate({"kind": "feedback", **turn, "correction": text[:500]})
        return True

    async def reflect_lessons(self, limit: int = 3) -> int:
        """Im Leerlauf: über Fehler, Korrekturen und 👎 nachdenken → je höchstens eine kurze Lektion. Liefert die
        Zahl neuer bzw. zusammengeführter Lektionen."""
        store = getattr(self.memory, "lessons", None)
        if store is None or not self.cfg.memory.learning or not store.pending():
            return 0
        learned = 0
        for cand in store.take_candidates(limit):
            messages = [{"role": "system", "content": prompts.text(self.cfg, "reflect_system")},
                        {"role": "user", "content": prompts.reflect_request(self.cfg, cand)}]
            try:
                reply = await self.llm.chat(messages)
            except Exception as e:  # noqa: BLE001 – dann eben später
                store.add_candidate(cand)
                log.info("Nachdenken über einen Fehler verschoben: %s", e)
                break
            lesson = prompts.parse_lesson(reply)
            if lesson:
                await store.add(lesson, cand.get("groups") or [], cand.get("kind", "error"))
                learned += 1
        store.prune()
        self._cache_owner = None  # der Server hatte dafür einen anderen Prompt im Cache
        return learned

    async def _run(self, user_text: str, emit: Emit, confirm: Confirm, images: list[str] | None = None) -> str:
        now = datetime.now()
        self._turn_time = now.strftime("%H:%M")
        self._turn_stamp = prompts.note_stamp(self.cfg, now)
        conv = self.memory.conversation
        # Steht noch Mail-Text im Prompt, kann er auch in späteren Anfragen wirken – dann bleibt der Schutz an
        self._tainted = any(m.get("role") == "tool" and is_taint_source(m.get("tool_name", ""), m.get("full_content")
                                                                       or m.get("content") or "")
                            for m in conv.epoch_messages())
        start_len = len(conv.history)
        await emit({"type": "state", "state": "thinking"})
        self._turn_memories = await self._memories_for(user_text)
        self._turn_lessons, self._turn_lesson_ids = await self._lessons_for(user_text)
        store = getattr(self.memory, "lessons", None)
        if (store is not None and self.cfg.memory.learning and self._last_turn and CORRECTION.match(user_text)
                and len(user_text.split()) >= 3):
            # Korrektur der vorigen Antwort → darüber im Leerlauf nachdenken; die damals mitgegebenen Lektionen
            # haben offenbar nicht geholfen
            store.add_candidate({"kind": "correction", **self._last_turn, "correction": user_text[:500]})
            store.mark_helped(self._last_turn.get("lessons") or [], -1)
        await self._check_vision()
        conv.add({"role": "user", "content": user_text, **({"attachments": list(images)} if images else {})})
        self._turn_open = True
        spoken: list[str] = []
        tool_notes: list[str] = []
        msg_id = uuid.uuid4().hex[:8]
        await emit({"type": "assistant_start", "id": msg_id, "plan": self._plan})
        try:
            seen: dict[str, int] = {}  # gleiche Tool-Aufrufe zählen (Schleifenerkennung)
            failures, recovered = 0, False  # fehlgeschlagene Werkzeug-Aufrufe, danach noch ein erfolgreicher?
            retried = False
            finished = False
            used_tools: set[str] = set()
            for _ in range(max(1, self.cfg.tools.max_steps)):
                added = self.choose_tools(used_tools)
                if added or self._tools_dropped:
                    await self._tools_added(emit, msg_id, added, self._tools_dropped)
                if self._over_limit():  # kurz vor dem Limit: Platz schaffen (ausblenden oder zusammenfassen)
                    await self._make_room(emit, msg_id)
                try:
                    content, calls = await self._step(emit, msg_id)
                except ContextOverflow as e:
                    # Server meldet „zu groß“ (z. B. kleiner als eingestellt): Platz schaffen und weitermachen
                    if retried or not await self._make_room(emit, msg_id, overflow=e):
                        raise
                    retried = True
                    continue
                retried = False
                entry = {"role": "assistant", "content": content}
                if calls:
                    entry["tool_calls"] = calls
                conv.add(entry)
                if content:
                    spoken.append(content)
                if not calls:
                    finished = True
                    break
                await emit({"type": "segment_end", "id": msg_id})
                looping = False
                done: dict[int, tuple[str, str, str]] = {}
                for i, call in enumerate(calls):
                    fn = call.get("function", {})
                    used_tools.add(fn.get("name", ""))
                    key = fn.get("name", "") + json.dumps(fn.get("arguments"), sort_keys=True, ensure_ascii=False)
                    seen[key] = seen.get(key, 0) + 1
                    if seen[key] >= 3:
                        # Nicht noch einmal ausführen – das Modell dreht sich im Kreis
                        name = fn.get("name", "")
                        done[i] = (name, prompts.text(self.cfg, "repeat_skipped"), f"{name}: Wiederholung übersprungen")
                        looping = looping or seen[key] >= 4
                # Mehrere Aufrufe ohne Rückfrage (lesend) gleichzeitig – das Modell hat sie ohnehin unabhängig
                # voneinander geplant. Bringt einer fremde Inhalte (Mail, Bildschirm), bleibt es bei der Reihenfolge.
                todo = [i for i in range(len(calls)) if i not in done]
                if len(todo) > 1 and not any(c.get("function", {}).get("name") in TAINT_SOURCES for c in calls):
                    quick = [i for i in todo if self._runs_unasked(calls[i])]
                    if len(quick) > 1:
                        results = await asyncio.gather(*(self._execute(calls[i], emit, confirm) for i in quick))
                        done.update(zip(quick, results))
                for i, call in enumerate(calls):
                    name, result, note = done[i] if i in done else await self._execute(call, emit, confirm)
                    conv.add({"role": "tool", "content": result, "tool_name": name})
                    tool_notes.append(note)
                    if FAILED_RESULT.match(result or ""):
                        failures += 1
                    elif failures:
                        recovered = True
                if looping:
                    break
            if not finished:
                # Limit erreicht oder Schleife: ohne Tools zusammenfassen lassen, statt hart abzubrechen
                content, _ = await self._step(emit, msg_id, final=True)
                if not content:
                    content = prompts.text(self.cfg, "paused")
                    await emit({"type": "token", "id": msg_id, "text": content})
                hint = prompts.text(self.cfg, "continue_hint")
                await emit({"type": "token", "id": msg_id, "text": "\n\n" + hint})
                content = f"{content}\n\n{hint}"
                conv.add({"role": "assistant", "content": content})
                spoken.append(content)
        except LLMError as e:
            self._turn_open = False
            if len(conv.history) <= start_len + 1:
                del conv.history[start_len:]
            else:  # erledigte Schritte behalten – mit „weiter“ geht es dort weiter
                self._repair_history()
                conv.add({"role": "assistant", "content": prompts.text(self.cfg, "interrupted").format(error=e)})
                conv.save()
            await emit({"type": "error", "text": str(e)})
            await emit({"type": "assistant_end", "id": msg_id, "text": ""})
            await emit({"type": "state", "state": "idle"})
            return ""
        except asyncio.CancelledError:
            self._turn_open = False
            self._repair_history()
            conv.add({"role": "assistant", "content": "(Vom Nutzer abgebrochen.)"})
            conv.save()
            await self.memory.log_exchange(user_text, "\n\n".join(spoken + ["(abgebrochen)"]), tool_notes)
            await emit({"type": "assistant_end", "id": msg_id, "text": "\n\n".join(spoken), "cancelled": True})
            await emit({"type": "state", "state": "idle"})
            raise
        self._turn_open = False
        self._note_recent()

        answer = "\n\n".join(spoken)
        groups = {self.groups_of.get(n, "") for n in used_tools} - {""}
        record = self._turn_record(user_text, tool_notes, answer, groups)
        self._turns[msg_id] = record
        while len(self._turns) > 30:
            self._turns.pop(next(iter(self._turns)))
        self._last_turn = record
        if store is not None and self.cfg.memory.learning:
            if failures and (recovered or not finished):  # erst schiefgegangen, dann anders gelöst (oder gar nicht)
                store.add_candidate({"kind": "error", **record})
            elif not failures and self._turn_lesson_ids:
                store.mark_helped(self._turn_lesson_ids)  # glatt gelaufen – die Lektionen haben (wohl) geholfen
        await emit({"type": "assistant_end", "id": msg_id, "text": answer, "feedback": True})
        if self._plan and answer.strip():
            await emit({"type": "plan", "id": msg_id, "text": answer, "steps": plan_steps(answer)})
        # Antwort ist fertig – das Nachbereiten (Tagebuch, ggf. Komprimieren) läuft still im Hintergrund
        await emit({"type": "state", "state": "idle"})
        conv.save()
        await self.memory.log_exchange(user_text, answer, tool_notes)
        if self.cfg.memory.compact_idle and self._over_limit(idle=True):
            # Fast voll: jetzt in Ruhe Platz schaffen (und vorwärmen), damit die nächste Frage nicht darauf wartet
            try:
                await self._make_room(emit, msg_id, idle=True)
            except Exception as e:  # noqa: BLE001
                log.warning("Platz schaffen nach der Antwort fehlgeschlagen: %s", e)
        return answer

    async def _tools_added(self, emit: Emit, msg_id: str, groups: set[str], dropped: set[str] = frozenset()) -> None:
        tid = uuid.uuid4().hex[:8]
        await emit({"type": "llm_phase", "id": tid, "msg": msg_id, "phase": "tools_added", "groups": sorted(groups),
                    "dropped": sorted(dropped)})
        await emit({"type": "llm_phase", "id": tid, "msg": msg_id, "phase": "done", "tools_added": sorted(groups),
                    "tools_dropped": sorted(dropped), "seconds": 0})

    # ---------- Platz schaffen: erst ausblenden (Microcompact), dann zusammenfassen (Auto-Compact) ----------
    async def _make_room(self, emit: Emit, msg_id: str, idle: bool = False, overflow: ContextOverflow | None = None,
                         ) -> bool:
        """Wie Claude Code: Zuerst alte Werkzeug-Ergebnisse ausblenden – das braucht keinen Modellaufruf, neu
        eingelesen wird nur ab dem ersten ausgeblendeten Ergebnis. Nur wenn das nicht genug Luft schafft, fasst das
        Modell den Chat zusammen. idle: nach der Antwort (danach gleich vorwärmen). overflow: der Server meldete
        „zu groß“ – dann reicht Ausblenden, wenn es den Überhang abdeckt."""
        if await self._clear_old_results(emit, msg_id, overflow=overflow):
            if idle:
                await self._prewarm_locked(emit)
            return True
        if not self.can_compact():
            # Überlauf, aber nichts zum Zusammenfassen: ausblenden, was geht – den Rest kürzt notfalls die Notbremse
            return overflow is not None and await self._clear_old_results(emit, msg_id, force=True)
        reason = "overflow" if overflow else "idle" if idle else "full"
        return await self.compact_epoch(emit, msg_id, in_turn=not idle, reason=reason)

    def _clear_candidates(self) -> tuple[list[int], int]:
        """Was Ausblenden brächte: (Indizes, eingesparte echte Token) – ohne etwas zu ändern."""
        conv = self.memory.conversation
        plan = self.plan()
        idx = conv.clearable(int(plan.keep_share * plan.window / self.token_ratio()))
        freed = 0
        for i in idx:
            m = conv.history[i]
            if m["role"] == "tool":
                freed += msg_tokens(m) - msg_tokens({**m, "content": self._cleared_stub(m, dry=True)})
            else:
                from .memory.context import _short_call
                freed += msg_tokens(m) - msg_tokens({**m, "tool_calls": [_short_call(c) for c in m["tool_calls"]]})
        return idx, int(freed * self.token_ratio())

    def _cleared_stub(self, m: dict, dry: bool = False) -> str:
        """Platzhalter für ein ausgeblendetes Ergebnis: Anfang, Länge und wo das Ganze liegt (read_file)."""
        from .tools import proc
        content = m.get("full_content") or m.get("content") or ""
        saved = proc.SAVED_NOTE.search(content)
        if saved:
            path = saved.group(1)
        else:
            path = "~/.cache/orbwise/outputs/xxxxxxxxxxxxxxxxxxxxxxxx.txt" if dry else \
                proc.save_output(content, m.get("tool_name") or "ausgabe")
        limit = self.plan().stub_chars
        head = content[:limit]
        if len(content) > limit and "\n" in head[limit // 2:]:
            head = head[:head.rfind("\n")]  # an einem Zeilenende abschneiden
        key = "cleared_saved" if path else "cleared_again"
        return prompts.text(self.cfg, key).format(chars=len(content), head=head.strip(), path=path,
                                                  tool=m.get("tool_name") or "")

    def _typical_step(self) -> int:
        """Typische Größe eines Werkzeug-Schritts dieses Chats (echte Token) – so viel braucht der nächste."""
        conv = self.memory.conversation
        sizes = [sum(msg_tokens(conv.history[i]) for i in [call, *results]) for call, results in conv.tool_steps()[-3:]]
        return int(max(300, sum(sizes) / len(sizes) if sizes else 0) * self.token_ratio())

    async def _clear_old_results(self, emit: Emit, msg_id: str, overflow: ContextOverflow | None = None,
                                 force: bool = False) -> bool:
        """Alte Werkzeug-Ergebnisse ausblenden – wenn es genug bringt: mindestens 10 % des Fensters, und danach muss
        mindestens ein weiterer typischer Schritt Platz haben (sonst wäre gleich wieder Schluss und das Neueinlesen
        umsonst). Ausblenden kostet nur das Neueinlesen ab dem ersten ausgeblendeten Ergebnis – Zusammenfassen
        zusätzlich das Schreiben der Zusammenfassung (auf lokaler Hardware oft eine Minute). Bei Überlauf: wenn es
        den Überhang abdeckt."""
        idx, freed = self._clear_candidates()
        if not idx:
            return False
        window, limit = self.model_window(), self.compact_limit()
        self.build_messages(display=False)
        before = self._full_prompt
        if force:
            pass
        elif overflow is not None:
            if not (overflow.n_prompt and overflow.n_ctx):
                return False
            needed = overflow.n_prompt - (overflow.n_ctx - self.answer_reserve())
            if freed < needed:
                return False
        elif freed < 0.1 * window or limit - (before - freed) < 1.2 * self._typical_step():
            return False
        conv = self.memory.conversation
        n = conv.clear_results(idx, self._cleared_stub)
        self._cache_owner = None  # der Server kennt diesen Anfang noch nicht – nach der Antwort vorwärmen
        conv.save()
        self.build_messages()
        after = self._full_prompt
        cid = uuid.uuid4().hex[:8]
        await emit({"type": "llm_phase", "id": cid, "msg": msg_id, "phase": "clear", "results": n, "tokens": before})
        await emit({"type": "llm_phase", "id": cid, "msg": msg_id, "phase": "done", "cleared": n, "before": before,
                    "after": after, "seconds": 0})
        await emit({"type": "context", **(self.last_context or {})})
        log.info("Alte Werkzeug-Ergebnisse ausgeblendet: %s Ergebnisse, %s → %s Token", n, before, after)
        return True

    def _over_limit(self, idle: bool = False) -> bool:
        """Würde der nächste Prompt die Grenze erreichen? Nach einer Antwort (idle) schon dann, wenn eine typische
        weitere Runde dieses Chats sie reißen würde – dann lieber jetzt in Ruhe als später, während jemand wartet."""
        self.build_messages(display=False)
        limit = self.compact_limit()
        if idle:
            # nur wenn es wirklich fast voll ist: eine große Aufgabe als bisher einzige Runde ist kein Maßstab für
            # die nächste Frage (sonst wurde schon bei 8k von 16k komprimiert) – große Aufgaben komprimieren
            # unterwegs an der Grenze
            conv = self.memory.conversation
            turns = max(1, sum(1 for m in conv.epoch_messages() if m["role"] == "user"))
            typical = max(500, int(conv.history_tokens() * self.token_ratio() / turns))
            next_turn = min(typical, int(0.06 * self.model_window()))
            return self._full_prompt + next_turn >= limit
        return self._full_prompt >= limit

    def can_compact(self) -> bool:
        """Lohnt eine Komprimierung? Nur wenn seit der letzten etwas dazukam und der Verlauf groß genug ist, dass
        sich das einmalige Neueinlesen lohnt (wie „clear_at_least“ beim Context-Editing) – System-Prompt und
        Werkzeuge kann sie nicht verkleinern."""
        conv = self.memory.conversation
        marker = (conv.chat_id, len(conv.epochs), len(conv.history))
        if marker == self._compacted_at or len(conv.epoch_indices()) <= conv.epoch.get("carried", 0) + 1:
            return False
        worth = max(int(0.25 * self.model_window()), self.summary_budget() + 1500)
        return conv.history_tokens() * self.token_ratio() >= worth

    def _last_steps(self, q: int) -> list[int]:
        """Indizes des letzten Werkzeug-Schritts (Aufruf + Ergebnisse) nach der Frage q."""
        hist = self.memory.conversation.history
        after = [i for i in range(q + 1, len(hist)) if not hist[i].get("packed")]
        calls = [i for i in after if hist[i]["role"] == "assistant" and hist[i].get("tool_calls")]
        return [i for i in after if i >= calls[-1]] if calls else []

    def _compact_appendix(self, messages: list[dict]) -> str:
        """Was nicht vom Modell abhängt: die letzten Nutzernachrichten (gekürzt) und berührte Dateien/Ordner – im
        kleinen Fenster weniger und kürzer."""
        plan = self.plan()
        cut = plan.appendix_chars
        users = [" ".join((m.get("content") or "").split()) for m in messages if m["role"] == "user"]
        users = [u if len(u) <= cut else u[:cut - 3] + "…" for u in users if u][-plan.appendix_users:]
        paths: list[str] = []
        for m in messages:
            for call in m.get("tool_calls") or []:
                args = call.get("function", {}).get("arguments")
                for key in ("path", "target", "directory"):
                    value = args.get(key) if isinstance(args, dict) else None
                    if isinstance(value, str) and value and value not in paths:
                        paths.append(value)
        parts = []
        if users:
            parts.append(prompts.compact_text(self.cfg, "user_list") + "\n" + "\n".join(f"- „{u}“" for u in users))
        if paths:
            parts.append(prompts.compact_text(self.cfg, "files") + "\n" +
                         "\n".join(f"- {p}" for p in paths[-plan.appendix_paths:]))
        todos = self.memory.conversation.meta.get("todos")
        if todos and any(t.get("state") != "done" for t in todos):  # offene Aufgabenliste geht mit
            from .tools.todo_tools import format_todos
            parts.append(prompts.compact_text(self.cfg, "todos") + "\n" + format_todos(todos))
        return "\n\n".join(parts)

    def _recent_files(self, messages: list[dict], budget: int) -> str:
        """Coding ab 16k (wie Claude Code): aktueller Inhalt der zuletzt bearbeiteten bzw. gelesenen Dateien, frisch
        von der Platte – nach dem Zusammenfassen muss das Modell sie nicht erneut lesen. budget: Token für alle."""
        from .memory.context import _args
        paths: list[str] = []
        for m in reversed(messages):
            for call in reversed(m.get("full_tool_calls") or m.get("tool_calls") or []):
                fn = call.get("function", {})
                path = _args(call).get("path") if fn.get("name") in ("edit_file", "write_file", "read_file") else None
                if isinstance(path, str) and path and path not in paths:
                    paths.append(path)
        found: list[tuple[str, str]] = []
        for raw in paths:
            if len(found) == 3:
                break
            path = Path(os.path.expanduser(raw))
            if not path.is_absolute() and self.project_dir():
                path = Path(self.project_dir()) / path
            try:
                text = path.read_text(encoding="utf-8", errors="replace") if path.is_file() else ""
            except OSError:
                continue
            if text and "\x00" not in text[:4096]:
                found.append((raw, text))
        if not found:
            return ""
        share = budget * 3 // len(found)
        blocks = []
        for raw, text in found:
            shown = text if len(text) <= share else text[:share].rsplit("\n", 1)[0]
            more = "" if shown == text else "\n" + prompts.compact_text(self.cfg, "file_more").format(
                offset=shown.count("\n") + 2)
            blocks.append(f"### {raw}\n```\n{shown}\n```{more}")
        return prompts.compact_text(self.cfg, "current_files") + "\n" + "\n\n".join(blocks)

    async def compact_epoch(self, emit: Emit, msg_id: str = "", in_turn: bool = False, reason: str = "full",
                            focus: str = "") -> bool:
        """Das Modell fasst den bisherigen Chat strukturiert zusammen – mit genau dem Prompt, den der Server schon im
        Cache hat, plus einer Anweisung (es liest also kaum Neues ein). Danach beginnt eine neue Epoche:
        Zusammenfassung im System-Prompt, die aktuelle Frage und die letzten Schritte wörtlich. Nichts wird
        gelöscht (Chat-Datei, Oberfläche, Journal und Suchindex behalten alles); mitten in einer Aufgabe geht es
        danach automatisch weiter."""
        conv = self.memory.conversation
        hist = conv.history
        idx = conv.epoch_indices()
        if len(idx) < 2:
            return False
        q = conv.turn_start()
        open_question = in_turn and q == len(hist) - 1  # Frage noch ohne Antwort – sie kommt in die neue Epoche
        if open_question:
            carry = [q]
        elif in_turn:
            carry = [q] + self._last_steps(q)
        else:
            last = len(hist) - 1
            final = last > q and hist[last]["role"] == "assistant" and not hist[last].get("tool_calls")
            carry = [q] + ([last] if final else [])
        carry = [i for i in carry if i in set(idx)]
        if not [i for i in idx if i not in set(carry)]:
            return False  # alles ginge ohnehin wörtlich mit – nichts zu komprimieren
        summarized = [hist[i] for i in idx if not (open_question and i == q)]

        window = self.model_window()
        ratio = self.token_ratio()
        plan = self.plan()
        messages = self.build_messages(display=False, trim=False)  # = was der Server schon kennt (+ neue Ergebnisse)
        before = self._full_prompt
        if open_question:
            messages = messages[:-1]
        # Zusammenfassung so lang, wie Platz ist (spät komprimiert = etwas kürzer), aber mindestens summary_floor
        # (gemessen an genau dieser Anfrage: Nachrichten + Werkzeuge)
        request_t = int((sum(msg_tokens(m) for m in messages) + self.schema_tokens) * ratio)
        free = window - request_t - plan.instruction_reserve - 100
        # knapp (z. B. die letzte Antwort schob den Prompt über die Grenze): lieber kürzer zusammenfassen als
        # Nachrichten weglassen – das würde den Cache brechen
        budget = max(plan.summary_min, min(plan.summary_max, free))
        instruction = prompts.compact_instruction(self.cfg, self.mode, int(budget * 0.6), focus, small=plan.small)
        # Passt die Anfrage samt Zusammenfassung nicht (riesige neue Ergebnisse, Server kleiner als gedacht):
        # erst die neuesten Werkzeug-Ergebnisse kürzen (die kennt der Server noch nicht), notfalls Älteres weglassen
        room = int(window / ratio) - budget - est_tokens(instruction) - self.schema_tokens - 100  # + Werkzeuge
        excess = sum(msg_tokens(m) for m in messages) - room
        if excess > 0:
            shrink_tool_results([m for m in reversed(messages) if m["role"] == "tool"], excess)
            while len(messages) > 2 and sum(msg_tokens(m) for m in messages) > room:
                del messages[1]
            while len(messages) > 1 and messages[1]["role"] == "tool":
                del messages[1]
        request = messages + [{"role": "user", "content": instruction}]

        cid, started = uuid.uuid4().hex[:8], time.monotonic()
        await emit({"type": "state", "state": "thinking"})
        await emit({"type": "llm_phase", "id": cid, "msg": msg_id, "phase": "compress", "tokens": before,
                    "percent": round(100 * before / window) if window else None, "steps": len(summarized),
                    "reason": reason})
        summary, written = await self._summarize(request, emit, cid, msg_id, budget)
        if not summary:  # Modell lieferte nichts: grob, aber ohne Verlust des Wichtigsten
            summary = ((conv.running_summary + "\n\n") if conv.running_summary else "") + \
                render_transcript(summarized, max_chars=budget * 3)
        appendix = self._compact_appendix(summarized)
        summary = summary.strip()[: budget * 4] + (f"\n\n{appendix}" if appendix else "")

        # Mitgenommene Schritte begrenzen: lange Ergebnisse kürzen (das Original bleibt in der Chat-Datei)
        carry_room = int(window * plan.carry_share / ratio)
        steps = [i for i in carry if hist[i]["role"] == "tool"]
        excess = sum(msg_tokens(hist[i]) for i in carry) - carry_room
        for i in steps:
            if excess <= 0:
                break
            content = hist[i].get("content") or ""
            keep = max(400, len(content) - excess * 3)
            if keep < len(content):
                hist[i].setdefault("full_content", content)
                hist[i]["content"] = clip(content, keep)
                excess -= (len(content) - keep) // 3
        # Notiz der mitgenommenen Frage neu (ohne alte Erinnerungen) – der Prompt wird ohnehin neu gelesen
        if q in carry and not open_question and hist[q].get("note_meta"):
            meta = hist[q]["note_meta"]
            hist[q]["note"] = prompts.context_note(
                self.cfg, meta.get("time", ""), "", meta.get("plan", False), meta.get("approved", ""),
                extra=(meta.get("hint", "") + "\n" + (prompts.compact_text(self.cfg, "continue") if in_turn else "")),
                lessons=meta.get("lessons", ""))
        turn_tools = {c.get("function", {}).get("name", "") for m in hist[q:] for c in m.get("tool_calls") or []} - {""}
        # ab 16k: die Werkzeugauswahl bleibt (passt sie in die Grenze, bleiben Systemprompt und Werkzeuge im Cache)
        inherit = conv.epoch.get("groups") if not plan.small else None
        files = self._recent_files(summarized, int(plan.window * 0.08)) \
            if self.mode == "coding" and not plan.small else ""
        # was zuletzt gebraucht wurde, gilt auch nach dem Zusammenfassen (sonst fehlen z. B. bei der nächsten
        # Paperless-Frage die Paperless-Werkzeuge)
        old = conv.epoch
        used_units = sorted(set().union(*(set(u) for u in old.get("used") or []), self._turn_used))
        conv.start_epoch(carry, summary, reason=reason, before=before, carried=len(carry), mode=self.mode,
                         inherit=inherit, files=files, recent=old.get("recent") or [], used=[used_units] if used_units
                         else [], pinned=old.get("pinned") or [])
        # Werkzeuge (und ihre Hinweise) der neuen Epoche gleich festlegen – samt denen dieser Runde, damit der
        # nächste Schritt nichts „dazuladen“ muss und Anzeige wie Vorwärmen den echten Prompt sehen
        self.choose_tools(turn_tools)
        self._compacted_at = (conv.chat_id, len(conv.epochs), len(hist))
        self._cache_owner = None
        conv.save()
        self.build_messages()
        after = self._full_prompt
        await emit({"type": "context", **(self.last_context or {})})
        seconds = round(time.monotonic() - started, 1)
        await emit({"type": "llm_phase", "id": cid, "msg": msg_id, "phase": "done", "compress": True,
                    "steps": len(summarized), "before": before, "after": after, "tokens": written,
                    "seconds": seconds})
        await emit({"type": "compacted", "chat": conv.chat_id, "summary": summary, "reason": reason,
                    "before": before, "after": after, "seconds": seconds})
        log.info("Kontext komprimiert (%s): %s → %s Token in %.1f s", reason, before, after, seconds)
        if not in_turn:
            await self._prewarm_locked(emit)  # neuer Anfang gleich einlesen, solange niemand wartet
        return True

    async def _summarize(self, request: list[dict], emit: Emit, cid: str, msg_id: str,
                         budget: int) -> tuple[str, int]:
        """Zusammenfassung streamen (Fortschritt in der Aktivität). Gleiche Werkzeuge wie zuletzt, damit der
        Server seinen Cache nutzt – aber tool_choice "none" und ohne Denkkette. Ruft das Modell trotzdem ein
        Werkzeug auf (Ollama kennt tool_choice nicht), einmal ohne Werkzeuge wiederholen."""
        for tools in (self.schemas, None):
            parts: list[str] = []
            last, result = 0.0, {}
            try:
                async for ev in self._call_llm(request, tools, think=False, max_tokens=budget,
                                               tool_choice="none" if tools else None):
                    if ev["type"] == "token":
                        parts.append(ev["text"])
                        if time.monotonic() - last >= 0.5:
                            last = time.monotonic()
                            await emit({"type": "llm_phase", "id": cid, "msg": msg_id, "phase": "compress",
                                        "written": len("".join(parts)) // 3})
                    elif ev["type"] == "done":
                        result = ev.get("message") or {}
            except LLMError as e:  # auch „zu groß“ – dann fasst der Aufrufer ohne Modell zusammen
                log.warning("Zusammenfassen fehlgeschlagen: %s", e)
                return "", 0
            text = strip_think(result.get("content") or "".join(parts)).strip()
            if RAW_TOOL_CALL.match(text):  # keine Zusammenfassung, sondern ein Aufruf – ohne Werkzeuge wiederholen
                log.info("Beim Zusammenfassen kam ein Werkzeug-Aufruf – noch einmal ohne Werkzeuge")
            elif text or not result.get("tool_calls"):
                return text, est_tokens(text)
        return "", 0

    # ---------- Vorwärmen ----------
    def _cache_key(self) -> tuple:
        conv = self.memory.conversation
        return (self._profile_key(), conv.chat_id, len(conv.epochs))

    def cache_cold(self) -> bool:
        """Hat der Modell-Server gerade etwas anderes im Cache als den offenen Chat?"""
        return self._cache_key() != self._cache_owner

    def busy(self) -> bool:
        """Läuft eine Anfrage? (Vorwärmen zählt nicht – es blockiert nichts, die nächste Frage nutzt es.)"""
        return self.lock.locked() and not self._prewarming

    async def prewarm(self, emit: Emit | None = None) -> bool:
        """Den Prompt-Anfang des offenen Chats im Leerlauf einlesen lassen (nach Telegram/Routinen, Chatwechsel,
        Start oder sobald getippt wird) – die nächste Frage liest dann nur noch ihren neuen Teil ein."""
        if self.lock.locked() or getattr(self.llm, "switching", None) or not self.cache_cold() \
                or getattr(self.llm, "gpu_borrowed", None):  # Bild-Modus hat gerade den Grafikspeicher
            return False
        async with self.lock:
            self._prewarming = True
            try:
                return await self._prewarm_locked(emit)
            finally:
                self._prewarming = False

    async def _prewarm_locked(self, emit: Emit | None = None) -> bool:
        if not self.cache_cold():
            return False
        key = self._cache_key()  # vorher merken – der Nutzer könnte währenddessen den Chat wechseln
        await self._check_vision()
        self.choose_tools(set())
        messages = self.build_messages(display=False)
        if messages[-1]["role"] != "user":
            # Leere Nutzernachricht ans Ende: so liegt der Verlauf samt letzter Antwort im Cache, wie die nächste
            # Frage ihn sendet. Eine Antwort am Ende setzt llama-server sonst als angefangene Antwort fort (anders
            # formatiert, bei Werkzeug-Aufrufen ein Fehler); ein leerer Chat braucht für manche Vorlagen ohnehin eine.
            messages.append({"role": "user", "content": ""})
        pid, started = uuid.uuid4().hex[:8], time.monotonic()
        tokens = self._full_prompt
        if emit:
            phase = {"type": "llm_phase", "id": pid, "phase": "prewarm", "tokens": tokens}
            tps = self._prefill_tps.get(self._profile_key())
            if tps:  # höchstens so lange – liegt der Anfang (Systemprompt, Werkzeuge) im Cache, geht es schneller
                phase["eta_s"] = round(tokens / tps, 1)
            await emit(phase)
        stats: dict = {}
        try:
            async for ev in self._call_llm(messages, self.schemas, think=False, max_tokens=1):
                if ev["type"] == "done":
                    stats = ev.get("stats") or {}
        except Exception as e:  # noqa: BLE001 – Vorwärmen ist nur eine Beschleunigung
            log.info("Vorwärmen übersprungen: %s", e)
            if emit:
                await emit({"type": "llm_phase", "id": pid, "phase": "done", "prewarm": True, "error": True,
                            "seconds": round(time.monotonic() - started, 1)})
            return False
        self._learn_prefill(stats)
        self._cache_owner = key
        if emit:
            done = {"type": "llm_phase", "id": pid, "phase": "done", "prewarm": True,
                    "tokens": stats.get("prompt_total") or tokens, "seconds": round(time.monotonic() - started, 1)}
            if stats.get("prompt_tokens") is not None and stats.get("prompt_total"):
                done["new"] = stats["prompt_tokens"]  # wirklich eingelesen – der Rest kam aus dem Cache
            await emit(done)
        return True

    # ---------- Freigaben ----------
    @staticmethod
    def _auto_ok(name: str, args: dict, cwd: str | None = None) -> tuple[bool, str]:
        """Auto „Auto“: alles ohne Root – außer Löschen, Ausschalten, Senden ins Netz, Startdateien/Zugangsdaten."""
        from .lang import T
        from .tools.filepolicy import protected_path, writable_path
        from .tools.safety import auto_shell_ok
        if name == "run_shell":
            return auto_shell_ok(str(args.get("command") or ""), cwd)
        path = str(args.get("path") or "")
        if protected_path(path, cwd):
            return False, T("ändert Startdateien, Autostart, Zugangsdaten oder Orbwise selbst – fragt auch im "
                            "Auto-Modus", "changes start-up files, autostart, credentials or Orbwise itself – Auto "
                            "still asks")
        if not writable_path(path, cwd):
            return False, T("nur mit Root-Rechten beschreibbar", "only writable with root privileges")
        return True, ""

    @staticmethod
    def _file_edit_ok(name: str, args: dict, cwd: str | None = None) -> bool:
        """Auto „Dateien“: Dateiänderung im eigenen Home ohne Root/Löschen (siehe tools/filepolicy.py)."""
        from .tools.filepolicy import editable_path
        from .tools.safety import file_edit_ok
        if name in ("write_file", "edit_file"):
            return editable_path(str(args.get("path") or ""), cwd)
        if name == "run_shell":
            return file_edit_ok(str(args.get("command") or ""), cwd)
        return False

    # ---------- Ein Modellschritt ----------
    def _prompt_phase(self) -> dict:
        """Was vor dem ersten Token passiert: Prompt einlesen (mit geschätzter Dauer) oder Modell neu laden."""
        key = self._profile_key()
        tokens = int((self.last_context or {}).get("used", 0) * self.token_ratio())
        last = self._last_prompt.get(key, 0)
        new = tokens - last if 0 < last <= tokens else tokens  # gleicher Anfang liegt im KV-Cache
        phase = {"phase": "prompt", "tokens": tokens, "new": max(new, 0)}
        if self._prefill_tps.get(key):
            phase["eta_s"] = round(phase["new"] / self._prefill_tps[key], 1)
        hist = self.memory.conversation.history
        backend = getattr(getattr(self.llm, "profile", None), "backend", "")
        if (backend == "ollama" and self.cfg.vision.backend == "ollama" and hist and hist[-1].get("role") == "tool"
                and hist[-1].get("tool_name") in ("look_at_screen", "look_at_image")):
            phase["phase"] = "loading"  # Ollama hat fürs Bild das Vision-Modell geladen – jetzt zurück
        return phase

    def _learn_prefill(self, stats: dict) -> None:
        key = self._profile_key()
        if stats.get("prompt_total"):
            self._last_prompt[key] = int(stats["prompt_total"])
        tps, n = stats.get("prompt_tps"), stats.get("prompt_tokens") or 0
        if tps and n >= 200:  # kleine Häppchen messen eher die Latenz als die Geschwindigkeit
            old = self._prefill_tps.get(key)
            self._prefill_tps[key] = round(tps if old is None else 0.7 * old + 0.3 * tps, 1)
            self._save_prefill()

    async def _step(self, emit: Emit, msg_id: str, final: bool = False) -> tuple[str, list]:
        """Ein Modellschritt: streamt Tokens an die UI, liefert (Text, Tool-Aufrufe).
        Nebenbei meldet llm_phase, was das Modell gerade tut (Einlesen, Denken, Aufruf schreiben, Antworten) –
        für die Aktivität, damit lange Pausen nicht wie Stillstand aussehen."""
        filt = ThinkFilter()
        result: dict = {}
        step_id, started = uuid.uuid4().hex[:8], time.monotonic()
        current, last_sent, thought_n = "", 0.0, 0

        async def phase(kind: str, throttle: bool = False, **data) -> None:
            nonlocal current, last_sent
            now = time.monotonic()
            if kind == current and (not throttle or now - last_sent < 0.5):
                return
            current, last_sent = kind, now
            await emit({"type": "llm_phase", "id": step_id, "msg": msg_id, "phase": kind, **data})

        await emit({"type": "state", "state": "thinking"})
        budget = 0 if final else self.think_budget()
        thoughts: list[str] = []
        cut: str | None = None  # Denk-Budget voll: Gedanken, mit denen der Schritt ohne Denken weitergeht

        def over_budget() -> bool:
            return bool(budget) and cut is None and est_tokens("".join(thoughts)) > budget

        for _attempt in range(2):
            try:
                stream = self._stream_fitting(final=final, no_think=cut is not None, tail=cut)
                async for ev in stream:
                    if ev["type"] == "context":
                        await emit(ev)
                        current = ""
                        info = self._prompt_phase()
                        await phase(info.pop("phase"), **info)
                    elif ev["type"] == "retry":
                        await phase("retry", n_prompt=ev.get("n_prompt"), n_ctx=ev.get("n_ctx"))
                    elif ev["type"] == "prompt_progress":
                        todo = max(1, ev["total"] - ev["cache"])
                        current = ""  # jede Fortschrittsmeldung zählt (gedrosselt)
                        if time.monotonic() - last_sent >= 0.5 or ev["processed"] >= ev["total"]:
                            await phase("prompt", tokens=ev["total"], new=todo,
                                        progress=round(min(1.0, max(0, ev["processed"] - ev["cache"]) / todo), 3))
                    elif ev["type"] == "tool_delta":
                        await phase("tool_args", throttle=True, name=ev.get("name", ""), chars=ev.get("chars", 0))
                    elif ev["type"] == "token":
                        text = filt.feed(ev["text"])
                        thought = filt.take_thought()
                        if thought:
                            thought_n += 1
                            thoughts.append(thought)
                            await phase("thinking", throttle=True, tokens=thought_n)
                            await emit({"type": "reasoning", "id": msg_id, "text": thought})
                            if over_budget():
                                break
                        if text:
                            await phase("writing")
                            await emit({"type": "token", "id": msg_id, "text": text})
                    elif ev["type"] == "reasoning":
                        # Denkkette: nicht Teil der Antwort, wird nur angezeigt (Orb-Zoom) und nicht vorgelesen
                        thought_n += 1
                        thoughts.append(ev.get("text", ""))
                        await phase("thinking", throttle=True, tokens=thought_n)
                        await emit({"type": "reasoning", "id": msg_id, "text": ev.get("text", "")})
                        if over_budget():
                            break
                    elif ev["type"] == "done":
                        result = ev["message"]
                        stats = ev.get("stats") or {}
                        self._learn_prefill(stats)
                        self._cache_owner = self._cache_key()  # der Server hat jetzt diesen Chat im Cache
                        current = ""
                        await phase("done", seconds=round(time.monotonic() - started, 1),
                                    calls=[c.get("function", {}).get("name", "") for c in result.get("tool_calls") or []],
                                    **{k: stats.get(k) for k in ("tokens", "tps", "prompt_tokens", "prompt_total",
                                                                 "prompt_cached", "prompt_ms", "load_ms")
                                       if stats.get(k) is not None})
                        if stats.get("tps"):
                            await emit({"type": "llm_stats", **stats})
                        total = prompt_size(stats, (self.last_context or {}).get("used", 0))
                        if self.last_context is not None and total:
                            self.last_context["real"] = total
                            if stats.get("prompt_cached") is not None:
                                self.last_context["cached"] = stats["prompt_cached"]
                            self.learn_tokens(self.last_context.get("used", 0), total)
                            await emit({"type": "context", **self.last_context})
            except LLMError:  # z. B. Kontext zu voll – die Zeile in der Aktivität nicht ewig „läuft“ lassen
                current = ""
                await phase("done", error=True, seconds=round(time.monotonic() - started, 1))
                raise
            await stream.aclose()  # bricht die laufende Anfrage ab, falls das Denk-Budget voll ist
            if cut is not None or not over_budget():
                break
            # genug überlegt: denselben Schritt ohne Denken fortsetzen – die Gedanken gehen als Notiz mit
            cut = prompts.text(self.cfg, "think_cut").format(thoughts=clip("".join(thoughts).strip(), budget * 4))
            filt = ThinkFilter()
            current = ""
            await phase("think_cut", tokens=est_tokens("".join(thoughts)), budget=budget)
        tail = filt.flush()
        if tail:
            await emit({"type": "token", "id": msg_id, "text": tail})
        content = strip_think(result.get("content", ""))
        return content, ([] if final else result.get("tool_calls") or [])

    async def _stream_fitting(self, final: bool = False, no_think: bool = False, tail: str | None = None):
        """Stream eines Schritts. Meldet der Server „zu groß“, entscheidet der Aufrufer: Lässt sich komprimieren,
        geschieht das (stabiler Prompt-Anfang); sonst als Notbremse mit kleinerem Budget gekürzt neu versuchen.
        final=True: ohne Tools, mit der Bitte um eine Zwischenbilanz. no_think/tail: Denk-Budget war voll – ohne
        Denken weiter, die bisherigen Gedanken als vorübergehende Notiz am Ende (nicht im Verlauf)."""
        self._budget_scale = 1.0
        for attempt in range(3):
            try:
                messages = self.build_messages()
                yield {"type": "context", **(self.last_context or {})}
                if final:
                    messages.append({"role": "user", "content": prompts.text(self.cfg, "final_nudge")})
                if tail:
                    messages.append({"role": "user", "content": tail})
                if no_think:
                    kwargs = {"think": False}
                else:
                    kwargs = {} if self._think is None else {"think": self._think}
                    if self._think and self._think_level:
                        kwargs["effort"] = self._think_level
                async for ev in self._call_llm(messages, None if final else self.schemas, **kwargs):
                    yield ev
                return
            except ContextOverflow as e:
                if attempt == 2 or (attempt == 0 and not final and (self.can_compact() or self._clear_candidates()[0])):
                    raise
                yield {"type": "retry", "n_prompt": e.n_prompt, "n_ctx": e.n_ctx}
                ratio = (e.n_ctx - self.answer_reserve()) / e.n_prompt if e.n_ctx and e.n_prompt else 0.7
                self._budget_scale *= max(0.3, min(0.85, ratio * 0.9))
                log.warning("Kontext zu klein (%s/%s Token) – kürze Verlauf und versuche es erneut",
                            e.n_prompt, e.n_ctx)

    def _repair_history(self) -> None:
        """Nach Abbruch: offene Tool-Calls mit Platzhalter-Ergebnissen schließen."""
        hist = self.memory.conversation.history
        for i in range(len(hist) - 1, -1, -1):
            m = hist[i]
            if m["role"] == "assistant" and m.get("tool_calls"):
                answered = sum(1 for x in hist[i + 1:] if x["role"] == "tool")
                for call in m["tool_calls"][answered:]:
                    hist.append({"role": "tool", "content": "[abgebrochen]",
                                 "tool_name": call.get("function", {}).get("name", "")})
                break
            if m["role"] == "user":
                break

    def _final_risk(self, name: str, spec, args: dict, ctx: ToolContext) -> tuple[str, str]:
        """Risiko eines Aufrufs nach Auto-Modus, Planmodus und Schutz vor fremden Inhalten."""
        cwd = ctx.cwd
        risk, reason = spec.assess(ctx, args)
        if risk == CONFIRM and self.auto_mode == "files" and not self._plan and self._file_edit_ok(name, args, cwd):
            risk, reason = SAFE, prompts.text(self.cfg, "auto_files")
        elif risk == CONFIRM and self.auto_mode == "auto" and not self._plan and name in ("run_shell", "write_file", "edit_file"):
            ok, why = self._auto_ok(name, args, cwd)
            risk, reason = (SAFE, prompts.text(self.cfg, "auto_full")) if ok else (risk, why or reason)
        if risk == SAFE and getattr(self, "_tainted", False) and spec.group in TAINT_GUARDED:
            risk, reason = CONFIRM, prompts.text(self.cfg, "tainted_confirm")
        if risk == SAFE and name in ("run_shell", "ssh_run") and self.auto_mode == "off":
            risk, reason = CONFIRM, prompts.text(self.cfg, "auto_read_off")
        return risk, reason

    def _runs_unasked(self, call: dict) -> bool:
        """Läuft dieser Aufruf ohne Rückfrage (und bringt keine fremden Inhalte)? Dann darf er parallel laufen."""
        fn = call.get("function", {})
        name = fn.get("name", "")
        spec = get_tool(name)
        if not spec or not spec.is_enabled(self.cfg) or not self.tool_allowed(name) or name in TAINT_SOURCES:
            return False
        args = coerce_args(spec, fn.get("arguments"))
        if missing_args(spec, args):
            return False
        ctx = ToolContext(cfg=self.cfg, memory=self.memory, cwd=self.project_dir(), output_chars=self.output_chars())
        try:
            return self._final_risk(name, spec, args, ctx)[0] == SAFE
        except Exception:  # noqa: BLE001 – im Zweifel der Reihe nach
            return False

    async def _execute(self, call: dict, emit: Emit, confirm: Confirm) -> tuple[str, str, str]:
        fn = call.get("function", {})
        name = fn.get("name", "")
        call_id = uuid.uuid4().hex[:8]
        spec = get_tool(name)
        if spec and (not spec.is_enabled(self.cfg) or not self.tool_allowed(name)):
            spec = None
        if not spec:
            return name, f"Unbekanntes Tool '{name}'. Verfügbar: {', '.join(n for n, t in self.tools.items() if t.is_enabled(self.cfg) and self.tool_allowed(n))}", f"{name}: unbekannt"
        args = coerce_args(spec, fn.get("arguments"))
        missing = missing_args(spec, args)
        if missing:
            return name, f"Fehlende Parameter: {', '.join(missing)}", f"{name}: Parameter fehlen"
        cwd = self.project_dir()
        ctx = ToolContext(cfg=self.cfg, memory=self.memory, emit=emit, call_id=call_id, services=self.services,
                          cwd=cwd, output_chars=self.output_chars())
        risk, reason = self._final_risk(name, spec, args, ctx)
        args_str = json.dumps(args, ensure_ascii=False)
        await emit({"type": "tool_call", "id": call_id, "name": name, "args": args, "risk": risk, "reason": reason,
                    "group": spec.group})
        if self._plan and risk != SAFE:  # Planmodus: Veränderndes nur vormerken, nicht ausführen, nicht nachfragen
            result = prompts.text(self.cfg, "plan_skipped")
            await emit({"type": "tool_result", "id": call_id, "status": "planned", "text": result})
            return name, result, f"{name} {args_str} → im Plan vorgemerkt"

        if risk == BLOCKED:
            result = f"BLOCKIERT ({reason}). Dieser Befehl wird aus Sicherheitsgründen nie ausgeführt."
            await emit({"type": "tool_result", "id": call_id, "status": "blocked", "text": result})
            return name, result, f"{name} {args_str} → blockiert ({reason})"
        edited: list[str] = []
        if risk == CONFIRM:
            await emit({"type": "state", "state": "confirm"})
            decision = await confirm(call_id, name, args, reason)
            changes = {}
            if isinstance(decision, tuple):  # (bestätigt, im Fenster bearbeitete Felder)
                decision, changes = decision[0], decision[1] or {}
            if not decision:
                result = "Der Nutzer hat die Ausführung abgelehnt."
                await emit({"type": "tool_result", "id": call_id, "status": "denied", "text": result})
                return name, result, f"{name} {args_str} → abgelehnt"
            # nur die freigegebenen Felder übernehmen (z. B. An/Betreff/Text einer Mail)
            for key in spec.editable:
                if key in changes and str(changes[key]) != str(args.get(key, "")):
                    args[key] = str(changes[key])[:50_000]
                    edited.append(key)
            if edited:
                args_str = json.dumps(args, ensure_ascii=False)

        await emit({"type": "state", "state": "executing", "tool": name})
        try:
            result = await spec.func(ctx, **args)
            status = "ok"
            if is_taint_source(name, result):
                self._tainted = True
        except asyncio.CancelledError:
            await emit({"type": "tool_result", "id": call_id, "status": "error", "text": "abgebrochen"})
            raise
        except Exception as e:  # noqa: BLE001
            log.exception("Tool %s fehlgeschlagen", name)
            result, status = f"Fehler: {e}", "error"
        if spec.group == "vision" and getattr(getattr(self.llm, "profile", None), "backend", "") == "ollama":
            self._cache_owner = None  # Ollama hat fürs Bild ggf. das Hauptmodell verdrängt
        if edited:  # das Modell soll wissen, was tatsächlich ausgeführt wurde
            result += "\n(Vom Nutzer vor dem Ausführen geändert: " + ", ".join(edited) + " – " + \
                json.dumps({k: args[k] for k in edited}, ensure_ascii=False)[:1500] + ")"
        await emit({"type": "tool_result", "id": call_id, "status": status, "text": clip(result, 3000)})
        first = result.strip().splitlines()[0] if result.strip() else ""
        room = ctx.output_chars or 0
        if room and len(result) > room * 1.5:
            # Rückfallnetz: kein Ergebnis sprengt das Kontextfenster – das Original liegt vollständig in einer Datei.
            # Großzügig, damit Werkzeuge, die selbst seitenweise liefern (read_file: bis ⅓ mehr), nie zerschnitten werden
            result = clip_saved(result, room, name)
        return name, result, f"{name} {args_str} → {first[:160]}"

    def output_chars(self) -> int:
        """Höchstlänge eines Werkzeug-Ergebnisses nach dem Fenster (klein ≈ 10 %, groß ≈ 15 %) – damit Ausblenden
        und Komprimieren immer Platz haben. Die Werkzeuge nehmen davon höchstens ihren Config-Wert
        (ToolContext.limit); mehr steht dann in einer Datei (siehe tools/proc.py)."""
        return self.plan().output_chars()
