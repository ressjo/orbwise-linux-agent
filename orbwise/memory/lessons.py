"""Erfahrungen: kurze Lektionen, die Orbwise aus Fehlern, Korrekturen und 👎 lernt („Docker auf dem NAS braucht sudo“,
„Telekom-Briefe → Korrespondent Telekom Deutschland“).

Damit das Kontextfenster dadurch nicht wächst, geht nie alles mit: zu einer Frage nur die passenden Lektionen
(Ähnlichkeit über einer Schwelle, Bereich passt), höchstens ein festes, kleines Budget (context_plan.lessons) – ob es
10 oder 300 Lektionen gibt, der Prompt bleibt gleich groß. Lektionen sind kurz, ähnliche werden zusammengeführt statt
angehängt, nutzlose fallen weg.

Ablage: <memory>/lessons.json (Lektionen samt Embedding) und lessons_pending.jsonl (Kandidaten, über die Orbwise im
Leerlauf nachdenkt – siehe Agent.reflect_lessons).
"""

from __future__ import annotations

import json
import os
import re
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path

import numpy as np

MAX_CHARS = 220          # eine Lektion: ein bis zwei Sätze
MIN_SIM = 0.55           # nur wirklich passende Lektionen gehen mit
MERGE_SIM = 0.88         # so ähnlich → dieselbe Lektion (ersetzen statt anhängen)
PENDING_MAX = 50         # unbearbeitete Kandidaten (älteste fallen weg)

Embed = Callable[[list[str]], Awaitable[list["np.ndarray | None"]]]


@dataclass
class Lesson:
    id: str
    text: str
    scope: list[str] = field(default_factory=list)  # Werkzeug-Gruppen, z. B. ["paperless"]
    source: str = "error"                           # error | correction | feedback | manual
    created: float = 0.0
    last_used: float = 0.0
    uses: int = 0       # so oft mitgegeben
    helped: int = 0     # so oft lief die Runde danach glatt (oder 👍)
    vec: list[float] | None = None

    def public(self) -> dict:
        d = asdict(self)
        d.pop("vec", None)
        return d


def _words(text: str) -> set[str]:
    return {w for w in re.findall(r"[\w/.:-]{3,}", text.lower())}


def _overlap(a: str, b: str) -> float:
    wa, wb = _words(a), _words(b)
    return len(wa & wb) / max(1, len(wa | wb))


def _cos(a, b) -> float:
    a, b = np.asarray(a, dtype=np.float32), np.asarray(b, dtype=np.float32)
    den = float(np.linalg.norm(a) * np.linalg.norm(b)) or 1.0
    return float(a @ b / den)


def clean(text: str) -> str:
    text = " ".join((text or "").split()).strip(" -•*")
    return text[:MAX_CHARS].rstrip()


