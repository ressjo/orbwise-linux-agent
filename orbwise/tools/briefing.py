"""„Guten Morgen“-Briefing: Datum und die gewählten Punkte (Wetter, Termine, Erinnerungen/Fristen,
Paperless-Posteingang, Nachrichten, Updates, Speicher) in der gewählten Reihenfolge.

Einstellungen: Abschnitt `briefing:` in der Config; Änderungen aus dem Dashboard (Reiter BRIEFING) liegen in
state.json und haben Vorrang, bis sie dort zurückgesetzt werden.
"""

from __future__ import annotations

import asyncio
import shutil
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from .. import state
from ..config import BRIEFING_SECTIONS, BriefingConfig
from ..memory.files import german_date
from . import proc
from .calendar_tools import CalendarError, events_between
from .mail import inbox_brief
from .packages import package_manager
from .paperless import inbox_summary
from .registry import ToolContext, tool
from .reminder_tools import store_for
from .weather import WeatherError, weather_report
from .web import news_headlines

LOW_SPACE_GB = 20

LABELS = {
    "weather": ("Wetter", "Weather"),
    "calendar": ("Termine", "Calendar"),
    "reminders": ("Erinnerungen & Fristen", "Reminders & deadlines"),
    "mail": ("E-Mail (ungelesen)", "E-mail (unread)"),
    "paperless_inbox": ("Paperless-Posteingang", "Paperless inbox"),
    "news": ("Nachrichten", "News"),
    "updates": ("Systemupdates", "System updates"),
    "storage": ("Speicher & NAS", "Storage & NAS"),
}


# ---------------------------------------------------------------- Einstellungen (Config + Dashboard)

def _state_path(cfg) -> Path:
    return cfg.memory.dir.parent / "state.json"


def settings(cfg) -> BriefingConfig:
    """Wirksame Einstellungen: Config, überschrieben durch das, was im Dashboard gespeichert wurde."""
    override = state.load(_state_path(cfg)).get("briefing")
    if not isinstance(override, dict):
        return cfg.briefing
    return BriefingConfig.model_validate({**cfg.briefing.model_dump(), **override})


def save_settings(cfg, data: dict[str, Any] | None) -> BriefingConfig:
    """Speichert Dashboard-Einstellungen (None = zurück zur Config)."""
    chosen = None if data is None else \
        BriefingConfig.model_validate({**cfg.briefing.model_dump(), **data}).model_dump()

    def change(saved: dict) -> None:
        if chosen is None:
            saved.pop("briefing", None)
        else:
            saved["briefing"] = chosen
    state.update(_state_path(cfg), change)
    return settings(cfg)


def availability(cfg) -> dict[str, str]:
    """Punkt → '' (verfügbar) oder kurzer Grund, warum er gerade nichts liefern kann."""
    s = settings(cfg)
    en = cfg.language == "en"
    return {
        "weather": "" if cfg.weather.location else "weather.location " + ("missing" if en else "fehlt"),
        "calendar": "" if cfg.calendar.enabled else ("calendar not set up" if en else "Kalender nicht eingerichtet"),
        "reminders": "",
        "mail": "" if cfg.mail.enabled else ("mailbox not set up" if en else "Postfach nicht eingerichtet"),
        "paperless_inbox": "" if cfg.paperless.enabled else ("Paperless not set up" if en else "Paperless nicht eingerichtet"),
        "news": "" if s.news_topics else ("no topics yet" if en else "keine Themen eingetragen"),
        "updates": "",
        "storage": "",
    }


# ---------------------------------------------------------------- Punkte

async def _weather(ctx: ToolContext, s: BriefingConfig) -> str | None:
    if not ctx.cfg.weather.location:
        return "Wetter: kein Standardort eingestellt (weather.location)."
    try:
        return await weather_report(ctx.cfg.weather.location, 1)
    except WeatherError as e:
        return f"Wetter: {e}"


async def _calendar(ctx: ToolContext, s: BriefingConfig) -> str | None:
    if not ctx.cfg.calendar.enabled:
        return None
    days = 1 + s.lookahead_days
    try:
        events = await events_between(ctx.cfg, datetime.now().date(), days)
    except CalendarError as e:
        return f"Kalender: {e}"
    span = "heute" if days == 1 else ("heute und morgen" if days == 2 else f"heute und in den nächsten {days - 1} Tagen")
    if not events:
        return f"Termine {span}: keine."
    return f"Termine {span}:\n" + "\n".join(f"- {e.line()}" for e in events)


