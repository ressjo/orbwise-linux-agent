"""Welche Werkzeuge gehen mit der Anfrage mit: Grundausstattung, passende Gruppen nach Stichworten, kürzlich genutzte,
alle auf einmal, wenn das schnell genug eingelesen ist – und load_tools für den Rest.

Mixin für Agent – nutzt dessen Zustand (self.memory, self.cfg, Budgets und Einlese-Tempo aus agent.py)."""

from __future__ import annotations

import json
import logging

from . import toolselect
from .memory import est_tokens

log = logging.getLogger(__name__)

# Alle Werkzeuge gehen nur mit, wenn ihre Beschreibungen in höchstens so vielen Sekunden eingelesen sind
ALL_TOOLS_SECONDS = 10.0

BIG_TOOLS_SECONDS = 60.0  # ab 32k Fenster: so lange darf das einmalige Einlesen aller Werkzeuge dauern

# Coding-Modus: die Werkzeuge nach OpenCode (Gruppe „code“: read, edit, write, grep, glob, shell, websearch, webfetch,
# subagent, question …) plus Gedächtnis und Aufgabenliste – mehr Kontext bleibt für den Code frei
CODING_GROUPS = {"code", "memory_tools", "todo_tools"}


class ToolSelectionMixin:
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
        Modellwechsel und jeder Komprimierung neu eingelesen werden – bei ~140 Token/s (27B auf 8 GB) kosten 16k
        Token Werkzeugbeschreibungen über 2 Minuten, auf einer schnellen Karte 5 s. Unbekannte Geschwindigkeit:
        alle (wird beim ersten Einlesen gelernt und gemerkt). Etwas Abstand zwischen An und Aus, damit die Auswahl
        nicht hin und her springt (jeder Wechsel kostet ein Neueinlesen)."""
        tps = self._prefill_tps.get(self._profile_key())
        if not tps:
            return True
        seconds = tool_tokens * self.token_ratio() / tps
        # Großes Fenster: Die Werkzeuge stehen ganz vorn im Prompt. Kommt mitten im Chat eine Gruppe dazu, muss der
        # ganze Chat neu eingelesen werden (bei 60k Token eine Minute und mehr) – ein fester, vollständiger Satz kostet
        # dagegen nur einmal je Epoche bzw. Neustart. Also alle, solange sie höchstens 25 % des Budgets belegen und
        # ihr Einlesen keine Minute dauert (auf sehr langsamen Karten bleibt es bei der Auswahl).
        if self.model_window() >= 32768 and tool_tokens <= 0.25 * self.context_budget() and seconds <= BIG_TOOLS_SECONDS:
            return True
        all_now = self.memory.conversation.epoch.get("tools", "") is None
        return seconds <= (ALL_TOOLS_SECONDS * 1.25 if all_now else ALL_TOOLS_SECONDS)

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
