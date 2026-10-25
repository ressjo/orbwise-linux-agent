"""Werkzeuge des Coding-Modus – nach dem Vorbild von OpenCode 2 (read, edit, write, grep, glob, shell, websearch,
webfetch, subagent, question).

Unterschiede zu den Werkzeugen des Tools-Modus (files.py, shell.py):
- read zeigt Zeilennummern („12: inhalt“) und liest auch Ordner; AGENTS.md aus Unterordnern kommt einmal mit.
- edit verzeiht typografische Anführungszeichen/Striche und abweichende Einrückung, solange die Stelle eindeutig
  ist, und liefert einen Diff. Wurde die Datei seit dem letzten read außerhalb geändert, muss neu gelesen werden.
- grep liefert Treffer-Zeilen mit Nummern (ripgrep, Regex), glob sucht Pfade nach Muster.
- shell kennt workdir und background (Job, siehe jobs.py).
Websuche läuft über dieselben Wege wie web_search (zuerst die Brave-API).
"""

from __future__ import annotations

import asyncio
import dataclasses
import difflib
import errno
import fnmatch
import json
import os
import re
import shutil
import time
from pathlib import Path
from typing import Annotated

from . import jobs, proc
from .files import READ_MAX_BYTES, SKIP_DIRS, _human_size
from .registry import BLOCKED, CONFIRM, SAFE, ToolContext, tool
from .safety import apply_privilege
from .secretpaths import is_secret_path, secret_reason

LINE_MAX = 2000  # längere Zeilen werden gekürzt (minifizierte Dateien)
AGENTS_FILES = ("AGENTS.md",)
AGENTS_CHARS = 4000
SEARCH_LIMIT = 100


# ---------------------------------------------------------------- Hilfen
def _meta(ctx: ToolContext) -> dict:
    try:
        return ctx.memory.conversation.meta
    except AttributeError:
        return {}


def _remember_read(ctx: ToolContext, p: Path) -> None:
    """Lesezeitpunkt merken – edit erkennt so Änderungen von außen (Editor, git checkout) seit dem Lesen."""
    times = _meta(ctx).setdefault("read_times", {})
    try:
        times[str(p.resolve())] = p.stat().st_mtime_ns
    except OSError:
        return
    while len(times) > 200:
        times.pop(next(iter(times)))


def _stale(ctx: ToolContext, p: Path) -> bool:
    seen = _meta(ctx).get("read_times", {}).get(str(p.resolve()))
    try:
        return seen is not None and seen != p.stat().st_mtime_ns
    except OSError:
        return False


def _rel(ctx: ToolContext, p: Path | str) -> str:
    """Pfad relativ zum Projektordner (kürzer im Prompt), sonst absolut."""
    p = Path(p)
    if ctx.cwd:
        try:
            return str(p.resolve().relative_to(Path(ctx.cwd).resolve())) or "."
        except (ValueError, OSError):
            pass
    return str(p)


def _root(ctx: ToolContext, path: str) -> Path:
    return ctx.path(path) if path else Path(ctx.cwd or os.path.expanduser("~"))


def _secret_risk(ctx: ToolContext, args: dict) -> tuple[str, str]:
    path = args.get("path") or ""
    if path and is_secret_path(str(ctx.path(path))):
        return BLOCKED, secret_reason()
    return SAFE, ""


def diff_text(old: str, new: str, name: str, context: int = 2, max_lines: int = 80) -> tuple[str, int, int]:
    """Knapper Unified-Diff (höchstens max_lines Zeilen) und die Zahl hinzugefügter/entfernter Zeilen."""
    lines = list(difflib.unified_diff(old.splitlines(), new.splitlines(), f"a/{name}", f"b/{name}",
                                      n=context, lineterm=""))
    added = sum(1 for ln in lines if ln.startswith("+") and not ln.startswith("+++"))
    removed = sum(1 for ln in lines if ln.startswith("-") and not ln.startswith("---"))
    if len(lines) > max_lines:
        lines = lines[:max_lines] + [f"… ({len(lines) - max_lines} weitere Diff-Zeilen)"]
    return "\n".join(lines), added, removed


def _agents_note(ctx: ToolContext, target: Path, is_dir: bool) -> str:
    """AGENTS.md zwischen der gelesenen Stelle und dem Projektordner (exklusiv – die des Projekts steht schon im
    Systemprompt), jede nur einmal je Chat."""
    if not ctx.cwd:
        return ""
    try:
        root = Path(ctx.cwd).resolve()
        here = target.resolve() if is_dir else target.resolve().parent
    except OSError:
        return ""
    if here != root and root not in here.parents:
        return ""
    shown = _meta(ctx).setdefault("agents_shown", [])
    notes = []
    while here != root:
        for name in AGENTS_FILES:
            f = here / name
            if f.is_file() and str(f) not in shown and f != target.resolve():
                shown.append(str(f))
                try:
                    text = f.read_text(encoding="utf-8", errors="replace").strip()
                except OSError:
                    continue
                notes.append(f"[Anweisungen aus {_rel(ctx, f)} – gelten für diesen Ordner]\n{text[:AGENTS_CHARS]}")
        here = here.parent
    return ("\n\n" + "\n\n".join(reversed(notes))) if notes else ""


# ---------------------------------------------------------------- read
def _list_dir(ctx: ToolContext, p: Path, offset: int, limit: int) -> str:
    try:
        entries = sorted(p.iterdir(), key=lambda e: (not e.is_dir(), e.name.lower()))
    except PermissionError:
        return f"Keine Berechtigung für {p}."
    names = [e.name + ("/" if e.is_dir() else "") for e in entries]
    if not names:
        return f"{_rel(ctx, p)}/ ist leer."
    start = max(1, offset)
    chosen = names[start - 1:start - 1 + max(1, min(limit, 2000))]
    end = start - 1 + len(chosen)
    more = f" – weiter mit offset={end + 1}" if end < len(names) else ""
    return f"[{_rel(ctx, p)}/: Einträge {start}–{end} von {len(names)}{more}]\n" + "\n".join(chosen)


