"""Datumsangaben in natürlicher Sprache auflösen – deterministisch statt im Kopf des Modells.

Kleine lokale Modelle verrechnen sich bei „nächsten Dienstag“, „in 3 Wochen“, Wochentagen zu einem Datum oder
Kalenderwochen. Die Werkzeuge (Erinnerungen, Kalender, Gedächtnis, Routinen) verstehen solche Angaben deshalb
selbst, und date_info beantwortet Datumsfragen samt gesetzlicher Feiertage in Deutschland.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

WEEKDAYS_DE = ["montag", "dienstag", "mittwoch", "donnerstag", "freitag", "samstag", "sonntag"]
WEEKDAYS_EN = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
MONTHS = {
    "januar": 1, "jänner": 1, "january": 1, "jan": 1, "februar": 2, "february": 2, "feb": 2, "märz": 3, "maerz": 3,
    "march": 3, "mär": 3, "mar": 3, "april": 4, "apr": 4, "mai": 5, "may": 5, "juni": 6, "june": 6, "jun": 6,
    "juli": 7, "july": 7, "jul": 7, "august": 8, "aug": 8, "september": 9, "sept": 9, "sep": 9, "oktober": 10,
    "october": 10, "okt": 10, "oct": 10, "november": 11, "nov": 11, "dezember": 12, "december": 12, "dez": 12,
    "dec": 12,
}
NUMBERS = {"ein": 1, "eine": 1, "einen": 1, "einem": 1, "einer": 1, "one": 1, "a": 1, "an": 1, "zwei": 2, "two": 2,
           "drei": 3, "three": 3, "vier": 4, "four": 4, "fünf": 5, "five": 5, "sechs": 6, "six": 6, "sieben": 7,
           "seven": 7, "acht": 8, "eight": 8, "neun": 9, "nine": 9, "zehn": 10, "ten": 10, "zwölf": 12,
           "twelve": 12}
DAYTIME = {"morgens": 8, "früh": 8, "vormittags": 10, "mittags": 12, "nachmittags": 15, "abends": 19, "nachts": 22,
           "morning": 8, "noon": 12, "afternoon": 15, "evening": 19, "night": 22}

_WD = "|".join(WEEKDAYS_DE + WEEKDAYS_EN)
_MON = "|".join(sorted(MONTHS, key=len, reverse=True))
_NUM = r"\d+|" + "|".join(NUMBERS)


@dataclass
class When:
    day: date
    at: time | None = None  # Uhrzeit, falls genannt

    def datetime(self, default: time = time(9, 0)) -> datetime:
        return datetime.combine(self.day, self.at or default)


def _num(word: str) -> int:
    return int(word) if word.isdigit() else NUMBERS[word]


def _weekday(name: str) -> int:
    return (WEEKDAYS_DE + WEEKDAYS_EN).index(name) % 7


def _add_months(d: date, months: int) -> date:
    y, m = divmod(d.month - 1 + months, 12)
    year, month = d.year + y, m + 1
    for day in range(d.day, 27, -1):  # 31. Januar + 1 Monat → 28./29. Februar
        try:
            return date(year, month, day)
        except ValueError:
            continue
    return date(year, month, min(d.day, 28))


def _time(text: str) -> time | None:
    m = re.search(r"\b(\d{1,2})[:.](\d{2})\b(?!\.\d)", text)  # 9:30, 9.30 (nicht 14.03.2026)
    if m and int(m.group(1)) < 24 and int(m.group(2)) < 60 and not re.search(r"\d{1,2}\.\d{1,2}\.", m.group(0) + "."):
        hour, minute = int(m.group(1)), int(m.group(2))
        if re.search(r"\b(pm|abends|nachmittags)\b", text) and hour < 12:
            hour += 12
        return time(hour, minute)
    m = re.search(r"\b(?:um|at|gegen)\s+(\d{1,2})(?:\s*(uhr|am|pm|h))?\b", text) or \
        re.search(r"\b(\d{1,2})\s*(uhr|am|pm|h)\b", text)
    if m and int(m.group(1)) < 24:
        hour = int(m.group(1))
        if (m.group(2) == "pm" or re.search(r"\b(abends|nachmittags|evening|afternoon)\b", text)) and hour < 12:
            hour += 12
        return time(hour, 0)
    for word, hour in DAYTIME.items():
        if re.search(rf"\b{word}\b", text):
            return time(hour, 0)
    return None


def resolve(text: str, today: date | None = None, future: bool = True) -> When | None:
    """„nächsten Dienstag 9:00“, „in 3 Wochen“, „14. März“, „letzten Freitag“, „KW 12“, „2026-03-14“ … → When.
    future: Angaben ohne Richtung (bloßer Wochentag, Datum ohne Jahr) zeigen nach vorn – für Rückblicke
    (Gedächtnis) future=False. None, wenn kein Datum erkennbar ist (eine reine Uhrzeit gilt für heute)."""
    today = today or date.today()
    t = " ".join(text.lower().replace(",", " ").split())
    at = _time(t)
    day: date | None = None

    if m := re.search(r"\b(\d{4})-(\d{1,2})-(\d{1,2})\b", t):
        day = date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    elif m := re.search(r"\b(\d{1,2})\.(\d{1,2})\.(\d{2,4})?(?!\d)", t):
        year = int(m.group(3)) if m.group(3) else None
        if year is not None and year < 100:
            year += 2000
        day = _fixed(today, int(m.group(2)), int(m.group(1)), year, future)
    elif (m := re.search(rf"\b(\d{{1,2}})\.?\s*({_MON})\b\.?\s*(\d{{4}})?", t)) or \
            (m2 := re.search(rf"\b({_MON})\b\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?\b\s*(\d{{4}})?", t)):
        if m:
            d_, mon, year = int(m.group(1)), MONTHS[m.group(2)], m.group(3)
        else:
            mon, d_, year = MONTHS[m2.group(1)], int(m2.group(2)), m2.group(3)
        day = _fixed(today, mon, d_, int(year) if year else None, future)
    elif re.search(r"\bübermorgen\b|\bday after tomorrow\b", t):
        day = today + timedelta(days=2)
    elif re.search(r"\bvorgestern\b|\bday before yesterday\b", t):
        day = today - timedelta(days=2)
    elif re.search(r"\bmorgen\b(?!s)|\btomorrow\b", t):
        day = today + timedelta(days=1)
    elif re.search(r"\bgestern\b|\byesterday\b", t):
        day = today - timedelta(days=1)
    elif m := re.search(rf"\b(in|vor)\s+({_NUM})\s+(tag|tagen|woche|wochen|monat|monaten|jahr|jahren)\b", t) or \
            re.search(rf"\b(in)\s+({_NUM})\s+(days?|weeks?|months?|years?)\b", t):
        n = _num(m.group(2)) * (-1 if m.group(1) == "vor" else 1)
        day = _shift(today, n, m.group(3))
    elif m := re.search(rf"\b({_NUM})\s+(days?|weeks?|months?|years?)\s+ago\b", t):
        day = _shift(today, -_num(m.group(1)), m.group(2))
    elif m := re.search(rf"\b(?:(nächste[nrs]?|kommende[nrs]?|next|letzte[nrs]?|vergangene[nrs]?|last|"
                        rf"diese[nrs]?|this|am|on)\s+)?({_WD})\b", t):
        mod, wd = (m.group(1) or ""), _weekday(m.group(2))
        delta = (wd - today.weekday()) % 7
        if mod.startswith(("letzt", "vergangen", "last")):
            day = today - timedelta(days=(today.weekday() - wd) % 7 or 7)
        elif mod.startswith(("dies", "this")):
            day = today - timedelta(days=today.weekday()) + timedelta(days=wd)
        elif mod.startswith(("nächst", "kommend", "next")) or future:
            day = today + timedelta(days=delta or 7)
        else:
            day = today - timedelta(days=(today.weekday() - wd) % 7 or 7)
    elif m := re.search(r"\b(?:kw|kalenderwoche|week)\s*(\d{1,2})\b", t):
        week, year = int(m.group(1)), today.year
        if future and date.fromisocalendar(year, week, 7) < today:
            year += 1
        day = date.fromisocalendar(year, week, 1)
    elif re.search(r"\b(ende des monats|monatsende|end of (the )?month)\b", t):
        day = _add_months(today.replace(day=1), 1) - timedelta(days=1)
    elif re.search(r"\b(nächste woche|next week)\b", t):
        day = today - timedelta(days=today.weekday()) + timedelta(days=7)
    elif re.search(r"\b(heute|today|tonight|heute abend)\b", t) or at is not None:
        day = today
    if day is None:
        return None
    return When(day, at)


def _fixed(today: date, month: int, day_: int, year: int | None, future: bool) -> date:
    if year is not None:
        return date(year, month, day_)
    d = date(today.year, month, day_)
    if future and d < today:
        return date(today.year + 1, month, day_)
    if not future and d > today:
        return date(today.year - 1, month, day_)
    return d


def _shift(today: date, n: int, unit: str) -> date:
    if unit.startswith(("tag", "day")):
        return today + timedelta(days=n)
    if unit.startswith(("woche", "week")):
        return today + timedelta(weeks=n)
    if unit.startswith(("monat", "month")):
        return _add_months(today, n)
    return _add_months(today, 12 * n)


# ---------------------------------------------------------------- Feiertage (Deutschland)

def easter(year: int) -> date:
    """Ostersonntag (Gauß/Anonymous Gregorian algorithm)."""
    a, b, c = year % 19, year // 100, year % 100
    d, e = b // 4, b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = c // 4, c % 4
    l_ = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l_) // 451
    month = (h + l_ - 7 * m + 114) // 31
    return date(year, month, (h + l_ - 7 * m + 114) % 31 + 1)


def holidays(year: int, region: str = "") -> dict[date, str]:
    """Gesetzliche Feiertage: bundesweit, mit Bundesland (z. B. „BW“) auch die regionalen."""
    e = easter(year)
    out = {date(year, 1, 1): "Neujahr", e - timedelta(days=2): "Karfreitag", e + timedelta(days=1): "Ostermontag",
           date(year, 5, 1): "Tag der Arbeit", e + timedelta(days=39): "Christi Himmelfahrt",
           e + timedelta(days=50): "Pfingstmontag", date(year, 10, 3): "Tag der Deutschen Einheit",
           date(year, 12, 25): "1. Weihnachtstag", date(year, 12, 26): "2. Weihnachtstag"}
    r = region.strip().upper()
    if r in ("BW", "BY", "ST"):
        out[date(year, 1, 6)] = "Heilige Drei Könige"
    if r in ("BE", "MV"):
        out[date(year, 3, 8)] = "Internationaler Frauentag"
    if r in ("BW", "BY", "HE", "NW", "RP", "SL"):
        out[e + timedelta(days=60)] = "Fronleichnam"
    if r == "SL":
        out[date(year, 8, 15)] = "Mariä Himmelfahrt"
    if r == "TH":
        out[date(year, 9, 20)] = "Weltkindertag"
    if r in ("BB", "MV", "SN", "ST", "TH", "HB", "HH", "NI", "SH"):
        out[date(year, 10, 31)] = "Reformationstag"
    if r in ("BW", "BY", "NW", "RP", "SL"):
        out[date(year, 11, 1)] = "Allerheiligen"
    if r == "SN":  # Buß- und Bettag: Mittwoch vor dem 23. November
        nov23 = date(year, 11, 23)
        out[nov23 - timedelta(days=(nov23.weekday() - 2) % 7 or 7)] = "Buß- und Bettag"
    return dict(sorted(out.items()))
