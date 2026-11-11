"""Kalender-Tests gegen einen echten CalDAV-Server (Radicale, lokal gestartet)."""

import socket
import subprocess
import sys
import time
from datetime import date, datetime, timedelta

import pytest
from conftest import run

from orbwise.extras import calendar_tools as cal
from orbwise.tools import briefing
from orbwise.tools.registry import CONFIRM, SAFE, ToolContext, get_tool, load_all_tools

USER, PASSWORD = "alex", "app-pass-1234"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def radicale(tmp_path_factory):
    root = tmp_path_factory.mktemp("radicale")
    users = root / "users"
    users.write_text(f"{USER}:{PASSWORD}\n")
    port = free_port()
    proc = subprocess.Popen([
        sys.executable, "-m", "radicale", f"--storage-filesystem-folder={root / 'data'}",
        f"--server-hosts=127.0.0.1:{port}", "--auth-type=htpasswd", f"--auth-htpasswd-filename={users}",
        "--auth-htpasswd-encryption=plain", "--logging-level=error"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    url = f"http://127.0.0.1:{port}"
    for _ in range(50):
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
            break
        except OSError:
            time.sleep(0.1)
    import caldav
    principal = caldav.DAVClient(url=url, username=USER, password=PASSWORD).principal()
    principal.make_calendar(name="Privat")
    arbeit = principal.make_calendar(name="Arbeit")
    today = date.today()
    arbeit.save_event(dtstart=datetime.combine(today, datetime.min.time()).replace(hour=9).astimezone(),
                      dtend=datetime.combine(today, datetime.min.time()).replace(hour=10).astimezone(),
                      summary="Arbeitsmeeting")
    yield url
    proc.terminate()
    proc.wait(5)


@pytest.fixture
def ccfg(cfg, radicale, monkeypatch):
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
    monkeypatch.setenv("TZ", "Europe/Berlin")
    cal.local_tz.cache_clear()
    cal.invalidate_cache()
    cfg.calendar.url = radicale
    cfg.calendar.username = USER
    cfg.calendar.password = PASSWORD
    cfg.calendar.calendars = ["Privat"]
    cfg.calendar.default_calendar = "Privat"
    yield cfg
    # Kalender "Privat" nach jedem Test leeren
    client = cal.CalendarClient(cfg)
    for c in client.read_calendars():
        for obj in c.events():
            obj.delete()
    cal.local_tz.cache_clear()


def ctx(cfg):
    return ToolContext(cfg=cfg, memory=None)


def day_str(d: date) -> str:
    return d.isoformat()


# ---------------------------------------------------------------- Parsing (ohne Server)

def test_parse_day_and_start(monkeypatch):
    monkeypatch.setenv("TZ", "Europe/Berlin")
    cal.local_tz.cache_clear()
    t = date(2026, 9, 27)  # Sonntag
    assert cal.parse_day("", t) == t and cal.parse_day("morgen", t) == date(2026, 9, 28)
    assert cal.parse_day("Freitag", t) == date(2026, 10, 2)
    assert cal.parse_day("03.10.", t) == date(2026, 10, 3)
    assert cal.parse_day("01.02.", t) == date(2027, 2, 1)
    start, all_day = cal.parse_start("2026-11-15 10:00", t)
    assert not all_day and start.utcoffset() == timedelta(hours=1)          # Winterzeit
    assert cal.parse_start("2026-09-30T10:00", t)[0].utcoffset() == timedelta(hours=2)  # Sommerzeit
    assert cal.parse_start("morgen 14.30 Uhr", t)[0].hour == 14
    assert cal.parse_start("2026-10-05", t) == (date(2026, 10, 5), True)
    with pytest.raises(cal.CalendarError):
        cal.parse_day("irgendwann", t)
    with pytest.raises(cal.CalendarError):
        cal.parse_start("heute 25:00", t)
    cal.local_tz.cache_clear()


def test_tools_gated_and_risks(cfg):
    from orbwise.tools.registry import tool_schemas
    load_all_tools()
    names = lambda c: {s["function"]["name"] for s in tool_schemas(c)}  # noqa: E731
    assert "calendar_add" not in names(cfg)
    cfg.calendar.url, cfg.calendar.username, cfg.calendar.password = "http://x", "u", "p"
    assert {"calendar_events", "calendar_free", "calendar_add", "calendar_update", "calendar_delete"} <= names(cfg)
    c = ctx(cfg)
    assert get_tool("calendar_add").assess(c, {})[0] == SAFE
    assert get_tool("calendar_update").assess(c, {"query": "x"})[0] == CONFIRM
    assert get_tool("calendar_delete").assess(c, {"query": "x"})[0] == CONFIRM


# ---------------------------------------------------------------- Gegen Radicale

def test_add_and_list(ccfg):
    tomorrow = date.today() + timedelta(days=1)
    out = run(cal.calendar_add(ctx(ccfg), "Zahnarzt", f"{day_str(tomorrow)} 10:00", 45, location="Freiburg"))
    assert out.startswith("Eingetragen in 'Privat':") and "10:00–10:45 Zahnarzt (Freiburg)" in out
    listed = run(cal.calendar_events(ctx(ccfg), day_str(tomorrow)))
    assert "10:00–10:45 Zahnarzt (Freiburg)" in listed
    # Nur der Kalender "Privat" wird gelesen
    assert "Arbeitsmeeting" not in run(cal.calendar_events(ctx(ccfg), "heute"))


def test_all_day_multi_day(ccfg):
    start = date.today() + timedelta(days=3)
    out = run(cal.calendar_add(ctx(ccfg), "Urlaub", day_str(start), end=day_str(start + timedelta(days=2))))
    assert "ganztägig bis" in out
    week = run(cal.calendar_events(ctx(ccfg), day_str(start), 7))
    assert "Urlaub" in week and "ganztägig" in week


def test_free_slots(ccfg):
    d = date.today() + timedelta(days=5)
    run(cal.calendar_add(ctx(ccfg), "Termin A", f"{day_str(d)} 10:00", 60))
    run(cal.calendar_add(ctx(ccfg), "Termin B", f"{day_str(d)} 13:30", 90))
    out = run(cal.calendar_free(ctx(ccfg), day_str(d), "09:00", "18:00"))
    assert "frei: 09:00–10:00, 11:00–13:30, 15:00–18:00" in out


def test_update_moves_and_keeps_duration(ccfg):
    d = date.today() + timedelta(days=2)
    run(cal.calendar_add(ctx(ccfg), "Friseur", f"{day_str(d)} 11:00", 30))
    out = run(cal.calendar_update(ctx(ccfg), "friseur", new_start=f"{day_str(d)} 15:00"))
    assert out == f"Geändert: {cal._day(d)} 15:00–15:30 Friseur"
    listed = run(cal.calendar_events(ctx(ccfg), day_str(d)))
    assert "15:00–15:30 Friseur" in listed and "11:00" not in listed


def test_ambiguous_and_delete(ccfg):
    d1, d2 = date.today() + timedelta(days=1), date.today() + timedelta(days=4)
    run(cal.calendar_add(ctx(ccfg), "Training", f"{day_str(d1)} 18:00"))
    run(cal.calendar_add(ctx(ccfg), "Training", f"{day_str(d2)} 18:00"))
    assert "Mehrere Termine passen" in run(cal.calendar_delete(ctx(ccfg), "training"))
    assert run(cal.calendar_delete(ctx(ccfg), "training", day_str(d1))).startswith("Gelöscht:")
    remaining = run(cal.calendar_events(ctx(ccfg), day_str(d1), 7))
    assert remaining.count("Training") == 1
    assert "Kein Termin" in run(cal.calendar_delete(ctx(ccfg), "gibt es nicht"))


def test_recurring_is_not_modified(ccfg):
    client = cal.CalendarClient(ccfg)
    privat = client.target_calendar()
    start = datetime.combine(date.today() + timedelta(days=1), datetime.min.time()).replace(hour=7)
    privat.save_event(
        "BEGIN:VCALENDAR\nVERSION:2.0\nPRODID:-//test//\nBEGIN:VEVENT\nUID:serie-1\n"
        f"DTSTART:{start.strftime('%Y%m%dT%H%M%S')}Z\nDTEND:{(start + timedelta(hours=1)).strftime('%Y%m%dT%H%M%S')}Z\n"
        "SUMMARY:Laufen\nRRULE:FREQ=WEEKLY;COUNT=4\nEND:VEVENT\nEND:VCALENDAR\n")
    out = run(cal.calendar_delete(ctx(ccfg), "laufen", day_str(date.today() + timedelta(days=1))))
    assert "Serientermin" in out
    assert run(cal.calendar_events(ctx(ccfg), "heute", 30)).count("Laufen") == 4


def test_wrong_password(ccfg):
    ccfg.calendar.password = "falsch"
    cal.invalidate_cache()
    assert "Anmeldung am Kalender fehlgeschlagen" in run(cal.calendar_events(ctx(ccfg)))
    ccfg.calendar.password = PASSWORD


def test_unknown_calendar_name(ccfg):
    ccfg.calendar.calendars = ["Familie"]
    out = run(cal.calendar_events(ctx(ccfg)))
    assert "nicht gefunden" in out and "Privat" in out and "Arbeit" in out
    ccfg.calendar.calendars = ["Privat"]


def test_briefing_includes_todays_events(ccfg, monkeypatch):
    now = datetime.now()
    later = min(now + timedelta(minutes=30), now.replace(hour=23, minute=0))
    run(cal.calendar_add(ctx(ccfg), "Paket abholen", later.strftime("%Y-%m-%d %H:%M"), 15))
    monkeypatch.setattr(briefing.shutil, "which", lambda n: None)
    out = run(briefing.daily_briefing(ctx(ccfg)))
    assert "Termine heute und in den nächsten 2 Tagen:" in out and "Paket abholen" in out


def test_status(ccfg):
    assert run(cal.calendar_status(ccfg)) == {"enabled": True, "online": True, "calendars": ["Privat"]}


def test_plain_https_session(ccfg):
    client = cal.CalendarClient(ccfg)
    client.read_calendars()
    _, dav, _ = next(iter(cal._cache.values()))
    assert getattr(dav.session, "_disable_http3", True) is True


def test_retry_after_connection_drop(ccfg, monkeypatch):
    d = date.today() + timedelta(days=6)
    run(cal.calendar_add(ctx(ccfg), "Kino", f"{day_str(d)} 20:00", 120))
    real = cal.CalendarClient.events
    calls = []

    def flaky(self, start, end):
        calls.append(1)
        if len(calls) == 1:
            raise ConnectionError("peer closed: keepalive timeout")
        return real(self, start, end)

    monkeypatch.setattr(cal.CalendarClient, "events", flaky)
    out = run(cal.calendar_events(ctx(ccfg), day_str(d)))
    assert "20:00–22:00 Kino" in out and len(calls) == 2


def test_persistent_error_is_reported(ccfg, monkeypatch):
    def broken(self, start, end):
        raise ConnectionError("kaputt")

    monkeypatch.setattr(cal.CalendarClient, "events", broken)
    out = run(cal.calendar_events(ctx(ccfg)))
    assert "Kalenderfehler (ConnectionError): kaputt" in out and "orbwise.log" in out


def test_broken_event_is_skipped(ccfg, monkeypatch):
    d = date.today() + timedelta(days=8)
    run(cal.calendar_add(ctx(ccfg), "Gut", f"{day_str(d)} 09:00"))
    run(cal.calendar_add(ctx(ccfg), "Kaputt", f"{day_str(d)} 11:00"))
    real = cal._normalize

    def picky(obj, name):
        ev = real(obj, name)
        if ev and ev.title == "Kaputt":
            raise ValueError("seltsames iCloud-Format")
        return ev

    monkeypatch.setattr(cal, "_normalize", picky)
    out = run(cal.calendar_events(ctx(ccfg), day_str(d)))
    assert "Gut" in out and "Kaputt" not in out
    assert "1 Termin(e) konnten nicht gelesen werden" in out