class LessonStore:
    def __init__(self, directory: Path, embed: Embed | None = None, max_count: int = 300):
        self.path = Path(directory) / "lessons.json"
        self.pending_path = Path(directory) / "lessons_pending.jsonl"
        self.embed = embed
        self.max_count = max(10, max_count)
        self.lessons: list[Lesson] = self._load()

    # ---------- Datei ----------
    def _load(self) -> list[Lesson]:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        out = []
        for d in raw if isinstance(raw, list) else []:
            try:
                out.append(Lesson(**{k: v for k, v in d.items() if k in Lesson.__dataclass_fields__}))
            except TypeError:
                continue
        return out

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps([asdict(x) for x in self.lessons], ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, self.path)

    async def _vec(self, text: str) -> list[float] | None:
        if not self.embed:
            return None
        try:
            v = (await self.embed([text]))[0]
        except Exception:  # noqa: BLE001 – ohne Embedding geht es über Wortüberlappung
            return None
        return None if v is None else [float(x) for x in np.asarray(v).ravel()]

    # ---------- Verwalten ----------
    def get(self, lesson_id: str) -> Lesson | None:
        return next((x for x in self.lessons if x.id == lesson_id), None)

    async def add(self, text: str, scope: list[str] | None = None, source: str = "error") -> tuple[Lesson, bool]:
        """Neue Lektion – oder eine sehr ähnliche bestehende ersetzen (→ (Lektion, zusammengeführt))."""
        text = clean(text)
        if not text:
            raise ValueError("leere Lektion")
        vec = await self._vec(text)
        twin = self._twin(text, vec)
        now = time.time()
        if twin:
            twin.text, twin.vec, twin.created = text, vec or twin.vec, now
            twin.scope = sorted(set(twin.scope) | set(scope or []))
            self.save()
            return twin, True
        lesson = Lesson(id=uuid.uuid4().hex[:10], text=text, scope=sorted(set(scope or [])), source=source,
                        created=now, vec=vec)
        self.lessons.append(lesson)
        self._cap()
        self.save()
        return lesson, False

    def _twin(self, text: str, vec: list[float] | None) -> Lesson | None:
        best, score = None, 0.0
        for x in self.lessons:
            s = _cos(vec, x.vec) if vec and x.vec and len(vec) == len(x.vec) else None
            hit = (s is not None and s >= MERGE_SIM) or _overlap(text, x.text) >= 0.75
            value = s if s is not None else _overlap(text, x.text)
            if hit and value > score:
                best, score = x, value
        return best

    async def update(self, lesson_id: str, text: str | None = None, scope: list[str] | None = None) -> Lesson | None:
        x = self.get(lesson_id)
        if not x:
            return None
        if text is not None and clean(text):
            x.text = clean(text)
            x.vec = await self._vec(x.text)
        if scope is not None:
            x.scope = sorted({s.strip() for s in scope if s.strip()})
        self.save()
        return x

    def delete(self, lesson_id: str) -> bool:
        before = len(self.lessons)
        self.lessons = [x for x in self.lessons if x.id != lesson_id]
        if len(self.lessons) != before:
            self.save()
            return True
        return False

    def _cap(self) -> None:
        if len(self.lessons) > self.max_count:  # am längsten ungenutzt zuerst weg
            self.lessons.sort(key=lambda x: max(x.last_used, x.created), reverse=True)
            del self.lessons[self.max_count:]

    def prune(self) -> list[str]:
        """Lektionen, die oft mitgingen, aber kaum geholfen haben, entfernen."""
        gone = [x.id for x in self.lessons if x.uses >= 8 and x.helped < x.uses * 0.25]
        if gone:
            self.lessons = [x for x in self.lessons if x.id not in gone]
            self.save()
        return gone

    # ---------- Auswahl für eine Frage ----------
    async def relevant(self, query: str, groups: set[str] | None = None, k: int = 3,
                       skip: set[str] | None = None) -> list[Lesson]:
        """Die passendsten Lektionen (beste zuerst) – nur über der Schwelle; der Bereich (Werkzeug-Gruppe) passt
        zur Frage oder die Lektion ist allgemein."""
        if not self.lessons or not query.strip():
            return []
        vec = await self._vec(query)
        groups = groups or set()
        scored = []
        for x in self.lessons:
            if skip and x.id in skip:
                continue
            if x.vec and vec and len(vec) == len(x.vec):
                score = _cos(vec, x.vec)
            else:
                score = _overlap(query, x.text) * 1.6  # ohne Embeddings: Wortüberlappung
            if x.scope and groups & set(x.scope):
                score += 0.08  # gleicher Bereich wie die Frage
            elif x.scope and groups:
                score -= 0.05
            if score >= MIN_SIM:
                scored.append((score, x))
        scored.sort(key=lambda t: t[0], reverse=True)
        return [x for _, x in scored[:k]]

    def mark_used(self, ids: list[str]) -> None:
        now = time.time()
        for x in self.lessons:
            if x.id in ids:
                x.uses += 1
                x.last_used = now
        if ids:
            self.save()

    def mark_helped(self, ids: list[str], delta: int = 1) -> None:
        for x in self.lessons:
            if x.id in ids:
                x.helped = max(0, x.helped + delta)
        if ids:
            self.save()

    # ---------- Kandidaten (Leerlauf-Reflexion) ----------
    def add_candidate(self, record: dict) -> None:
        items = self.pending()
        items.append({**record, "ts": time.time()})
        items = items[-PENDING_MAX:]
        self.pending_path.parent.mkdir(parents=True, exist_ok=True)
        self.pending_path.write_text("".join(json.dumps(i, ensure_ascii=False) + "\n" for i in items),
                                     encoding="utf-8")

    def pending(self) -> list[dict]:
        try:
            lines = self.pending_path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return []
        out = []
        for line in lines:
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
        return out

    def take_candidates(self, n: int = 5) -> list[dict]:
        items = self.pending()
        taken, rest = items[:n], items[n:]
        self.pending_path.parent.mkdir(parents=True, exist_ok=True)
        self.pending_path.write_text("".join(json.dumps(i, ensure_ascii=False) + "\n" for i in rest), encoding="utf-8")
        return taken
