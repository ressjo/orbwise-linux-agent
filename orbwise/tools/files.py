"""Dateien suchen (lokal + NAS), Inhalte durchsuchen, Dateien lesen und öffnen."""

from __future__ import annotations

import asyncio
import os
import shutil
import time
from datetime import datetime
from pathlib import Path
from typing import Annotated

from . import proc
from .registry import BLOCKED, CONFIRM, SAFE, ToolContext, tool
from .secretpaths import is_secret_path, secret_reason

TEXT_LIMIT = 8000
READ_MAX_BYTES = 4_000_000  # größere Dateien werden nur bis hier gelesen (seitenweise)
SKIP_DIRS = {".git", "node_modules", ".cache", "__pycache__", ".venv", ".local/share/Trash", ".steam"}


def _human_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PB"


def _describe(paths: list[str], limit: int) -> str:
    rows = []
    for p in paths:
        try:
            st = os.stat(p)
        except OSError:
            continue
        rows.append((st.st_mtime, p, st.st_size, os.path.isdir(p)))
    rows.sort(reverse=True)  # neueste zuerst
    out = [f"{len(rows)} Treffer" + (f" (zeige {limit})" if len(rows) > limit else "")]
    for mtime, p, size, is_dir in rows[:limit]:
        when = datetime.fromtimestamp(mtime).strftime("%d.%m.%Y %H:%M")
        out.append(f"{p}  [{'Ordner' if is_dir else _human_size(size)}, {when}]")
    return "\n".join(out)


def _walk_find(roots: list[Path], query: str, ext: str, max_hits: int, deadline: float) -> list[str]:
    q = query.lower()
    hits: list[str] = []
    for root in roots:
        for dirpath, dirnames, filenames in os.walk(root, onerror=lambda e: None):
            dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")]
            for name in dirnames + filenames:
                low = name.lower()
                if q in low and (not ext or low.endswith(ext)):
                    hits.append(os.path.join(dirpath, name))
                    if len(hits) >= max_hits:
                        return hits
            if time.monotonic() > deadline:
                return hits
    return hits


async def _find(ctx: ToolContext, query: str, roots: list[Path], ext: str, use_locate: bool,
                max_hits: int = 300) -> list[str]:
    ext = ("." + ext.lstrip(".").lower()) if ext else ""
    roots = [r for r in roots if r.exists()]
    if not roots:
        return []
    locate = shutil.which("plocate") or shutil.which("locate")
    if use_locate and locate and query:
        rc, out = await proc.run(ctx, [locate, "-i", "-l", str(max_hits * 3), query], timeout=30, stream=False)
        if rc == 0:
            root_strs = [str(r) for r in roots]
            paths = [p for p in out.splitlines() if any(p.startswith(r) for r in root_strs)]
            paths = [p for p in paths if "/." not in p and (not ext or p.lower().endswith(ext))]
            if paths:
                return paths[:max_hits]
    fd = shutil.which("fd") or shutil.which("fdfind")
    if fd:
        argv = [fd, "--ignore-case", "--max-results", str(max_hits), "--absolute-path"]
        if ext:
            argv += ["--extension", ext.lstrip(".")]
        argv += ["--", query or "."] + [str(r) for r in roots]
        rc, out = await proc.run(ctx, argv, timeout=90, stream=False)
        return [p for p in out.splitlines() if p.strip()]
    deadline = time.monotonic() + 60
    return await asyncio.to_thread(_walk_find, roots, query, ext, max_hits, deadline)


@tool("Sucht Dateien und Ordner nach Namen (Teilstring, ohne Groß/Klein) auf dem lokalen System, neueste zuerst.")
async def find_files(
    ctx: ToolContext,
    query: Annotated[str, "Teil des Namens, z. B. 'rechnung'"],
    path: Annotated[str, "Startordner, z. B. ~/Dokumente"] = "",
    extension: Annotated[str, "Dateiendung ohne Punkt"] = "",
    max_results: int = 25,
) -> str:
    roots = [ctx.path(path)] if path else ([Path(ctx.cwd)] if ctx.cwd else ctx.cfg.tools.search_paths)
    paths = await _find(ctx, query, roots, extension, use_locate=not path)
    if not paths:
        return f"Keine Dateien zu '{query}' gefunden."
    return _describe(paths, max(1, min(max_results, 100)))


