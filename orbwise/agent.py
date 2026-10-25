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

from . import prompts
from .agent_code import CodingMixin
from .agent_compact import CompactMixin
from .agent_safety import TAINT_SOURCES, SafetyMixin, is_taint_source  # noqa: F401 – auch für Aufrufer
from .agent_speed import SpeedMixin
from .agent_tools import CODING_GROUPS, CODING_ONLY, ToolSelectionMixin  # noqa: F401
from .config import Config
from .context_plan import SUMMARY_MIN, ContextPlan, plan_for
from .llm import ContextOverflow, LLMError, strip_think
from .memory import Memory, est_tokens
from .memory.context import PROMPT_KEYS, TRIM_NOTE, msg_tokens
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


__all__ = ["Agent", "CODING_GROUPS", "SUMMARY_MIN", "ThinkFilter", "is_taint_source", "prompt_size"]
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


class Agent(CompactMixin, CodingMixin, ToolSelectionMixin, SafetyMixin, SpeedMixin):
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
        self._turn_open = False  # läuft gerade eine Anfrage (deren Notiz noch eingefroren werden darf)?
        self._think: bool | None = None
        self._think_level: str | None = None  # Stufe des Modells (z. B. low/medium/high), nur bei Modellen mit Stufen
        # Sprache der gewählten Stimme, wenn sie nicht die der Oberfläche ist – dann antwortet Orbwise in ihr
        self.reply_language: str | None = None
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
        # Warum der Server neu einlesen musste: Fingerabdruck des vorigen Aufrufs (siehe reread.py)
        self._prompt_prev = None
        self._reread: dict | None = None
        self.api_used = False  # ein anderes Programm hat über /v1 das Modell benutzt (dessen Prompt liegt im Cache)
        self._prewarming = False
        self._prewarm_failed: tuple | None = None  # (Profil, Zeit) des letzten gescheiterten Vorwärmens
        self._compacted_at: tuple | None = None  # (Chat, Epochen, Nachrichten) der letzten Komprimierung
        # Auto-Knopf: "off" = jeder Shell-Befehl fragt, "read" = erkannte lesende Befehle laufen ohne Rückfrage,
        # "files" = zusätzlich Dateien im eigenen Home anlegen/schreiben/kopieren/verschieben (ohne Root, ohne Löschen),
        # "auto" = Shell-Befehle und Dateien ohne Root (Löschen, Ausschalten, Senden ins Netz, Startdateien fragen)
        self.auto_mode = "read"
        self.last_context: dict | None = None  # letzter Prompt-Aufbau (für die Kontext-Anzeige)
        self._vision = False  # sieht das aktive Modell angehängte Bilder selbst? (siehe _check_vision)
        self.tools = load_all_tools()
        self.groups_of = {name: spec.group for name, spec in self.tools.items()}
        # die Coding-Werkzeuge (Gruppe „code“, nach OpenCode) gibt es nur im Coding-Modus
        self.all_schemas = [s for s in tool_schemas(cfg) if self.groups_of.get(s["function"]["name"]) not in CODING_ONLY]
        self._coding_schemas: tuple[list[dict], int] | None = None
        self._snap: tuple | None = None  # Schnappschuss vor der laufenden Antwort (Coding-Modus, siehe agent_code)
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
        sb = self.sandbox_state()
        if sb:  # Sandbox: alles läuft auf der Arbeitskopie
            return sb["path"]
        project = self.memory.conversation.meta.get("project") or ""
        path = Path(os.path.expanduser(project)) if project else None
        return str(path) if path and path.is_dir() else None

    def tool_allowed(self, name: str) -> bool:
        group = self.groups_of.get(name, "")
        return group in CODING_GROUPS if self.mode == "coding" else group not in CODING_ONLY

    def _mode_schemas(self) -> tuple[list[dict], int]:
        if self.mode != "coding":
            return self.all_schemas, self.all_schema_tokens
        if self._coding_schemas is None:
            schemas = [s for s in tool_schemas(self.cfg) if self.tool_allowed(s["function"]["name"])]
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
                project=self.project_dir() or prompts.text(c, "no_project"), env=self.coding_env())
                    + c.persona_extra).strip()
        base = prompts.base_prompt(
            c, name=c.assistant_name, os=_os_name(), host=platform.node(),
            user=c.user_name or getpass.getuser(), home=Path.home(),
            nas=", ".join(map(str, c.tools.nas_paths)))
        names = self._all_tool_names() if tools is None else tools
        names = {n for n in names if n in self.tools and self.tools[n].is_enabled(c)}  # Config kann sich ändern
        language = "\n" + prompts.reply_language_note(c, self.reply_language) if self.reply_language else ""
        return (base + prompts.hints(c, names) + c.persona_extra + language).strip()

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
        if self.mode == "coding" and self.project_instructions():
            sections.append(self.project_instructions())
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
        hint = self.coding_notes()  # fertige Hintergrund-Jobs, zurückgesetzte Antworten
        msg["note"] = prompts.context_note(self.cfg, stamp, memories, plan=self._plan,
                                           approved_plan=self._approved_plan, extra=hint)
        msg["note_meta"] = {"time": stamp, "plan": self._plan, "approved": self._approved_plan, "hint": hint}

    def _prompt_message(self, m: dict) -> dict:
        out = {k: v for k, v in m.items() if k in ("role", "content", "tool_calls", "tool_name")}
        if m.get("note"):
            out["content"] = m["note"] + (m.get("content") or "")
        if m.get("docs"):  # angehängte PDFs: ausgelesener Text hinter der Frage
            from . import attachments
            out["content"] = (out.get("content") or "") + "".join("\n\n" + attachments.doc_block(d) for d in m["docs"])
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
            history = [{k: v for k, v in m.items() if k in PROMPT_KEYS or k == "note"}
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
        (abzüglich Platz für die Antwort) – z. B. 8192 Token auf einer kleinen Karte."""
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
        return self.plan().reserve(self._thinking())

    def think_budget(self) -> int:
        """Höchstlänge der Denkkette in Token für diese Anfrage (0 = unbegrenzt bzw. kein Denken)."""
        return self.plan().think_budget() if self._think else 0

    def _set_think(self, think) -> None:
        """think: None (Profil), False/"off", True/"on" (an, Stufe nach Modell) oder eine Stufe des Modells."""
        if isinstance(think, str):
            self._think = think != "off"
            self._think_level = think if think not in ("off", "on") else None
        else:
            self._think = think
            self._think_level = None

    async def _fit_think(self) -> None:
        """Denken an das Modell anpassen: kann es nicht denken → aus; Stufen nur, wenn das Modell selbst welche hat
        (und nur seine eigenen – eine unbekannte Stufe wird zu seiner Voreinstellung). Nur an/aus oder unbekannt →
        ohne Stufe. Eigene Stufen legt Orbwise nicht darüber."""
        if not self._think or not hasattr(self.llm, "thinking_caps"):
            return
        try:
            caps = await self.llm.thinking_caps()
        except Exception:  # noqa: BLE001
            caps = None
        if caps == "none":
            self._think, self._think_level = False, None
        elif caps != "levels":
            self._think_level = None
        elif self._think_level:
            levels, _default = await self._think_levels()
            if self._think_level not in levels:
                self._think_level = None  # Voreinstellung des Modells

    async def _think_levels(self) -> tuple[list[str], str | None]:
        fn = getattr(self.llm, "thinking_levels", None)
        if fn is None:
            return [], None
        try:
            return await fn()
        except Exception:  # noqa: BLE001
            return [], None

    def _think_kwargs(self, no_think: bool = False) -> dict:
        """think/effort für den Aufruf – Vorwärmen und Frage müssen gleich senden: die Chat-Vorlage rendert je nach
        Denkmodus schon den Anfang anders, sonst läge eine Variante im Cache, die die Frage nicht nutzt."""
        if no_think:
            return {"think": False}
        kwargs: dict = {} if self._think is None else {"think": self._think}
        if self._think and self._think_level:
            kwargs["effort"] = self._think_level
        return kwargs

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

    def _call_llm(self, messages: list[dict], tools: list[dict] | None, **opts):
        """chat_stream mit Zusatzoptionen (think, max_tokens, tool_choice) – nur die, die das LLM-Objekt kennt.
        Merkt sich nebenbei, ob und wo sich der Prompt gegenüber dem vorigen Aufruf geändert hat (self._reread)."""
        from . import reread
        fp = reread.fingerprint(self._cache_key(), messages, tools, self.token_ratio(),
                                mode=(opts.get("think"), opts.get("effort")))
        self._reread = reread.compare(fp, self._prompt_prev)
        self._prompt_prev = fp
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
            await self._fit_think()
            self._tainted = False
            self._prewarm_failed = None  # eine echte Frage darf es wieder versuchen
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
            await self._fit_think()
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

    async def _run(self, user_text: str, emit: Emit, confirm: Confirm, images: list[str] | None = None) -> str:
        now = datetime.now()
        self._turn_time = now.strftime("%H:%M")
        self._turn_stamp = prompts.note_stamp(self.cfg, now)
        conv = self.memory.conversation
        # Steht noch Mail-Text (oder ein angehängtes PDF) im Prompt, kann er auch in späteren Anfragen wirken – dann
        # bleibt der Schutz an
        self._tainted = any(m.get("docs") or m.get("role") == "tool" and is_taint_source(
                                m.get("tool_name", ""), m.get("full_content") or m.get("content") or "")
                            for m in conv.epoch_messages())
        start_len = len(conv.history)
        await emit({"type": "state", "state": "thinking"})
        self._turn_memories = await self._memories_for(user_text)
        await self._check_vision()
        from . import attachments
        pics = [n for n in images or [] if not attachments.is_doc(n)]
        # angehängte PDFs: ihr Text kommt mit – zusammen höchstens ~40 % des Fensters (geschätzte Token × 3 Zeichen)
        docs = [d for d in (attachments.doc(self.cfg, n) for n in images or [] if attachments.is_doc(n)) if d]
        docs = attachments.fit_docs(docs, int(self.context_budget() * 0.4 * 3)) if docs else []
        conv.add({"role": "user", "content": user_text, **({"attachments": pics} if pics else {}),
                  **({"docs": docs} if docs else {})})
        if docs:  # ein PDF ist fremder Inhalt wie eine Mail – versteckte Anweisungen darin dürfen nichts auslösen
            self._tainted = True
        self._turn_open = True
        spoken: list[str] = []
        tool_notes: list[str] = []
        msg_id = uuid.uuid4().hex[:8]
        await emit({"type": "assistant_start", "id": msg_id, "plan": self._plan})
        await self.snapshot_begin()
        try:
            seen: dict[str, int] = {}  # gleiche Tool-Aufrufe zählen (Schleifenerkennung)
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
            await self.snapshot_end(emit, msg_id)
            await emit({"type": "assistant_end", "id": msg_id, "text": ""})
            await emit({"type": "state", "state": "idle"})
            return ""
        except asyncio.CancelledError:
            self._turn_open = False
            self._repair_history()
            conv.add({"role": "assistant", "content": "(Vom Nutzer abgebrochen.)"})
            conv.save()
            await self.memory.log_exchange(user_text, "\n\n".join(spoken + ["(abgebrochen)"]), tool_notes)
            await self.snapshot_end(emit, msg_id)
            await emit({"type": "assistant_end", "id": msg_id, "text": "\n\n".join(spoken), "cancelled": True})
            await emit({"type": "state", "state": "idle"})
            raise
        self._turn_open = False
        self._note_recent()

        answer = "\n\n".join(spoken)
        await self.snapshot_end(emit, msg_id)
        await emit({"type": "assistant_end", "id": msg_id, "text": answer})
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
                        # der Server hat jetzt diesen Chat im Cache – nach der Notbremse ohne Denken aber in der anderen
                        # Variante als die nächste Frage (mit Denken) → dann vorwärmen lassen
                        self._cache_owner = self._cache_key() if cut is None else None
                        current = ""
                        await phase("done", seconds=round(time.monotonic() - started, 1), reread=self._reread_info(stats),
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
                kwargs = self._think_kwargs(no_think)
                # auch die Zwischenbilanz mit derselben Werkzeugliste – ohne sie stünde vorn im Prompt etwas anderes
                # und der ganze Chat müsste neu eingelesen werden (und bei der nächsten Frage gleich noch einmal)
                async for ev in self._call_llm(messages, self.schemas, **kwargs):
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
                          cwd=cwd, output_chars=self.output_chars(), agent=self, confirm=confirm,
                          sandbox=self.sandbox_state())
        risk, reason = self._final_risk(name, spec, args, ctx)
        args_str = json.dumps(args, ensure_ascii=False)
        preview = ""
        if spec.preview and risk != BLOCKED:  # z. B. Diff einer Dateiänderung fürs Bestätigungsfenster
            try:
                preview = spec.preview(ctx, args)
            except Exception as e:  # noqa: BLE001
                log.debug("Vorschau für %s fehlgeschlagen: %s", name, e)
        await emit({"type": "tool_call", "id": call_id, "name": name, "args": args, "risk": risk, "reason": reason,
                    "group": spec.group, **({"preview": preview} if preview else {})})
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
        notes = self.coding_notes() if spec.group == "code" else ""
        if notes:  # z. B. ein Hintergrund-Job ist inzwischen fertig – das Modell erfährt es sofort
            result += "\n\n[" + notes + "]"
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
