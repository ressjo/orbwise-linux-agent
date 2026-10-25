"""Wetter über Open-Meteo (kostenlos, ohne API-Key): aktuelles Wetter und Tagesvorhersage."""

from __future__ import annotations

from datetime import date, datetime
from typing import Annotated, Any

import httpx

from ..memory.files import WEEKDAYS
from .registry import ToolContext, tool

GEO_URL = "https://geocoding-api.open-meteo.com/v1/search"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"

# Für Tests austauschbar (httpx.MockTransport)
TRANSPORT: httpx.AsyncBaseTransport | None = None
_geo_cache: dict[str, dict] = {}

WMO = {
    0: "klar", 1: "überwiegend klar", 2: "teils bewölkt", 3: "bedeckt",
    45: "Nebel", 48: "Nebel mit Reif",
    51: "leichter Nieselregen", 53: "Nieselregen", 55: "starker Nieselregen",
    56: "gefrierender Nieselregen", 57: "starker gefrierender Nieselregen",
    61: "leichter Regen", 63: "Regen", 65: "starker Regen",
    66: "gefrierender Regen", 67: "starker gefrierender Regen",
    71: "leichter Schneefall", 73: "Schneefall", 75: "starker Schneefall", 77: "Schneegriesel",
    80: "leichte Regenschauer", 81: "Regenschauer", 82: "heftige Regenschauer",
    85: "leichte Schneeschauer", 86: "Schneeschauer",
    95: "Gewitter", 96: "Gewitter mit Hagel", 99: "schweres Gewitter mit Hagel",
}


class WeatherError(RuntimeError):
    pass


def describe(code: Any) -> str:
    try:
        return WMO.get(int(code), "unbekannt")
    except (TypeError, ValueError):
        return "unbekannt"


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=15, transport=TRANSPORT)


async def geocode(name: str) -> dict:
    key = name.strip().lower()
    if key in _geo_cache:
        return _geo_cache[key]
    try:
        async with _client() as c:
            r = await c.get(GEO_URL, params={"name": name, "count": 1, "language": "de", "format": "json"})
    except httpx.HTTPError as e:
        raise WeatherError(f"Wetterdienst nicht erreichbar ({e.__class__.__name__}).") from e
    results = r.json().get("results") if r.status_code == 200 else None
    if not results:
        raise WeatherError(f"Ort '{name}' nicht gefunden.")
    place = results[0]
    _geo_cache[key] = place
    return place


async def forecast(lat: float, lon: float, days: int) -> dict:
    params = {
        "latitude": lat, "longitude": lon, "timezone": "auto", "forecast_days": days,
        "current": "temperature_2m,apparent_temperature,weather_code,wind_speed_10m,relative_humidity_2m",
        "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max,"
                 "precipitation_sum,sunrise,sunset",
    }
    try:
        async with _client() as c:
            r = await c.get(FORECAST_URL, params=params)
    except httpx.HTTPError as e:
        raise WeatherError(f"Wetterdienst nicht erreichbar ({e.__class__.__name__}).") from e
    if r.status_code != 200:
        raise WeatherError(f"Wetterdienst meldet Fehler {r.status_code}.")
    return r.json()


def _num(v: Any, digits: int = 0) -> str:
    if v is None:
        return "?"
    return f"{round(float(v), digits):g}".replace(".", ",")


def _day_label(day: str, index: int) -> str:
    if index == 0:
        return "Heute"
    if index == 1:
        return "Morgen"
    d = date.fromisoformat(day)
    return f"{WEEKDAYS[d.weekday()]} {d.strftime('%d.%m.')}"


def format_weather(place: dict, data: dict) -> str:
    name = place.get("name", "?")
    region = place.get("admin1") or place.get("country") or ""
    lines = [f"Wetter für {name}" + (f" ({region})" if region else "") + ":"]
    cur = data.get("current") or {}
    if cur:
        lines.append(
            f"Jetzt {_num(cur.get('temperature_2m'))} °C (gefühlt {_num(cur.get('apparent_temperature'))} °C), "
            f"{describe(cur.get('weather_code'))}, Wind {_num(cur.get('wind_speed_10m'))} km/h, "
            f"Luftfeuchte {_num(cur.get('relative_humidity_2m'))} %."
        )
    daily = data.get("daily") or {}
    for i, day in enumerate(daily.get("time", [])):
        def col(key):
            values = daily.get(key) or []
            return values[i] if i < len(values) else None
        line = (f"{_day_label(day, i)}: {_num(col('temperature_2m_min'))} bis {_num(col('temperature_2m_max'))} °C, "
                f"{describe(col('weather_code'))}, Regenrisiko {_num(col('precipitation_probability_max'))} %")
        rain = col("precipitation_sum")
        if rain:
            line += f" ({_num(rain, 1)} mm)"
        if i == 0 and col("sunrise") and col("sunset"):
            sunrise = datetime.fromisoformat(col("sunrise")).strftime("%H:%M")
            sunset = datetime.fromisoformat(col("sunset")).strftime("%H:%M")
            line += f", Sonne {sunrise}–{sunset}"
        lines.append(line + ".")
    return "\n".join(lines)


async def weather_report(location: str, days: int) -> str:
    place = await geocode(location)
    data = await forecast(place["latitude"], place["longitude"], max(1, min(days, 7)))
    return format_weather(place, data)


@tool("Aktuelles Wetter und Vorhersage (Temperatur, Regen, Wind); ohne Ort der Standardort.")
async def weather(
    ctx: ToolContext,
    location: Annotated[str, "z. B. 'Freiburg'"] = "",
    days: Annotated[int, "1–7 (Standard 3)"] = 3,
) -> str:
    place = location.strip() or ctx.cfg.weather.location
    if not place:
        return ("Kein Ort angegeben und kein Standardort eingestellt (weather.location in der Config). "
                "Frag den Nutzer nach seinem Ort und merke ihn dir mit remember.")
    try:
        return await weather_report(place, days)
    except WeatherError as e:
        return str(e)
