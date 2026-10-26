"""Menschenlesbare Gedächtnis-Dateien: Tages-Journal, Tageszusammenfassungen und Fakten."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

WEEKDAYS = ["Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag", "Samstag", "Sonntag"]
DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def day_str(d: date | datetime | None = None) -> str:
    return (d or datetime.now()).strftime("%Y-%m-%d")


def german_date(d: date | datetime) -> str:
    return f"{WEEKDAYS[d.weekday()]}, {d.strftime('%d.%m.%Y')}"


def valid_day(day: str) -> bool:
    if not DAY_RE.match(day):
        return False
    try:
        datetime.strptime(day, "%Y-%m-%d")
    except ValueError:
        return False
    return True


@dataclass
class JournalEntry:
    time: str
    speaker: str
    text: str
    chat: str = ""


# Kopfzeile eines Eintrags; die Chat-Kennung steht unsichtbar als HTML-Kommentar dahinter
ENTRY_RE = re.compile(r"^### (\d{2}:\d{2}:\d{2}) — (.+?)(?: <!-- chat:([\w-]+) -->)?$", re.M)
CHAT_TAG_RE = re.compile(r" <!-- chat:[\w-]+ -->")


class Journal:
    """Ein Markdown-Protokoll pro Tag: journal/YYYY-MM-DD.md"""

    def __init__(self, directory: Path):
        self.dir = directory
        self.dir.mkdir(parents=True, exist_ok=True)

    def path(self, day: str) -> Path:
        return self.dir / f"{day}.md"

    def append(self, speaker: str, text: str, when: datetime | None = None, chat: str = "") -> None:
        when = when or datetime.now()
        path = self.path(day_str(when))
        new = not path.exists()
        tag = f" <!-- chat:{chat} -->" if chat else ""
        with path.open("a", encoding="utf-8") as f:
            if new:
                f.write(f"# Journal {german_date(when)}\n\n")
            f.write(f"### {when.strftime('%H:%M:%S')} — {speaker}{tag}\n{text.strip()}\n\n")

    def days(self) -> list[str]:
        return sorted((p.stem for p in self.dir.glob("*.md") if valid_day(p.stem)), reverse=True)

    def read(self, day: str, raw: bool = False) -> str | None:
        """Tagesprotokoll; ohne raw ohne die unsichtbaren Chat-Kennungen."""
        if not valid_day(day):
            return None
        p = self.path(day)
        if not p.exists():
            return None
        text = p.read_text(encoding="utf-8")
        return text if raw else CHAT_TAG_RE.sub("", text)

    def entries(self, day: str) -> list[JournalEntry]:
        text = self.read(day, raw=True) or ""
        matches = list(ENTRY_RE.finditer(text))
        out = []
        for i, m in enumerate(matches):
            end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
            out.append(JournalEntry(m.group(1), m.group(2), text[m.end():end].strip(), m.group(3) or ""))
        return out

    def remove_chat(self, chat: str) -> list[str]:
        """Entfernt alle Einträge eines Chats; liefert die betroffenen Tage. Leere Tage werden gelöscht."""
        affected = []
        for day in self.days():
            text = self.read(day, raw=True) or ""
            if f"<!-- chat:{chat} -->" not in text:
                continue
            matches = list(ENTRY_RE.finditer(text))
            head = text[: matches[0].start()] if matches else text
            keep = []
            for i, m in enumerate(matches):
                end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
                if m.group(3) != chat:
                    keep.append(text[m.start():end])
            affected.append(day)
            if keep:
                self.path(day).write_text(head + "".join(keep), encoding="utf-8")
            else:
                self.path(day).unlink()
        return affected

    def mtime(self, day: str) -> float:
        p = self.path(day)
        return p.stat().st_mtime if p.exists() else 0.0


class Summaries:
    """Von Orbwise erzeugte Tageszusammenfassungen: summaries/YYYY-MM-DD.md"""

    def __init__(self, directory: Path):
        self.dir = directory
        self.dir.mkdir(parents=True, exist_ok=True)

    def path(self, day: str) -> Path:
        return self.dir / f"{day}.md"

    def write(self, day: str, text: str) -> None:
        d = datetime.strptime(day, "%Y-%m-%d")
        self.path(day).write_text(f"# Zusammenfassung {german_date(d)}\n\n{text.strip()}\n", encoding="utf-8")

    def delete(self, day: str) -> None:
        if valid_day(day) and self.path(day).exists():
            self.path(day).unlink()
        self.forget_state(day)

    # Welcher Journal-Stand wurde zusammengefasst? (Inhalt statt Dateizeit – eine Journal-Datei mit einer Zeit in der
    # Zukunft, z. B. aus einem Backup, ließ den Tag sonst alle paar Minuten neu zusammenfassen)
    def _state_path(self) -> Path:
        return self.dir / ".state.json"

    def _state(self) -> dict:
        try:
            return json.loads(self._state_path().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def summarized(self, day: str) -> str | None:
        return self._state().get(day)

    def mark(self, day: str, digest: str) -> None:
        state = self._state()
        state[day] = digest
        tmp = self._state_path().with_suffix(".tmp")
        tmp.write_text(json.dumps(state, indent=0, sort_keys=True), encoding="utf-8")
        tmp.replace(self._state_path())

    def forget_state(self, day: str) -> None:
        state = self._state()
        if state.pop(day, None) is not None:
            self._state_path().write_text(json.dumps(state, indent=0, sort_keys=True), encoding="utf-8")


    def read(self, day: str) -> str | None:
        if not valid_day(day):
            return None
        p = self.path(day)
        return p.read_text(encoding="utf-8") if p.exists() else None

    def days(self) -> list[str]:
        return sorted((p.stem for p in self.dir.glob("*.md") if valid_day(p.stem)), reverse=True)

    def mtime(self, day: str) -> float:
        p = self.path(day)
        return p.stat().st_mtime if p.exists() else 0.0


FACT_RE = re.compile(r"^- (.+?)(?: _\((\d{4}-\d{2}-\d{2})\)_)?$")


class Facts:
    """Kuratierte Langzeit-Fakten in facts.md (eine Zeile pro Fakt, von Hand editierbar)."""

    HEADER = "# Fakten, die Orbwise sich dauerhaft merkt\n\n"

    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self.path.write_text(self.HEADER, encoding="utf-8")

    def list(self) -> list[tuple[str, str]]:
        out = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            m = FACT_RE.match(line.strip())
            if m:
                out.append((m.group(1).strip(), m.group(2) or ""))
        return out

    def _write(self, facts: list[tuple[str, str]]) -> None:
        lines = [f"- {f} _({d})_" if d else f"- {f}" for f, d in facts]
        self.path.write_text(self.HEADER + "\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")

    def add(self, fact: str) -> bool:
        fact = " ".join(fact.split())
        if not fact:
            return False
        facts = self.list()
        if any(f.lower() == fact.lower() for f, _ in facts):
            return False
        facts.append((fact, day_str()))
        self._write(facts)
        return True

    def replace(self, old: str, new: str) -> bool:
        """Ersetzt einen Fakt durch eine neuere Fassung (z. B. geänderter Pfad) – mit heutigem Datum ans Ende."""
        new = " ".join(new.split())
        facts = self.list()
        keep = [(f, d) for f, d in facts if f != old]
        if len(keep) == len(facts) or not new:
            return False
        self._write(keep + [(new, day_str())])
        return True

    def remove(self, query: str) -> list[str]:
        q = query.lower().strip()
        if not q:
            return []
        facts = self.list()
        keep = [(f, d) for f, d in facts if q not in f.lower()]
        removed = [f for f, _ in facts if q in f.lower()]
        if removed:
            self._write(keep)
        return removed

    def as_text(self, max_chars: int) -> str:
        """Neueste Fakten haben Vorrang, falls das Budget nicht reicht."""
        lines: list[str] = []
        used = 0
        for fact, d in reversed(self.list()):
            line = f"- {fact}" + (f" ({d})" if d else "")
            if used + len(line) > max_chars:
                break
            lines.append(line)
            used += len(line) + 1
        return "\n".join(reversed(lines))


def digest(text: str) -> str:
    """Kurzer Fingerabdruck eines Journals (Inhalt, nicht Dateizeit)."""
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:16]
