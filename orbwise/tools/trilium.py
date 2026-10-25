"""Trilium Notes (bzw. TriliumNext) über die ETAPI: Notizen suchen, lesen, anlegen, ergänzen, überschreiben.

Einrichtung: in Trilium unter Optionen → ETAPI einen Token erzeugen und in der Config eintragen:
    trilium:
      url: http://localhost:8080
      token: "…"
"""

from __future__ import annotations

import asyncio
import html
import re
from datetime import date
from html.parser import HTMLParser
from typing import Annotated, Any

import httpx

from ..lang import T
from .netutil import client_kwargs, explain, normalize_url
from .proc import clip_saved
from .registry import CONFIRM, ToolContext, tool

# Für Tests austauschbar (httpx.MockTransport)
TRANSPORT: httpx.AsyncBaseTransport | None = None


class TriliumError(RuntimeError):
    pass


def _enabled(cfg: Any) -> bool:
    return cfg.trilium.enabled


# ---------------------------------------------------------------- HTML ↔ Text

class _TextExtractor(HTMLParser):
    BLOCK = {"p", "div", "h1", "h2", "h3", "h4", "h5", "h6", "blockquote", "pre", "table", "tr", "figure"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self.lists: list[list] = []  # Stapel aus [typ, zähler]
        self.href: str | None = None
        self.in_pre = False

    def _nl(self, n: int = 1) -> None:
        text = "".join(self.out)
        have = len(text) - len(text.rstrip("\n"))
        if text and have < n:
            self.out.append("\n" * (n - have))

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            self._nl(2)
            self.out.append("#" * int(tag[1]) + " ")
        elif tag in self.BLOCK:
            self._nl(2 if tag in ("p", "pre", "blockquote", "table") else 1)
            if tag == "pre":
                self.out.append("```\n")
                self.in_pre = True
        elif tag == "br":
            self.out.append("\n")
        elif tag in ("ul", "ol"):
            self._nl(1)
            self.lists.append([tag, 0])
        elif tag == "li":
            self._nl(1)
            indent = "  " * max(0, len(self.lists) - 1)
            if self.lists and self.lists[-1][0] == "ol":
                self.lists[-1][1] += 1
                self.out.append(f"{indent}{self.lists[-1][1]}. ")
            else:
                self.out.append(f"{indent}- ")
        elif tag == "input" and a.get("type") == "checkbox":
            self.out.append("[x] " if "checked" in a else "[ ] ")
        elif tag in ("td", "th"):
            self.out.append(" | ")
        elif tag == "a":
            self.href = a.get("href")
        elif tag in ("strong", "b"):
            self.out.append("**")
        elif tag == "code" and not self.in_pre:
            self.out.append("`")

    def handle_endtag(self, tag):
        if tag == "pre":
            self._nl(1)
            self.out.append("```")
            self.in_pre = False
            self._nl(2)
        elif tag in self.BLOCK or tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            self._nl(2 if tag != "tr" else 1)
        elif tag in ("ul", "ol"):
            if self.lists:
                self.lists.pop()
            self._nl(2 if not self.lists else 1)
        elif tag == "a":
            if self.href and not self.href.startswith("#"):
                self.out.append(f" ({self.href})")
            self.href = None
        elif tag in ("strong", "b"):
            self.out.append("**")
        elif tag == "code" and not self.in_pre:
            self.out.append("`")

    def handle_data(self, data):
        if self.in_pre:
            self.out.append(data)
        else:
            self.out.append(re.sub(r"\s+", " ", data))


def html_to_text(content: str) -> str:
    p = _TextExtractor()
    p.feed(content)
    p.close()
    text = "".join(p.out)
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n[ \t]+(?=[^\s-])", "\n", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _inline(text: str) -> str:
    t = html.escape(text, quote=False)
    t = re.sub(r"`([^`]+)`", r"<code>\1</code>", t)
    t = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", t)
    t = re.sub(r"(?<![\w*])\*([^*\n]+)\*(?![\w*])", r"<em>\1</em>", t)
    t = re.sub(r"\[([^\]]+)\]\((https?://[^)\s]+)\)", r'<a href="\2">\1</a>', t)
    return t


def text_to_html(text: str) -> str:
    """Mini-Markdown → HTML für Triliums Texteditor."""
    if re.search(r"<(p|ul|ol|h\d|div|br)\b", text):
        return text  # ist bereits HTML
    out: list[str] = []
    lines = text.replace("\r\n", "\n").split("\n")
    i = 0
    para: list[str] = []

    def flush_para():
        if para:
            out.append("<p>" + "<br>".join(_inline(p) for p in para) + "</p>")
            para.clear()

    while i < len(lines):
        line = lines[i]
        stripped = line.strip()
        if stripped.startswith("```"):
            flush_para()
            code = []
            i += 1
            while i < len(lines) and not lines[i].strip().startswith("```"):
                code.append(lines[i])
                i += 1
            out.append('<pre><code class="language-text-plain">' + html.escape("\n".join(code), quote=False)
                       + "</code></pre>")
            i += 1
            continue
        m = re.match(r"^(#{1,6})\s+(.*)", stripped)
        if m:
            flush_para()
            level = max(2, len(m.group(1)))  # h1 ist in Trilium der Notiztitel
            out.append(f"<h{level}>{_inline(m.group(2))}</h{level}>")
            i += 1
            continue
        if re.match(r"^([-*•]|\d+[.)])\s+", stripped):
            flush_para()
            ordered = bool(re.match(r"^\d+[.)]", stripped))
            items = []
            is_todo = False
            while i < len(lines) and re.match(r"^\s*([-*•]|\d+[.)])\s+", lines[i]):
                item = re.sub(r"^\s*([-*•]|\d+[.)])\s+", "", lines[i])
                todo = re.match(r"^\[([ xX])\]\s*(.*)", item)
                if todo:
                    is_todo = True
                    checked = ' checked="checked"' if todo.group(1).lower() == "x" else ""
                    items.append(f'<li><label class="todo-list__label"><input type="checkbox"{checked} disabled="disabled">'
                                 f'<span class="todo-list__label__description">{_inline(todo.group(2))}</span></label></li>')
                else:
                    items.append(f"<li>{_inline(item)}</li>")
                i += 1
            tag = "ol" if ordered else "ul"
            cls = ' class="todo-list"' if is_todo else ""
            out.append(f"<{tag}{cls}>" + "".join(items) + f"</{tag}>")
            continue
        if not stripped:
            flush_para()
        else:
            para.append(stripped)
        i += 1
    flush_para()
    return "".join(out) or "<p></p>"


# ---------------------------------------------------------------- Client

class TriliumClient:
    def __init__(self, cfg):
        t = cfg.trilium
        self.max_chars = t.max_chars
        self.url = normalize_url(t.url, "/etapi")
        self.client = httpx.AsyncClient(
            base_url=self.url + "/etapi",
            headers={"Authorization": t.api_token},
            transport=TRANSPORT,
            **client_kwargs(t.verify_ssl, t.timeout),
        )

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        await self.client.aclose()

    async def request(self, method: str, path: str, **kw) -> httpx.Response:
        try:
            r = await self.client.request(method, path, **kw)
        except httpx.HTTPError as e:
            raise TriliumError(explain(e, "Trilium", self.url)) from e
        if r.status_code == 401:
            raise TriliumError("Der ETAPI-Token ist ungültig (Trilium → Optionen → ETAPI).")
        return r

    async def json(self, method: str, path: str, **kw) -> Any:
        r = await self.request(method, path, **kw)
        if r.status_code == 404:
            return None
        if r.status_code >= 400:
            raise TriliumError(f"Trilium-Fehler {r.status_code}: {_error_text(r)}")
        return r.json()

    async def note(self, note_id: str) -> dict | None:
        if not re.fullmatch(r"[A-Za-z0-9_]{1,64}", note_id):
            return None
        return await self.json("GET", f"/notes/{note_id}")

    async def content(self, note: dict) -> str:
        r = await self.request("GET", f"/notes/{note['noteId']}/content")
        if r.status_code >= 400:
            return ""
        raw = r.text
        if note.get("type") == "text" or "html" in (note.get("mime") or ""):
            return html_to_text(raw)
        return raw

    async def raw_content(self, note_id: str) -> str:
        r = await self.request("GET", f"/notes/{note_id}/content")
        if r.status_code >= 400:
            raise TriliumError(f"Inhalt nicht lesbar ({r.status_code}).")
        return r.text

    async def set_content(self, note_id: str, content: str) -> None:
        r = await self.request("PUT", f"/notes/{note_id}/content", content=content.encode(),
                               headers={"Content-Type": "text/plain"})
        if r.status_code >= 400:
            raise TriliumError(f"Speichern fehlgeschlagen ({r.status_code}): {_error_text(r)}")

    async def search(self, query: str, limit: int) -> list[dict]:
        data = await self.json("GET", "/notes", params={
            "search": query, "limit": limit, "orderBy": "dateModified", "orderDirection": "desc"})
        return (data or {}).get("results", [])

    async def resolve(self, ref: str) -> dict:
        """Notiz per ID oder (Teil-)Titel finden."""
        ref = ref.strip()
        if not ref:
            raise TriliumError("Keine Notiz angegeben.")
        note = await self.note(ref)
        if note:
            return note
        title = ref.replace("\\", "\\\\").replace('"', '\\"')
        for query in (f'note.title = "{title}"', f'note.title *=* "{title}"'):
            results = await self.search(query, 10)
            if len(results) == 1 or (results and query.startswith('note.title = "')):
                return results[0]
            if results:
                names = ", ".join(f"'{n['title']}' ({n['noteId']})" for n in results[:8])
                raise TriliumError(f"Mehrere Notizen passen zu '{ref}': {names}. Bitte per ID auswählen.")
        raise TriliumError(f"Keine Notiz namens '{ref}' gefunden.")

    async def inbox(self) -> dict:
        note = await self.json("GET", f"/inbox/{date.today().isoformat()}")
        if not note:
            raise TriliumError("Trilium-Inbox nicht gefunden.")
        return note


def _error_text(r: httpx.Response) -> str:
    try:
        return r.json().get("message", r.text)[:200]
    except ValueError:
        return r.text[:200]


def _when(note: dict) -> str:
    stamp = note.get("utcDateModified") or note.get("dateModified") or ""
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})[ T](\d{2}:\d{2})", stamp)
    return f"{m.group(3)}.{m.group(2)}.{m.group(1)} {m.group(4)}" if m else stamp


