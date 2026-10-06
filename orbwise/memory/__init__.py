"""Persistentes Gedächtnis von Orbwise.

~/.local/share/orbwise/memory/
    journal/YYYY-MM-DD.md     Rohprotokoll jedes Tages
    summaries/YYYY-MM-DD.md   Tageszusammenfassungen
    facts.md                  dauerhafte Fakten
    chats/<id>.json           Chats (Verlauf + Zusammenfassungen je Epoche), chats/active = aktueller Chat
    index.sqlite              Suchindex (aus den .md-Dateien rekonstruierbar)
"""

from __future__ import annotations

import contextlib
import logging
import re
import time
from datetime import datetime

from ..config import MemoryConfig
from .chats import ChatStore, chat_mode
from .context import Conversation, est_tokens
from .files import Facts, Journal, Summaries, day_str
from .index import Hit, MemoryIndex, split_text
from .lessons import LessonStore

log = logging.getLogger(__name__)

__all__ = ["Memory", "Conversation", "Hit", "est_tokens"]

DAY_CHUNK_CHARS = 12000


class Memory:
    def __init__(self, cfg: MemoryConfig, llm):
        self.cfg = cfg
        self.llm = llm
        cfg.dir.mkdir(parents=True, exist_ok=True)
        self.journal = Journal(cfg.dir / "journal")
        self.summaries = Summaries(cfg.dir / "summaries")
        self.facts = Facts(cfg.dir / "facts.md")
        self.index = MemoryIndex(cfg.dir / "index.sqlite", embedder=llm)
        self.chats = ChatStore(cfg.dir / "chats", legacy_session=cfg.dir / "session.json")
        self.lessons = LessonStore(cfg.dir, embed=self.index._embed, max_count=cfg.lessons_max)
        self.conversation = self.chats.open_active()  # Chat, in dem der Agent gerade arbeitet
        self._outer: list[Conversation] = []  # während in_chat: die darunterliegenden Chats (unten = Nutzer-Chat)
        self.last_activity = time.time()
        self._rebuilding = False

    def close(self) -> None:
        self.index.close()

    # ---------- Schreiben ----------
    async def log_exchange(self, user_text: str, assistant_text: str, tool_notes: list[str]) -> None:
        now = datetime.now()
        chat = self.conversation.chat_id
        self.journal.append("Du", user_text, now, chat)
        for note in tool_notes:
            self.journal.append("Tool", note, now, chat)
        self.journal.append("Jarvis", assistant_text or "(keine Antwort)", now, chat)
        self.last_activity = time.time()
        text = f"Nutzer: {user_text}\n" + "".join(f"Tool: {n}\n" for n in tool_notes) + f"Jarvis: {assistant_text}"
        await self.index.add("journal", day_str(now), f"chat:{chat}" if chat else f"journal:{day_str(now)}", text)

    # ---------- Chats ----------
    @property
    def active(self) -> Conversation:
        """Der Chat, den der Nutzer im Dashboard offen hat – auch während eine Routine oder eine Telegram-Anfrage
        vorübergehend in ihrem eigenen Chat arbeitet."""
        return self._outer[0] if self._outer else self.conversation

    def _loaded(self, chat_id: str) -> Conversation | None:
        return next((c for c in (self.conversation, *self._outer) if c.chat_id == chat_id), None)

    @property
    def mode(self) -> str:
        """Modus des Chats, in dem gerade gearbeitet wird: "tools" oder "coding"."""
        return chat_mode(self.conversation.meta)

    def new_chat(self) -> Conversation:
        """Neuer Chat im selben Modus – ist der aktuelle noch leer, wird er weiterverwendet."""
        if not self.conversation.history:
            return self.conversation
        self.conversation.save()
        self.conversation = self.chats.create(mode=self.mode)
        return self.conversation

    def switch_mode(self, mode: str) -> Conversation:
        """Zwischen Tools- und Coding-Modus wechseln: öffnet den zuletzt benutzten Chat dieses Modus."""
        if mode != self.mode:
            self.conversation.save()
            self.conversation = self.chats.open_active(mode)
        self.chats.set_mode(mode)
        return self.conversation

    def set_project(self, path: str) -> None:
        self.conversation.meta["project"] = path
        self.conversation.save()

    @contextlib.contextmanager
    def in_chat(self, chat_id: str, title: str):
        """Für Routinen: vorübergehend in einem eigenen Chat arbeiten, ohne den aktiven Chat des Nutzers umzustellen."""
        previous = self.conversation
        previous.save()
        conv = self.chats.open(chat_id) if chat_id and self.chats.exists(chat_id) else self.chats.create(activate=False)
        conv.meta["title"] = title
        self._outer.append(previous)
        self.conversation = conv
        try:
            yield conv
        finally:
            conv.save()
            self.conversation = self._outer.pop()

    def switch_chat(self, chat_id: str) -> Conversation:
        if chat_id != self.conversation.chat_id:
            self.conversation.save()
            self.conversation = self.chats.set_active(chat_id)
        return self.conversation

    def star_chat(self, chat_id: str, starred: bool) -> None:
        conv = self._loaded(chat_id)  # geladene Chats direkt ändern – sonst überschreibt ihr nächstes save() das
        if conv:
            conv.meta["starred"] = bool(starred)
            conv.save()
        else:
            self.chats.set_star(chat_id, starred)

    def rename_chat(self, chat_id: str, title: str) -> None:
        conv = self._loaded(chat_id)
        if conv:
            from .context import make_title
            conv.meta["title"] = make_title(title, 80) or "Neuer Chat"
            conv.save()
        else:
            self.chats.rename(chat_id, title)

    def forget_chat(self, chat_id: str) -> list[str]:
        """Chat komplett vergessen: Datei, Tagebuch-Einträge, Suchindex, betroffene Tageszusammenfassungen
        (werden aus dem Rest neu erstellt). Gelernte Fakten bleiben. Liefert die betroffenen Tage."""
        self.chats.path(chat_id)  # prüft die ID
        if self._outer and self._loaded(chat_id):
            raise RuntimeError("Chat wird gerade benutzt")  # der Server verhindert das vorher (409)
        # Erst den Suchindex (nur ein Cache) – scheitert er, darf das Löschen nicht halb stehen bleiben
        try:
            self.index.delete_source(f"chat:{chat_id}")
        except Exception as e:  # noqa: BLE001
            log.warning("Suchindex beim Löschen nicht bereinigt (%s) – wird neu aufgebaut", e)
            self.index.needs_rebuild = True
        days = self.journal.remove_chat(chat_id)
        for day in days:
            self.summaries.delete(day)
            with contextlib.suppress(Exception):
                self.index.delete_source(f"summary:{day}")
        self.chats.delete(chat_id)
        if chat_id == self.conversation.chat_id:
            self.conversation = self.chats.create(mode=self.mode)
        return days

    async def remember(self, fact: str) -> bool:
        return (await self.remember_fact(fact))[0]

    async def remember_fact(self, fact: str) -> tuple[bool, str | None]:
        """Merkt sich einen Fakt. Gibt es schon eine fast gleiche, ältere Fassung („NAS unter /mnt/nas“ →
        „NAS unter /media/nas“), wird diese ersetzt statt einen Widerspruch anzuhängen. → (gespeichert, ersetzt)"""
        fact = " ".join(fact.split())
        old = await self._similar_fact(fact)
        if old is not None and old.lower() != fact.lower():
            self.facts.replace(old, fact)
            self.index.delete_source("facts")
            await self.reindex_facts()
            return True, old
        added = self.facts.add(fact)
        if added:
            await self.index.add("fact", day_str(), "facts", fact)
        return added, None

    async def _similar_fact(self, fact: str) -> str | None:
        """Bestehender Fakt, der dasselbe meint: viele gleiche Wörter und (mit Embeddings) sehr ähnlicher Sinn."""
        def overlap(a: str, b: str) -> float:
            wa, wb = set(re.findall(r"[\w/.:-]+", a.lower())), set(re.findall(r"[\w/.:-]+", b.lower()))
            return len(wa & wb) / max(1, len(wa | wb))

        facts = [f for f, _ in self.facts.list()]
        if not facts or not fact:
            return None
        best = max(facts, key=lambda f: overlap(f, fact))
        score = overlap(best, fact)
        vecs = await self.index._embed([fact, best])
        if vecs[0] is not None and vecs[1] is not None:
            a, b = vecs
            sim = float(a @ b / ((float((a @ a) ** 0.5) * float((b @ b) ** 0.5)) or 1))
            return best if sim >= 0.88 and score >= 0.5 else None
        return best if score >= 0.7 else None

    async def forget(self, query: str) -> list[str]:
        removed = self.facts.remove(query)
        if removed:
            # Fakten-Chunks neu aufbauen (klein, daher billig)
            self.index.delete_source("facts")
            await self.reindex_facts()
        return removed

    async def reindex_facts(self) -> None:
        for fact, day in self.facts.list():
            await self.index.add("fact", day or day_str(), "facts", fact)

    # ---------- Lesen ----------
    async def retrieve(self, query: str, exclude_after: float | None = None) -> list[Hit]:
        chat = self.conversation.chat_id
        return await self.index.search(query, k=self.cfg.retrieval_top_k, exclude_after=exclude_after,
                                       exclude_source=f"chat:{chat}" if chat else None)

    def format_hits(self, hits: list[Hit], budget: int | None = None, taken: list[int] | None = None) -> str:
        """Treffer nach Relevanz (beste zuerst), bis das Budget voll ist – weniger Budget behält die besten.
        taken: sammelt die IDs der aufgenommenen Treffer (damit sie in derselben Epoche nicht noch einmal kommen)."""
        out, used = [], 0
        budget = self.cfg.retrieval_max_tokens if budget is None else budget
        labels = {"journal": "Gespräch", "summary": "Tageszusammenfassung", "fact": "Fakt"}
        for h in hits:
            entry = f"[{h.day} · {labels.get(h.kind, h.kind)}]\n{h.text.strip()}"
            t = est_tokens(entry)
            if used + t > budget:
                continue
            out.append(entry)
            used += t
            if taken is not None:
                taken.append(h.id)
        return "\n\n".join(out)

    def facts_text(self, max_tokens: int | None = None) -> str:
        """Fakten für den Systemprompt – höchstens max_tokens (Standard: facts_max_tokens; kleines Fenster weniger)."""
        return self.facts.as_text((max_tokens or self.cfg.facts_max_tokens) * 3)

    # ---------- Tageszusammenfassungen ----------
    async def summarize_day(self, day: str) -> str | None:
        text = self.journal.read(day)  # ohne Chat-Kennungen
        if not text:
            return None
        system = {"role": "system", "content": (
            "Du fasst das Tagesprotokoll zwischen einem Nutzer und seinem KI-Assistenten Jarvis zusammen. "
            "Schreibe auf Deutsch prägnante Stichpunkte: was wurde gemacht, welche Befehle/Pakete/Dateien "
            "waren beteiligt, Ergebnisse, Vorlieben des Nutzers, offene Punkte. Keine Einleitung.")}
        parts = split_text(text, DAY_CHUNK_CHARS)
        partials = []
        for part in parts:
            partials.append(await self.llm.chat([system, {"role": "user", "content": part}]))
        summary = partials[0] if len(partials) == 1 else await self.llm.chat([
            system, {"role": "user", "content": "Fasse diese Teilzusammenfassungen zu einer zusammen:\n\n"
                     + "\n\n".join(partials)}])
        self.summaries.write(day, summary)
        self.index.delete_source(f"summary:{day}")
        created = datetime.strptime(day, "%Y-%m-%d").timestamp() + 86399
        await self.index.add("summary", day, f"summary:{day}", summary, created=min(created, time.time()))
        return summary

    def days_needing_summary(self, include_today: bool) -> list[str]:
        today = day_str()
        out = []
        for day in self.journal.days():
            if day == today and not include_today:
                continue
            if self.journal.mtime(day) > self.summaries.mtime(day):
                out.append(day)
        return out

    async def summarize_pending(self) -> list[str]:
        idle = time.time() - self.last_activity > self.cfg.summarize_idle_minutes * 60
        done = []
        for day in self.days_needing_summary(include_today=idle):
            try:
                await self.summarize_day(day)
                done.append(day)
            except Exception as e:  # noqa: BLE001
                log.warning("Zusammenfassung für %s fehlgeschlagen: %s", day, e)
                break
        return done

    # ---------- Wartung ----------
    async def heal_index_if_needed(self) -> bool:
        """War der Suchindex beschädigt und wurde leer neu angelegt: aus den Markdown-Dateien wieder aufbauen."""
        if not self.index.needs_rebuild or self._rebuilding:
            return False
        self._rebuilding = True
        try:
            n = await self.rebuild_index()
            log.info("Suchindex neu aufgebaut: %s Einträge", n)
        finally:
            self._rebuilding = False
        return True

    async def rebuild_index(self) -> int:
        self.index.reset()  # Datei frisch anlegen – klappt auch, wenn sie beschädigt ist
        for day in sorted(self.journal.days()):
            entries = self.journal.entries(day)
            block: list[str] = []
            source = f"journal:{day}"
            for e in entries:
                if e.speaker == "Du" and block:
                    await self.index.add("journal", day, source, "\n".join(block),
                                         created=datetime.strptime(f"{day} {e.time}", "%Y-%m-%d %H:%M:%S").timestamp())
                    block = []
                if e.speaker == "Du":
                    source = f"chat:{e.chat}" if e.chat else f"journal:{day}"
                label = {"Du": "Nutzer"}.get(e.speaker, e.speaker)
                block.append(f"{label}: {e.text}")
            if block:
                await self.index.add("journal", day, source, "\n".join(block),
                                     created=datetime.strptime(day, "%Y-%m-%d").timestamp())
        for day in self.summaries.days():
            body = self.summaries.read(day) or ""
            body = body.split("\n", 2)[-1]
            await self.index.add("summary", day, f"summary:{day}", body,
                                 created=datetime.strptime(day, "%Y-%m-%d").timestamp() + 86399)
        await self.reindex_facts()
        self.index.needs_rebuild = False
        return self.index.count()
