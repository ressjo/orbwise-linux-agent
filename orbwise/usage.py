"""Kontingent kostenpflichtiger bzw. kontingentierter Dienste laut Dienst selbst – zurzeit die Brave Search API.

Brave schickt bei jeder Antwort sein Kontingent mit (X-RateLimit-Limit/-Remaining/-Reset, jeweils „pro Sekunde, pro
Monat“). Orbwise zählt nicht selbst, sondern zeigt genau diese Angaben: bei Tarifen mit festem Monatskontingent
verbraucht/Limit/Rest, bei Guthaben-Tarifen (Monatswert 0) nur „Guthaben-Tarif“ – das Guthaben meldet Brave nicht,
es steht im Brave-Dashboard. Ist das Kontingent aufgebraucht (HTTP 429), weicht die Websuche aus."""

from __future__ import annotations

import json
import logging
import time
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


DASHBOARD = {"brave": "https://api-dashboard.search.brave.com"}


def record(cfg, service: str, headers=None, now: float | None = None, status: int = 200) -> None:
    """Das Kontingent aus den Antwort-Headern übernehmen. Ein festes Monatskontingent gibt es nur, wenn der Dienst ein
    Limit > 0 meldet – Brave rechnet auch über Guthaben ab und meldet dann 0 als Monatswert, was nicht „aufgebraucht“
    heißt. Gesperrt wird nur nach einer echten Absage (HTTP 429 bei Rest 0); jede erfolgreiche Antwort hebt das wieder
    auf."""
    now = now or time.time()
    data = _read(cfg)
    entry = data.get(service) or {}
    entry.pop("months", None)  # frühere eigene Zählung
    if status == 200:
        entry.pop("blocked_until", None)
    if headers is not None:
        limit = _monthly(headers.get("x-ratelimit-limit"))
        remaining = _monthly(headers.get("x-ratelimit-remaining"))
        reset = _monthly(headers.get("x-ratelimit-reset"))
        if limit is not None and limit > 0 and remaining is not None:
            entry.update(plan="quota", limit=limit, remaining=remaining, at=now)
            if reset is not None:
                entry["reset_at"] = now + reset
            if status == 429 and remaining <= 0 and reset:
                entry["blocked_until"] = now + reset
        elif limit is not None:  # kein festes Monatslimit (Abrechnung über Guthaben)
            for key in ("limit", "remaining", "reset_at"):
                entry.pop(key, None)
            entry.update(plan="credit", at=now)
    data[service] = entry
    _write(cfg, data)


def exhausted(cfg, service: str, now: float | None = None) -> float | None:
    """Hat der Dienst das Monatskontingent als aufgebraucht gemeldet (HTTP 429)? → ab wann es wieder geht; sonst None."""
    until = (_read(cfg).get(service) or {}).get("blocked_until")
    if until and until > (now or time.time()):
        return float(until)
    return None


def summary(cfg, now: float | None = None) -> dict:
    """Für die Aktivität: je Dienst Tarif und Kontingent laut Dienst (Stand der letzten Antwort)."""
    out = {}
    if getattr(cfg.tools, "brave_key", ""):
        entry = _read(cfg).get("brave") or {}
        quota = (entry.get("limit") or 0) > 0  # ältere Einträge: 0 = kein festes Limit (Guthaben)
        plan = "quota" if quota else entry.get("plan") or ("credit" if entry.get("at") else None)
        limit, remaining = (entry.get("limit"), entry.get("remaining")) if quota else (None, None)
        out["brave"] = {"plan": plan, "limit": limit, "remaining": remaining,
                        "used": limit - remaining if quota and remaining is not None else None,
                        "reset_at": entry.get("reset_at") if quota else None, "at": entry.get("at"),
                        "blocked_until": exhausted(cfg, "brave", now), "dashboard": DASHBOARD["brave"]}
    return out
