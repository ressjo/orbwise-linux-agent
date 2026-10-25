"""Kalender über CalDAV (iCloud, Nextcloud, Radicale, …): Termine abfragen, freie Zeiten finden, anlegen,
verschieben und löschen.

iCloud: url https://caldav.icloud.com, Benutzer = Apple-ID, Passwort = app-spezifisches Passwort.
"""

from __future__ import annotations

import asyncio
import functools
import logging
import os
import re
import threading
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Annotated, Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ..lang import T
from ..memory.files import WEEKDAYS
from .registry import CONFIRM, ToolContext, tool

log = logging.getLogger(__name__)

# Kurz halten: iCloud schließt untätige Verbindungen nach einiger Zeit
CACHE_SECONDS = 120
SEARCH_BACK_DAYS = 1
SEARCH_AHEAD_DAYS = 90
_cache: dict[tuple, tuple[float, Any, list]] = {}
_cache_lock = threading.Lock()


class CalendarError(RuntimeError):
    pass


def _enabled(cfg: Any) -> bool:
    return cfg.calendar.enabled


@functools.lru_cache(maxsize=1)
def local_tz():
    """Echte Zeitzone des Systems (inkl. Sommer-/Winterzeit), nicht nur der aktuelle Versatz."""
    try:
        if os.environ.get("TZ"):
            return ZoneInfo(os.environ["TZ"].lstrip(":"))
        target = os.path.realpath("/etc/localtime")
        if "zoneinfo/" in target:
            return ZoneInfo(target.split("zoneinfo/", 1)[1])
        with open("/etc/localtime", "rb") as f:
            return ZoneInfo.from_file(f)
    except (OSError, ValueError, ZoneInfoNotFoundError):
        return datetime.now().astimezone().tzinfo


def _utc(value):
    """Für das Speichern: Zeitpunkte in UTC (vermeidet Probleme mit Zeitzonen-Definitionen), Datum bleibt Datum."""
    return value.astimezone(timezone.utc) if isinstance(value, datetime) else value


# ---------------------------------------------------------------- Datums-Hilfen

def parse_day(text: str, today: date | None = None) -> date:
    today = today or date.today()
    t = text.strip().lower().rstrip(".")
    if t in ("", "heute", "today"):
        return today
    if t in ("morgen", "tomorrow"):
        return today + timedelta(days=1)
    if t == "übermorgen":
        return today + timedelta(days=2)
    weekdays = [w.lower() for w in WEEKDAYS]
    if t in weekdays:  # nächster solcher Wochentag (heute zählt mit)
        return today + timedelta(days=(weekdays.index(t) - today.weekday()) % 7)
    for fmt in ("%Y-%m-%d", "%d.%m.%Y"):
        try:
            return datetime.strptime(t, fmt).date()
        except ValueError:
            pass
    m = re.fullmatch(r"(\d{1,2})\.(\d{1,2})", t)
    if m:
        d = date(today.year, int(m.group(2)), int(m.group(1)))
        return d if d >= today else d.replace(year=today.year + 1)
    from ..dates import resolve  # „nächsten Dienstag“, „in 2 Wochen“, „14. März“ …
    when = resolve(text, today)
    if when is not None:
        return when.day
    raise CalendarError(f"Datum '{text}' nicht verstanden (z. B. 2026-10-03, 03.10.2026, heute, morgen, "
                        "nächsten Dienstag).")


def parse_start(text: str, today: date | None = None) -> tuple[datetime | date, bool]:
    """Beginn eines Termins: 'YYYY-MM-DD HH:MM', 'morgen 10:00', 'Freitag 14.30 Uhr', '03.10.2026 18:00' →
    datetime (lokal); nur ein Datum → ganztägig."""
    t = re.sub(r"(\d{4}-\d{2}-\d{2})T", r"\1 ", text.strip())
    m = (re.match(r"^(.*?)\s*(\d{1,2}):(\d{2})(?::\d{2})?\s*(?:uhr)?$", t, re.I)
         or re.match(r"^(.+?)\s+(\d{1,2})\.(\d{2})\s*(?:uhr)?$", t, re.I))
    if m:
        hour, minute = int(m.group(2)), int(m.group(3))
        if hour > 23 or minute > 59:
            raise CalendarError(f"Uhrzeit in '{text}' ungültig.")
        day = parse_day(m.group(1), today)
        return datetime.combine(day, datetime.min.time(), tzinfo=local_tz()).replace(hour=hour, minute=minute), False
    from ..dates import resolve
    when = resolve(t, today or date.today())
    if when is not None and when.at is not None:  # „nächsten Dienstag um 9“
        return datetime.combine(when.day, when.at, tzinfo=local_tz()), False
    return parse_day(t, today), True