async def _parent_titles(tc: TriliumClient, note: dict) -> str:
    titles = []
    for pid in (note.get("parentNoteIds") or [])[:3]:
        if pid == "root":
            continue
        parent = await tc.note(pid)
        if parent:
            titles.append(parent["title"])
    return ", ".join(titles)


async def _guard(coro) -> str:
    try:
        return await coro
    except TriliumError as e:
        return str(e)


# ---------------------------------------------------------------- Tools

@tool("Durchsucht die Trilium-Notizen (Titel und Inhalt, neueste zuerst); liefert Titel, ID und Vorschau.",
      enabled=_enabled)
async def trilium_search(
    ctx: ToolContext,
    query: Annotated[str, "Suchbegriffe; Trilium-Syntax (#label …) geht auch"],
    limit: Annotated[int, "Standard 10"] = 10,
) -> str:
    async def run() -> str:
        async with TriliumClient(ctx.cfg) as tc:
            results = await tc.search(query, max(1, min(limit, 50)))
            if not results:
                return f"Keine Trilium-Notizen zu '{query}' gefunden."
            previews = await asyncio.gather(*(tc.content(n) for n in results[:5]), return_exceptions=True)
            lines = [f"{len(results)} Notiz(en) gefunden:"]
            for i, n in enumerate(results):
                line = f"- {n['title']} (ID {n['noteId']}, geändert {_when(n)})"
                if i < len(previews) and isinstance(previews[i], str) and previews[i].strip():
                    snippet = " ".join(previews[i].split())[:200]
                    line += f"\n  {snippet}{'…' if len(previews[i]) > 200 else ''}"
                lines.append(line)
            return "\n".join(lines)
    return await _guard(run())


