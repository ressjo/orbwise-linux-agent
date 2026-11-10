"""Werkzeug-Aufrufe, die ein Modell als Text schreibt statt über die Schnittstelle des Servers.

Üblich ist: Die Chat-Vorlage des Modells formatiert den Aufruf, der Server erkennt ihn und liefert `tool_calls`.
Passt die Vorlage nicht zum Server (ältere llama.cpp-Version, anderer Server, eigenes Modelfile), kommt der Aufruf
als gewöhnlicher Text an – der Agent hielte ihn für die Antwort und der Nutzer sähe rohes JSON. Dieses Modul erkennt
die verbreiteten Schreibweisen und macht daraus wieder Aufrufe:

- Hermes/Qwen:   <tool_call>{"name": "read", "arguments": {"path": "a.py"}}</tool_call>
- Qwen-Coder:    <tool_call><function=read><parameter=path>a.py</parameter></function></tool_call>
- Llama & Co.:   <function=read>{"path": "a.py"}</function>
- Mistral:       [TOOL_CALLS] [{"name": "read", "arguments": {"path": "a.py"}}]   bzw.   [TOOL_CALLS]read{"path": …}
- nacktes JSON:  {"name": "read", "arguments": {"path": "a.py"}}   (nur, wenn die Antwort sonst nichts enthält)

Übernommen wird nur, was ein bekanntes Werkzeug nennt – ein JSON-Beispiel in einer Antwort bleibt Text.
Gate hält solchen Text beim Streamen zurück, damit er gar nicht erst im Chat erscheint.
"""

from __future__ import annotations

import json
import re

MARKERS = ("<tool_call>", "<function=", "[TOOL_CALLS]")
_START_ONLY = '{"name"'  # nacktes JSON: nur am Anfang der Antwort zurückhalten

_TOOL_CALL = re.compile(r"<tool_call>\s*(.*?)\s*(?:</tool_call>|\Z)", re.S)
_FUNCTION = re.compile(r"<function=([\w.-]+)>\s*(.*?)\s*(?:</function>|\Z)", re.S)
_PARAMETER = re.compile(r"<parameter=([\w.-]+)>\n?(.*?)\n?</parameter>", re.S)
_MISTRAL_NAMED = re.compile(r"\[TOOL_CALLS\]\s*([\w.-]+)\s*(\{.*\})", re.S)
_MISTRAL_LIST = re.compile(r"\[TOOL_CALLS\]\s*(\[.*\])", re.S)


def _call(name, args, known: set[str]) -> dict | None:
    if isinstance(args, str):
        try:
            args = json.loads(args) if args.strip() else {}
        except ValueError:
            return None
    if not isinstance(name, str) or name not in known or not isinstance(args, dict):
        return None
    return {"function": {"name": name, "arguments": args}}


def _from_json(raw: str, known: set[str]) -> list[dict] | None:
    """{"name": …, "arguments"|"parameters": …} oder eine Liste davon → Aufrufe; None, wenn es keine sind."""
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    items = data if isinstance(data, list) else [data]
    calls = []
    for item in items:
        if not isinstance(item, dict):
            return None
        fn = item.get("function") if isinstance(item.get("function"), dict) else item
        call = _call(fn.get("name"), fn.get("arguments", fn.get("parameters", {})), known)
        if call is None:
            return None
        calls.append(call)
    return calls or None


def _from_function(name: str, body: str, known: set[str]) -> dict | None:
    """<function=name>…</function>: Parameter als JSON oder als <parameter=key>wert</parameter>."""
    params = _PARAMETER.findall(body)
    if not params:
        return _call(name, body, known)
    args: dict = {}
    for key, value in params:
        try:  # Zahlen, true/false, Listen kommen als JSON-Text; alles andere ist einfach Text
            parsed = json.loads(value)
            args[key] = parsed if not isinstance(parsed, str) else value
        except ValueError:
            args[key] = value
    return _call(name, args, known)


def parse(text: str, known: set[str]) -> tuple[str, list[dict]]:
    """→ (Text ohne die Aufrufe, Aufrufe). Ohne erkannten, vollständigen Aufruf: (text, [])."""
    if not text or not any(m in text for m in MARKERS) and not text.lstrip().startswith("{"):
        return text, []
    calls: list[dict] = []
    spans: list[tuple[int, int]] = []
    for m in _TOOL_CALL.finditer(text):
        body = m.group(1)
        inner = _FUNCTION.fullmatch(body)
        found = [_from_function(inner.group(1), inner.group(2), known)] if inner else _from_json(body, known)
        if not found or None in found:
            return text, []
        calls += found
        spans.append(m.span())
    if not calls:
        for m in _FUNCTION.finditer(text):
            call = _from_function(m.group(1), m.group(2), known)
            if call is None:
                return text, []
            calls.append(call)
            spans.append(m.span())
    if not calls and "[TOOL_CALLS]" in text:
        m = _MISTRAL_LIST.search(text)
        found = _from_json(m.group(1), known) if m else None
        if not found and (m := _MISTRAL_NAMED.search(text)):
            call = _call(m.group(1), m.group(2), known)
            found = [call] if call else None
        if not found:
            return text, []
        calls, spans = found, [(text.index("[TOOL_CALLS]"), len(text))]
    if not calls:  # nacktes JSON – nur, wenn die ganze Antwort daraus besteht
        found = _from_json(text.strip(), known) if text.lstrip().startswith("{") else None
        return ("", found) if found else (text, [])
    rest, pos = [], 0
    for a, b in spans:
        rest.append(text[pos:a])
        pos = b
    rest.append(text[pos:])
    return "".join(rest).strip(), calls


class Gate:
    """Hält beim Streamen Text zurück, der ein Werkzeug-Aufruf sein könnte: alles ab einer Markierung (und ein
    möglicher Anfang einer Markierung am Ende). feed() liefert, was gezeigt werden darf; held() den Rest – der
    Aufrufer zeigt ihn am Ende doch noch, wenn kein Aufruf daraus wurde."""

    def __init__(self):
        self._buf = ""
        self._closed = False  # eine Markierung kam – ab hier wird nichts mehr gezeigt
        self._any = False  # schon sichtbarer Text ausgegeben (nacktes JSON zählt nur ganz am Anfang)

    def feed(self, text: str) -> str:
        self._buf += text
        if self._closed:
            return ""
        idx = min((i for i in (self._buf.find(m) for m in MARKERS) if i >= 0), default=-1)
        if idx < 0 and not self._any:
            lead = self._buf.lstrip()
            if lead and (_START_ONLY.startswith(lead) or lead.startswith(_START_ONLY)):
                if lead.startswith(_START_ONLY):
                    self._closed = True
                return ""  # könnte nacktes JSON werden – abwarten
        if idx >= 0:
            out, self._buf, self._closed = self._buf[:idx], self._buf[idx:], True
        else:
            keep = max((k for m in MARKERS for k in range(1, len(m)) if self._buf.endswith(m[:k])), default=0)
            out, self._buf = self._buf[:len(self._buf) - keep], self._buf[len(self._buf) - keep:]
        self._any = self._any or bool(out.strip())
        return out

    def held(self) -> str:
        text, self._buf = self._buf, ""
        return text