def _missing(ctx: ToolContext, raw: str, p: Path) -> str:
    base = p.name.lower()
    try:
        near = [e.name for e in p.parent.iterdir() if base and (base in e.name.lower() or e.name.lower() in base)]
    except OSError:
        near = []
    if not near:
        try:
            names = [e.name for e in p.parent.iterdir()]
        except OSError:
            names = []
        near = difflib.get_close_matches(p.name, names, n=3, cutoff=0.6)
    hint = (" Meintest du: " + ", ".join(str(Path(raw).parent / n) for n in near[:3]) + "?") if near else ""
    return f"Nicht gefunden: {raw}.{hint}"


@tool("Liest eine Datei oder einen Ordner. Jede Zeile beginnt mit ihrer Nummer als „<zeile>: “ – das Präfix gehört "
      "nicht zum Inhalt. Ordner: ein Eintrag je Zeile (Ordner mit /, versteckte inklusive). Große Dateien in "
      "Abschnitten mit offset/limit lesen; lieber einmal mehr lesen als viele kleine Stücke, und mit grep gezielt "
      "suchen. Auch PDFs.", risk=_secret_risk, name="read")
async def code_read(
    ctx: ToolContext,
    path: Annotated[str, "Datei oder Ordner (relativ zum Projekt oder absolut)"],
    offset: Annotated[int, "Optional: ab dieser Zeile bzw. diesem Eintrag (ab 1)"] = 1,
    limit: Annotated[int, "Optional: höchstens so viele Zeilen/Einträge (Standard und Höchstwert 2000)"] = 2000,
) -> str:
    p = ctx.path(path)
    limit = max(1, min(int(limit or 2000), 2000))
    offset = max(1, int(offset or 1))
    if p.is_dir():
        return _list_dir(ctx, p, offset, limit) + _agents_note(ctx, p, True)
    if not p.is_file():
        return _missing(ctx, path, p)
    try:
        with p.open("rb") as f:
            data = f.read(READ_MAX_BYTES)
    except PermissionError:
        return f"Keine Leseberechtigung für {p}."
    if data[:5] == b"%PDF-":
        from ..attachments import pdf_text
        try:
            text, pages = pdf_text(data)
        except ValueError as e:
            return str(e)
        _remember_read(ctx, p)
        return proc.clip_saved(f"[{_rel(ctx, p)}: PDF, {pages} Seiten]\n{text}", ctx.limit(), "pdf")
    if b"\x00" in data[:4096]:
        return f"{_rel(ctx, p)} ist eine Binärdatei ({_human_size(p.stat().st_size)})."
    text = data.decode(errors="replace")
    _remember_read(ctx, p)
    if not text:
        return f"[{_rel(ctx, p)}: leere Datei]"
    lines = text.splitlines()
    if offset > len(lines):
        return f"offset={offset} liegt hinter dem Dateiende ({len(lines)} Zeilen)."
    room = ctx.limit() * 4 // 3  # die Rückfall-Kürzung im Agenten lässt ⅓ mehr durch – nie mitten in einer Zeile
    chosen, used = [], 0
    for n in range(offset, len(lines) + 1):
        line = lines[n - 1]
        if len(line) > LINE_MAX:
            line = line[:LINE_MAX] + f" … (+{len(line) - LINE_MAX} Zeichen)"
        row = f"{n}: {line}"
        if len(chosen) >= limit or (chosen and used + len(row) + 1 > room):
            break
        chosen.append(row)
        used += len(row) + 1
    end = offset + len(chosen) - 1
    cut = " – Datei größer als gelesen" if len(data) >= READ_MAX_BYTES else ""
    if end < len(lines):
        foot = f"(Zeilen {offset}–{end} von {len(lines)}{cut} – weiter mit offset={end + 1})"
    else:
        foot = f"(Ende der Datei – {len(lines)} Zeilen{cut})"
    return "\n".join(chosen) + "\n" + foot + _agents_note(ctx, p, False)


# ---------------------------------------------------------------- edit / write
_QUOTES = str.maketrans({"‘": "'", "’": "'", "‚": "'", "‛": "'", "“": '"', "”": '"', "„": '"', "‟": '"',
                         "‐": "-", "‑": "-", "‒": "-", "–": "-", "—": "-", "―": "-", "−": "-",
                         " ": " ", " ": " ", " ": " ", " ": " ", "　": " "})
_LINE_PREFIX = re.compile(r"^\s*\d+: ?")


def _normalize(text: str) -> str:
    return text.translate(_QUOTES)  # jedes Zeichen bleibt ein Zeichen – Positionen stimmen weiter


def _strip_prefixes(text: str) -> str:
    """Versehentlich aus read übernommene „12: “-Präfixe entfernen – nur, wenn wirklich jede Zeile eins hat."""
    lines = text.split("\n")
    filled = [ln for ln in lines if ln.strip()]
    if filled and all(_LINE_PREFIX.match(ln) for ln in filled):
        return "\n".join(_LINE_PREFIX.sub("", ln, count=1) if ln.strip() else ln for ln in lines)
    return text


def _find_all(text: str, needle: str) -> list[tuple[int, int]]:
    out, i = [], text.find(needle)
    while needle and i != -1:
        out.append((i, i + len(needle)))
        i = text.find(needle, i + len(needle))
    return out


def _indent(line: str) -> str:
    return line[:len(line) - len(line.lstrip())]


