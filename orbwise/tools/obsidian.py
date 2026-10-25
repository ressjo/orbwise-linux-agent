"""Obsidian-Notizen: direkt auf den Markdown-Dateien des Vaults suchen, lesen, befragen, anlegen und ergänzen.

Kein Plugin und kein laufendes Obsidian nötig – der Vault ist ein normaler Ordner. Einrichtung in der Config:
    obsidian:
      vault: ~/Obsidian/Arbeit
      inbox: Inbox
"""

from __future__ import annotations

import math
import os
import re
import shutil
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import quote

import yaml

from ..lang import T
from . import proc
from .passages import _words, rank_passages, select_passages, split_passages
from .registry import CONFIRM, ToolContext, tool

MAX_FILE_BYTES = 2_000_000
BAD_NAME_CHARS = re.compile(r'[\\/:*?"<>|#^\[\]]+')
INLINE_TAG = re.compile(r"(?<![\w#&/])#([\w][\w/-]*)")
LIST_ITEM = re.compile(r"^\s*([-*+]|\d+\.)\s")


class ObsidianError(RuntimeError):
    pass


def _enabled(cfg: Any) -> bool:
    return cfg.obsidian.enabled


def _vault(cfg) -> Path:
    vault = cfg.obsidian.path
    if not vault or not vault.is_dir():
        raise ObsidianError(f"Der Obsidian-Vault '{cfg.obsidian.vault}' existiert nicht (obsidian.vault in der Config).")
    return vault.resolve()


def _inside(vault: Path, path: Path) -> Path:
    """Pfad auflösen und sicherstellen, dass er im Vault liegt."""
    p = path.resolve()
    if p != vault and not p.is_relative_to(vault):
        raise ObsidianError("Pfade außerhalb des Obsidian-Vaults sind nicht erlaubt.")
    return p


def _rel(vault: Path, path: Path) -> str:
    return path.relative_to(vault).as_posix()


def iter_notes(vault: Path):
    """Alle Markdown-Notizen, ohne versteckte Ordner (.obsidian, .trash, .git)."""
    for root, dirs, files in os.walk(vault):
        dirs[:] = sorted(d for d in dirs if not d.startswith("."))
        for name in files:
            if name.lower().endswith(".md") and not name.startswith("."):
                yield Path(root) / name


def _read(path: Path) -> str:
    try:
        if path.stat().st_size > MAX_FILE_BYTES:
            return ""
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def split_frontmatter(text: str) -> tuple[dict, str, str]:
    """(Frontmatter als dict, roher Frontmatter-Block inkl. ---, Rest)."""
    m = re.match(r"^---\r?\n(.*?)\r?\n---[ \t]*(?:\r?\n|$)", text, re.S)
    if not m:
        return {}, "", text
    try:
        meta = yaml.safe_load(m.group(1)) or {}
    except yaml.YAMLError:
        meta = {}
    return (meta if isinstance(meta, dict) else {}), m.group(0), text[m.end():]


def note_tags(text: str) -> set[str]:
    meta, _, body = split_frontmatter(text)
    raw = meta.get("tags") or meta.get("tag") or []
    if isinstance(raw, str):
        raw = re.split(r"[,\s]+", raw)
    tags = {str(t).lstrip("#").lower() for t in raw if t}
    body = re.sub(r"```.*?```", "", body, flags=re.S)
    tags |= {t.lower() for t in INLINE_TAG.findall(body)}
    return tags


def _when(path: Path) -> str:
    return datetime.fromtimestamp(path.stat().st_mtime).strftime("%d.%m.%Y %H:%M")


