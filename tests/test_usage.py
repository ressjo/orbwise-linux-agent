"""Brave-Kontingent: nur Braves Angaben aus den Antwort-Headern merken (keine eigene Zählung), bei leerem Kontingent
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


def test_record_keeps_braves_quota(cfg):
    cfg.tools.brave_api_key = "secret"
    assert usage.summary(cfg)["brave"]["plan"] is None  # noch keine Antwort von Brave
    now = datetime(2026, 10, 18, 12).timestamp()
    usage.record(cfg, "brave", httpx.Headers(BRAVE_HEADERS), now=now)
    usage.record(cfg, "brave", httpx.Headers({"X-RateLimit-Limit": "kaputt"}), now=now + 5)  # ändert nichts
    s = usage.summary(cfg, now=now)["brave"]
    assert (s["plan"], s["limit"], s["remaining"], s["used"]) == ("quota", 2000, 1877, 123)
    assert s["reset_at"] == now + 86400 and s["at"] == now and "month" not in s
    usage.record(cfg, "brave", httpx.Headers({**BRAVE_HEADERS, "X-RateLimit-Remaining": "0, 1800"}), status=429,
                 now=now)
    assert usage.summary(cfg, now=now)["brave"]["used"] == 200
    cfg.tools.brave_api_key = ""
    assert usage.summary(cfg) == {}


def test_old_counter_is_dropped(cfg):
    """Frühere eigene Zählung in usage.json: wird nicht mehr gezeigt und beim nächsten Schreiben entfernt."""
    cfg.tools.brave_api_key = "secret"
    path = cfg.memory.dir.parent / "usage.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{"brave": {"months": {"2026-10": 42}}}')
    assert usage.summary(cfg)["brave"]["plan"] is None
    usage.record(cfg, "brave", httpx.Headers(BRAVE_HEADERS))
    assert "months" not in path.read_text()


def test_blocked_only_after_a_real_refusal(cfg):
    now = time.time()
    empty = httpx.Headers({**BRAVE_HEADERS, "X-RateLimit-Remaining": "0, 0"})
    usage.record(cfg, "brave", empty, now=now)  # HTTP 200 mit Rest 0: Brave hat geantwortet → nicht sperren
    assert usage.exhausted(cfg, "brave", now=now) is None
    usage.record(cfg, "brave", empty, status=429, now=now)
    assert usage.exhausted(cfg, "brave", now=now + 10) == pytest.approx(now + 86400)
    assert usage.exhausted(cfg, "brave", now=now + 90000) is None
    usage.record(cfg, "brave", httpx.Headers(BRAVE_HEADERS), now=now)  # wieder erfolgreich → frei
    assert usage.exhausted(cfg, "brave", now=now) is None


def test_credit_billing_has_no_monthly_limit(cfg):
    """Brave mit Guthaben: Monatswert 0 heißt „kein festes Limit“, nicht „aufgebraucht“."""
    cfg.tools.brave_api_key = "secret"
    credit = httpx.Headers({"X-RateLimit-Limit": "50, 0", "X-RateLimit-Remaining": "49, 0", "X-RateLimit-Reset": "1, 0"})
    usage.record(cfg, "brave", credit)
    usage.record(cfg, "brave", credit, status=429)
    s = usage.summary(cfg)["brave"]
    assert s["plan"] == "credit" and s["limit"] is None and s["used"] is None and s["blocked_until"] is None
    assert s["dashboard"].startswith("https://api-dashboard.search.brave.com")
    assert usage.exhausted(cfg, "brave") is None


def test_old_usage_file_does_not_block(cfg):
    """Stand vor dem Fix: remaining 0 gespeichert, ohne Sperre – die Suche muss sofort wieder gehen."""
    cfg.tools.brave_api_key = "secret"
    path = cfg.memory.dir.parent / "usage.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{"brave": {"months": {}, "limit": 0, "remaining": 0, "reset_at": %d}}' % (time.time() + 9999))
    s = usage.summary(cfg)["brave"]
    assert usage.exhausted(cfg, "brave") is None and s["limit"] is None and s["plan"] is None


def test_web_search_records_quota_and_skips_brave_when_used_up(cfg, monkeypatch):
    cfg.tools.brave_api_key = "secret"
    calls = []

    def handler(r):
        calls.append(r)
        return httpx.Response(200, headers=BRAVE_HEADERS, json={"web": {"results": [
            {"title": "T", "url": "https://t.de", "description": "C"}]}})
    monkeypatch.setattr(web, "TRANSPORT", httpx.MockTransport(handler))
    run(web.web_search(ctx(cfg), "x"))
    run(web.web_search(ctx(cfg), "y"))
    assert len(calls) == 2 and usage.summary(cfg)["brave"]["used"] == 123

    usage.record(cfg, "brave", httpx.Headers({**BRAVE_HEADERS, "X-RateLimit-Remaining": "0, 0"}), status=429)
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
        assert brave["plan"] == "quota" and brave["used"] == 123 and brave["remaining"] == 1877