def _line_matches(text: str, old: str) -> list[tuple[int, int, str]]:
    """Zeilenweise ohne Einrückung vergleichen: (Start, Ende, tatsächliche Einrückung der ersten Zeile)."""
    want = old.split("\n")
    while want and not want[-1].strip():
        want.pop()
    if not want:
        return []
    lines = text.split("\n")
    offsets, pos = [], 0
    for ln in lines:
        offsets.append(pos)
        pos += len(ln) + 1
    want_s = [ln.strip() for ln in want]
    hits = []
    for i in range(len(lines) - len(want) + 1):
        if all(lines[i + k].strip() == want_s[k] for k in range(len(want))):
            last = i + len(want) - 1
            hits.append((offsets[i], offsets[last] + len(lines[last]), _indent(lines[i])))
    return hits


def _reindent(new: str, have: str, want: str) -> str:
    """newString an die echte Einrückung anpassen, wenn das Modell sie in oldString falsch hatte."""
    if have == want:
        return new
    out = []
    for ln in new.split("\n"):
        if not ln.strip():
            out.append(ln)
        elif ln.startswith(have):
            out.append(want + ln[len(have):])
        else:
            out.append(want + ln.lstrip() if not have else ln)
    return "\n".join(out)


def plan_edit(ctx: ToolContext, args: dict) -> tuple[Path, str | None, str, str]:
    """Rechnet die Änderung aus, ohne zu schreiben: (Pfad, alter Inhalt oder None, neuer Inhalt, Fehler)."""
    p = ctx.path(args.get("path", ""))
    old = str(args.get("oldString", "")).replace("\r\n", "\n")
    new = str(args.get("newString", "")).replace("\r\n", "\n")
    if is_secret_path(str(p)):
        return p, None, "", secret_reason()
    if old == new:
        return p, None, "", "oldString und newString sind gleich – nichts zu ändern."
    if not p.exists():
        if old:
            return p, None, "", (f"{_rel(ctx, p)} gibt es nicht. Neue Dateien mit write anlegen (oder edit mit leerem "
                                 "oldString).")
        return p, None, new, ""
    if not p.is_file():
        return p, None, "", f"{_rel(ctx, p)} ist keine Datei."
    try:
        raw = p.read_bytes().decode("utf-8")
    except UnicodeDecodeError:
        return p, None, "", f"{_rel(ctx, p)} ist keine UTF-8-Textdatei."
    except OSError as e:
        return p, None, "", f"{_rel(ctx, p)} lässt sich nicht lesen: {e}"
    if _stale(ctx, p):
        return p, None, "", (f"{_rel(ctx, p)} wurde seit dem letzten read geändert (Editor, git …) – erst mit read neu "
                             "lesen, dann ändern.")
    crlf = "\r\n" in raw
    text = raw.replace("\r\n", "\n")
    if not old:
        if text.strip():
            return p, None, "", "oldString ist leer, die Datei aber nicht – die zu ersetzende Stelle angeben."
        result = new
    else:
        old2 = _strip_prefixes(old)
        if old2 != old:
            old, new = old2, _strip_prefixes(new)
        spans = [(a, b, None) for a, b in _find_all(text, old)]
        if not spans:
            spans = [(a, b, None) for a, b in _find_all(_normalize(text), _normalize(old))]
        if not spans:
            spans = _line_matches(text, old)
        if not spans:
            first = old.strip().splitlines()[0].strip() if old.strip() else old
            lines = text.split("\n")
            close = difflib.get_close_matches(first, [ln.strip() for ln in lines], n=1, cutoff=0.6)
            hint = ""
            if close:
                n = next(i for i, ln in enumerate(lines, 1) if ln.strip() == close[0])
                hint = f" Ähnlichste Zeile {n}: {lines[n - 1]!r}"
            return p, None, "", (f"oldString kommt in {_rel(ctx, p)} nicht vor – mit read nachsehen und den Ausschnitt "
                                 f"exakt übernehmen (ohne Zeilennummern).{hint}")
        if len(spans) > 1 and not args.get("replaceAll"):
            where = ", ".join(str(text.count("\n", 0, a) + 1) for a, _b, _i in spans[:8])
            return p, None, "", (f"oldString kommt {len(spans)}-mal vor (Zeilen {where}) – mehr umgebenden Text "
                                 "mitgeben, damit die Stelle eindeutig ist, oder replaceAll=true.")
        result = text
        for a, b, actual in sorted(spans, reverse=True):
            piece = new if actual is None else _reindent(new, _indent(old.split("\n")[0]), actual)
            result = result[:a] + piece + result[b:]
    if crlf:
        result = result.replace("\n", "\r\n")
    return p, raw, result, ""


def _edit_risk(ctx: ToolContext, args: dict) -> tuple[str, str]:
    if not args.get("path"):
        return SAFE, ""
    p = ctx.path(args["path"])
    if is_secret_path(str(p)):
        return BLOCKED, secret_reason()
    return CONFIRM, f"{'ändert' if p.exists() else 'erstellt'} {_rel(ctx, p)}"


def edit_preview(ctx: ToolContext, args: dict) -> str:
    p, before, after, error = plan_edit(ctx, args)
    if error:
        return error
    return diff_text(before or "", after, _rel(ctx, p))[0]


def _write_file(ctx: ToolContext, p: Path, before: str | None, after: str) -> str:
    p.parent.mkdir(parents=True, exist_ok=True)
    if ctx.sandbox:
        error = _write_in_copy(ctx.sandbox["path"], p, after.encode("utf-8"))
        if error:
            return error
    else:
        p.write_bytes(after.encode("utf-8"))
    _remember_read(ctx, p)
    diff, added, removed = diff_text((before or "").replace("\r\n", "\n"), after.replace("\r\n", "\n"), _rel(ctx, p))
    head = f"{'Geändert' if before is not None else 'Angelegt'}: {_rel(ctx, p)} (+{added} −{removed})"
    return head + ("\n" + diff if diff else "")


