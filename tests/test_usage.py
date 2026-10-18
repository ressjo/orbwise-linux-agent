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


def test_exhausted_only_until_reset(cfg):
    now = time.time()
    usage.record(cfg, "brave", httpx.Headers({**BRAVE_HEADERS, "X-RateLimit-Remaining": "0, 0"}), now=now)
    assert usage.exhausted(cfg, "brave", now=now + 10) == pytest.approx(now + 86400)
    assert usage.exhausted(cfg, "brave", now=now + 90000) is None
    usage.record(cfg, "brave", httpx.Headers(BRAVE_HEADERS), now=now)
    assert usage.exhausted(cfg, "brave", now=now) is None


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

    usage.record(cfg, "brave", httpx.Headers({**BRAVE_HEADERS, "X-RateLimit-Remaining": "0, 0"}), count=False)
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
