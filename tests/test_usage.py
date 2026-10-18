"""Brave-Verbrauch: Anfragen je Monat zählen, Kontingent aus den Antwort-Headern merken, bei leerem Kontingent
nicht mehr fragen – und in der Aktivität bzw. bei /api/metrics anzeigen."""

import time
from datetime import datetime

import httpx
import pytest
from conftest import run
from fastapi.testclient import TestClient

from orbwise import usage
from orbwise.tools import web
from orbwise.tools.registry import ToolContext

BRAVE_HEADERS = {"X-RateLimit-Limit": "1, 2000", "X-RateLimit-Remaining": "0, 1877", "X-RateLimit-Reset": "1, 86400"}


def ctx(cfg):
    return ToolContext(cfg=cfg, memory=None)


def test_record_counts_per_month_and_keeps_the_quota(cfg):
    cfg.tools.brave_api_key = "secret"
    oct_ = datetime(2026, 10, 18, 12).timestamp()
    usage.record(cfg, "brave", httpx.Headers(BRAVE_HEADERS), now=oct_)
    usage.record(cfg, "brave", httpx.Headers({"X-RateLimit-Limit": "kaputt"}), now=oct_)  # zählt trotzdem
    s = usage.summary(cfg, now=oct_)["brave"]
    assert s["month"] == 2 and s["limit"] == 2000 and s["remaining"] == 1877 and s["reset_at"] == oct_ + 86400
    nov = datetime(2026, 11, 2, 9).timestamp()
    usage.record(cfg, "brave", None, now=nov)
    s = usage.summary(cfg, now=nov)["brave"]
    assert s["month"] == 1 and s["last_months"] == [("2026-10", 2)]
    usage.record(cfg, "brave", httpx.Headers({**BRAVE_HEADERS, "X-RateLimit-Remaining": "0, 1800"}), count=False,
                 now=nov)  # HTTP 429: nur das Kontingent
    assert usage.summary(cfg, now=nov)["brave"]["month"] == 1
    cfg.tools.brave_api_key = ""
    assert usage.summary(cfg) == {}


def test_blocked_only_after_a_real_refusal(cfg):
    now = time.time()
    empty = httpx.Headers({**BRAVE_HEADERS, "X-RateLimit-Remaining": "0, 0"})
    usage.record(cfg, "brave", empty, now=now)  # HTTP 200 mit Rest 0: Brave hat geantwortet → nicht sperren
    assert usage.exhausted(cfg, "brave", now=now) is None
    usage.record(cfg, "brave", empty, count=False, status=429, now=now)
    assert usage.exhausted(cfg, "brave", now=now + 10) == pytest.approx(now + 86400)
    assert usage.exhausted(cfg, "brave", now=now + 90000) is None
    usage.record(cfg, "brave", httpx.Headers(BRAVE_HEADERS), now=now)  # wieder erfolgreich → frei
    assert usage.exhausted(cfg, "brave", now=now) is None


def test_credit_billing_has_no_monthly_limit(cfg):
    """Brave mit Guthaben: Monatswert 0 heißt „kein festes Limit“, nicht „aufgebraucht“."""
    cfg.tools.brave_api_key = "secret"
    credit = httpx.Headers({"X-RateLimit-Limit": "1, 0", "X-RateLimit-Remaining": "1, 0", "X-RateLimit-Reset": "1, 0"})
    usage.record(cfg, "brave", credit)
    usage.record(cfg, "brave", credit, count=False, status=429)
    s = usage.summary(cfg)["brave"]
    assert s["month"] == 1 and s["limit"] is None and s["remaining"] is None and s["blocked_until"] is None
    assert usage.exhausted(cfg, "brave") is None


def test_old_usage_file_does_not_block(cfg):
    """Stand vor dem Fix: remaining 0 gespeichert, ohne Sperre – die Suche muss sofort wieder gehen."""
    cfg.tools.brave_api_key = "secret"
    path = cfg.memory.dir.parent / "usage.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{"brave": {"months": {}, "limit": 0, "remaining": 0, "reset_at": %d}}' % (time.time() + 9999))
    assert usage.exhausted(cfg, "brave") is None and usage.summary(cfg)["brave"]["limit"] is None


def test_web_search_counts_and_skips_brave_when_used_up(cfg, monkeypatch):
    cfg.tools.brave_api_key = "secret"
    calls = []

    def handler(r):
        calls.append(r)
        return httpx.Response(200, headers=BRAVE_HEADERS, json={"web": {"results": [
            {"title": "T", "url": "https://t.de", "description": "C"}]}})
    monkeypatch.setattr(web, "TRANSPORT", httpx.MockTransport(handler))
    run(web.web_search(ctx(cfg), "x"))
    run(web.web_search(ctx(cfg), "y"))
    assert len(calls) == 2 and usage.summary(cfg)["brave"]["month"] == 2

    usage.record(cfg, "brave", httpx.Headers({**BRAVE_HEADERS, "X-RateLimit-Remaining": "0, 0"}), count=False,
                 status=429)
    cfg.tools.search_fallback = True
    monkeypatch.setattr(web, "_ddgs", lambda q, n: [{"title": "D", "href": "https://d.de", "body": "E"}])
    out = run(web.web_search(ctx(cfg), "z"))
    assert len(calls) == 2  # Brave gar nicht erst gefragt
    assert "aufgebraucht" in out and out.endswith("1. D\nhttps://d.de\nE")
    cfg.tools.search_fallback = False
    out = run(web.web_search(ctx(cfg), "z"))
    assert out.startswith("Websuche fehlgeschlagen") and "aufgebraucht" in out and len(calls) == 2


def test_metrics_include_brave_usage(cfg, monkeypatch):
    monkeypatch.setenv("ORBWISE_FAKE_LLM", "1")
    monkeypatch.setenv("ORBWISE_SKIP_WARMUP", "1")
    from orbwise.server import create_app
    with TestClient(create_app(cfg), base_url="http://localhost:8765") as client:
        assert client.get("/api/metrics").json()["usage"] == {}
        cfg.tools.brave_api_key = "secret"
        usage.record(cfg, "brave", httpx.Headers(BRAVE_HEADERS))
        brave = client.get("/api/metrics").json()["usage"]["brave"]
        assert brave["month"] == 1 and brave["remaining"] == 1877