def _write_in_copy(ws: str, p: Path, data: bytes) -> str | None:
    """Sandbox: Der Container könnte eine Datei der Arbeitskopie zwischen Freigabe und Schreiben gegen einen Symlink
    auf eine Datei des Rechners tauschen – darum direkt vor dem Schreiben erneut prüfen und keinem Link am Ziel folgen.
    (Freigegebene Dateien außerhalb der Kopie werden normal geschrieben.)"""
    root = Path(os.path.abspath(ws))
    if not Path(os.path.abspath(p)).is_relative_to(root):
        p.write_bytes(data)
        return None
    if p.is_symlink() and p.resolve().is_relative_to(root.resolve()):
        p = p.resolve()  # Link innerhalb der Kopie: auf sein Ziel schreiben
    if not p.parent.resolve().is_relative_to(root.resolve()):
        return f"Nicht geschrieben: {p} zeigt aus der Arbeitskopie hinaus (Symlink)."
    try:
        fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o666)
    except OSError as e:
        if e.errno == errno.ELOOP:
            return f"Nicht geschrieben: {p} ist ein Symlink (zeigt womöglich aus der Arbeitskopie hinaus)."
        raise
    with os.fdopen(fd, "wb") as f:
        f.write(data)
    return None


@tool("Ändert eine Datei durch Ersetzen von exaktem Text. Ausschnitte aus read ohne das Zeilen-Präfix „12: “ und "
      "mit genau derselben Einrückung übernehmen. Schlägt fehl, wenn oldString nicht vorkommt; kommt er mehrfach vor, "
      "mehr Kontext mitgeben oder replaceAll=true (z. B. zum Umbenennen). Nur die nötige Stelle ersetzen – jede in "
      "newString fehlende Zeile wird gelöscht.", risk=_edit_risk, name="edit", preview=lambda c, a: edit_preview(c, a))
async def code_edit(
    ctx: ToolContext,
    path: Annotated[str, "Datei"],
    oldString: Annotated[str, "Exakter Text, der ersetzt wird (leer nur für eine neue/leere Datei)"],
    newString: Annotated[str, "Neuer Text an seiner Stelle (muss sich von oldString unterscheiden)"],
    replaceAll: Annotated[bool, "Optional: jedes Vorkommen ersetzen (Standard false: muss eindeutig sein)"] = False,
) -> str:
    p, before, after, error = plan_edit(ctx, {"path": path, "oldString": oldString, "newString": newString,
                                              "replaceAll": replaceAll})
    if error:
        return error
    return _write_file(ctx, p, before, after)


def _write_risk(ctx: ToolContext, args: dict) -> tuple[str, str]:
    if not args.get("path"):
        return SAFE, ""
    p = ctx.path(args["path"])
    if is_secret_path(str(p)):
        return BLOCKED, secret_reason()
    return CONFIRM, f"{'überschreibt' if p.exists() else 'erstellt'} {_rel(ctx, p)}"


def _read_text(p: Path) -> str | None:
    try:
        return p.read_text(encoding="utf-8") if p.is_file() else None
    except (OSError, UnicodeDecodeError):
        return None


def write_preview(ctx: ToolContext, args: dict) -> str:
    p = ctx.path(args.get("path", ""))
    return diff_text((_read_text(p) or "").replace("\r\n", "\n"), str(args.get("content", "")), _rel(ctx, p))[0]


@tool("Schreibt eine Datei (legt sie an oder überschreibt sie komplett); fehlende Ordner werden angelegt. Für "
      "Teiländerungen an bestehenden Dateien edit nutzen.", risk=_write_risk, name="write",
      preview=lambda c, a: write_preview(c, a))
async def code_write(
    ctx: ToolContext,
    path: Annotated[str, "Zieldatei"],
    content: Annotated[str, "Vollständiger Inhalt"],
) -> str:
    p = ctx.path(path)
    if p.exists() and not p.is_file():
        return f"{_rel(ctx, p)} ist ein Ordner."
    if p.is_file() and _stale(ctx, p):
        return f"{_rel(ctx, p)} wurde seit dem letzten read geändert – erst mit read neu lesen."
    return _write_file(ctx, p, _read_text(p) if p.exists() else None, content)


# ---------------------------------------------------------------- grep / glob
def _py_grep(root: Path, rx: re.Pattern, include: str, limit: int, deadline: float) -> list[tuple[str, int, str]]:
    hits: list[tuple[str, int, str]] = []
    files = [root] if root.is_file() else None
    walker = ((str(root.parent), [], [root.name]),) if files else os.walk(root, onerror=lambda e: None)
    for dirpath, dirnames, filenames in walker:
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")]
        for name in filenames:
            if include and not fnmatch.fnmatch(name, include):
                continue
            path = os.path.join(dirpath, name)
            try:
                if os.path.getsize(path) > 5_000_000:
                    continue
                with open(path, "rb") as f:
                    data = f.read()
            except OSError:
                continue
            if b"\x00" in data[:4096]:
                continue
            for n, line in enumerate(data.decode(errors="replace").splitlines(), 1):
                if rx.search(line):
                    hits.append((path, n, line))
                    if len(hits) >= limit:
                        return hits
            if time.monotonic() > deadline:
                return hits
    return hits


@tool("Durchsucht Dateiinhalte mit ripgrep (Regex oder mit literal=true wörtlich) und liefert Datei, Zeilennummer "
      "und Zeile jedes Treffers. Mit path/include eingrenzen. Beachtet .gitignore.", name="grep")
