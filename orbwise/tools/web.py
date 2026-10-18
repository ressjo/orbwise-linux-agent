"""Websuche (Brave Search API, eigene SearXNG-Instanz oder Suchseiten via ddgs) und Abruf von Webseiten."""

from __future__ import annotations

import asyncio
import logging
import re
import shutil
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Annotated
from urllib.parse import quote_plus

import httpx

from . import proc
from .registry import ToolContext, tool

log = logging.getLogger(__name__)

UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"

# Name → (Startseite, Such-URL mit {q})
SITES: dict[str, tuple[str, str]] = {
    "youtube": ("https://www.youtube.com", "https://www.youtube.com/results?search_query={q}"),
    "google": ("https://www.google.de", "https://www.google.de/search?q={q}"),
    "duckduckgo": ("https://duckduckgo.com", "https://duckduckgo.com/?q={q}"),
    "wikipedia": ("https://de.wikipedia.org", "https://de.wikipedia.org/w/index.php?search={q}"),
    "amazon": ("https://www.amazon.de", "https://www.amazon.de/s?k={q}"),
    "ebay": ("https://www.ebay.de", "https://www.ebay.de/sch/i.html?_nkw={q}"),
    "kleinanzeigen": ("https://www.kleinanzeigen.de", "https://www.kleinanzeigen.de/s-{q}/k0"),
    "idealo": ("https://www.idealo.de", "https://www.idealo.de/preisvergleich/MainSearchProductCategory.html?q={q}"),
    "github": ("https://github.com", "https://github.com/search?q={q}"),
    "reddit": ("https://www.reddit.com", "https://www.reddit.com/search/?q={q}"),
    "maps": ("https://www.google.com/maps", "https://www.google.com/maps/search/{q}"),
    "openstreetmap": ("https://www.openstreetmap.org", "https://www.openstreetmap.org/search?query={q}"),
    "netflix": ("https://www.netflix.com", "https://www.netflix.com/search?q={q}"),
    "twitch": ("https://www.twitch.tv", "https://www.twitch.tv/search?term={q}"),
    "spotify": ("https://open.spotify.com", "https://open.spotify.com/search/{q}"),
    "chefkoch": ("https://www.chefkoch.de", "https://www.chefkoch.de/rs/s0/{q}/Rezepte.html"),
    "leo": ("https://dict.leo.org", "https://dict.leo.org/englisch-deutsch/{q}"),
    "deepl": ("https://www.deepl.com/translator", "https://www.deepl.com/translator#en/de/{q}"),
    "archwiki": ("https://wiki.archlinux.org", "https://wiki.archlinux.org/index.php?search={q}"),
    "aur": ("https://aur.archlinux.org", "https://aur.archlinux.org/packages?K={q}"),
    "heise": ("https://www.heise.de", "https://www.heise.de/suche/?q={q}"),
    "tagesschau": ("https://www.tagesschau.de", "https://www.tagesschau.de/suche?searchText={q}"),
    "wetter": ("https://www.wetter.com", "https://www.wetter.com/suche/?q={q}"),
}
ALIASES = {
    "yt": "youtube", "google maps": "maps", "karte": "maps", "karten": "maps", "osm": "openstreetmap",
    "wiki": "wikipedia", "ebay kleinanzeigen": "kleinanzeigen", "arch wiki": "archwiki",
    "amazon.de": "amazon", "youtube.com": "youtube",
}
DOMAIN_RE = re.compile(r"^(https?://)?([\w-]+\.)+[a-z]{2,}(/\S*)?$", re.I)


def website_url(site: str, query: str = "", extra: dict[str, str] | None = None) -> str:
    """URL für eine bekannte Seite (optional mit Suche), eine Domain oder als Fallback die DuckDuckGo-Suche."""
    name = site.strip().lower()
    q = quote_plus(query.strip()) if query.strip() else ""
    for key, url in (extra or {}).items():
        if key.lower() == name:
            if "{q}" in url:
                return url.replace("{q}", q) if q else re.sub(r"^(https?://[^/]+).*$", r"\1", url)
            return url
    name = ALIASES.get(name, name)
    if name in SITES:
        home, search = SITES[name]
        return search.format(q=q) if q else home
    if DOMAIN_RE.match(name):
        url = name if name.startswith("http") else f"https://{name}"
        return url
    # Unbekannt: DuckDuckGo-„!ducky“ springt direkt zum ersten Treffer
    return "https://duckduckgo.com/?q=" + quote_plus(f"!ducky {site} {query}".strip())


@tool("Öffnet eine Website im Browser – eine bekannte Seite (YouTube, Amazon, Wikipedia …), eine Domain wie "
      "heise.de oder direkt eine Suche auf der Seite.")
