"""Briefing: wählbare Punkte, Reihenfolge, Vorausschau, Paperless-Posteingang, Nachrichten, Dashboard-Einstellungen."""

from datetime import datetime, timedelta

import httpx
from conftest import run
from fake_paperless import TOKEN, FakePaperless
from fastapi.testclient import TestClient

from orbwise.config import BriefingConfig
from orbwise.extras import paperless
from orbwise.tools import briefing, proc, reminder_tools, web
from orbwise.tools.registry import ToolContext


def ctx(cfg):
    return ToolContext(cfg=cfg, memory=None)


def only(cfg, *sections, **kw):
    cfg.briefing = BriefingConfig(sections=list(sections), **kw)


def test_sections_order_and_selection(cfg, monkeypatch):
    monkeypatch.setattr(briefing.shutil, "which", lambda n: None)
    only(cfg, "reminders", "weather")
    out = run(briefing.daily_briefing(ctx(cfg)))
    lines = out.splitlines()
    assert lines[0].startswith("Heute ist ") and lines[1].startswith("Keine Erinnerungen") and "Wetter" in lines[2]
    only(cfg, "weather")
    assert "Erinnerungen" not in run(briefing.daily_briefing(ctx(cfg)))
    assert BriefingConfig(sections=["news", "gibtsnicht", "news"]).sections == ["news"]


def test_reminders_look_ahead_and_overdue(cfg):
    c = ctx(cfg)
    store = reminder_tools.store_for(c)
    now = datetime.now()
    store.add("Paket abholen", now.replace(hour=23, minute=59))
    store.add("Steuer abgeben", (now + timedelta(days=2)).replace(hour=9, minute=0))
    store.add("Weit weg", now + timedelta(days=10))
    store.add("Vergessen", now - timedelta(hours=1))
    only(cfg, "reminders", lookahead_days=2)
    out = run(briefing.daily_briefing(c))
    assert "Erinnerungen & Fristen:" in out and "heute 23:59 Paket abholen" in out
    assert f"{(now + timedelta(days=2)).strftime('%d.%m.')} 09:00 Steuer abgeben" in out
    assert "(überfällig) Vergessen" in out and "Weit weg" not in out
    only(cfg, "reminders", lookahead_days=0)
    out = run(briefing.daily_briefing(c))
    assert "Heutige Erinnerungen" in out and "Steuer abgeben" not in out


def test_paperless_inbox(cfg, monkeypatch):
    fake = FakePaperless()
    fake.docs[0]["tags"].append(12)  # Handyvertrag liegt im Posteingang
    monkeypatch.setattr(paperless, "TRANSPORT", fake.transport())
    only(cfg, "paperless_inbox")
    assert run(briefing.daily_briefing(ctx(cfg))).count("\n") == 0  # nicht eingerichtet → Punkt entfällt
    cfg.paperless.url, cfg.paperless.token = "http://paperless.local", TOKEN
    out = run(briefing.daily_briefing(ctx(cfg)))
    assert "Paperless-Posteingang: 1 Dokument – „Handyvertrag Telekom“ [7]" in out and "paperless_review_next" in out
    only(cfg, "paperless_inbox", inbox_tag="steuer")
    assert "„Stromrechnung 2026“ [8]" in run(briefing.daily_briefing(ctx(cfg)))
    fake.tags[2]["is_inbox_tag"] = False
    only(cfg, "paperless_inbox")
    assert "kein Posteingangs-Tag" in run(briefing.daily_briefing(ctx(cfg)))