async def code_grep(
    ctx: ToolContext,
    pattern: Annotated[str, "Regulärer Ausdruck (ripgrep-Syntax) bzw. Text bei literal=true"],
    path: Annotated[str, "Optional: Datei oder Ordner (Standard: Projektordner)"] = "",
    include: Annotated[str, "Optional: Dateimuster, z. B. '*.py' oder '*.{ts,tsx}'"] = "",
    literal: Annotated[bool, "Optional: pattern wörtlich statt als Regex"] = False,
    caseSensitive: Annotated[bool, "Optional: Groß/Kleinschreibung beachten (Standard true)"] = True,
    limit: Annotated[int, "Optional: höchstens so viele Treffer-Zeilen (Standard 100)"] = SEARCH_LIMIT,
) -> str:
    root = _root(ctx, path)
    if not root.exists():
        return _missing(ctx, path, root)
    limit = max(1, min(int(limit or SEARCH_LIMIT), 500))
    rg = shutil.which("rg")
    hits: list[tuple[str, int, str]] = []
    if rg:
        argv = [rg, "--line-number", "--no-heading", "--color=never", "--max-columns", "300",
                "--max-columns-preview", "--max-filesize", "5M", "--max-count", str(limit)]
        argv += [] if caseSensitive else ["--ignore-case"]
        argv += ["--fixed-strings"] if literal else []
        argv += ["--glob", include] if include else []
        argv += ["-e", pattern, "--", str(root)]
        rc, out = await proc.run(ctx, argv, timeout=60, stream=False)
        if rc not in (0, 1):
            return f"grep fehlgeschlagen: {out.strip()[:500]}"
        for row in out.splitlines():
            m = re.match(r"^(.*?):(\d+):(.*)$", row) if not root.is_file() else re.match(r"^(\d+):(.*)$", row)
            if not m:
                continue
            if root.is_file():
                hits.append((str(root), int(m.group(1)), m.group(2)))
            else:
                hits.append((m.group(1), int(m.group(2)), m.group(3)))
    else:
        try:
            rx = re.compile(re.escape(pattern) if literal else pattern, 0 if caseSensitive else re.IGNORECASE)
        except re.error as e:
            return f"Ungültiger regulärer Ausdruck: {e}"
        hits = await asyncio.to_thread(_py_grep, root, rx, include, limit + 1, time.monotonic() + 30)
    if not hits:
        return f"Keine Treffer für {pattern!r}."
    total = len(hits)
    out_lines, last = [], None
    for path_, n, line in hits[:limit]:
        if path_ != last:
            out_lines.append(("" if last is None else "\n") + _rel(ctx, path_))
            last = path_
        line = line.strip()
        out_lines.append(f"  {n}: {line[:300]}")
    head = f"{min(total, limit)} Treffer" + (" (weitere gekürzt – genauer suchen)" if total > limit else "")
    return proc.clip_saved(head + "\n" + "\n".join(out_lines), ctx.limit(), "grep")


def _py_glob(root: Path, pattern: str, hidden: bool, limit: int) -> list[Path]:
    it = root.glob(pattern) if "/" in pattern or "**" in pattern else root.rglob(pattern)
    out = []
    for p in it:
        rel = p.relative_to(root).parts
        if any(part in SKIP_DIRS for part in rel) or (not hidden and any(part.startswith(".") for part in rel)):
            continue
        if p.is_file():
            out.append(p)
            if len(out) >= limit * 5:
                break
    return out


@tool("Sucht Dateipfade nach einem Glob-Muster (z. B. '**/*.py', 'src/**/*.tsx'); neueste zuerst. Beachtet "
      ".gitignore.", name="glob")
async def code_glob(
    ctx: ToolContext,
    pattern: Annotated[str, "Glob-Muster"],
    path: Annotated[str, "Optional: Ordner (Standard: Projektordner)"] = "",
    hidden: Annotated[bool, "Optional: versteckte Dateien/Ordner einschließen"] = False,
    limit: Annotated[int, "Optional: höchstens so viele Dateien (Standard 100)"] = SEARCH_LIMIT,
) -> str:
    root = _root(ctx, path)
    if not root.is_dir():
        return _missing(ctx, path, root) if not root.exists() else f"{_rel(ctx, root)} ist kein Ordner."
    limit = max(1, min(int(limit or SEARCH_LIMIT), 500))
    rg = shutil.which("rg")
    if rg:
        argv = [rg, "--files", "--color=never", "--glob", pattern] + (["--hidden", "--glob", "!.git"] if hidden else [])
        rc, out = await proc.run(ctx, argv, timeout=60, stream=False, cwd=str(root))
        if rc not in (0, 1):
            return f"glob fehlgeschlagen: {out.strip()[:500]}"
        paths = [root / ln for ln in out.splitlines() if ln.strip()]
    else:
        paths = await asyncio.to_thread(_py_glob, root, pattern, hidden, limit)
    if not paths:
        return f"Keine Dateien passen zu {pattern!r}."

    def mtime(p: Path) -> float:
        try:
            return p.stat().st_mtime
        except OSError:
            return 0.0
    paths.sort(key=mtime, reverse=True)
    more = f" (zeige {limit} – Muster genauer fassen)" if len(paths) > limit else ""
    return f"{len(paths)} Dateien{more}\n" + "\n".join(_rel(ctx, p) for p in paths[:limit])


# ---------------------------------------------------------------- shell
def _workdir(ctx: ToolContext, workdir: str) -> str:
    return str(ctx.path(workdir)) if workdir else (ctx.cwd or os.path.expanduser("~"))