async def _reminders(ctx: ToolContext, s: BriefingConfig) -> str | None:
    today = datetime.now().date()
    last = today + timedelta(days=s.lookahead_days)
    items = [r for r in store_for(ctx).upcoming() if r.due_dt.date() <= last]
    if not items:
        return "Keine Erinnerungen" + (" heute." if not s.lookahead_days else f" bis zum {last.strftime('%d.%m.')}")

    def when(r) -> str:
        d = r.due_dt.date()
        day = "heute" if d == today else "morgen" if d == today + timedelta(days=1) else d.strftime("%d.%m.")
        overdue = " (überfällig)" if r.due_dt < datetime.now() else ""
        return f"{day} {r.due_dt.strftime('%H:%M')}{overdue}"

    title = "Heutige Erinnerungen" if not s.lookahead_days else "Erinnerungen & Fristen"
    return f"{title}:\n" + "\n".join(f"- {when(r)} {r.text}" for r in items)


async def _mail(ctx: ToolContext, s: BriefingConfig) -> str | None:
    return await inbox_brief(ctx.cfg)


async def _paperless(ctx: ToolContext, s: BriefingConfig) -> str | None:
    return await inbox_summary(ctx.cfg, s.inbox_tag)


async def _news(ctx: ToolContext, s: BriefingConfig) -> str | None:
    if not s.news_topics or not s.news_count:
        return None
    results = await asyncio.gather(*(news_headlines(ctx.cfg, t, s.news_count) for t in s.news_topics),
                                   return_exceptions=True)
    blocks = []
    for topic, items in zip(s.news_topics, results, strict=True):
        if isinstance(items, BaseException):
            blocks.append(f"- {topic}: keine Nachrichten abrufbar ({items})")
        elif not items:
            blocks.append(f"- {topic}: nichts Neues in den letzten 24 Stunden")
        else:
            blocks.append(f"- {topic}: " + " | ".join(
                x["title"] + (f" ({x['source']})" if x.get("source") else "") for x in items))
    return "Nachrichten (letzte 24 h):\n" + "\n".join(blocks)


async def _updates(ctx: ToolContext, s: BriefingConfig) -> str | None:
    if package_manager(ctx) == "apt":
        if not shutil.which("apt"):
            return None
        rc, out = await proc.run(ctx, ["apt", "list", "--upgradable"], timeout=60, stream=False)
        pkgs = [ln.split("/")[0] for ln in out.splitlines() if "/" in ln and not ln.startswith(("Listing", "Auflistung", "WARNING"))]
    else:
        if not shutil.which("checkupdates"):
            return None
        rc, out = await proc.run(ctx, ["checkupdates"], timeout=45, stream=False)
        if rc not in (0, 2):
            return None
        pkgs = [line.split()[0] for line in out.splitlines() if line.strip()] if rc == 0 else []
    if not pkgs:
        return "Systemupdates: keine."
    important = [p for p in pkgs if p.split("-")[0] in ("linux", "nvidia", "mesa", "ollama", "systemd", "firefox")]
    text = f"Systemupdates: {len(pkgs)} verfügbar"
    if important:
        text += f" (darunter {', '.join(important[:5])})"
    return text + "."


async def _storage(ctx: ToolContext, s: BriefingConfig) -> str | None:
    notes = []
    for mount in [Path("/"), Path.home(), *ctx.cfg.tools.nas_paths]:
        try:
            free = shutil.disk_usage(mount).free / 1024 ** 3
        except OSError:
            if mount in ctx.cfg.tools.nas_paths:
                notes.append(f"NAS {mount} ist nicht erreichbar")
            continue
        if free < LOW_SPACE_GB:
            notes.append(f"wenig Platz auf {mount}: nur noch {free:.0f} GB frei")
    for nas in ctx.cfg.tools.nas_paths:
        if nas.exists() and not any(nas.iterdir()):
            notes.append(f"NAS {nas} scheint nicht gemountet")
    return ("Speicher: " + "; ".join(dict.fromkeys(notes)) + ".") if notes else None


SECTIONS = {"weather": _weather, "calendar": _calendar, "reminders": _reminders, "mail": _mail, "paperless_inbox": _paperless,
            "news": _news, "updates": _updates, "storage": _storage}
assert set(SECTIONS) == set(BRIEFING_SECTIONS)


async def build_briefing(ctx: ToolContext) -> str:
    s = settings(ctx.cfg)
    now = datetime.now()
    parts = [f"Heute ist {german_date(now)}, {now.strftime('%H:%M')} Uhr."]
    results = await asyncio.gather(*(SECTIONS[name](ctx, s) for name in s.sections), return_exceptions=True)
    for name, result in zip(s.sections, results, strict=True):
        if isinstance(result, BaseException):
            parts.append(f"{LABELS[name][0]}: nicht verfügbar ({result})")
        elif result:
            parts.append(result)
    if s.instructions.strip():
        parts.append(f"(Wunsch des Nutzers für das Briefing: {s.instructions.strip()})")
    return "\n".join(parts)


@tool("Tagesüberblick für „Guten Morgen“ / „Was steht heute an?“ (vom Nutzer gewählte Punkte) – kurz und "
      "freundlich zusammenfassen.")
async def daily_briefing(ctx: ToolContext) -> str:
    return await build_briefing(ctx)