# ---------------------------------------------------------------- Termine

@dataclass
class Event:
    uid: str
    title: str
    start: datetime | date
    end: datetime | date
    all_day: bool
    location: str
    calendar: str
    recurring: bool
    obj: Any = None

    def start_dt(self) -> datetime:
        if self.all_day:
            return datetime.combine(self.start, datetime.min.time(), tzinfo=local_tz())
        return self.start

    def end_dt(self) -> datetime:
        if self.all_day:
            return datetime.combine(self.end, datetime.min.time(), tzinfo=local_tz())
        return self.end

    def line(self) -> str:
        loc = f" ({self.location})" if self.location else ""
        if self.all_day:
            last = self.end - timedelta(days=1)
            span = f" bis {_day(last)}" if last > self.start else ""
            return f"{_day(self.start)} ganztägig{span}: {self.title}{loc}"
        return (f"{_day(self.start.date())} {self.start.strftime('%H:%M')}–{self.end.strftime('%H:%M')} "
                f"{self.title}{loc}")


def _day(d: date) -> str:
    return f"{WEEKDAYS[d.weekday()][:2]} {d.strftime('%d.%m.')}"


def _to_local(value: Any, default: datetime | date | None = None):
    if value is None:
        return default
    v = value.dt if hasattr(value, "dt") else value
    if isinstance(v, datetime):
        return (v.replace(tzinfo=local_tz()) if v.tzinfo is None else v).astimezone(local_tz())
    return v


def _normalize(obj, cal_name: str) -> Event | None:
    comp = obj.icalendar_component
    if comp is None or comp.name != "VEVENT":
        return None
    start = _to_local(comp.get("dtstart"))
    all_day = not isinstance(start, datetime)
    end = _to_local(comp.get("dtend"))
    if end is None:
        dur = comp.get("duration")
        end = start + (dur.dt if dur else (timedelta(days=1) if all_day else timedelta(hours=1)))
    return Event(uid=str(comp.get("uid", "")), title=str(comp.get("summary", "(ohne Titel)")), start=start, end=end,
                 all_day=all_day, location=str(comp.get("location", "") or ""), calendar=cal_name,
                 recurring=bool(comp.get("rrule") or comp.get("recurrence-id")), obj=obj)


def _cal_name(cal) -> str:
    try:
        return cal.get_display_name() or ""
    except Exception:  # noqa: BLE001
        return getattr(cal, "name", "") or ""


def _use_plain_https(client) -> None:
    """HTTP/3 (QUIC) abschalten: iCloud beendet untätige QUIC-Verbindungen („keepalive timeout“), was bei den
    Folgeabrufen einzelner Termine zu Fehlern führt. Klassisches HTTPS ist hier robuster."""
    try:
        import niquests
        old = client.session
        client.session = niquests.Session(disable_http3=True, multiplexed=False)
        old.close()
    except (ImportError, TypeError, AttributeError):
        pass