def _shell_risk(ctx: ToolContext, args: dict) -> tuple[str, str]:
    from .shell import _risk
    return _risk(dataclasses.replace(ctx, cwd=_workdir(ctx, str(args.get("workdir") or ""))), args)


@tool("Führt einen Bash-Befehl aus (git, Build, Tests, Paketverwaltung …). Pfade mit Leerzeichen quoten; workdir "
      "statt „cd …&&“ verwenden. Für Dateien die eigenen Werkzeuge nehmen (read statt cat, edit statt sed, write "
      "statt echo >). Lange Ausgaben werden gekürzt und vollständig in einer Datei abgelegt. background=true für "
      "Dev-Server und lange Builds: kehrt sofort zurück, du wirst benachrichtigt, wenn der Job endet – nicht "
      "abfragen. Keine interaktiven Programme.", risk=_shell_risk, name="shell")
async def code_shell(
    ctx: ToolContext,
    command: Annotated[str, "Der Befehl"],
    workdir: Annotated[str, "Optional: Arbeitsordner (Standard: Projektordner)"] = "",
    timeout: Annotated[int, "Optional: Zeitlimit in Sekunden (Standard 120; im Hintergrund keins)"] = 0,
    background: Annotated[bool, "Optional: im Hintergrund starten und sofort zurückkehren"] = False,
) -> str:
    cwd = _workdir(ctx, workdir)
    if not os.path.isdir(cwd):
        return f"Arbeitsordner {workdir} gibt es nicht."
    limit = timeout if timeout and timeout > 0 else ctx.cfg.tools.shell_timeout
    limit = min(limit, ctx.cfg.tools.update_timeout)
    prefix = None
    if ctx.sandbox:  # Sandbox: im Container des Chats (Arbeitskopie unter demselben Pfad eingebunden)
        from .. import sandbox
        try:
            binary = await sandbox.ensure_running(ctx.cfg, ctx.sandbox, progress=lambda t: ctx.output(t + "\n"))
        except (sandbox.SandboxError, OSError) as e:
            return f"Sandbox nicht verfügbar: {e}"
        command = sandbox.strip_sudo(command)
        prefix = sandbox.exec_argv(binary, ctx.sandbox["name"], cwd, command, None if background else limit)
    else:
        command = apply_privilege(command, ctx.cfg.tools.privilege_cmd)
    if background:
        coding = getattr(ctx.cfg, "coding", None)
        jid, stop_argv = jobs.new_id(), None
        if prefix:
            from .. import sandbox
            prefix, stop_argv = sandbox.job_argv(prefix[0], ctx.sandbox["name"], cwd, command, jid)
        try:
            job = await jobs.start(command, cwd, limit=getattr(coding, "max_jobs", 4), emit=ctx.emit, argv=prefix,
                                   jid=jid, stop_argv=stop_argv)
        except RuntimeError as e:
            return str(e)
        if ctx.emit:
            await ctx.emit({"type": "job", "phase": "link", "id": job.id, "call": ctx.call_id})
        return (f"Läuft im Hintergrund als Job {job.id} (Ausgabe vollständig in {job.log}). Du bekommst eine Notiz, "
                "wenn er endet – nicht abfragen; bei Bedarf mit job_output hineinsehen oder ihn stoppen.")
    rc, out = await proc.run(ctx, prefix or command, timeout=limit + (15 if prefix else 0), cwd=cwd)
    if prefix and rc == 124:
        out += f"\n[Abgebrochen: Zeitlimit von {int(limit)} s überschritten]"
    return proc.format_result(rc, out, ctx.limit())


@tool("Zeigt Status und letzte Ausgabe eines Hintergrund-Jobs (aus shell mit background=true) oder beendet ihn.",
      name="job_output")
async def code_job_output(
    ctx: ToolContext,
    id: Annotated[str, "Job-ID"],
    stop: Annotated[bool, "Optional: den Job beenden"] = False,
) -> str:
    job = jobs.stop(id) if stop else jobs.JOBS.get(id)
    if not job:
        known = ", ".join(f"{j.id} ({j.command[:40]})" for j in jobs.JOBS.values()) or "keine"
        return f"Job {id} gibt es nicht. Bekannte Jobs: {known}"
    if stop:
        await asyncio.sleep(0.3)
    return job.summary(30)


# ---------------------------------------------------------------- Web (Brave-API zuerst)
@tool("Sucht im Internet (Dokumentation, Fehlermeldungen, aktuelle Versionen) und liefert Titel, URL und "
      "Kurztext der Treffer. Details danach mit webfetch.", name="websearch")
async def code_websearch(ctx: ToolContext, query: Annotated[str, "Suchanfrage"]) -> str:
    from .web import web_search
    return await web_search(ctx, query=query, max_results=8)


@tool("Ruft eine Webseite ab (Dokumentation, README, API-Referenz) und liefert ihren Inhalt als Markdown "
      "(format='text' oder 'html' möglich).", name="webfetch")
async def code_webfetch(
    ctx: ToolContext,
    url: Annotated[str, "Vollständige http(s)-URL"],
    format: Annotated[str, "Optional: markdown (Standard), text oder html"] = "markdown",
) -> str:
    from .web import fetch_page
    return await fetch_page(ctx, url, format if format in ("markdown", "text", "html") else "markdown")


# ---------------------------------------------------------------- Screenshot (headless Chromium)
BROWSERS = ("chromium", "chromium-browser", "google-chrome", "google-chrome-stable")
_LOCAL_URL = re.compile(r"^https?://(localhost|127\.0\.0\.1|0\.0\.0\.0|\[::1\])(:\d+)?(/|$)", re.I)