async def open_website(
    ctx: ToolContext,
    site: Annotated[str, "Name der Seite oder Domain, z. B. 'youtube', 'amazon', 'heise.de'"],
    query: Annotated[str, "Optional: Suchbegriff auf dieser Seite, z. B. 'lofi hip hop'"] = "",
) -> str:
    url = website_url(site, query, ctx.cfg.websites)
    opener = next((c for c in (["xdg-open"], ["gio", "open"]) if shutil.which(c[0])), None)
    if not opener:
        return f"Kein Programm zum Öffnen gefunden (xdg-utils fehlt). URL: {url}"
    ok, err = await proc.launch([*opener, url])
    if ok:
        return f"Geöffnet: {url}"
    hint = "" if proc.has_display() else " Orbwise hat keinen Zugriff auf die grafische Sitzung."
    return f"Öffnen fehlgeschlagen ({err}).{hint} URL: {url}"


# Für Tests austauschbar (httpx.MockTransport)
TRANSPORT: httpx.AsyncBaseTransport | None = None


async def _searxng(url: str, query: str, n: int) -> list[dict]:
    async with httpx.AsyncClient(timeout=20, transport=TRANSPORT, headers={"User-Agent": UA}) as client:
        r = await client.get(f"{url.rstrip('/')}/search", params={"q": query, "format": "json", "language": "de"})
    if r.status_code in (403, 429):
        # Öffentliche Instanzen sperren die JSON-Schnittstelle meist für Programme
        raise RuntimeError(f"SearXNG-Instanz verweigert den Zugriff (HTTP {r.status_code}) – JSON-Format dort "
                           "vermutlich deaktiviert")
    r.raise_for_status()
    return [{"title": x.get("title", ""), "href": x.get("url", ""), "body": x.get("content", "")}
            for x in r.json().get("results", [])[:n]]


BRAVE_URL = "https://api.search.brave.com/res/v1/web/search"
TAG_RE = re.compile(r"<[^>]+>")


BRAVE_NEWS_URL = "https://api.search.brave.com/res/v1/news/search"


async def _brave_get(key: str, url: str, query: str, n: int, language: str, cfg=None, **extra) -> dict:
    """Offizielle Brave Search API – kein Auslesen von Suchseiten, daher kein Bot-Blockieren. Mit cfg wird die
    Anfrage gezählt und das Monatskontingent laut Brave gemerkt (usage.py); ist es aufgebraucht, gar nicht erst fragen."""
    from .. import usage
    if cfg is not None and (until := usage.exhausted(cfg, "brave")):
        raise RuntimeError("Brave hat das Monatskontingent als aufgebraucht gemeldet (HTTP 429) – Orbwise fragt bis "
                           + datetime.fromtimestamp(until).strftime("%d.%m.%Y") + " nicht erneut")
    params: dict[str, str | int] = {"q": query, "count": n, **extra}
    params.update({"country": "DE", "search_lang": "de"} if language == "de" else {"search_lang": "en"})
    headers = {"X-Subscription-Token": key, "Accept": "application/json"}
    async with httpx.AsyncClient(timeout=20, transport=TRANSPORT, headers=headers) as client:
        r = await client.get(url, params=params)
    if cfg is not None and r.status_code in (200, 429):
        usage.record(cfg, "brave", r.headers, count=r.status_code == 200, status=r.status_code)
    if r.status_code in (401, 403) or (r.status_code == 422 and "TOKEN" in r.text.upper()):
        raise RuntimeError(f"Brave-API-Schlüssel ungültig oder ohne Berechtigung (HTTP {r.status_code})")
    if r.status_code == 429:
        raise RuntimeError("Brave-API: Kontingent oder Rate-Limit erreicht (HTTP 429) – später erneut versuchen")
    r.raise_for_status()
    return r.json()


async def _brave(key: str, query: str, n: int, language: str = "de", cfg=None) -> list[dict]:
    data = await _brave_get(key, BRAVE_URL, query, n, language, cfg)
    return [{"title": TAG_RE.sub("", x.get("title", "")), "href": x.get("url", ""),
             "body": TAG_RE.sub("", x.get("description", ""))}
            for x in (data.get("web") or {}).get("results", [])[:n]]


def _ddgs_news(query: str, n: int) -> list[dict]:
    from ddgs import DDGS
    return DDGS().news(query, region="de-de", timelimit="d", max_results=n) or []