def _snippet(body: str, stems: set[str], width: int = 200) -> str:
    flat = " ".join(body.split())
    low = flat.lower()
    pos = min((i for i in (low.find(s) for s in stems) if i >= 0), default=0)
    start = max(0, pos - width // 3)
    text = flat[start:start + width]
    return ("…" if start else "") + text + ("…" if start + width < len(flat) else "")


def resolve_note(vault: Path, ref: str) -> Path:
    """Notiz per relativem Pfad (mit oder ohne .md) oder Titel finden."""
    ref = ref.strip().strip("[]").split("|")[0].strip()
    if not ref:
        raise ObsidianError("Bitte angeben, welche Notiz gemeint ist.")
    for cand in (ref, ref + ".md"):
        p = vault / cand
        if p.is_file() and p.suffix.lower() == ".md":
            return _inside(vault, p)
    name = Path(ref).name.removesuffix(".md").lower()
    notes = list(iter_notes(vault))
    for matches in ([n for n in notes if n.stem.lower() == name], [n for n in notes if name in n.stem.lower()]):
        if len(matches) == 1:
            return matches[0]
        if matches:
            listing = ", ".join(_rel(vault, n) for n in matches[:8])
            raise ObsidianError(f"Mehrere Notizen passen zu '{ref}': {listing}. Bitte den Pfad angeben.")
    raise ObsidianError(f"Keine Notiz namens '{ref}' im Obsidian-Vault gefunden.")


def safe_filename(title: str) -> str:
    name = BAD_NAME_CHARS.sub(" ", title).strip(" .")
    return re.sub(r"\s+", " ", name)[:120] or "Neue Notiz"


def _split_tags(tags: str) -> list[str]:
    return [t.lstrip("#") for t in re.split(r"[,\s]+", tags or "") if t.lstrip("#")]


async def _guard(coro) -> str:
    try:
        return await coro
    except ObsidianError as e:
        return str(e)


def _embedder(ctx: ToolContext):
    llm = getattr(ctx.memory, "llm", None)
    return getattr(llm, "embed", None)


# ---------------------------------------------------------------- Tools

@tool("Durchsucht die Obsidian-Notizen (Titel und Inhalt). Ohne Suchbegriff: zuletzt geänderte. Liefert Pfade.", enabled=_enabled)
async def obsidian_search(
    ctx: ToolContext,
    query: str = "",
    folder: Annotated[str, "Nur in diesem Ordner"] = "",
    tag: Annotated[str, "Nur mit diesem Tag"] = "",
    limit: int = 8,
) -> str:
    async def run() -> str:
        vault = _vault(ctx.cfg)
        base = _inside(vault, vault / folder.strip()) if folder.strip() else vault
        if not base.is_dir():
            raise ObsidianError(f"Ordner '{folder}' gibt es im Vault nicht.")
        q = query.strip().lower()
        stems = _words(q) or ({q} if q else set())
        want_tag = tag.strip().lstrip("#").lower()
        hits: list[tuple[float, float, Path, str]] = []
        for path in iter_notes(base):
            text = _read(path)
            if want_tag and not any(t == want_tag or t.startswith(want_tag + "/") for t in note_tags(text)):
                continue
            mtime = path.stat().st_mtime
            if not stems:
                hits.append((0.0, mtime, path, text))
                continue
            title, low = path.stem.lower(), text.lower()
            score = sum(5 for s in stems if s in title)
            score += sum(1 + math.log1p(low.count(s)) for s in stems if s in low)
            if q and q in title:
                score += 5
            if score > 0:
                hits.append((score, mtime, path, text))
        if not hits:
            return f"Keine Obsidian-Notizen zu '{query or tag or folder}' gefunden."
        hits.sort(key=lambda h: (-h[0], -h[1]))
        n = max(1, min(int(limit), 25))
        lines = [f"{len(hits)} Notiz(en) gefunden" + (f" (zeige {n})" if len(hits) > n else "") + ":"]
        for _, _, path, text in hits[:n]:
            body = split_frontmatter(text)[2]
            line = f"- {_rel(vault, path)} (geändert {_when(path)})"
            snippet = _snippet(body, stems)
            if snippet.strip():
                line += f"\n  {snippet}"
            lines.append(line)
        lines.append("Ganzer Text: obsidian_read(note) · Frage zur Notiz: obsidian_ask(note, question)")
        return "\n".join(lines)

    return await _guard(run())


@tool("Liest eine Obsidian-Notiz (Pfad oder Titel), lange abschnittsweise.", enabled=_enabled)
async def obsidian_read(
    ctx: ToolContext,
    note: Annotated[str, "Pfad oder Titel"],
    offset: Annotated[int, "Ab diesem Zeichen weiterlesen"] = 0,
) -> str:
    async def run() -> str:
        vault = _vault(ctx.cfg)
        path = resolve_note(vault, note)
        text = _read(path)
        start = max(0, int(offset))
        limit = ctx.limit(ctx.cfg.obsidian.max_chars)
        part = text[start:start + limit]
        rest = len(text) - start - len(part)
        more = f"\n… noch {rest} Zeichen – weiter mit offset={start + len(part)}" if rest > 0 else ""
        return f"Notiz: {_rel(vault, path)} (geändert {_when(path)})\n---\n{part.strip() or '(leer)'}{more}"

    return await _guard(run())


@tool("Liefert die zur Frage relevantesten Stellen einer Obsidian-Notiz; daraus antworten.", enabled=_enabled)
async def obsidian_ask(
    ctx: ToolContext,
    note: Annotated[str, "Pfad oder Titel"],
    question: str,
) -> str:
    async def run() -> str:
        vault = _vault(ctx.cfg)
        path = resolve_note(vault, note)
        body = split_frontmatter(_read(path))[2].strip()
        head = f"Notiz: {_rel(vault, path)} (geändert {_when(path)})"
        if not body:
            return f"{head}\n(Die Notiz ist leer.)"
        budget = ctx.limit(ctx.cfg.obsidian.max_chars)  # kleines Kontextfenster: weniger auf einmal
        if len(body) <= budget:
            return f"{head}\n--- vollständiger Text ---\n{body}"
        passages = split_passages(body)
        order = await rank_passages(question, passages, _embedder(ctx))
        chosen = select_passages(passages, order, budget)
        parts = [f"[Stelle {i + 1}/{len(passages)}]\n{passages[i]}" for i in chosen]
        return (f"{head}\n--- die {len(chosen)} relevantesten von {len(passages)} Textstellen zur Frage "
                f"„{question}“ ---\n" + "\n\n".join(parts) +
                "\n(Steht die Antwort nicht darin: obsidian_read für den ganzen Text.)")

    return await _guard(run())


@tool("Legt eine Obsidian-Notiz an (Standard: Inbox-Ordner), Inhalt in Markdown.", enabled=_enabled)
async def obsidian_create_note(
    ctx: ToolContext,
    title: Annotated[str, "Wird der Dateiname"],
    content: str,
    folder: Annotated[str, "Ordner im Vault; leer = Inbox"] = "",
    tags: Annotated[str, "Kommagetrennt"] = "",
) -> str:
    async def run() -> str:
        vault = _vault(ctx.cfg)
        target_dir = _inside(vault, vault / (folder.strip().strip("/") or ctx.cfg.obsidian.inbox))
        name = safe_filename(title)
        path = target_dir / f"{name}.md"
        n = 2
        while path.exists():
            path = target_dir / f"{name} ({n}).md"
            n += 1
        meta = {"created": datetime.now().strftime("%Y-%m-%dT%H:%M")}
        tag_list = _split_tags(tags)
        if tag_list:
            meta["tags"] = tag_list
        front = "---\n" + yaml.safe_dump(meta, allow_unicode=True, sort_keys=False, default_flow_style=None) + "---\n"
        target_dir.mkdir(parents=True, exist_ok=True)
        path.write_text(front + "\n" + content.strip() + "\n", encoding="utf-8")
        return f"Notiz '{path.stem}' angelegt: {_rel(vault, path)}"

    return await _guard(run())


@tool("Hängt Text an eine Obsidian-Notiz an (Einkaufsliste, Logbuch …).", enabled=_enabled)
async def obsidian_append(
    ctx: ToolContext,
    note: Annotated[str, "Pfad oder Titel"],
    content: str,
) -> str:
    async def run() -> str:
        vault = _vault(ctx.cfg)
        path = resolve_note(vault, note)
        old = _read(path).rstrip("\n")
        new = content.strip("\n")
        last = old.splitlines()[-1] if old.strip() else ""
        first = new.splitlines()[0] if new else ""
        sep = "" if not old.strip() else ("\n" if LIST_ITEM.match(last) and LIST_ITEM.match(first) else "\n\n")
        path.write_text(old + sep + new + "\n", encoding="utf-8")
        return f"An '{path.stem}' angehängt ({_rel(vault, path)})."

    return await _guard(run())


def _update_risk(ctx: ToolContext, args: dict) -> tuple[str, str]:
    note = args.get("note", "")
    return CONFIRM, T(f"überschreibt den Inhalt der Obsidian-Notiz '{note}'", f"overwrites the Obsidian note '{note}'")


@tool("Ersetzt den ganzen Inhalt einer Obsidian-Notiz (Frontmatter bleibt). Zum Ergänzen obsidian_append.", risk=_update_risk, enabled=_enabled)
async def obsidian_update_note(
    ctx: ToolContext,
    note: Annotated[str, "Pfad oder Titel"],
    content: str,
) -> str:
    async def run() -> str:
        vault = _vault(ctx.cfg)
        path = resolve_note(vault, note)
        _, front, _ = split_frontmatter(_read(path))
        body = content.strip() + "\n"
        if front and not content.lstrip().startswith("---"):
            body = front.rstrip("\n") + "\n\n" + body
        path.write_text(body, encoding="utf-8")
        return f"Notiz '{path.stem}' aktualisiert."

    return await _guard(run())


@tool("Öffnet eine Notiz in der Obsidian-App.",
      enabled=_enabled)
async def obsidian_open(
    ctx: ToolContext,
    note: Annotated[str, "Pfad oder Titel"],
) -> str:
    async def run() -> str:
        vault = _vault(ctx.cfg)
        path = resolve_note(vault, note)
        opener = next((c for c in (["xdg-open"], ["gio", "open"]) if shutil.which(c[0])), None)
        if not opener:
            return "Weder xdg-open noch gio ist installiert (Paket xdg-utils)."
        target = str(path)
        if shutil.which("xdg-mime"):
            _, handler = await proc.run(ctx, ["xdg-mime", "query", "default", "x-scheme-handler/obsidian"],
                                        timeout=5, stream=False)
            if handler.strip():
                target = f"obsidian://open?vault={quote(vault.name)}&file={quote(_rel(vault, path))}"
        ok, err = await proc.launch([*opener, target])
        if ok:
            return f"Geöffnet: {_rel(vault, path)}" + (" in Obsidian" if target.startswith("obsidian:") else "")
        return f"Öffnen fehlgeschlagen ({err})."

    return await _guard(run())


async def obsidian_status(cfg) -> dict:
    """Für orbwise doctor und /api/status."""
    if not cfg.obsidian.vault:
        return {"enabled": False, "online": False}
    vault = cfg.obsidian.path
    if not vault.is_dir():
        return {"enabled": True, "online": False, "vault": str(vault),
                "error": f"Ordner {vault} nicht gefunden (obsidian.vault in der Config)"}
    return {"enabled": True, "online": True, "vault": str(vault), "count": sum(1 for _ in iter_notes(vault))}