def _shot_target(ctx: ToolContext, url: str) -> str:
    """http(s)-URL bleibt, sonst Datei (relativ zum Projekt) als file://-URL."""
    url = url.strip()
    if re.match(r"^(https?|file)://", url, re.I):
        return url
    return ctx.path(url).resolve().as_uri()


def _shot_risk(ctx: ToolContext, args: dict) -> tuple[str, str]:
    raw = str(args.get("url") or "")
    if raw and not re.match(r"^https?://", raw, re.I):
        path = raw.removeprefix("file://")
        if is_secret_path(str(ctx.path(path))):
            return BLOCKED, secret_reason()
    return SAFE, ""


def _browser_argv(binary: str, url: str, out: str, width: int, height: int, profile: str, root: bool) -> list[str]:
    # eigenes, leeres Profil – nie das des Nutzers; als root (Container) geht Chromium nur ohne eigene Sandbox
    # --disable-dev-shm-usage: in Containern ist /dev/shm oft nur 64 MB – sonst stürzt der Renderer ohne Bild ab
    return [binary, "--headless=new", "--disable-gpu", "--disable-dev-shm-usage", "--hide-scrollbars", "--no-first-run",
            f"--user-data-dir={profile}", f"--window-size={width},{height}", "--virtual-time-budget=4000",
            f"--screenshot={out}", *(["--no-sandbox"] if root else []), url]


def _no_server(url: str) -> str:
    return (f"Unter {url} antwortet nichts – erst den Server starten (shell mit background=true, z. B. "
            "python -m http.server 8000), oder für eine einzelne HTML-Datei einfach den Dateipfad angeben.")


def _shot_error(url: str, log: str) -> str:
    """Chromiums eigener Grund (letzte Fehlerzeilen) statt eines geratenen – plus der häufigste Fall."""
    lines = [ln.strip() for ln in log.splitlines() if ln.strip() and ("ERROR" in ln or "error" in ln.lower())]
    why = " | ".join(lines[-3:])[-500:] or log.strip()[-300:]
    hint = (f" Läuft unter {url} wirklich ein Server (z. B. mit shell background=true gestartet)?"
            if url.startswith("http") else f" Gibt es die Datei {url.removeprefix('file://')}?")
    return "Chromium hat kein Bild geliefert." + hint + (f" Chromium meldet: {why}" if why else "")