async def news_headlines(cfg, topic: str, n: int) -> list[dict]:
    """Aktuelle Schlagzeilen (letzte 24 h) zu einem Thema: [{title, source, url}] – Brave-API, sonst ddgs."""
    tools = cfg.tools
    if tools.brave_key:
        try:
            data = await _brave_get(tools.brave_key, BRAVE_NEWS_URL, topic, n, cfg.language, cfg, freshness="pd")
            return [{"title": TAG_RE.sub("", x.get("title", "")), "url": x.get("url", ""),
                     "source": (x.get("meta_url") or {}).get("hostname", "")} for x in data.get("results", [])[:n]]
        except Exception:
            if not tools.search_fallback:
                raise
    items = await asyncio.to_thread(_ddgs_news, topic, n)
    return [{"title": x.get("title", ""), "url": x.get("url", ""), "source": x.get("source", "")} for x in items[:n]]


async def search_status(cfg) -> dict:
    """Für orbwise doctor: welcher Suchweg aktiv ist und ob die Brave-API antwortet (eine Testanfrage)."""
    tools = cfg.tools
    if not tools.brave_key:
        return {"mode": "searxng" if tools.searxng_url else "scrape", "online": None}
    try:
        await _brave(tools.brave_key, "orbwise", 1, cfg.language, cfg)
        return {"mode": "brave", "online": True, "usage": usage_summary(cfg)}
    except Exception as e:  # noqa: BLE001
        return {"mode": "brave", "online": False, "error": str(e), "usage": usage_summary(cfg)}


def usage_summary(cfg) -> dict:
    from .. import usage
    return usage.summary(cfg).get("brave") or {}


def _ddgs(query: str, n: int) -> list[dict]:
    from ddgs import DDGS
    return DDGS().text(query, region="de-de", max_results=n) or []


@tool("Sucht im Internet und liefert Titel, URL und Kurzbeschreibung der Treffer. Für Details danach fetch_url.")
async def web_search(
    ctx: ToolContext,
    query: Annotated[str, "Suchanfrage"],
    max_results: Annotated[int, "Anzahl Treffer (Standard 5)"] = 5,
) -> str:
    n = max(1, min(max_results, 10))
    tools = ctx.cfg.tools
    # Reihenfolge: offizielle Brave-API → eigene SearXNG-Instanz → Suchseiten auslesen (ddgs). Mit Brave-Schlüssel
    # wird nur mit search_fallback: true auf das Auslesen ausgewichen (Bot-Sperren vermeiden).
    sources: list[tuple[str, Callable[[], Awaitable[list[dict]]]]] = []
    if tools.brave_key:
        sources.append(("Brave", lambda: _brave(tools.brave_key, query, n, ctx.cfg.language, ctx.cfg)))
    if tools.searxng_url:
        sources.append(("SearXNG", lambda: _searxng(tools.searxng_url, query, n)))
    if not tools.brave_key or tools.search_fallback:
        sources.append(("DuckDuckGo", lambda: asyncio.to_thread(_ddgs, query, n)))
    errors: list[str] = []
    for name, search in sources:
        try:
            results = await search()
        except Exception as e:  # noqa: BLE001 – nächste Quelle versuchen
            log.warning("Websuche über %s fehlgeschlagen: %s", name, e)
            errors.append(str(e))
            continue
        note = "".join(f"(Hinweis: {e}; Ergebnisse stattdessen von {name}.)\n\n" for e in errors)
        if not results:
            return note + "Keine Suchergebnisse."
        return note + "\n\n".join(f"{i}. {r.get('title', '')}\n{r.get('href', '')}\n{r.get('body', '')}"
                                  for i, r in enumerate(results, 1))
    return "Websuche fehlgeschlagen: " + "; ".join(errors)


@tool("Ruft eine Webseite ab und gibt den lesbaren Haupttext zurück.")
async def fetch_url(ctx: ToolContext, url: Annotated[str, "Vollständige URL (http/https)"]) -> str:
    if not url.startswith(("http://", "https://")):
        return "Nur http(s)-URLs werden unterstützt."
    try:
        async with httpx.AsyncClient(timeout=25, follow_redirects=True, headers={"User-Agent": UA}) as client:
            r = await client.get(url)
    except httpx.HTTPError as e:
        return f"Abruf fehlgeschlagen: {e}"
    if r.status_code >= 400:
        return f"Abruf fehlgeschlagen: HTTP {r.status_code}"
    ctype = r.headers.get("content-type", "")
    if "html" not in ctype and "text" not in ctype and "json" not in ctype:
        return f"Kein Textinhalt ({ctype})."
    text = r.text
    if "html" in ctype:
        import trafilatura
        text = await asyncio.to_thread(trafilatura.extract, r.text, include_links=False) or ""
    return proc.clip_saved(text.strip() or "(kein lesbarer Text gefunden)", ctx.limit(), "webseite")