@tool("Liest eine Trilium-Notiz.", enabled=_enabled)
async def trilium_read(
    ctx: ToolContext,
    note: Annotated[str, "ID oder Titel"],
) -> str:
    async def run() -> str:
        async with TriliumClient(ctx.cfg) as tc:
            n = await tc.resolve(note)
            text = await tc.content(n)
            parents = await _parent_titles(tc, n)
            head = f"Notiz: {n['title']} (ID {n['noteId']}, geändert {_when(n)}"
            head += f", in: {parents})" if parents else ")"
            return head + "\n\n" + (clip_saved(text, ctx.limit(tc.max_chars), "trilium") if text.strip() else "(leer)")
    return await _guard(run())


@tool("Legt eine Trilium-Notiz an (Standard: Inbox), Inhalt in einfachem Markdown.",
      enabled=_enabled)
async def trilium_create_note(
    ctx: ToolContext,
    title: str,
    content: str,
    parent: Annotated[str, "ID oder Titel des Ordners; leer = Inbox"] = "",
) -> str:
    async def run() -> str:
        async with TriliumClient(ctx.cfg) as tc:
            parent_note = await tc.resolve(parent) if parent.strip() else await tc.inbox()
            data = await tc.json("POST", "/create-note", json={
                "parentNoteId": parent_note["noteId"], "title": title.strip() or "Neue Notiz",
                "type": "text", "content": text_to_html(content)})
            if not data or "note" not in data:
                raise TriliumError("Trilium hat die Notiz nicht angelegt.")
            n = data["note"]
            return f"Notiz '{n['title']}' angelegt (ID {n['noteId']}) unter '{parent_note['title']}'."
    return await _guard(run())


