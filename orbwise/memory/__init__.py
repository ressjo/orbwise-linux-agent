"""Persistentes Gedächtnis von Orbwise.

~/.local/share/orbwise/memory/
    journal/YYYY-MM-DD.md     Rohprotokoll jedes Tages
    summaries/YYYY-MM-DD.md   Tageszusammenfassungen
    facts.md                  dauerhafte Fakten (Tools-Chats)
    projects/<pid>/           eigenes Gedächtnis je Coding-Projekt: facts.md, journal/, project.txt (Pfad)
    chats/<id>.json           Chats (Verlauf + Zusammenfassungen je Epoche), chats/active = aktueller Chat
    index.sqlite              Suchindex (aus den .md-Dateien rekonstruierbar)
"""

from __future__ import annotations

import contextlib
import hashlib
import logging
import os
import re
import time
from datetime import datetime
from pathlib import Path

from ..config import MemoryConfig
from .chats import ChatStore, chat_mode
from .context import Conversation, est_tokens
from .files import Facts, Journal, Summaries, day_str, digest, valid_day
from .index import Hit, MemoryIndex, split_text

log = logging.getLogger(__name__)

__all__ = ["Memory", "Conversation", "Hit", "est_tokens"]

DAY_CHUNK_CHARS = 12000
TOOLS_KINDS = ("journal", "summary", "fact")


def project_id(project: str) -> str:
    """Kurzer, stabiler Schlüssel des Projektordners für sein eigenes Gedächtnis ("none" ohne Projekt)."""
    if not project:
        return "none"
    path = os.path.realpath(os.path.expanduser(project))
    return hashlib.sha1(path.encode("utf-8")).hexdigest()[:12]


