"""Warum musste der Modell-Server den Prompt neu einlesen? Vergleicht jeden Aufruf mit dem vorigen.

Der Prompt-Cache liefert den Anfang, der sich seit dem letzten Aufruf nicht geändert hat – eingelesen wird nur ab der
ersten Änderung. Steht diese Änderung weit vorn (Werkzeugliste im Systemteil, ausgeblendete alte Ergebnisse, anderer
Chat), muss fast der ganze Chat neu eingelesen werden; bei 60k Token dauert das. Hier wird festgestellt, wo und warum,
damit die Oberfläche es sagen kann – und ob der Server den unveränderten Anfang trotzdem nicht mehr hatte (Neustart,
ein anderes Programm über die API, Hybrid-Modell ohne passenden Checkpoint)."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field

from .memory import est_tokens
from .memory.context import msg_tokens

SERVER_GAP = 2000  # so viele Token weniger aus dem Cache als erwartet → der Server hatte den Anfang nicht mehr


@dataclass
class Fingerprint:
    owner: tuple  # (Profil, Chat, Epoche)
    tools: tuple[str, ...]
    hashes: list[str] = field(default_factory=list)
    ends: list[int] = field(default_factory=list)  # geschätzte Token bis zum Ende jeder Nachricht (inkl. Werkzeuge)
    roles: list[str] = field(default_factory=list)
    empty_tail: bool = False  # Vorwärmen: leere Nutzernachricht am Ende
    mode: tuple = (None, None)  # gesendeter Denkmodus (think, effort) – die Vorlage rendert damit anders


def fingerprint(owner: tuple, messages: list[dict], tools: list[dict] | None, ratio: float = 1.0,
                mode: tuple = (None, None)) -> Fingerprint:
    names = tuple(sorted(t.get("function", {}).get("name", "") for t in tools or []))
    total = est_tokens(json.dumps(tools, ensure_ascii=False)) if tools else 0
    fp = Fingerprint(owner, names, mode=tuple(mode))
    for m in messages:
        fp.hashes.append(hashlib.sha1(json.dumps(m, sort_keys=True, ensure_ascii=False, default=str).encode())
                         .hexdigest())
        total += msg_tokens(m)
        fp.ends.append(int(total * ratio))
        fp.roles.append(m.get("role", ""))
    fp.empty_tail = bool(messages) and messages[-1].get("role") == "user" and not messages[-1].get("content")
    return fp


def compare(new: Fingerprint, old: Fingerprint | None) -> dict:
    """{reason, common (geschätzte gleiche Token am Anfang), …}. reason: first | model | chat | compact | think |
    tools | system | cleared | edited | appended."""
    if old is None:
        return {"reason": "first", "common": 0}
    if new.owner[0] != old.owner[0]:
        return {"reason": "model", "common": 0}
    if new.owner[1] != old.owner[1]:
        return {"reason": "chat", "common": 0}
    if new.owner[2] != old.owner[2]:
        return {"reason": "compact", "common": 0}
    if new.mode != old.mode:  # anderer Denkmodus: die Chat-Vorlage rendert schon den Anfang anders
        return {"reason": "think", "common": 0}
    if new.tools != old.tools:
        return {"reason": "tools", "common": 0, "added": sorted(set(new.tools) - set(old.tools)),
                "removed": sorted(set(old.tools) - set(new.tools))}
    n = min(len(new.hashes), len(old.hashes))
    i = next((k for k in range(n) if new.hashes[k] != old.hashes[k]), n)
    common = new.ends[i - 1] if i else 0
    # alles Alte steht unverändert vorn (beim Vorwärmen bis auf die leere Nutzernachricht am Ende) → nur angehängt
    if i >= len(old.hashes) or (old.empty_tail and i == len(old.hashes) - 1):
        return {"reason": "appended", "common": common}
    if i == 0:
        return {"reason": "system", "common": 0}
    return {"reason": "cleared" if new.roles[i] == "tool" or old.roles[i] == "tool" else "edited",
            "common": common, "at": i}


def with_server(diff: dict, stats: dict, api_used: bool = False) -> dict:
    """Mit den Zahlen des Servers abgleichen: neu eingelesen (prompt_tokens) und aus dem Cache (prompt_cached)."""
    out = dict(diff)
    new, cached = stats.get("prompt_tokens"), stats.get("prompt_cached")
    if new is not None:
        out["new"] = int(new)
    if diff["reason"] == "appended" and cached is not None and diff["common"] - int(cached) > SERVER_GAP:
        out["reason"] = "api" if api_used else "server"
    return out
