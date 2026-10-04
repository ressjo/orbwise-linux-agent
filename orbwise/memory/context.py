"""Gesprächsverlauf in Epochen – Abschnitten zwischen zwei Komprimierungen (Vorbild: Claude Code).

Innerhalb einer Epoche wird der Prompt nur hinten verlängert: Was einmal an das Modell ging, bleibt Byte für Byte
gleich (auch die Kontext-Notiz einer Nutzernachricht wird beim ersten Senden eingefroren). So kann der
Modell-Server seinen Cache weiterverwenden und liest nur Neues ein. Wird das Fenster voll, fasst das Modell die
Epoche einmal strukturiert zusammen; die nächste Epoche beginnt mit dieser Zusammenfassung und den letzten
Schritten.

Nichts geht verloren: Der volle Verlauf bleibt in der Chat-Datei (und in der Oberfläche); Journal und Suchindex
behalten alles, und Details aus komprimierten Teilen holt das Gedächtnis bei Bedarf zurück.
"""

from __future__ import annotations

import copy
import json
import logging
import time
from collections.abc import Callable
from pathlib import Path

log = logging.getLogger(__name__)

PROMPT_KEYS = ("role", "content", "tool_calls", "tool_name", "attachments")  # was davon an das Modell geht
CLEAR_MIN_CHARS = 600  # kürzere Werkzeug-Ergebnisse lohnen das Ausblenden nicht
CLEAR_ARG_CHARS = 800  # längere Aufruf-Argumente (z. B. der Inhalt von write_file) werden beim Ausblenden gekürzt


def est_tokens(text: str) -> int:
    # Grobe, bewusst konservative Schätzung (Deutsch ≈ 3–3,5 Zeichen pro Token)
    return len(text) // 3 + 1


def msg_tokens(msg: dict) -> int:
    n = est_tokens((msg.get("note") or "") + (msg.get("content") or "")) + 4
    if msg.get("attachments"):  # angehängte Bilder (grob – je nach Modell und Größe)
        n += 800 * len(msg["attachments"])
    if msg.get("tool_calls"):
        n += est_tokens(json.dumps(msg["tool_calls"], ensure_ascii=False))
    return n


