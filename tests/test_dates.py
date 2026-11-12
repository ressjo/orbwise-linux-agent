"""Datumsangaben in natürlicher Sprache: deterministisch statt im Kopf des Modells."""

from datetime import date, datetime, time

import pytest
from conftest import run

from orbwise.dates import easter, holidays, resolve
from orbwise.extras.calendar_tools import parse_start
from orbwise.reminders import parse_when
from orbwise.tools.registry import ToolContext, get_tool, load_all_tools

TODAY = date(2026, 10, 3)  # ein Samstag


@pytest.mark.parametrize("text,day,at", [
    ("nächsten Dienstag 9:00", date(2026, 10, 6), time(9, 0)),
    ("letzten Freitag", date(2026, 10, 2), None),
    ("in 3 Wochen", date(2026, 10, 24), None),
    ("in einer Woche", date(2026, 10, 10), None),
    ("vor 2 Wochen", date(2026, 9, 19), None),
    ("14. März", date(2027, 3, 14), None),
    ("14.3.", date(2027, 3, 14), None),
    ("14.03.2026 18:30", date(2026, 3, 14), time(18, 30)),
    ("2026-12-24", date(2026, 12, 24), None),
    ("übermorgen um 9", date(2026, 10, 5), time(9, 0)),
    ("morgen abends", date(2026, 10, 4), time(19, 0)),
    ("am Freitag um 14 Uhr", date(2026, 10, 9), time(14, 0)),
    ("Ende des Monats", date(2026, 10, 31), None),
    ("KW 44", date(2026, 10, 26), None),
    ("next tuesday at 3 pm", date(2026, 10, 6), time(15, 0)),
    ("March 14", date(2027, 3, 14), None),
    ("2 weeks ago", date(2026, 9, 19), None),
])
def test_resolve(text, day, at):
    when = resolve(text, TODAY)
    assert (when.day, when.at) == (day, at)


def test_looking_back_and_nothing_to_find():
    assert resolve("Dienstag", TODAY, future=False).day == date(2026, 9, 29)  # Rückblick: der letzte Dienstag
    assert resolve("14. März", TODAY, future=False).day == date(2026, 3, 14)
    assert resolve("Dienstag", TODAY).day == date(2026, 10, 6)
    assert resolve("hallo wie geht's", TODAY) is None


def test_holidays():
    assert easter(2026) == date(2026, 4, 5) and easter(2027) == date(2027, 3, 28)
    nationwide = holidays(2026)
    assert nationwide[date(2026, 5, 25)] == "Pfingstmontag" and nationwide[date(2026, 10, 3)] == "Tag der Deutschen Einheit"
    assert date(2026, 6, 4) not in nationwide and holidays(2026, "BW")[date(2026, 6, 4)] == "Fronleichnam"
    assert holidays(2026, "SN")[date(2026, 11, 18)] == "Buß- und Bettag"


def test_tools_understand_spoken_dates(cfg, memory, monkeypatch):
    now = datetime(2026, 10, 3, 14, 0)
    assert parse_when(at="nächsten Dienstag 9:00", now=now) == datetime(2026, 10, 6, 9, 0)
    assert parse_when(at="in 2 Wochen", now=now) == datetime(2026, 10, 17, 9, 0)  # ohne Uhrzeit: 9:00
    with pytest.raises(ValueError):
        parse_when(at="heute", now=now)  # heute ohne Uhrzeit – nachfragen
    start, all_day = parse_start("nächsten Dienstag um 14 Uhr", TODAY)
    assert (start.date(), start.hour, all_day) == (date(2026, 10, 6), 14, False)
    assert parse_start("in 3 Wochen", TODAY) == (date(2026, 10, 24), True)

    load_all_tools()
    out = run(get_tool("recall").func(ToolContext(cfg=cfg, memory=memory), date="gestern"))
    assert "Einträge" in out or "Protokoll" in out  # „gestern“ wurde zu einem Tag aufgelöst, kein Formatfehler


def test_date_info(cfg):
    load_all_tools()
    info = get_tool("date_info").func
    ctx = ToolContext(cfg=cfg, memory=None)
    today = date.today()
    out = run(info(ctx, "Wie viele Tage vom 1.1.2026 bis 1.3.2026?"))
    assert "59 Tage" in out and "8 Wochen und 3 Tage" in out
    out = run(info(ctx, "2026-10-03"))
    assert "Samstag" in out and "KW 40" in out and "Tag der Deutschen Einheit" in out
    cfg.holiday_region = "BW"
    out = run(info(ctx, f"Feiertage {today.year + 1}"))
    assert "Fronleichnam" in out and "(BW)" in out
    assert "Keine Datumsangabe" in run(info(ctx, "Hallo"))
