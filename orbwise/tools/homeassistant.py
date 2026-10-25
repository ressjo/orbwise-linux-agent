"""Home Assistant über die REST-API: Geräte finden, Zustände lesen, Lichter/Schalter/Heizung/Rollos/Szenen steuern.

Einrichtung: Home Assistant → Profil (unten links) → Sicherheit → „Langlebige Zugriffstoken“ → Token erstellen:
    homeassistant:
      url: http://homeassistant.local:8123
      token: "…"
"""

from __future__ import annotations

import json
import re
from typing import Annotated, Any

import httpx

from ..lang import T
from .netutil import client_kwargs, explain, html_instead_of_json, normalize_url
from .registry import CONFIRM, SAFE, ToolContext, tool

# Für Tests austauschbar (httpx.MockTransport)
TRANSPORT: httpx.AsyncBaseTransport | None = None

ENTITY_RE = re.compile(r"^[a-z_]+\.[a-z0-9_]+$")
# Zuordnung Entität → Raum per Template (Home Assistant kennt Räume nicht in der REST-API)
AREA_TEMPLATE = ("{% set ns = namespace(d={}) %}{% for s in states %}{% set a = area_name(s.entity_id) %}"
                 "{% if a %}{% set ns.d = dict(ns.d, **{s.entity_id: a}) %}{% endif %}{% endfor %}{{ ns.d | tojson }}")
SENSITIVE_DOMAINS = {"lock", "alarm_control_panel"}
SENSITIVE_WORDS = ("garage", "tor", "gate", "door", "tür", "tuer")
ON_OFF_DOMAINS = {"light", "switch", "fan", "input_boolean", "media_player", "climate", "humidifier", "siren",
                  "automation", "remote", "vacuum", "water_heater"}
ACTIONS = ("on", "off", "toggle", "brightness", "color", "temperature", "open", "close", "stop", "position",
           "activate", "lock", "unlock", "set")


class HAError(RuntimeError):
    pass


def _enabled(cfg: Any) -> bool:
    return cfg.homeassistant.enabled


class HAClient:
    def __init__(self, cfg):
        h = cfg.homeassistant
        self.url = normalize_url(h.url, "/api")
        self.client = httpx.AsyncClient(base_url=self.url + "/api",
                                        headers={"Authorization": f"Bearer {h.api_token}"},
                                        transport=TRANSPORT, **client_kwargs(h.verify_ssl, h.timeout))

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        await self.client.aclose()

    async def request(self, method: str, path: str, **kw) -> Any:
        for attempt in (1, 2):
            try:
                r = await self.client.request(method, path, **kw)
                break
            except (httpx.ConnectError, httpx.TimeoutException) as e:
                if attempt == 2 or "certificate" in str(e).lower():
                    raise HAError(explain(e, "Home Assistant", self.url)) from e
            except httpx.HTTPError as e:
                raise HAError(explain(e, "Home Assistant", self.url)) from e
        if r.status_code == 401:
            raise HAError("Der Home-Assistant-Token ist ungültig (Profil → Sicherheit → Langlebige Zugriffstoken).")
        if r.status_code == 404:
            return None
        if r.status_code >= 400:
            raise HAError(f"Home-Assistant-Fehler {r.status_code}: {r.text[:200]}")
        if html_instead_of_json(r):
            raise HAError(f"Unter {self.url} antwortet eine Webseite statt der Home-Assistant-API – Adresse prüfen.")
        ctype = r.headers.get("content-type", "")
        return r.json() if "json" in ctype else r.text

    async def states(self) -> list[dict]:
        return await self.request("GET", "/states") or []

    async def state(self, entity_id: str) -> dict | None:
        return await self.request("GET", f"/states/{entity_id}")

    async def areas(self) -> dict[str, str]:
        try:
            raw = await self.request("POST", "/template", json={"template": AREA_TEMPLATE})
            data = json.loads(raw) if isinstance(raw, str) else raw
            return data if isinstance(data, dict) else {}
        except (HAError, ValueError):
            return {}


async def _guard(coro) -> str:
    try:
        return await coro
    except HAError as e:
        return str(e)