def chat_project(meta: dict) -> str | None:
    """Projekt eines Coding-Chats ("" ohne Projekt) – None bei Tools-Chats. Bei aktiver Sandbox zählt das echte
    Projekt, nicht die Arbeitskopie."""
    if chat_mode(meta) != "coding":
        return None
    return meta.get("project") or ""


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
        self.conversation = self.chats.open_active()  # Chat, in dem der Agent gerade arbeitet
        self._outer: list[Conversation] = []  # während in_chat: die darunterliegenden Chats (unten = Nutzer-Chat)
        self.last_activity = time.time()
        self.summary_ran = False
        self._rebuilding = False
        self._project_facts: dict[str, Facts] = {}
        try:
            self.migrate_scopes()
        except Exception as e:  # noqa: BLE001
            log.warning("Gedächtnis-Trennung (Coding/Tools) nicht migriert: %s", e)

    # ---------- Bereiche: Tools-Gedächtnis oder eins je Coding-Projekt ----------
    def project_of(self, conv: Conversation | None = None) -> str | None:
        return chat_project((conv or self.conversation).meta)

    def scope_id(self, conv: Conversation | None = None) -> str | None:
        """None = Tools-Gedächtnis, sonst die Projekt-ID."""
        project = self.project_of(conv)
        return None if project is None else project_id(project)

    def project_dir(self, pid: str) -> Path:
        return self.cfg.dir / "projects" / pid

    def _project_seen(self, pid: str, project: str) -> None:
        marker = self.project_dir(pid) / "project.txt"
        if project and not marker.exists():
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.write_text(project + "\n", encoding="utf-8")

    def project_name(self, pid: str) -> str:
        try:
            return (self.project_dir(pid) / "project.txt").read_text(encoding="utf-8").strip()
        except OSError:
            return ""

    def facts_of(self, pid: str | None) -> Facts:
        if pid is None:
            return self.facts
        if pid not in self._project_facts:
            self._project_facts[pid] = Facts(self.project_dir(pid) / "facts.md")
        return self._project_facts[pid]

    def journal_of(self, pid: str | None) -> Journal:
        return self.journal if pid is None else Journal(self.project_dir(pid) / "journal")

    def scoped_facts(self, conv: Conversation | None = None) -> Facts:
        return self.facts_of(self.scope_id(conv))

    def scope_journal(self, conv: Conversation | None = None) -> Journal:
        return self.journal_of(self.scope_id(conv))

    @staticmethod
    def kinds_of(pid: str | None) -> tuple[str, ...]:
        return TOOLS_KINDS if pid is None else (f"code:{pid}", f"codefact:{pid}")

    def scope_kinds(self, conv: Conversation | None = None) -> tuple[str, ...]:
        return self.kinds_of(self.scope_id(conv))

    @staticmethod
    def _fact_slot(pid: str | None) -> tuple[str, str]:
        """(kind, source) der Fakten-Einträge im Suchindex."""
        return ("fact", "facts") if pid is None else (f"codefact:{pid}", f"facts:{pid}")

    def project_ids(self) -> list[str]:
        base = self.cfg.dir / "projects"
        return sorted(p.name for p in base.iterdir() if p.is_dir()) if base.is_dir() else []

    def migrate_scopes(self) -> int:
        """Einmalig: Erinnerungen an bisherige Coding-Chats gehören ins Gedächtnis ihres Projekts.
        Die Fakten bleiben im Tools-Gedächtnis, die alten Tagebuch-Zeilen an ihrem Ort."""
        marker = self.cfg.dir / ".scopes_v1"
        if marker.exists():
            return 0
        moved = 0
        for chat in self.chats.list(mode="coding"):
            project = chat.get("project") or ""
            pid = project_id(project)
            self._project_seen(pid, project)
            moved += self.index.retag(f"chat:{chat['id']}", f"code:{pid}")
        marker.write_text("1\n", encoding="utf-8")
        return moved

    def close(self) -> None:
        self.index.close()

    # ---------- Schreiben ----------
    async def log_exchange(self, user_text: str, assistant_text: str, tool_notes: list[str]) -> None:
        now = datetime.now()
        chat = self.conversation.chat_id
        project = self.project_of()
        pid = None if project is None else project_id(project)
        if pid is not None:
            self._project_seen(pid, project)
        journal = self.journal_of(pid)  # Coding-Chats: Tagebuch des Projekts (nicht in den Tageszusammenfassungen)
        journal.append("Du", user_text, now, chat)
        for note in tool_notes:
            journal.append("Tool", note, now, chat)
        journal.append("Orbwise", assistant_text or "(keine Antwort)", now, chat)
        self.last_activity = time.time()
        text = f"Nutzer: {user_text}\n" + "".join(f"Tool: {n}\n" for n in tool_notes) + f"Orbwise: {assistant_text}"
        kind = "journal" if pid is None else f"code:{pid}"
        await self.index.add(kind, day_str(now), f"chat:{chat}" if chat else f"journal:{day_str(now)}", text)

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

    def start_fresh(self) -> Conversation:
        """Beim Start von Orbwise: Tools-Modus, leerer Chat (ein noch leerer wird weiterverwendet) – alte Chats bleiben
        in der Liste."""
        self.switch_mode("tools")
        return self.new_chat()

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
        for pid in self.project_ids():
            days += [d for d in self.journal_of(pid).remove_chat(chat_id) if d not in days]
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
        project = self.project_of()
        pid = None if project is None else project_id(project)
        if pid is not None:
            self._project_seen(pid, project)
        facts = self.facts_of(pid)
        kind, source = self._fact_slot(pid)
        old = await self._similar_fact(fact, facts)
        if old is not None and old.lower() != fact.lower():
            facts.replace(old, fact)
            self.index.delete_source(source)
            await self.reindex_facts(pid)
            return True, old
        added = facts.add(fact)
        if added:
            await self.index.add(kind, day_str(), source, fact)
        return added, None

    async def _similar_fact(self, fact: str, store: Facts | None = None) -> str | None:
        """Bestehender Fakt, der dasselbe meint: viele gleiche Wörter und (mit Embeddings) sehr ähnlicher Sinn."""
        def overlap(a: str, b: str) -> float:
            wa, wb = set(re.findall(r"[\w/.:-]+", a.lower())), set(re.findall(r"[\w/.:-]+", b.lower()))
            return len(wa & wb) / max(1, len(wa | wb))

        facts = [f for f, _ in (store or self.facts).list()]
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
        pid = self.scope_id()
        removed = self.facts_of(pid).remove(query)
        if removed:
            # Fakten-Chunks neu aufbauen (klein, daher billig)
            self.index.delete_source(self._fact_slot(pid)[1])
            await self.reindex_facts(pid)
        return removed

    async def forget_fact(self, fact: str, conv: Conversation | None = None) -> bool:
        """Genau einen Fakt des Gedächtnisses von conv (Tools oder Coding-Projekt) löschen – aus der Oberfläche."""
        pid = self.scope_id(conv)
        if not self.facts_of(pid).remove_exact(fact):
            return False
        self.index.delete_source(self._fact_slot(pid)[1])
        await self.reindex_facts(pid)
        return True

    def forget_day(self, day: str, conv: Conversation | None = None) -> bool:
        """Tagebuch eines Tages vergessen: Protokoll, Tageszusammenfassung und ihre Einträge im Suchindex. Gelernte
        Fakten und die Chats selbst bleiben."""
        if not valid_day(day):
            return False
        pid = self.scope_id(conv)
        path = self.journal_of(pid).path(day)
        existed = path.exists()
        path.unlink(missing_ok=True)
        fact_kind = self._fact_slot(pid)[0]
        kinds = tuple(k for k in self.kinds_of(pid) if k != fact_kind)
        try:
            self.index.delete_day(kinds, day)
        except Exception as e:  # noqa: BLE001 – der Index ist nur ein Cache
            log.warning("Suchindex für %s nicht bereinigt (%s) – wird neu aufgebaut", day, e)
            self.index.needs_rebuild = True
        if pid is None and self.summaries.read(day) is not None:
            existed = True
            self.summaries.delete(day)
            with contextlib.suppress(Exception):
                self.index.delete_source(f"summary:{day}")
        return existed

    async def reindex_facts(self, pid: str | None = None) -> None:
        kind, source = self._fact_slot(pid)
        for fact, day in self.facts_of(pid).list():
            await self.index.add(kind, day or day_str(), source, fact)

    # ---------- Lesen ----------
    async def retrieve(self, query: str, exclude_after: float | None = None) -> list[Hit]:
        chat = self.conversation.chat_id
        return await self.index.search(query, k=self.cfg.retrieval_top_k, exclude_after=exclude_after,
                                       kinds=self.scope_kinds(), exclude_source=f"chat:{chat}" if chat else None)

    def format_hits(self, hits: list[Hit], budget: int | None = None, taken: list[int] | None = None) -> str:
        """Treffer nach Relevanz (beste zuerst), bis das Budget voll ist – weniger Budget behält die besten.
        taken: sammelt die IDs der aufgenommenen Treffer (damit sie in derselben Epoche nicht noch einmal kommen)."""
        out, used = [], 0
        budget = self.cfg.retrieval_max_tokens if budget is None else budget
        labels = {"journal": "Gespräch", "summary": "Tageszusammenfassung", "fact": "Fakt", "code": "Gespräch",
                  "codefact": "Projekt-Fakt"}
        for h in hits:
            entry = f"[{h.day} · {labels.get(h.kind.split(':')[0], h.kind)}]\n{h.text.strip()}"
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
        return self.scoped_facts().as_text((max_tokens or self.cfg.facts_max_tokens) * 3)

    # ---------- Tageszusammenfassungen ----------
    async def summarize_day(self, day: str) -> str | None:
        text = self.journal.read(day)  # ohne Chat-Kennungen
        if not text:
            return None
        system = {"role": "system", "content": (
            "Du fasst das Tagesprotokoll zwischen einem Nutzer und seinem KI-Assistenten Orbwise zusammen. "
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
        """Tage, deren Journal sich seit der letzten Zusammenfassung inhaltlich geändert hat. Vorhandene
        Zusammenfassungen ohne Vermerk (ältere Versionen) gelten als aktuell – der Vermerk wird nachgetragen."""
        today = day_str()
        out = []
        for day in self.journal.days():
            if day == today and not include_today:
                continue
            fp = digest(self.journal.read(day, raw=True) or "")
            done = self.summaries.summarized(day)
            if done is None and self.summaries.mtime(day) and day != today:
                self.summaries.mark(day, fp)
                continue
            if done != fp:
                out.append(day)
        return out

    async def summarize_pending(self) -> list[str]:
        idle = time.time() - self.last_activity > self.cfg.summarize_idle_minutes * 60
        done = []
        self.summary_ran = False  # hat das Modell gerechnet (auch ohne Ergebnis)? Dann liegt der Chat nicht mehr im Cache
        for day in self.days_needing_summary(include_today=idle):
            fp = digest(self.journal.read(day, raw=True) or "")
            self.summary_ran = True
            try:
                summary = await self.summarize_day(day)
            except Exception as e:  # noqa: BLE001
                log.warning("Zusammenfassung für %s fehlgeschlagen: %s", day, e)
                break
            self.summaries.mark(day, fp)  # auch bei leerem Tag – sonst käme er alle paar Minuten wieder
            if summary:
                done.append(day)
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
        # Ältere Coding-Chats stehen noch im Tools-Tagebuch – ihre Einträge gehören ins Gedächtnis ihres Projekts
        coding = {c["id"]: f"code:{project_id(c.get('project') or '')}" for c in self.chats.list(mode="coding")}
        await self._index_journal(self.journal, lambda chat: coding.get(chat, "journal"))
        for pid in self.project_ids():
            await self._index_journal(self.journal_of(pid), lambda chat, pid=pid: f"code:{pid}")
            await self.reindex_facts(pid)
        for day in self.summaries.days():
            body = self.summaries.read(day) or ""
            body = body.split("\n", 2)[-1]
            await self.index.add("summary", day, f"summary:{day}", body,
                                 created=datetime.strptime(day, "%Y-%m-%d").timestamp() + 86399)
        await self.reindex_facts()
        self.index.needs_rebuild = False
        return self.index.count()

    async def _index_journal(self, journal: Journal, kind_of) -> None:
        for day in sorted(journal.days()):
            block: list[str] = []
            source, chat = f"journal:{day}", ""
            for e in journal.entries(day):
                if e.speaker == "Du" and block:
                    await self.index.add(kind_of(chat), day, source, "\n".join(block),
                                         created=datetime.strptime(f"{day} {e.time}", "%Y-%m-%d %H:%M:%S").timestamp())
                    block = []
                if e.speaker == "Du":
                    chat = e.chat
                    source = f"chat:{e.chat}" if e.chat else f"journal:{day}"
                label = {"Du": "Nutzer"}.get(e.speaker, e.speaker)
                block.append(f"{label}: {e.text}")
            if block:
                await self.index.add(kind_of(chat), day, source, "\n".join(block),
                                     created=datetime.strptime(day, "%Y-%m-%d").timestamp())