def render_transcript(messages: list[dict], max_chars: int = 20000) -> str:
    """Verlauf als Text (Notfall-Zusammenfassung, wenn das Modell keine liefern kann). Lange Werkzeug-Ergebnisse
    behalten Anfang und Ende; reicht der Platz nicht, zählt das Neueste."""
    results = sum(1 for m in messages if m["role"] == "tool")
    per = max(300, (max_chars - 200 * len(messages)) // max(1, results))
    lines = []
    for m in messages:
        content = (m.get("content") or "").strip()
        if m["role"] == "user":
            lines.append(f"Nutzer: {content}")
        elif m["role"] == "assistant":
            if content:
                lines.append(f"Jarvis: {content}")
            for call in m.get("tool_calls") or []:
                fn = call.get("function", {})
                args = json.dumps(fn.get("arguments"), ensure_ascii=False)
                lines.append(f"Aufruf {fn.get('name')}: {args[:400] + '…' if len(args) > 400 else args}")
        elif m["role"] == "tool":
            if len(content) > per:
                head = per * 3 // 4
                content = f"{content[:head]}\n… [{len(content) - per} Zeichen ausgelassen] …\n{content[-(per - head):]}"
            lines.append(f"Ergebnis ({m.get('tool_name', '')}):\n{content}")
    text = "\n".join(lines)
    return text if len(text) <= max_chars else "… " + text[-max_chars:]


def make_title(text: str, limit: int = 60) -> str:
    """Chat-Titel aus der ersten Nutzernachricht."""
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    cut = text[:limit].rsplit(" ", 1)[0]
    return (cut or text[:limit]) + " …"


def new_epoch(start: int = 0, summary: str = "", **info) -> dict:
    """facts/groups = None: werden beim ersten Prompt der Epoche festgelegt (Schnappschuss) und bleiben dann gleich.
    shown: IDs der Erinnerungen, die in dieser Epoche schon mitgeschickt wurden."""
    return {"start": start, "summary": summary, "ts": time.time(), "facts": None, "groups": None, "shown": [],
            **info}


class Conversation:
    def __init__(self, state_path: Path | None = None, chat_id: str = ""):
        self.state_path = state_path
        self.history: list[dict] = []
        self.epochs: list[dict] = [new_epoch()]
        # Metadaten des Chats (Titel, Stern, Zeitstempel) – siehe memory/chats.py
        self.meta: dict = {"id": chat_id, "title": "", "starred": False, "created": time.time()}
        self.load()

    @property
    def chat_id(self) -> str:
        return self.meta.get("id", "")

    # ---------- Epochen ----------
    @property
    def epoch(self) -> dict:
        if not self.epochs:
            self.epochs.append(new_epoch())
        return self.epochs[-1]

    @property
    def running_summary(self) -> str:
        """Zusammenfassung alles Früheren (steht im System-Prompt der aktuellen Epoche)."""
        return self.epoch.get("summary") or ""

    @running_summary.setter
    def running_summary(self, text: str) -> None:
        self.epoch["summary"] = text or ""

    def epoch_indices(self) -> list[int]:
        """Indizes der Nachrichten, die in dieser Epoche an das Modell gehen (komprimierte stehen nur im Verlauf)."""
        return [i for i in range(self.epoch["start"], len(self.history)) if not self.history[i].get("packed")]

    def epoch_messages(self) -> list[dict]:
        return [self.history[i] for i in self.epoch_indices()]

    def turn_start(self) -> int:
        """Index der letzten Nutzernachricht (Beginn der laufenden bzw. letzten Runde)."""
        return max((i for i, m in enumerate(self.history) if m["role"] == "user"), default=0)

    def start_epoch(self, carry: list[int], summary: str, **info) -> dict:
        """Neue Epoche nach einer Komprimierung. carry: Indizes der Nachrichten, die wörtlich mitgehen (aufsteigend);
        alles andere der alten Epoche erreicht das Modell nur noch über die Zusammenfassung – gespeichert bleibt es."""
        start = carry[0] if carry else len(self.history)
        keep = set(carry)
        for i in range(start, len(self.history)):
            if i not in keep:
                self.history[i]["packed"] = True
        ep = new_epoch(start, summary, **info)
        self.epochs.append(ep)
        return ep

    # ---------- Persistenz (Gespräch überlebt Neustarts) ----------
    def load(self) -> None:
        if self.state_path and self.state_path.exists():
            try:
                data = json.loads(self.state_path.read_text(encoding="utf-8"))
                self.history = data.get("history", [])
                self.meta.update(data.get("meta") or {})
                self.epochs = data.get("epochs") or [self._migrated_epoch(data)]
            except (json.JSONDecodeError, OSError) as e:
                log.warning("Sitzungszustand nicht lesbar (%s) – starte neu", e)

    @staticmethod
    def _migrated_epoch(data: dict) -> dict:
        """Chats von früher: laufende Zusammenfassung (und Arbeitsstand einer komprimierten Aufgabe) werden zur
        Zusammenfassung der ersten Epoche."""
        summary = data.get("running_summary") or ""
        if data.get("task_note"):
            summary = (summary + "\n\nArbeitsstand der letzten Aufgabe:\n" + data["task_note"]).strip()
        return new_epoch(0, summary)

    def save(self) -> None:
        if not self.state_path:
            return
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.state_path.with_suffix(".tmp")
        self.meta["updated"] = time.time()
        data = {"meta": self.meta, "history": self.history, "epochs": self.epochs,
                "running_summary": self.running_summary}  # running_summary: für ältere Versionen lesbar
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        tmp.replace(self.state_path)

    def add(self, msg: dict) -> None:
        msg = dict(msg)
        msg.setdefault("ts", time.time())
        if msg.get("role") == "user" and not self.meta.get("title") and msg.get("content"):
            self.meta["title"] = make_title(msg["content"])
        self.history.append(msg)

    def reset(self) -> None:
        self.history = []
        self.epochs = [new_epoch()]
        self.save()

    # ---------- Alte Werkzeug-Ergebnisse ausblenden (wie Claude Codes Microcompact) ----------
    def tool_steps(self) -> list[tuple[int, list[int]]]:
        """Werkzeug-Schritte der Epoche: (Index des Aufrufs, Indizes seiner Ergebnisse)."""
        hist, steps = self.history, []
        for i in self.epoch_indices():
            m = hist[i]
            if m["role"] == "assistant" and m.get("tool_calls"):
                steps.append((i, []))
            elif m["role"] == "tool" and steps:
                steps[-1][1].append(i)
        return steps

    def clearable(self, keep_tokens: int) -> list[int]:
        """Was beim Ausblenden kürzer wird: Ergebnisse älterer Werkzeug-Schritte (die neuesten bleiben, bis
        keep_tokens erreicht sind – mindestens der letzte Schritt) und lange Argumente älterer Aufrufe."""
        steps = self.tool_steps()
        if len(steps) < 2:
            return []
        kept = sum(msg_tokens(self.history[i]) for i in steps[-1][1])
        border = len(steps) - 1
        while border > 0:
            size = sum(msg_tokens(self.history[i]) for i in steps[border - 1][1])
            if kept + size > keep_tokens:
                break
            kept += size
            border -= 1
        out = []
        for call, results in steps[:border]:
            if not self.history[call].get("cleared") and _long_args(self.history[call]):
                out.append(call)
            out += [i for i in results if not self.history[i].get("cleared")
                    and len(self.history[i].get("content") or "") > CLEAR_MIN_CHARS]
        return out

    def clear_results(self, indices: list[int], stub: Callable[[dict], str]) -> int:
        """Blendet aus: Ergebnisse werden zu einem Platzhalter (stub), lange Argumente gekürzt. Das Original bleibt in
        full_content bzw. full_tool_calls (Chat-Datei). Liefert die Zahl der ausgeblendeten Ergebnisse."""
        n = 0
        for i in indices:
            m = self.history[i]
            if m["role"] == "tool":
                m.setdefault("full_content", m.get("content") or "")
                m["content"] = stub(m)
                n += 1
            else:
                m.setdefault("full_tool_calls", copy.deepcopy(m["tool_calls"]))
                m["tool_calls"] = [_short_call(c) for c in m["tool_calls"]]
            m["cleared"] = True
        self.epoch["cleared"] = self.epoch.get("cleared", 0) + n
        return n

    # ---------- Budget ----------
    def history_tokens(self) -> int:
        return sum(msg_tokens(m) for m in self.epoch_messages())

    def window_start(self) -> float | None:
        """Zeitstempel der ältesten Nachricht, die noch wörtlich im Prompt steht (Beginn der Epoche) – ältere
        Teile dieses Chats darf das Gedächtnis wieder hervorholen."""
        idx = self.epoch_indices()
        return self.history[idx[0]].get("ts") if idx else None

    def trimmed_history(self, budget: int) -> list[dict]:
        """Notbremse, falls der Prompt trotz Komprimierung zu groß ist: älteste Teile der Epoche weglassen.
        Im Normalfall greift sie nie – die Komprimierung kommt vorher (sie hält den Prompt-Anfang stabil).

        Die aktuelle Runde (letzte Nutzerfrage + alle Tool-Aufrufe danach) bleibt immer erhalten – ohne
        Nutzerfrage lehnen manche Chat-Vorlagen (Qwen/Bonsai) die Anfrage ab. Ist sie allein zu groß,
        werden ihre Tool-Ergebnisse gekürzt (die ältesten zuerst)."""
        hist = self.epoch_messages()
        last_user = max((i for i, m in enumerate(hist) if m["role"] == "user"), default=0)
        turn = [dict(m) for m in hist[last_user:]]
        used = sum(msg_tokens(m) for m in turn)
        if used > budget:
            shrink_tool_results(turn, used - budget)
            used = sum(msg_tokens(m) for m in turn)
        earlier: list[dict] = []
        for m in reversed(hist[:last_user]):
            t = msg_tokens(m)
            if used + t > budget:
                break
            earlier.append(m)
            used += t
        earlier.reverse()
        # Nie mit einem verwaisten Tool-Ergebnis beginnen
        while earlier and earlier[0]["role"] == "tool":
            earlier.pop(0)
        return [{k: v for k, v in m.items() if k in PROMPT_KEYS or k == "note"} for m in earlier + turn]


def _long_args(msg: dict) -> bool:
    return any(isinstance(v, str) and len(v) > CLEAR_ARG_CHARS
               for c in msg.get("tool_calls") or [] for v in _args(c).values())


def _args(call: dict) -> dict:
    args = call.get("function", {}).get("arguments")
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except json.JSONDecodeError:
            return {}
    return args if isinstance(args, dict) else {}


def _short_call(call: dict) -> dict:
    """Aufruf mit gekürzten langen Argumenten (bleibt gültiges JSON – Chat-Vorlagen lesen die Argumente)."""
    args = _args(call)
    if not any(isinstance(v, str) and len(v) > CLEAR_ARG_CHARS for v in args.values()):
        return call
    args = {k: (f"{v[:200]}… [{len(v)} Zeichen ausgeblendet]" if isinstance(v, str) and len(v) > CLEAR_ARG_CHARS
                else v) for k, v in args.items()}
    return {**call, "function": {**call.get("function", {}), "arguments": args}}


TOOL_MIN_CHARS = 600
TRIM_NOTE = "\n… [gekürzt, damit alles ins Kontextfenster passt]"


def shrink_tool_results(messages: list[dict], excess_tokens: int) -> int:
    """Kürzt Tool-Ergebnisse (älteste zuerst) um insgesamt etwa excess_tokens. Liefert die gekürzten Tokens."""
    freed = 0
    for m in messages:
        if freed >= excess_tokens:
            break
        if m["role"] != "tool":
            continue
        content = m.get("content") or ""
        if len(content) <= TOOL_MIN_CHARS:
            continue
        keep = max(TOOL_MIN_CHARS, len(content) - (excess_tokens - freed) * 3 - len(TRIM_NOTE))
        if keep >= len(content):
            continue
        m["content"] = content[:keep] + TRIM_NOTE
        freed += est_tokens(content) - est_tokens(m["content"])
    return freed