def describe_state(s: dict, area: str = "") -> str:
    a = s.get("attributes") or {}
    name = a.get("friendly_name") or s["entity_id"]
    state = s.get("state", "?")
    extra = []
    if s["entity_id"].startswith("light.") and a.get("brightness") is not None and state == "on":
        extra.append(f"{round(a['brightness'] / 255 * 100)} %")
    if a.get("unit_of_measurement"):
        state = f"{state} {a['unit_of_measurement']}"
    if a.get("current_temperature") is not None:
        extra.append(f"ist {a['current_temperature']} °C, soll {a.get('temperature', '?')} °C")
    if a.get("current_position") is not None:
        extra.append(f"Position {a['current_position']} %")
    room = f" [{area}]" if area else ""
    return f"{s['entity_id']} – {name}{room}: {state}" + (f" ({', '.join(extra)})" if extra else "")


@tool("Findet Home-Assistant-Geräte nach Name, Raum oder Typ und zeigt den Zustand.", enabled=_enabled)
async def ha_find(
    ctx: ToolContext,
    query: Annotated[str, "Name/Raum, leer = alle des Typs"] = "",
    domain: Annotated[str, "z. B. light, switch, climate, cover, sensor, scene"] = "",
    limit: int = 25,
) -> str:
    async def run() -> str:
        async with HAClient(ctx.cfg) as ha:
            states = await ha.states()
            areas = await ha.areas()
        words = [w for w in re.split(r"\s+", query.lower().strip()) if w]
        dom = domain.strip().lower().rstrip(".")
        hits = []
        for s in states:
            eid = s.get("entity_id", "")
            if dom and not eid.startswith(dom + "."):
                continue
            hay = f"{eid} {(s.get('attributes') or {}).get('friendly_name', '')} {areas.get(eid, '')}".lower()
            hay = hay.replace("_", " ")
            if all(w in hay for w in words):
                hits.append(s)
        if not hits:
            return "Keine passenden Geräte in Home Assistant gefunden."
        hits.sort(key=lambda s: (areas.get(s["entity_id"], "~"), s["entity_id"]))
        n = max(1, min(int(limit), 80))
        lines = [describe_state(s, areas.get(s["entity_id"], "")) for s in hits[:n]]
        more = f"\n… und {len(hits) - n} weitere – Suche eingrenzen." if len(hits) > n else ""
        return "\n".join(lines) + more

    return await _guard(run())


@tool("Zeigt den Zustand eines Home-Assistant-Geräts mit allen Attributen.", enabled=_enabled)
async def ha_state(ctx: ToolContext, entity_id: Annotated[str, "z. B. light.kueche"]) -> str:
    entity_id = entity_id.strip().lower()
    if not ENTITY_RE.match(entity_id):
        return "Ungültige entity_id – erst mit ha_find suchen."

    async def run() -> str:
        async with HAClient(ctx.cfg) as ha:
            s = await ha.state(entity_id)
        if not s:
            return f"'{entity_id}' gibt es nicht – erst mit ha_find suchen."
        attrs = {k: v for k, v in (s.get("attributes") or {}).items()
                 if k not in ("icon", "entity_picture", "supported_features", "supported_color_modes")}
        return describe_state(s) + f"\nZuletzt geändert: {s.get('last_changed', '?')}\nAttribute: " + \
            json.dumps(attrs, ensure_ascii=False)[:1500]

    return await _guard(run())


def _control_risk(ctx: ToolContext, args: dict) -> tuple[str, str]:
    eid = str(args.get("entity_id", "")).lower()
    action = str(args.get("action", "")).lower()
    domain = eid.split(".")[0]
    if domain in SENSITIVE_DOMAINS or (domain == "cover" and any(w in eid for w in SENSITIVE_WORDS)):
        return CONFIRM, T(f"Sicherheitsrelevantes Gerät: {eid} → {action}", f"security-relevant device: {eid} → {action}")
    return SAFE, T("Smart-Home-Steuerung", "smart home control")