class CalendarClient:
    """Synchroner Zugriff (caldav ist synchron) – Aufrufe laufen über asyncio.to_thread."""

    def __init__(self, cfg):
        self.cfg = cfg.calendar
        self.skipped = 0  # Termine, die sich nicht lesen ließen

    def _calendars(self) -> list:
        key = (self.cfg.url, self.cfg.username)
        with _cache_lock:
            hit = _cache.get(key)
            if hit and time.time() - hit[0] < CACHE_SECONDS:
                return hit[2]
        import caldav
        from caldav.lib import error as dav_error
        try:
            client = caldav.DAVClient(url=self.cfg.url, username=self.cfg.username,
                                      password=self.cfg.api_password, timeout=self.cfg.timeout)
            _use_plain_https(client)
            calendars = client.principal().calendars()
        except dav_error.AuthorizationError as e:
            raise CalendarError("Anmeldung am Kalender fehlgeschlagen – Apple-ID und app-spezifisches Passwort "
                                "prüfen.") from e
        except Exception as e:  # noqa: BLE001 – Netzwerk, DNS, TLS …
            raise CalendarError(f"Kalender nicht erreichbar ({e.__class__.__name__}).") from e
        with _cache_lock:
            _cache[key] = (time.time(), client, calendars)
        return calendars

    def read_calendars(self) -> list:
        cals = self._calendars()
        wanted = [c.lower() for c in self.cfg.calendars]
        if not wanted:
            return cals
        chosen = [c for c in cals if _cal_name(c).lower() in wanted]
        if not chosen:
            names = ", ".join(_cal_name(c) for c in cals)
            raise CalendarError(f"Kalender {', '.join(self.cfg.calendars)} nicht gefunden. Vorhanden: {names}")
        return chosen

    def target_calendar(self, name: str = ""):
        wanted = (name or self.cfg.default_calendar).lower()
        cals = self._calendars()
        if wanted:
            for c in cals:
                if _cal_name(c).lower() == wanted:
                    return c
            raise CalendarError(f"Kalender '{name or self.cfg.default_calendar}' nicht gefunden. "
                                f"Vorhanden: {', '.join(_cal_name(c) for c in cals)}")
        return self.read_calendars()[0]

    def events(self, start: datetime, end: datetime) -> list[Event]:
        out: list[Event] = []
        for cal in self.read_calendars():
            name = _cal_name(cal)
            for obj in cal.search(start=start, end=end, event=True, expand=True):
                try:
                    ev = _normalize(obj, name)
                except Exception:  # noqa: BLE001 – ein kaputter Termin soll nicht die ganze Liste verhindern
                    log.warning("Termin konnte nicht gelesen werden: %s", getattr(obj, "url", "?"), exc_info=True)
                    self.skipped += 1
                    continue
                if ev and ev.end_dt() > start and ev.start_dt() < end:
                    out.append(ev)
        return sorted(out, key=lambda e: (e.start_dt(), e.title))

    def find(self, query: str, day: date | None = None) -> list[Event]:
        if day:
            start = datetime.combine(day, datetime.min.time(), tzinfo=local_tz())
            end = start + timedelta(days=1)
        else:
            start = datetime.now(local_tz()) - timedelta(days=SEARCH_BACK_DAYS)
            end = start + timedelta(days=SEARCH_AHEAD_DAYS)
        q = query.strip().lower()
        return [e for e in self.events(start, end) if q in e.title.lower()]

    def add(self, title: str, start, end, all_day: bool, location: str, notes: str, calendar: str) -> Event:
        cal = self.target_calendar(calendar)
        props: dict[str, Any] = {"dtstart": _utc(start), "dtend": _utc(end), "summary": title}
        if location:
            props["location"] = location
        if notes:
            props["description"] = notes
        obj = cal.save_event(**props)
        return Event(uid=str(obj.icalendar_component.get("uid", "")), title=title, start=start, end=end,
                     all_day=all_day, location=location, calendar=_cal_name(cal), recurring=False, obj=obj)

    def _master(self, ev: Event):
        if ev.recurring:
            raise CalendarError(f"'{ev.title}' ist ein Serientermin – den bitte direkt im Kalender auf dem "
                                "iPhone ändern, damit die Serie intakt bleibt.")
        for cal in self._calendars():
            if _cal_name(cal) == ev.calendar:
                return cal.event_by_uid(ev.uid)
        raise CalendarError("Kalender des Termins nicht gefunden.")

    def update(self, ev: Event, new_start=None, new_end=None, title: str = "", location: str | None = None) -> None:
        obj = self._master(ev)
        comp = obj.icalendar_component
        if new_start is not None:
            comp.pop("dtstart", None)
            comp.pop("dtend", None)
            comp.pop("duration", None)
            comp.add("dtstart", _utc(new_start))
            comp.add("dtend", _utc(new_end))
        if title:
            comp.pop("summary", None)
            comp.add("summary", title)
        if location is not None:
            comp.pop("location", None)
            if location:
                comp.add("location", location)
        obj.save()

    def delete(self, ev: Event) -> None:
        self._master(ev).delete()


def invalidate_cache() -> None:
    with _cache_lock:
        _cache.clear()


async def _call(fn, *args, **kwargs):
    """Führt einen Kalenderzugriff aus; bei Verbindungsproblemen einmal mit frischer Verbindung wiederholen."""
    for attempt in (1, 2):
        try:
            return await asyncio.to_thread(fn, *args, **kwargs)
        except CalendarError:
            raise
        except Exception as e:  # noqa: BLE001
            invalidate_cache()
            if attempt == 1:
                log.warning("Kalenderzugriff fehlgeschlagen (%s: %s) – neuer Versuch", type(e).__name__, e)
                continue
            log.exception("Kalenderzugriff endgültig fehlgeschlagen")
            raise CalendarError(f"Kalenderfehler ({type(e).__name__}): {e or 'ohne Details'} – "
                                "Details im Log ~/.local/state/orbwise.log") from e


