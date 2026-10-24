"""Platz im Kontextfenster schaffen: zuerst alte Werkzeug-Ergebnisse ausblenden (Microcompact), kurz vor dem Limit den
Chat einmal strukturiert zusammenfassen (neue Epoche mit Zusammenfassung und den letzten Schritten).

Mixin für Agent – nutzt dessen Zustand (self.memory, self.llm, self.cfg, Budgets aus agent.py)."""

from __future__ import annotations

import logging
import os
import re
import time
import uuid
from collections.abc import Awaitable, Callable
from pathlib import Path

from . import prompts
from .llm import ContextOverflow, LLMError, strip_think
from .memory import est_tokens
from .memory.context import msg_tokens, render_transcript, shrink_tool_results
from .tools.proc import clip

log = logging.getLogger(__name__)
# Werkzeug-Aufruf als Text: llama-server gibt ihn bei tool_choice "none" als Inhalt zurück
RAW_TOOL_CALL = re.compile(r'\s*(<tool_call>|<function=|\[TOOL_CALLS\]|\{\s*"name"\s*:)')
Emit = Callable[[dict], Awaitable[None]]


class CompactMixin:
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