def test_news_via_brave_and_error_per_topic(cfg, monkeypatch):
    cfg.tools.brave_api_key = "k"
    seen = []

    def handler(r):
        seen.append(r)
        if r.url.params["q"] == "Kaputt":
            return httpx.Response(429)
        return httpx.Response(200, json={"results": [
            {"title": f"<b>{r.url.params['q']}</b> 6.18 erschienen", "url": "https://x", "meta_url": {"hostname": "heise.de"}}]})

    monkeypatch.setattr(web, "TRANSPORT", httpx.MockTransport(handler))
    monkeypatch.setattr(web, "_ddgs_news", lambda q, n: (_ for _ in ()).throw(AssertionError("kein Scraping")))
    only(cfg, "news", news_topics=["Linux", "Kaputt"], news_count=2)
    out = run(briefing.daily_briefing(ctx(cfg)))
    assert "- Linux: Linux 6.18 erschienen (heise.de)" in out and "- Kaputt: keine Nachrichten abrufbar" in out
    assert str(seen[0].url).startswith(web.BRAVE_NEWS_URL) and seen[0].url.params["freshness"] == "pd"
    cfg.tools.brave_api_key = ""
    monkeypatch.setattr(web, "_ddgs_news", lambda q, n: [{"title": "Ohne Schlüssel", "url": "u", "source": "Tagesschau"}])
    assert "Ohne Schlüssel (Tagesschau)" in run(briefing.daily_briefing(ctx(cfg)))
    only(cfg, "news")  # keine Themen → Punkt entfällt
    assert "Nachrichten" not in run(briefing.daily_briefing(ctx(cfg)))


def test_apt_updates_and_instructions(cfg, monkeypatch):
    async def fake_run(ctx_, argv, timeout, stream=True, cwd=None):
        assert argv == ["apt", "list", "--upgradable"]
        return 0, "Auflistung… Fertig\nlinux-image-generic/noble 6.8 amd64 [aktualisierbar von: 6.7]\nvim/noble 9.1 amd64\n"

    monkeypatch.setattr(briefing.shutil, "which", lambda n: "/usr/bin/apt")
    monkeypatch.setattr(proc, "run", fake_run)
    cfg.tools.package_manager = "apt"
    only(cfg, "updates", instructions="Halte dich kurz.")
    out = run(briefing.daily_briefing(ctx(cfg)))
    assert "Systemupdates: 2 verfügbar (darunter linux-image-generic)." in out
    assert out.endswith("(Wunsch des Nutzers für das Briefing: Halte dich kurz.)")


def test_dashboard_settings_override_config_and_reset(cfg):
    cfg.briefing = BriefingConfig(news_topics=["Linux"])
    s = briefing.save_settings(cfg, {"sections": ["news", "weather"], "news_count": 99})
    assert s.sections == ["news", "weather"] and s.news_count == 14 and s.news_topics == ["Linux"]
    assert briefing.settings(cfg).sections == ["news", "weather"]
    assert briefing.save_settings(cfg, None) == cfg.briefing


def test_briefing_api(cfg, monkeypatch):
    monkeypatch.setenv("ORBWISE_FAKE_LLM", "1")
    monkeypatch.setenv("ORBWISE_SKIP_WARMUP", "1")
    monkeypatch.setattr(briefing.shutil, "which", lambda n: None)
    from orbwise.server import create_app
    with TestClient(create_app(cfg), base_url="http://localhost:8765") as client:
        data = client.get("/api/briefing").json()
        assert not data["customized"] and [s["id"] for s in data["sections"]][:2] == ["weather", "calendar"]
        assert next(s for s in data["sections"] if s["id"] == "news")["note"] == "keine Themen eingetragen"
        r = client.put("/api/briefing", json={"sections": ["reminders"], "instructions": "kurz", "evil": 1})
        assert r.status_code == 200 and r.json()["settings"]["sections"] == ["reminders"]
        assert client.get("/api/briefing").json()["customized"]
        text = client.post("/api/briefing/preview").json()["text"]
        assert "Keine Erinnerungen" in text and "Wetter" not in text and "kurz" in text
        assert client.put("/api/briefing", json=[1]).status_code == 400
        assert client.put("/api/briefing", json={"sections": ["weather"]},
                          headers={"Origin": "https://evil.example"}).status_code == 403
        client.delete("/api/briefing")
        assert not client.get("/api/briefing").json()["customized"]