async def _shot_bytes(ctx: ToolContext, url: str, width: int, height: int) -> bytes:
    """PNG der Seite – in der Sandbox im Container (dort läuft der Dev-Server), sonst mit dem Chromium des Rechners."""
    import shlex
    import tempfile
    if ctx.sandbox:
        from .. import sandbox
        binary = await sandbox.ensure_running(ctx.cfg, ctx.sandbox)
        argv = _browser_argv("$(command -v chromium || command -v chromium-browser)", url, "/tmp/orbwise-shot.png",
                             width, height, "/tmp/orbwise-shot-profile", True)
        cmd = ("command -v chromium >/dev/null || command -v chromium-browser >/dev/null || "
               "{ echo NOBROWSER >&2; exit 3; }; rm -f /tmp/orbwise-shot.png; "
               + (f"curl -s -o /dev/null --max-time 5 {shlex.quote(url)} || {{ echo NOSERVER >&2; exit 5; }}; "
                  if url.startswith("http") else "")
               + " ".join(a if a.startswith("$(") else shlex.quote(a) for a in argv)
               + " >/tmp/orbwise-shot.log 2>&1; [ -s /tmp/orbwise-shot.png ] && cat /tmp/orbwise-shot.png || "
               "{ tail -c 1500 /tmp/orbwise-shot.log >&2; exit 4; }")
        p = await asyncio.create_subprocess_exec(*sandbox.exec_argv(binary, ctx.sandbox["name"], ctx.sandbox["path"],
                                                                    cmd, 90),
                                                 stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        out, err = await p.communicate()
        if p.returncode == 3 or b"NOBROWSER" in err:
            raise RuntimeError("Im Container fehlt Chromium – Sandbox beenden und neu starten (das neue Image bringt "
                               "ihn mit) oder im Container `apt-get install -y chromium` ausführen.")
        if p.returncode == 5 or b"NOSERVER" in err:
            raise RuntimeError(_no_server(url))
        if p.returncode or not out:
            raise RuntimeError(_shot_error(url, err.decode(errors="replace")))
        return out
    if url.startswith("http"):
        import httpx
        try:
            async with httpx.AsyncClient(timeout=5, verify=False) as client:
                await client.get(url)
        except httpx.HTTPError:
            raise RuntimeError(_no_server(url)) from None
    binary = next((b for b in (shutil.which(n) for n in BROWSERS) if b), None)
    if not binary:
        raise RuntimeError("Kein Chromium/Chrome gefunden – z. B. `sudo apt install chromium` (oder die Sandbox "
                           "nutzen, dort ist er dabei).")
    with tempfile.TemporaryDirectory(prefix="orbwise-shot-") as tmp:
        out = os.path.join(tmp, "shot.png")
        p = await asyncio.create_subprocess_exec(*_browser_argv(binary, url, out, width, height,
                                                                os.path.join(tmp, "profile"),
                                                                hasattr(os, "geteuid") and os.geteuid() == 0),
                                                 stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
        try:
            _, err = await asyncio.wait_for(p.communicate(), 90)
        except asyncio.TimeoutError:
            p.kill()
            raise RuntimeError("Chromium hat nicht rechtzeitig fertig gerendert (90 s).") from None
        if not os.path.isfile(out) or not os.path.getsize(out):
            raise RuntimeError(_shot_error(url, err.decode(errors="replace")))
        return Path(out).read_bytes()


@tool("Screenshot einer Seite (Dev-Server auf localhost, HTML-Datei) – der Nutzer sieht ihn im Chat; mit Bildmodell "
      "kommt eine Beschreibung zurück. Nach UI-Änderungen nutzen.", risk=_shot_risk, name="screenshot")
async def code_screenshot(
    ctx: ToolContext,
    url: Annotated[str, "URL oder Datei"],
    width: int = 1280,
    height: int = 800,
    question: Annotated[str, "Optional: worauf achten"] = "",
) -> str:
    from .. import attachments
    width, height = max(200, min(int(width or 1280), 3840)), max(200, min(int(height or 800), 4000))
    target = _shot_target(ctx, url)
    try:
        data = await _shot_bytes(ctx, target, width, height)
        name = attachments.save(ctx.cfg, data, "screenshot") if data else ""
    except (RuntimeError, OSError, ValueError) as e:
        return f"Screenshot fehlgeschlagen: {e}"
    if not name or not name.endswith(".png"):
        return f"Screenshot fehlgeschlagen: Chromium hat kein Bild geliefert ({target})."
    if ctx.agent is not None:
        ctx.agent._shots = [*getattr(ctx.agent, "_shots", []), name]
    if ctx.emit:
        await ctx.emit({"type": "tool_images", "id": ctx.call_id, "images": [name]})
    head = f"Screenshot von {target} ({width}×{height}) wird dem Nutzer im Chat gezeigt."
    if not getattr(ctx.agent, "_vision", False):
        return head + " (Das geladene Modell sieht keine Bilder – keine Beschreibung.)"
    from . import vision
    prompt = ("Beschreibe knapp, was auf diesem Screenshot einer Web-Oberfläche zu sehen ist: Aufbau, Texte, "
              "auffällige Fehler (Überlappungen, abgeschnittene Texte, leere Bereiche, Fehlermeldungen).")
    if question.strip():
        prompt += f" Achte besonders auf: {question.strip()}"
    try:
        answer = await vision.ask_model(ctx, attachments.path_of(ctx.cfg, name), prompt)
    except vision.VisionError as e:
        return f"{head} (Beschreibung nicht möglich: {e})"
    source = "Screenshot" if _LOCAL_URL.match(target) or target.startswith("file://") else "Screenshot einer Webseite"
    return head + "\n" + vision._wrap(ctx.cfg, source, answer)


# ---------------------------------------------------------------- Unteragent, Rückfrage
@tool("Startet einen Unteragenten mit eigenem Kontext für eine abgegrenzte Aufgabe und liefert nur sein Ergebnis "
      "zurück – spart Platz im Kontext. agent='explore': schnell und nur lesend – Dateien nach Muster finden, Code "
      "nach Stichworten durchsuchen, Fragen zur Codebasis beantworten (im prompt die Gründlichkeit angeben: "
      "'quick', 'medium' oder 'very thorough'). agent='general': mehrstufige Aufgaben, darf auch ändern (mit "
      "denselben Freigaben). Den prompt vollständig formulieren – der Unteragent kennt den Chat nicht.",
      name="subagent")
async def code_subagent(
    ctx: ToolContext,
    agent: Annotated[str, "explore oder general"],
    description: Annotated[str, "Kurzes Etikett (3–5 Wörter) für die Anzeige"],
    prompt: Annotated[str, "Die vollständige Aufgabe"],
) -> str:
    if ctx.agent is None:
        return "Unteragenten sind hier nicht verfügbar."
    kind = agent.strip().lower()
    if kind not in ("explore", "general"):
        return "agent muss 'explore' oder 'general' sein."
    return await ctx.agent.run_subagent(ctx, kind, description, prompt)


@tool("Stellt dem Nutzer eine oder mehrere Fragen mit Antwortmöglichkeiten (der Nutzer kann auch frei antworten). "
      "Nur bei echter Unklarheit nutzen, die sich nicht aus Code, Doku oder sinnvollen Standards klären lässt – z. B. "
      "Wahl zwischen Ansätzen. Jede Frage: {\"question\": \"…?\", \"header\": \"kurz\", \"options\": [{\"label\": "
      "\"…\", \"description\": \"…\"}], \"multiple\": false}; 1–4 Fragen, 2–4 Optionen, empfohlene Option zuerst.",
      name="question")
async def code_question(
    ctx: ToolContext,
    questions: Annotated[list[dict], "Liste der Fragen"],
) -> str:
    if isinstance(questions, str):  # manche Modelle schicken die Liste als JSON-Text
        try:
            questions = json.loads(questions)
        except json.JSONDecodeError:
            questions = []
    if isinstance(questions, dict):
        questions = [questions]
    ask = ctx.services.get("ask")
    if not ask:
        return "Rückfragen sind hier nicht möglich – triff eine sinnvolle Annahme und nenne sie."
    clean = []
    for q in (questions or [])[:4]:
        if not isinstance(q, dict) or not str(q.get("question") or "").strip():
            continue
        opts = [o for o in (q.get("options") or []) if isinstance(o, dict) and str(o.get("label") or "").strip()]
        clean.append({"question": str(q["question"]).strip(), "header": str(q.get("header") or "")[:24],
                      "multiple": bool(q.get("multiple")),
                      "options": [{"label": str(o["label"]).strip()[:80],
                                   "description": str(o.get("description") or "")[:300]} for o in opts[:4]]})
    if not clean:
        return "Keine gültige Frage übergeben (je Frage mindestens 'question')."
    answers = await ask(ctx.call_id, clean)
    if not answers:
        return "Der Nutzer hat nicht geantwortet – triff eine sinnvolle Annahme und nenne sie."
    return "\n".join(f"{q['question']} → {', '.join(a) if isinstance(a, list) else a}"
                     for q, a in zip(clean, answers, strict=False))
