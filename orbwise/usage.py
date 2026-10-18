"""Verbrauch kostenpflichtiger bzw. kontingentierter Dienste je Monat – zurzeit die Brave Search API.

Brave schickt bei jeder Antwort sein Kontingent mit (X-RateLimit-Limit/-Remaining/-Reset, jeweils „pro Sekunde, pro
Monat“). Orbwise zählt selbst mit und merkt sich die Zahlen von Brave – die Aktivität zeigt so, wie viel vom
Monatskontingent schon verbraucht ist, und die Websuche weicht aus, wenn es aufgebraucht ist."""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime
from pathlib import Path

log = logging.getLogger(__name__)


def _path(cfg) -> Path:
    return Path(cfg.memory.dir).expanduser().parent / "usage.json"


def _read(cfg) -> dict:
    try:
        data = json.loads(_path(cfg).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write(cfg, data: dict) -> None:
    path = _path(cfg)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
        tmp.replace(path)
    except OSError as e:
        log.info("Verbrauch nicht gespeichert: %s", e)


def _monthly(value: str | None) -> int | None:
    """„1, 2000“ (pro Sekunde, pro Monat) → 2000; ein einzelner Wert gilt als Monatswert."""
    try:
        return int(str(value).split(",")[-1].strip())
    except (TypeError, ValueError):
        return None


def month_key(now: float | None = None) -> str:
    return datetime.fromtimestamp(now or time.time()).strftime("%Y-%m")


def record(cfg, service: str, headers=None, count: bool = True, now: float | None = None) -> None:
    """Eine Anfrage zählen (count=False: nur das Kontingent aus den Headern übernehmen, z. B. bei HTTP 429)."""
    now = now or time.time()
    data = _read(cfg)
    entry = data.setdefault(service, {})
    months = entry.setdefault("months", {})
    if count:
        key = month_key(now)
        months[key] = int(months.get(key, 0)) + 1
    if headers is not None:
        limit = _monthly(headers.get("x-ratelimit-limit"))
        remaining = _monthly(headers.get("x-ratelimit-remaining"))
        reset = _monthly(headers.get("x-ratelimit-reset"))
        if limit is not None and remaining is not None:
            entry.update(limit=limit, remaining=remaining, at=now)
            if reset is not None:
                entry["reset_at"] = now + reset
    data[service] = entry
    _write(cfg, data)


def exhausted(cfg, service: str, now: float | None = None) -> float | None:
    """Aufgebraucht laut der letzten Antwort? → Zeitpunkt, ab dem es wieder geht; sonst None."""
    entry = _read(cfg).get(service) or {}
    reset_at = entry.get("reset_at")
    if entry.get("remaining") == 0 and reset_at and reset_at > (now or time.time()):
        return float(reset_at)
    return None


def summary(cfg, now: float | None = None) -> dict:
    """Für die Aktivität: je Dienst Anfragen diesen Monat, Kontingent laut Dienst, frühere Monate."""
    out = {}
    if getattr(cfg.tools, "brave_key", ""):
        entry = _read(cfg).get("brave") or {}
        months = entry.get("months") or {}
        current = month_key(now)
        out["brave"] = {"month": int(months.get(current, 0)), "limit": entry.get("limit"),
                        "remaining": entry.get("remaining"), "reset_at": entry.get("reset_at"), "at": entry.get("at"),
                        "last_months": sorted(((k, v) for k, v in months.items() if k != current), reverse=True)[:6]}
    return out
