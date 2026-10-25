"""Programme über ihre .desktop-Einträge finden und starten."""

from __future__ import annotations

import configparser
import difflib
import os
import re
import shlex
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated

from . import proc
from .registry import ToolContext, tool


def _app_dirs() -> list[Path]:
    data_home = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share"))
    data_dirs = os.environ.get("XDG_DATA_DIRS", "/usr/local/share:/usr/share").split(":")
    dirs = [data_home / "applications", data_home / "flatpak/exports/share/applications",
            Path("/var/lib/flatpak/exports/share/applications"), Path("/var/lib/snapd/desktop/applications")]
    dirs += [Path(d) / "applications" for d in data_dirs if d]
    seen, out = set(), []
    for d in dirs:
        if d not in seen and d.is_dir():
            seen.add(d)
            out.append(d)
    return out


@dataclass
class App:
    id: str
    name: str
    generic: str
    keywords: str
    exec: str
    path: Path


def list_apps() -> list[App]:
    apps: dict[str, App] = {}
    for d in _app_dirs():
        for f in d.rglob("*.desktop"):
            app_id = str(f.relative_to(d)).replace("/", "-")
            if app_id in apps:
                continue  # Nutzer-Einträge haben Vorrang
            cp = configparser.ConfigParser(interpolation=None, strict=False)
            try:
                cp.read(f, encoding="utf-8")
                e = cp["Desktop Entry"]
            except (configparser.Error, KeyError, UnicodeDecodeError):
                continue
            if e.get("Type", "Application") != "Application" or e.get("NoDisplay", "false") == "true" \
                    or e.get("Hidden", "false") == "true":
                continue
            apps[app_id] = App(
                id=app_id,
                name=e.get("Name[de]") or e.get("Name", app_id),
                generic=e.get("GenericName[de]") or e.get("GenericName", ""),
                keywords=(e.get("Keywords[de]", "") + ";" + e.get("Keywords", "")),
                exec=e.get("Exec", ""),
                path=f,
            )
    return list(apps.values())


def match_app(query: str, apps: list[App]) -> App | None:
    q = query.lower().strip()
    best, best_score = None, 0.0
    for a in apps:
        names = [a.name.lower(), a.id.lower().removesuffix(".desktop"), a.generic.lower()]
        score = max(difflib.SequenceMatcher(None, q, n).ratio() for n in names if n)
        if any(q == n for n in names):
            score = 2.0
        elif any(q in n for n in names[:2]):
            score = max(score, 1.2)
        elif q in a.keywords.lower() or q in a.generic.lower():
            score = max(score, 0.9)
        if score > best_score:
            best, best_score = a, score
    return best if best_score >= 0.6 else None


def _exec_argv(exec_line: str, files: list[str] | None = None) -> list[str]:
    """Exec-Zeile einer .desktop-Datei in argv umwandeln; Dateien für %f/%F/%u/%U einsetzen."""
    files = files or []
    argv, placed = [], False
    for part in shlex.split(exec_line.replace("%%", "\x00")):
        if part in ("%f", "%u"):
            argv += files[:1]
            placed = True
        elif part in ("%F", "%U"):
            argv += files
            placed = True
        else:
            cleaned = re.sub(r"%[fFuUdDnNickvm]", "", part).replace("\x00", "%")
            if cleaned:
                argv.append(cleaned)
    if files and not placed:
        argv += files
    return argv


async def launch_app(app: App, files: list[str] | None = None) -> tuple[bool, str]:
    """Startet eine Anwendung (optional mit Dateien) und meldet, ob der Start fehlschlug."""
    files = files or []
    if shutil.which("gio"):
        ok, err = await proc.launch(["gio", "launch", str(app.path), *files])
        if ok:
            return ok, err
    if shutil.which("gtk-launch"):
        ok, err = await proc.launch(["gtk-launch", app.id.removesuffix(".desktop"), *files])
        if ok:
            return ok, err
    argv = _exec_argv(app.exec, files)
    if not argv:
        return False, "keine Exec-Zeile"
    return await proc.launch(argv)


def _no_display_hint() -> str:
    return (" Orbwise hat keinen Zugriff auf die grafische Sitzung (DISPLAY/WAYLAND_DISPLAY fehlt) – "
            "starte Orbwise aus der Desktop-Sitzung (Autostart oder Terminal im Desktop).")


@tool("Startet ein installiertes Programm (z. B. 'Firefox', 'Dateimanager', 'Steam', 'Terminal').")
async def open_app(ctx: ToolContext, name: Annotated[str, "Programm, z. B. 'Firefox'"]) -> str:
    app = match_app(name, list_apps())
    if app:
        ok, err = await launch_app(app)
        if ok:
            return f"Gestartet: {app.name}"
        return f"Start von {app.name} fehlgeschlagen ({err})." + ("" if proc.has_display() else _no_display_hint())
    binary = shutil.which(name.strip().lower())
    if binary:
        ok, err = await proc.launch([binary])
        return f"Gestartet: {binary}" if ok else f"Start von {binary} fehlgeschlagen ({err})."
    return f"Kein Programm namens '{name}' gefunden. Mit list_installed_apps nach installierten Programmen suchen."


@tool("Listet installierte Programme auf, optional gefiltert.")
async def list_installed_apps(ctx: ToolContext, filter: str = "") -> str:
    f = filter.lower()
    names = sorted({a.name for a in list_apps()
                    if not f or f in a.name.lower() or f in a.generic.lower() or f in a.keywords.lower()})
    return ", ".join(names[:150]) or "Keine passenden Programme gefunden."