def _pick(matches: list[Event], query: str) -> Event:
    if not matches:
        raise CalendarError(f"Kein Termin mit '{query}' gefunden.")
    if len(matches) > 1:
        raise CalendarError("Mehrere Termine passen – bitte genauer (Datum oder Titel):\n"
                            + "\n".join(e.line() for e in matches[:8]))
    return matches[0]


async def events_between(cfg, start_day: date, days: int) -> list[Event]:
    start = datetime.combine(start_day, datetime.min.time(), tzinfo=local_tz())
    return await _call(CalendarClient(cfg).events, start, start + timedelta(days=max(1, days)))


# ---------------------------------------------------------------- Tools

@tool("Zeigt Termine für einen Tag oder Zeitraum.", enabled=_enabled)
async def calendar_events(
    ctx: ToolContext,
    start: Annotated[str, "Erster Tag: 'heute', 'morgen', Wochentag oder YYYY-MM-DD"] = "",
    days: Annotated[int, "Anzahl Tage (Standard 1)"] = 1,
) -> str:
    client = CalendarClient(ctx.cfg)
    try:
        day = parse_day(start)
        begin = datetime.combine(day, datetime.min.time(), tzinfo=local_tz())
        events = await _call(client.events, begin, begin + timedelta(days=max(1, min(days, 62))))
    except CalendarError as e:
        return str(e)
    span = _day(day) if days <= 1 else f"{_day(day)} bis {_day(day + timedelta(days=days - 1))}"
    note = (f"\n(Hinweis: {client.skipped} Termin(e) konnten nicht gelesen werden – Details im Log.)"
            if client.skipped else "")
    if not events:
        return f"Keine lesbaren Termine ({span})." + note if client.skipped else f"Keine Termine ({span})."
    return f"Termine {span}:\n" + "\n".join(e.line() for e in events) + note


@tool("Findet freie Zeitfenster an einem Tag („Hab ich Freitag Nachmittag Zeit?“).", enabled=_enabled)
async def calendar_free(
    ctx: ToolContext,
    day: Annotated[str, "'heute', 'morgen', Wochentag oder YYYY-MM-DD"] = "",
    from_time: Annotated[str, "HH:MM (Standard 08:00)"] = "08:00",
    to_time: Annotated[str, "HH:MM (Standard 20:00)"] = "20:00",
    min_minutes: Annotated[int, "Mindestlänge (Standard 30)"] = 30,
) -> str:
    try:
        d = parse_day(day)
        window = []
        for t in (from_time, to_time):
            m = re.fullmatch(r"(\d{1,2})[:.](\d{2})", t.strip())
            if not m:
                raise CalendarError(f"Uhrzeit '{t}' nicht verstanden (HH:MM).")
            window.append(datetime.combine(d, datetime.min.time(), tzinfo=local_tz())
                          .replace(hour=int(m.group(1)), minute=int(m.group(2))))
        events = await events_between(ctx.cfg, d, 1)
    except CalendarError as e:
        return str(e)
    lo, hi = window
    busy = sorted((max(e.start_dt(), lo), min(e.end_dt(), hi)) for e in events if e.end_dt() > lo and e.start_dt() < hi)
    free, cursor = [], lo
    for s, e in busy:
        if s > cursor:
            free.append((cursor, s))
        cursor = max(cursor, e)
    if hi > cursor:
        free.append((cursor, hi))
    free = [(s, e) for s, e in free if (e - s) >= timedelta(minutes=max(1, min_minutes))]
    head = f"{_day(d)} zwischen {lo.strftime('%H:%M')} und {hi.strftime('%H:%M')}"
    if not free:
        return f"{head}: keine freie Zeit." + ("\nBelegt: " + "; ".join(e.line() for e in events) if events else "")
    text = f"{head} frei: " + ", ".join(f"{s.strftime('%H:%M')}–{e.strftime('%H:%M')}" for s, e in free)
    if events:
        text += "\nTermine: " + "; ".join(e.line() for e in events)
    return text


