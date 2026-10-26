"""Chat-Historie: jeder Chat ist eine eigene Datei memory/chats/<id>.json (Verlauf + Zusammenfassungen je Epoche).

Der aktive Chat steht in memory/chats/active. Ein früheres session.json wird beim ersten Start übernommen.
"""

from __future__ import annotations

import json
import logging
import re
import time
import uuid
from pathlib import Path

from .context import Conversation, make_title

log = logging.getLogger(__name__)

ID_RE = re.compile(r"^[a-f0-9]{8,32}$")
MODES = ("tools", "coding")  # Tools-Modus (Assistent) und Coding-Modus haben getrennte Chat-Verläufe


def chat_mode(meta: dict | None) -> str:
    mode = (meta or {}).get("mode") or "tools"
    return mode if mode in MODES else "tools"


def new_id() -> str:
    return uuid.uuid4().hex[:12]


class ChatStore:
    def __init__(self, directory: Path, legacy_session: Path | None = None):
        self.dir = directory
        self.dir.mkdir(parents=True, exist_ok=True)
        self.active_file = self.dir / "active"
        self.mode_file = self.dir / "mode"  # zuletzt benutzter Modus
        if legacy_session and legacy_session.exists():
            self._migrate(legacy_session)

    # ---------- Dateien ----------
    def path(self, chat_id: str) -> Path:
        if not ID_RE.match(chat_id or ""):
            raise KeyError(chat_id)
        return self.dir / f"{chat_id}.json"

    def exists(self, chat_id: str) -> bool:
        try:
            return self.path(chat_id).exists()
        except KeyError:
            return False

    def _migrate(self, legacy: Path) -> None:
        """Früheres einzelnes Gespräch (session.json) wird zum ersten Chat."""
        try:
            data = json.loads(legacy.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            data = {}
        history = data.get("history") or []
        if history:
            chat_id = new_id()
            first = next((m.get("content", "") for m in history if m.get("role") == "user"), "")
            created = history[0].get("ts", time.time())
            conv = Conversation(self.path(chat_id), chat_id)
            conv.history, conv.running_summary = history, data.get("running_summary", "")
            conv.meta.update({"title": make_title(first) if first else "Früheres Gespräch", "created": created,
                              "legacy": True})
            conv.save()
            self.active_file.write_text(chat_id, encoding="utf-8")
        legacy.rename(legacy.with_suffix(".json.migrated"))

    # ---------- Aktiver Chat (je Modus einer) ----------
    def _active_file(self, mode: str) -> Path:
        return self.active_file if mode == "tools" else self.dir / f"active-{mode}"

    def current_mode(self) -> str:
        try:
            mode = self.mode_file.read_text(encoding="utf-8").strip()
        except OSError:
            return "tools"
        return mode if mode in MODES else "tools"

    def set_mode(self, mode: str) -> None:
        self.mode_file.write_text(mode if mode in MODES else "tools", encoding="utf-8")

    def active_id(self, mode: str = "tools") -> str:
        try:
            chat_id = self._active_file(mode).read_text(encoding="utf-8").strip()
        except OSError:
            return ""
        return chat_id if self.exists(chat_id) else ""

    def open(self, chat_id: str) -> Conversation:
        return Conversation(self.path(chat_id), chat_id)

    def open_active(self, mode: str | None = None) -> Conversation:
        mode = mode or self.current_mode()
        chat_id = self.active_id(mode)
        return self.open(chat_id) if chat_id else self.create(mode=mode)

    def create(self, activate: bool = True, mode: str = "tools") -> Conversation:
        chat_id = new_id()
        conv = Conversation(self.path(chat_id), chat_id)
        if mode != "tools":
            conv.meta["mode"] = mode
        conv.save()
        if activate:
            self._active_file(mode).write_text(chat_id, encoding="utf-8")
        return conv

    def set_active(self, chat_id: str) -> Conversation:
        conv = self.open(chat_id)
        mode = chat_mode(conv.meta)
        self._active_file(mode).write_text(chat_id, encoding="utf-8")
        self.set_mode(mode)
        return conv

    # ---------- Verwaltung ----------
    def _read(self, path: Path) -> dict | None:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None

    def list(self, query: str = "", mode: str | None = None) -> list[dict]:
        """Alle Chats mit Inhalt: gesternte zuerst, sonst neueste zuerst. query sucht in Titel und Nachrichten;
        mode zeigt nur die Chats eines Modus (Tools oder Coding)."""
        actives = {self.active_id(m) for m in MODES} - {""}
        q = query.strip().lower()
        out = []
        for path in self.dir.glob("*.json"):
            data = self._read(path)
            if data is None:
                continue
            meta = data.get("meta") or {}
            chat_id = meta.get("id") or path.stem
            if mode and chat_mode(meta) != mode:
                continue
            active = chat_id if chat_id in actives else ""
            msgs = [m for m in data.get("history", []) if m.get("role") in ("user", "assistant") and m.get("content")]
            if not msgs and chat_id != active:
                continue
            if q and q not in (meta.get("title") or "").lower() and \
                    not any(q in (m.get("content") or "").lower() for m in msgs):
                continue
            last_user = next((m["content"] for m in reversed(msgs) if m["role"] == "user"), "")
            out.append({
                "id": chat_id, "title": meta.get("title") or "Neuer Chat",
                "created": meta.get("created"), "updated": meta.get("updated") or meta.get("created"),
                "starred": bool(meta.get("starred")), "messages": len(msgs), "active": chat_id == active,
                "preview": make_title(last_user, 90) if last_user else "", "legacy": bool(meta.get("legacy")),
                "mode": chat_mode(meta), "project": meta.get("project") or "",
                "sandbox": bool((meta.get("sandbox") or {}).get("enabled")),
            })
        out.sort(key=lambda c: (not c["starred"], -(c["updated"] or 0)))
        return out

    def _update_meta(self, chat_id: str, **changes) -> dict:
        path = self.path(chat_id)
        data = self._read(path)
        if data is None:
            raise KeyError(chat_id)
        data.setdefault("meta", {}).update(changes)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        tmp.replace(path)
        return data["meta"]

    def set_star(self, chat_id: str, starred: bool) -> dict:
        return self._update_meta(chat_id, starred=bool(starred))

    def rename(self, chat_id: str, title: str) -> dict:
        return self._update_meta(chat_id, title=make_title(title, 80) or "Neuer Chat")

    def delete(self, chat_id: str) -> None:
        self.path(chat_id).unlink(missing_ok=True)
        for mode in MODES:
            f = self._active_file(mode)
            if f.exists() and f.read_text(encoding="utf-8").strip() == chat_id:
                f.unlink()