def service_call(entity_id: str, action: str, value: str) -> tuple[str, str, dict] | str:
    """(domain, service, data) für eine Aktion – oder eine Fehlermeldung."""
    domain = entity_id.split(".")[0]
    data: dict[str, Any] = {"entity_id": entity_id}

    def number(lo: float, hi: float) -> float | None:
        try:
            v = float(str(value).replace(",", ".").rstrip("%°C "))
        except ValueError:
            return None
        return max(lo, min(hi, v))

    if action in ("on", "off", "toggle"):
        if domain == "cover":
            return "cover", {"on": "open_cover", "off": "close_cover", "toggle": "toggle"}[action], data
        if domain == "lock":
            return "lock", {"on": "lock", "off": "unlock"}.get(action, "lock"), data
        if domain in ("scene", "script", "button"):
            return service_call(entity_id, "activate", value)
        target = domain if domain in ON_OFF_DOMAINS else "homeassistant"
        return target, {"on": "turn_on", "off": "turn_off", "toggle": "toggle"}[action], data
    if action == "brightness":
        pct = number(0, 100)
        if pct is None or domain != "light":
            return "Helligkeit geht nur bei Lichtern und als Zahl 0–100."
        return "light", "turn_on", {**data, "brightness_pct": pct}
    if action == "color":
        if domain != "light" or not re.fullmatch(r"[a-z]{3,20}", value.strip().lower()):
            return "Farbe als englischer Farbname angeben, z. B. red, blue, warmwhite."
        return "light", "turn_on", {**data, "color_name": value.strip().lower()}
    if action == "temperature":
        t = number(5, 35)
        if t is None or domain not in ("climate", "water_heater"):
            return "Temperatur geht nur bei Heizung/Klima (climate) als Zahl in °C."
        return domain, "set_temperature", {**data, "temperature": t}
    if action in ("open", "close", "stop"):
        if domain != "cover":
            return "open/close/stop nur bei Rollos, Jalousien, Toren (cover)."
        return "cover", f"{action}_cover", data
    if action == "position":
        pos = number(0, 100)
        if pos is None or domain != "cover":
            return "Position nur bei cover und als Zahl 0–100."
        return "cover", "set_cover_position", {**data, "position": int(pos)}
    if action == "activate":
        if domain in ("scene", "script"):
            return domain, "turn_on", data
        if domain in ("button", "input_button"):
            return domain, "press", data
        return "activate nur bei Szenen, Skripten und Tastern."
    if action in ("lock", "unlock"):
        if domain != "lock":
            return "lock/unlock nur bei Schlössern."
        return "lock", action, data
    if action == "set":
        if domain in ("input_number", "number"):
            v = number(-1e9, 1e9)
            return (domain, "set_value", {**data, "value": v}) if v is not None else "Wert als Zahl angeben."
        if domain in ("input_select", "select"):
            return domain, "select_option", {**data, "option": value}
        if domain == "input_text":
            return domain, "set_value", {**data, "value": value[:255]}
        return "set nur bei input_number/select/input_text."
    return f"Unbekannte Aktion '{action}'. Möglich: {', '.join(ACTIONS)}."


@tool("Steuert ein Home-Assistant-Gerät.",
      risk=_control_risk, enabled=_enabled)
async def ha_control(
    ctx: ToolContext,
    entity_id: Annotated[str, "Aus ha_find"],
    action: Annotated[str, "on | off | toggle | brightness | color | temperature | open | close | stop | position "
                           "| activate | lock | unlock | set"],
    value: Annotated[str, "z. B. 40 (%), 21.5 (°C), red (englisch)"] = "",
) -> str:
    entity_id, action = entity_id.strip().lower(), action.strip().lower()
    if not ENTITY_RE.match(entity_id):
        return "Ungültige entity_id – erst mit ha_find suchen."
    call = service_call(entity_id, action, value)
    if isinstance(call, str):
        return call
    domain, service, data = call

    async def run() -> str:
        async with HAClient(ctx.cfg) as ha:
            if not await ha.state(entity_id):
                return f"'{entity_id}' gibt es nicht – erst mit ha_find suchen."
            await ha.request("POST", f"/services/{domain}/{service}", json=data)
            new = await ha.state(entity_id)
        return f"Erledigt ({domain}.{service}). Jetzt: {describe_state(new) if new else 'unbekannt'}"

    return await _guard(run())


async def ha_status(cfg) -> dict:
    """Für orbwise doctor."""
    if not cfg.homeassistant.enabled:
        return {"enabled": False, "online": False}
    try:
        async with HAClient(cfg) as ha:
            info = await ha.request("GET", "/config")
            count = len(await ha.states())
        return {"enabled": True, "online": True, "version": (info or {}).get("version", "?"), "entities": count,
                "url": ha.url}
    except HAError as e:
        return {"enabled": True, "online": False, "error": str(e)}