@tool("Legt einen Termin an. Mit Uhrzeit = Termin, nur Datum = ganztägig.", enabled=_enabled)
async def calendar_add(
    ctx: ToolContext,
    title: str,
    start: Annotated[str, "'YYYY-MM-DD HH:MM' oder wie gesagt ('nächsten Dienstag 14:00')"],
    duration_minutes: Annotated[int, "Standard 60"] = 60,
    end: Annotated[str, "Ende statt Dauer; ganztägig: letzter Tag"] = "",
    location: str = "",
    notes: str = "",
    calendar: Annotated[str, "Kalendername (Standard: Standardkalender)"] = "",
) -> str:
    try:
        begin, all_day = parse_start(start)
        if all_day:
            last = parse_day(end) if end.strip() else begin
            finish = last + timedelta(days=1)
        elif end.strip():
            finish, end_all_day = parse_start(end)
            if end_all_day:
                raise CalendarError("Für das Ende bitte Datum und Uhrzeit angeben.")
        else:
            finish = begin + timedelta(minutes=max(5, duration_minutes or 60))
        if finish <= begin:
            raise CalendarError("Das Ende liegt vor dem Beginn.")
        ev = await _call(CalendarClient(ctx.cfg).add, title.strip() or "Termin", begin, finish, all_day,
                         location.strip(), notes.strip(), calendar)
    except CalendarError as e:
        return str(e)
    return f"Eingetragen in '{ev.calendar}': {ev.line()}"


def _change_risk(ctx: ToolContext, args: dict) -> tuple[str, str]:
    return CONFIRM, T(f"ändert den Kalendertermin '{args.get('query', '')}'", f"changes the event '{args.get('query', '')}'")


def _delete_risk(ctx: ToolContext, args: dict) -> tuple[str, str]:
    return CONFIRM, T(f"löscht den Kalendertermin '{args.get('query', '')}'", f"deletes the event '{args.get('query', '')}'")


@tool("Verschiebt oder ändert einen Termin (Zeit, Titel, Ort).", risk=_change_risk,
      enabled=_enabled)
async def calendar_update(
    ctx: ToolContext,
    query: Annotated[str, "Teil des Titels"],
    day: Annotated[str, "Aktueller Tag (bei mehreren Treffern)"] = "",
    new_start: Annotated[str, "'YYYY-MM-DD HH:MM' (Dauer bleibt)"] = "",
    duration_minutes: int = 0,
    new_title: str = "",
    new_location: str = "",
) -> str:
    client = CalendarClient(ctx.cfg)
    try:
        ev = _pick(await _call(client.find, query, parse_day(day) if day.strip() else None), query)
        start = end = None
        if new_start.strip() or duration_minutes:
            if new_start.strip():
                start, all_day = parse_start(new_start)
            else:
                start, all_day = ev.start, ev.all_day
            if all_day:
                span = (ev.end - ev.start) if ev.all_day else timedelta(days=1)
                end = start + span
            else:
                old = ev.end_dt() - ev.start_dt()
                end = start + (timedelta(minutes=duration_minutes) if duration_minutes else
                               (old if not ev.all_day else timedelta(hours=1)))
        if start is None and not new_title and not new_location:
            return "Nichts zu ändern angegeben."
        await _call(client.update, ev, start, end, new_title.strip(), new_location.strip() or None)
    except CalendarError as e:
        return str(e)
    changed = Event(ev.uid, new_title.strip() or ev.title, start or ev.start, end or ev.end,
                    ev.all_day if start is None else not isinstance(start, datetime),
                    new_location.strip() or ev.location, ev.calendar, False)
    return f"Geändert: {changed.line()}"


@tool("Löscht einen Termin aus dem Kalender.", risk=_delete_risk, enabled=_enabled)
async def calendar_delete(
    ctx: ToolContext,
    query: Annotated[str, "Teil des Titels"],
    day: Annotated[str, "Tag (bei mehreren Treffern)"] = "",
) -> str:
    client = CalendarClient(ctx.cfg)
    try:
        ev = _pick(await _call(client.find, query, parse_day(day) if day.strip() else None), query)
        await _call(client.delete, ev)
    except CalendarError as e:
        return str(e)
    return f"Gelöscht: {ev.line()}"


async def calendar_status(cfg) -> dict:
    if not cfg.calendar.enabled:
        return {"enabled": False, "online": False}
    try:
        client = CalendarClient(cfg)
        cals = await _call(client.read_calendars)
        return {"enabled": True, "online": True, "calendars": [_cal_name(c) for c in cals]}
    except CalendarError as e:
        return {"enabled": True, "online": False, "error": str(e)}