@tool("Hängt Text an eine Trilium-Notiz an.",
      enabled=_enabled)
async def trilium_append(
    ctx: ToolContext,
    note: Annotated[str, "ID oder Titel"],
    content: str,
) -> str:
    async def run() -> str:
        async with TriliumClient(ctx.cfg) as tc:
            n = await tc.resolve(note)
            if n.get("type") not in ("text", "code"):
                raise TriliumError(f"'{n['title']}' ist eine Notiz vom Typ {n.get('type')} – Anhängen nicht möglich.")
            old = await tc.raw_content(n["noteId"])
            addition = text_to_html(content) if n.get("type") == "text" else content
            sep = "" if not old.strip() else ("\n" if n.get("type") == "code" else "")
            await tc.set_content(n["noteId"], old + sep + addition)
            return f"An '{n['title']}' angehängt."
    return await _guard(run())


def _update_risk(ctx: ToolContext, args: dict) -> tuple[str, str]:
    note = args.get("note", "")
    return CONFIRM, T(f"überschreibt den Inhalt der Trilium-Notiz '{note}'", f"overwrites the Trilium note '{note}'")


@tool("Ersetzt den Inhalt einer Trilium-Notiz (optional den Titel). Zum Ergänzen trilium_append.",
      risk=_update_risk, enabled=_enabled)
async def trilium_update_note(
    ctx: ToolContext,
    note: Annotated[str, "ID oder Titel"],
    content: str,
    title: str = "",
) -> str:
    async def run() -> str:
        async with TriliumClient(ctx.cfg) as tc:
            n = await tc.resolve(note)
            body = text_to_html(content) if n.get("type") == "text" else content
            await tc.set_content(n["noteId"], body)
            if title.strip() and title.strip() != n["title"]:
                await tc.json("PATCH", f"/notes/{n['noteId']}", json={"title": title.strip()})
            return f"Notiz '{title.strip() or n['title']}' aktualisiert."
    return await _guard(run())


async def trilium_status(cfg) -> dict:
    """Für doctor und /api/status."""
    if not cfg.trilium.enabled:
        return {"enabled": False, "online": False}
    try:
        async with TriliumClient(cfg) as tc:
            info = await tc.json("GET", "/app-info")
        return {"enabled": True, "online": True, "version": (info or {}).get("appVersion", "?")}
    except TriliumError as e:
        return {"enabled": True, "online": False, "error": str(e)}