@tool("Durchsucht das gemountete NAS nach Datei-/Ordnernamen, optional nach Inhalt.",
      enabled=lambda cfg: bool(cfg.tools.nas_paths))  # ohne NAS kein Werkzeug (spart Platz im Prompt)
async def search_nas(
    ctx: ToolContext,
    query: Annotated[str, "Namensteil bzw. Suchtext"],
    extension: str = "",
    content: Annotated[bool, "true = im Inhalt suchen"] = False,
    max_results: int = 25,
) -> str:
    nas = ctx.cfg.tools.nas_paths
    if not nas:
        return "Es ist kein NAS-Pfad konfiguriert (tools.nas_paths in ~/.config/orbwise/config.yaml)."
    mounted = [p for p in nas if p.exists() and any(p.iterdir())]
    if not mounted:
        return f"Das NAS ist nicht gemountet oder leer: {', '.join(map(str, nas))}"
    if content:
        return await _grep(ctx, query, mounted, extension, max_results)
    # locate schließt Netzlaufwerke meist aus – daher direkt fd/os.walk
    paths = await _find(ctx, query, mounted, extension, use_locate=False)
    if not paths:
        return f"Auf dem NAS nichts zu '{query}' gefunden."
    return _describe(paths, max(1, min(max_results, 100)))


async def _grep(ctx: ToolContext, query: str, roots: list[Path], extension: str, max_results: int) -> str:
    rg = shutil.which("rg")
    if rg:
        argv = [rg, "--ignore-case", "--files-with-matches", "--max-filesize", "20M", "--fixed-strings"]
        if extension:
            argv += ["--glob", f"*.{extension.lstrip('.')}"]
        argv += ["--", query] + [str(r) for r in roots]
    else:
        argv = ["grep", "-rIl", "-i", "-F", "--", query] + [str(r) for r in roots]
    rc, out = await proc.run(ctx, argv, timeout=120, stream=False)
    paths = [p for p in out.splitlines() if p.strip()]
    if not paths:
        return f"Kein Dateiinhalt enthält '{query}'."
    return _describe(paths, max(1, min(max_results, 100)))


@tool("Durchsucht den Inhalt von Textdateien (ripgrep).")
async def search_file_contents(
    ctx: ToolContext,
    query: str,
    path: Annotated[str, "Ordner (Standard: Home)"] = "",
    extension: str = "",
    max_results: int = 25,
) -> str:
    roots = [ctx.path(path)] if path else ([Path(ctx.cwd)] if ctx.cwd else ctx.cfg.tools.search_paths)
    return await _grep(ctx, query, roots, extension, max_results)


@tool("Listet den Inhalt eines Verzeichnisses auf.")
async def list_directory(ctx: ToolContext, path: Annotated[str, "Ordner, z. B. ~/Downloads"]) -> str:
    p = ctx.path(path)
    if not p.is_dir():
        return f"{p} ist kein Verzeichnis."
    try:
        entries = [str(e) for e in p.iterdir() if not e.name.startswith(".")]
    except PermissionError:
        return f"Keine Berechtigung für {p}."
    return _describe(entries, 60) if entries else f"{p} ist leer."


def _read_risk(ctx: ToolContext, args: dict) -> tuple[str, str]:
    if is_secret_path(str(ctx.path(args.get("path") or "")) if args.get("path") else ""):
        return BLOCKED, secret_reason()
    return SAFE, ""


@tool("Liest eine Textdatei (Konfiguration, Log, Notiz, Code). Große Dateien seitenweise mit offset (erste Zeile, "
      "ab 1) und limit (Zeilen).", risk=_read_risk)
async def read_file(
    ctx: ToolContext,
    path: str,
    offset: int = 0,
    limit: int = 0,
) -> str:
    p = ctx.path(path)
    if not p.is_file():
        return f"Datei nicht gefunden: {p}"
    try:
        with p.open("rb") as f:
            data = f.read(READ_MAX_BYTES)
    except PermissionError:
        return f"Keine Leseberechtigung für {p} (ggf. mit run_shell und sudo lesen)."
    if b"\x00" in data[:4096]:
        return f"{p} ist eine Binärdatei ({_human_size(p.stat().st_size)}). Zum Anzeigen open_file nutzen."
    text = data.decode(errors="replace")
    room = min(TEXT_LIMIT, ctx.limit() * 4 // 3)
    if not offset and not limit and len(text) <= room:
        return text
    # Seitenweise statt „Mitte abgeschnitten“: ganze Zeilen ab offset, bis der Platz voll ist – mit Hinweis,
    # wo es weitergeht (wie das Read-Werkzeug von Claude Code)
    lines = text.splitlines()
    start = max(0, int(offset) - 1)
    chosen, used = [], 0
    for line in lines[start:]:
        if (limit and len(chosen) >= int(limit)) or (chosen and used + len(line) + 1 > room):
            break
        chosen.append(line if len(line) <= room else line[:room] + " …")
        used += len(line) + 1
    end = start + len(chosen)
    more = f" – weiter mit offset={end + 1}" if end < len(lines) else ""
    cut = " (Datei größer als gelesen)" if len(data) >= READ_MAX_BYTES else ""
    return f"[{p}: Zeilen {start + 1}–{end} von {len(lines)}{cut}{more}]\n" + "\n".join(chosen)


async def _resolve_target(ctx: ToolContext, target: str) -> tuple[Path | None, str]:
    """Pfad direkt verwenden oder – bei bloßem Dateinamen – im Home/NAS danach suchen."""
    p = ctx.path(target)
    if p.is_absolute() and p.exists():
        return p, ""
    if not p.is_absolute() and (Path.home() / p).exists():
        return Path.home() / p, ""
    name = p.name
    if not name:
        return None, f"Nicht gefunden: {p}"
    roots = [*ctx.cfg.tools.search_paths, *[n for n in ctx.cfg.tools.nas_paths if n.exists()]]
    hits = await _find(ctx, name, roots, "", use_locate=True, max_hits=50)
    hits = [h for h in hits if os.path.exists(h)]
    exact = [h for h in hits if os.path.basename(h).lower() == name.lower()]
    candidates = exact or hits
    if len(candidates) == 1:
        return Path(candidates[0]), ""
    if not candidates:
        return None, f"Nicht gefunden: {target} (auch keine Datei mit diesem Namen im Home/NAS)."
    return None, ("Mehrere Dateien passen – bitte den vollständigen Pfad nennen:\n"
                  + _describe(candidates, 10))


@tool("Öffnet eine Datei, einen Ordner oder eine URL mit dem Standard- oder einem gewünschten Programm. Ein "
      "bloßer Dateiname genügt (wird gesucht).")
async def open_file(
    ctx: ToolContext,
    path: Annotated[str, "Pfad, Dateiname oder URL"],
    app: Annotated[str, "Programm, z. B. 'Kate', 'GIMP'"] = "",
) -> str:
    from .apps import _no_display_hint, launch_app, list_apps, match_app

    target = path.strip()
    if not target.startswith(("http://", "https://")):
        resolved, problem = await _resolve_target(ctx, target)
        if not resolved:
            return problem
        target = str(resolved)

    if app.strip():
        match = match_app(app, list_apps())
        if not match:
            return f"Programm '{app}' nicht gefunden – Datei nicht geöffnet."
        ok, err = await launch_app(match, [target])
        if ok:
            return f"Geöffnet mit {match.name}: {target}"
        return f"Öffnen mit {match.name} fehlgeschlagen ({err}).{'' if proc.has_display() else _no_display_hint()}"

    opener = next((c for c in (["xdg-open"], ["gio", "open"]) if shutil.which(c[0])), None)
    if not opener:
        return "Weder xdg-open noch gio ist installiert (Paket xdg-utils)."
    ok, err = await proc.launch([*opener, target])
    if ok:
        return f"Geöffnet: {target}"
    hint = "" if proc.has_display() else _no_display_hint()
    if not hint and not target.startswith("http"):
        mime = await _mime_default(ctx, target)
        if mime:
            hint = f" {mime}"
    return f"Öffnen fehlgeschlagen ({err}).{hint} Tipp: mit dem Parameter app ein Programm angeben."


async def _mime_default(ctx: ToolContext, target: str) -> str:
    if not shutil.which("xdg-mime"):
        return ""
    _, mime = await proc.run(ctx, ["xdg-mime", "query", "filetype", target], timeout=5, stream=False)
    mime = mime.strip()
    if not mime:
        return ""
    _, app = await proc.run(ctx, ["xdg-mime", "query", "default", mime], timeout=5, stream=False)
    app = app.strip()
    return (f"Dateityp {mime}, Standardprogramm: {app}." if app
            else f"Für den Dateityp {mime} ist kein Standardprogramm eingestellt.")


def _write_risk(ctx: ToolContext, args: dict) -> tuple[str, str]:
    p = ctx.path(args.get("path", ""))
    return (CONFIRM, f"{'überschreibt' if p.exists() else 'erstellt'} {p}") if args.get("path") else (SAFE, "")


@tool("Schreibt eine Datei (neu oder überschreiben). Für Änderungen an bestehenden Dateien edit_file.", risk=_write_risk)
async def write_file(
    ctx: ToolContext,
    path: str,
    content: str,
    append: Annotated[bool, "true = anhängen"] = False,
) -> str:
    p = ctx.path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a" if append else "w", encoding="utf-8") as f:
        f.write(content)
    return f"{'Angehängt an' if append else 'Geschrieben:'} {p} ({len(content)} Zeichen)"


def _edit_risk(ctx: ToolContext, args: dict) -> tuple[str, str]:
    return (CONFIRM, f"ändert {ctx.path(args['path'])}") if args.get("path") else (SAFE, "")


def _around(text: str, start: int, end: int, context: int = 3) -> tuple[int, int, str]:
    """Zeilennummern der Änderung und ein paar Zeilen drumherum (mit Nummern) – das Modell muss nicht neu lesen."""
    lines = text.splitlines()
    first = text.count("\n", 0, start) + 1
    last = text.count("\n", 0, max(start, end - 1)) + 1
    lo, hi = max(1, first - context), min(len(lines), last + context)
    shown = "\n".join(f"{n:>5}\t{lines[n - 1]}" for n in range(lo, hi + 1))
    return first, last, shown


@tool("Ändert eine Textdatei: ersetzt old_text exakt durch new_text. old_text muss genau einmal vorkommen – "
      "Einrückung wie in read_file, genug Kontext. Neue Dateien: write_file.", risk=_edit_risk)
async def edit_file(
    ctx: ToolContext,
    path: str,
    old_text: Annotated[str, "Ausschnitt exakt wie in der Datei"],
    new_text: Annotated[str, "Ersatz (leer = löschen)"],
    replace_all: Annotated[bool, "true = jedes Vorkommen"] = False,
) -> str:
    p = ctx.path(path)
    if is_secret_path(str(p)):
        return secret_reason()
    if not p.is_file():
        return f"Datei {p} gibt es nicht – für neue Dateien write_file nutzen."
    try:
        text = p.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return f"{p} ist keine Textdatei."
    if not old_text:
        return "old_text ist leer – gib den Ausschnitt an, der ersetzt werden soll."
    count = text.count(old_text)
    if count == 0:
        import difflib
        first = old_text.strip().splitlines()[0] if old_text.strip() else old_text
        close = difflib.get_close_matches(first.strip(), [ln.strip() for ln in text.splitlines()], n=1, cutoff=0.6)
        hint = ""
        if close:
            n = next(i for i, ln in enumerate(text.splitlines(), 1) if ln.strip() == close[0])
            hint = f" Ähnlichste Zeile {n}: {text.splitlines()[n - 1]!r}"
        return (f"old_text kommt in {p} nicht vor – mit read_file nachsehen und den Ausschnitt exakt (Einrückung, "
                f"Leerzeichen) übernehmen.{hint}")
    if count > 1 and not replace_all:
        return (f"old_text kommt {count}-mal in {p} vor – mehr umgebenden Text mitgeben, damit die Stelle eindeutig "
                "ist, oder replace_all=true.")
    start = text.index(old_text)
    new = text.replace(old_text, new_text) if replace_all else text[:start] + new_text + text[start + len(old_text):]
    p.write_text(new, encoding="utf-8")
    first, last, shown = _around(new, start, start + len(new_text))
    what = f"{count} Stellen" if replace_all and count > 1 else f"Zeilen {first}–{last}"
    return f"Geändert: {p} ({what}).\n{shown}"
