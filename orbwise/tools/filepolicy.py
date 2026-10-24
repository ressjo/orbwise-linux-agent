"""Auto-Modus „Dateien bearbeiten“: welche Dateiänderungen ohne Rückfrage laufen dürfen.

Nur im eigenen Home, nur ohne Root-Rechte, nie in versteckten Ordnern/Dateien (Shell-Startdateien, Autostart,
~/.ssh, ~/.config/orbwise …), nie an Zugangsdaten und nie Programmstarter (.desktop, ~/bin). Löschen fragt immer.
"""

from __future__ import annotations

import os
from pathlib import Path

from .secretpaths import is_secret_path

# Dateien, die beim Anmelden oder Starten etwas ausführen – nie automatisch ändern
EXEC_SUFFIXES = (".desktop", ".service", ".timer", ".socket", ".path")
EXEC_DIRS = ("bin",)
GLOB_CHARS = set("*?[]{}")


def in_project(path: str, project: str, cwd: str | None = None) -> bool | None:
    """Liegt der Pfad (Symlinks aufgelöst) im Projektordner? None = unklar (Glob, Variable, nicht auflösbar)."""
    raw = (path or "").strip()
    if not raw or set(raw) & GLOB_CHARS or "$" in raw or raw.startswith("-"):
        return None
    p = Path(os.path.expanduser(raw))
    p = p if p.is_absolute() else Path(cwd or project) / p
    try:
        resolved, root = p.resolve(), Path(project).resolve()
    except (OSError, RuntimeError):
        return None
    return resolved == root or root in resolved.parents


def project_editable(path: str, project: str, cwd: str | None = None) -> bool:
    """Coding-Modus: Dateien im Projektordner dürfen ab „Dateien“ ohne Rückfrage geändert werden – auch außerhalb
    von $HOME und in versteckten Ordnern wie .github. Nie: Zugangsdaten, das Git-Verzeichnis, ohne Schreibrecht."""
    if not in_project(path, project, cwd):
        return False
    p = Path(os.path.expanduser(path.strip()))
    p = (p if p.is_absolute() else Path(cwd or project) / p).resolve()
    rel = p.relative_to(Path(project).resolve()).parts
    if not rel or ".git" in rel or is_secret_path(p):
        return False
    if p.exists():
        return p.is_file() and os.access(p, os.W_OK)
    parent = p.parent
    while not parent.exists():
        parent = parent.parent
    return parent.is_dir() and os.access(parent, os.W_OK | os.X_OK)


def editable_path(path: str, cwd: str | None = None, project: str | None = None) -> bool:
    """Darf diese Datei/dieser Ordner im Auto-Modus „Dateien“ ohne Rückfrage angelegt oder geändert werden?
    Mit project (Coding-Modus): genau die Dateien im Projektordner (siehe project_editable)."""
    if project:
        return project_editable(path, project, cwd)
    raw = (path or "").strip()
    if not raw or set(raw) & GLOB_CHARS or "$" in raw or raw.startswith("-"):
        return False
    base = Path(cwd or os.path.expanduser("~"))
    p = Path(os.path.expanduser(raw))
    p = p if p.is_absolute() else base / p
    home = Path.home().resolve()
    try:
        resolved = p.resolve()  # Symlinks auflösen: ein Link im Home darf nicht nach /etc zeigen
    except (OSError, RuntimeError):
        return False
    try:
        rel = resolved.relative_to(home)
    except ValueError:
        return False
    parts = rel.parts
    if not parts or any(part.startswith(".") for part in parts):  # Home selbst und versteckte Pfade fragen
        return False
    if parts[0] in EXEC_DIRS or resolved.name.lower().endswith(EXEC_SUFFIXES):
        return False
    if is_secret_path(resolved) or is_secret_path(p):
        return False
    # ohne Root: bestehende Datei beschreibbar bzw. nächster vorhandener Ordner beschreibbar
    if resolved.exists():
        return os.access(resolved, os.W_OK) and (resolved.is_file() or resolved.is_dir())
    parent = resolved.parent
    while not parent.exists() and parent != home:
        parent = parent.parent
    return parent.is_dir() and os.access(parent, os.W_OK | os.X_OK)


def shell_edits_ok(segments: list[tuple[str, list[str], str]], redirects: list[tuple[str, str]]) -> bool:
    """Für die Shell-Bewertung: sind alle verändernden Teile reine Dateiänderungen im erlaubten Bereich?
    segments: (Befehl, Argumente, Arbeitsordner) der Teile, die nicht nur lesend sind;
    redirects: (Ziel, Arbeitsordner) aller Ausgabeumleitungen."""
    for name, args, cwd in segments:
        flags = [a for a in args if a.startswith("-") and a != "-"]
        paths = [a for a in args if not a.startswith("-")]
        if name in ("mkdir", "touch"):
            if not paths or not all(editable_path(p, cwd) for p in paths):
                return False
            if any(f not in ("-p", "--parents", "-v", "-c", "--no-create") for f in flags):
                return False
        elif name in ("cp", "mv"):
            allowed = {"-r", "-R", "-a", "-v", "-n", "-i", "-p", "-u", "-f", "--recursive", "--archive",
                       "--no-clobber", "--update", "--verbose", "--preserve", "-rv", "-av", "-rp", "-vr", "-va"}
            if any(f not in allowed for f in flags) or len(paths) < 2:
                return False
            *sources, dest = paths
            if not editable_path(dest, cwd):
                return False
            # Quellen: mv verändert auch den alten Ort; cp darf keine Zugangsdaten kopieren
            for src in sources:
                if name == "mv" and not editable_path(src, cwd):
                    return False
                if is_secret_path(os.path.join(cwd, os.path.expanduser(src))):
                    return False
        elif name == "tee":
            if any(f not in ("-a", "--append") for f in flags) or not paths:
                return False
            if not all(editable_path(p, cwd) for p in paths):
                return False
        else:
            return False  # rm, rmdir, chmod, sed -i, … fragen weiter
    return all(t == "/dev/null" or editable_path(t, cwd) for t, cwd in redirects)


# ---------------------------------------------------------------- Auto-Modus „Auto“ (alles ohne Root)
# Dateien, über die sich etwas dauerhaft einnisten oder Orbwise selbst umgestellt werden könnte – fragen immer
SHELL_START = {".bashrc", ".bash_profile", ".bash_login", ".bash_logout", ".profile", ".zshrc", ".zprofile",
               ".zshenv", ".zlogin", ".xprofile", ".xinitrc", ".xsession", ".xsessionrc", ".pam_environment"}
PROTECTED_DIRS = (".config/autostart", ".config/systemd", ".config/environment.d", ".config/plasma-workspace/env",
                  ".config/fish/conf.d", ".ssh", ".gnupg", ".config/orbwise", ".local/share/orbwise",
                  ".local/share/applications", ".kde/Autostart")


def _resolve(path: str, cwd: str | None) -> Path | None:
    raw = (path or "").strip()
    if not raw:
        return None
    p = Path(os.path.expanduser(os.path.expandvars(raw)))
    p = p if p.is_absolute() else Path(cwd or os.path.expanduser("~")) / p
    try:
        return p.resolve()
    except (OSError, RuntimeError):
        return None


def protected_path(path: str, cwd: str | None = None) -> bool:
    """Shell-Startdateien, Autostart, systemd-Units, ~/.ssh, Orbwise selbst, Programmstarter, Zugangsdaten."""
    resolved = _resolve(path, cwd)
    if resolved is None or set(path) & GLOB_CHARS:
        return True  # unklar → lieber fragen
    if resolved.name in SHELL_START or resolved.name.lower().endswith(EXEC_SUFFIXES):
        return True
    home = Path.home().resolve()
    for d in PROTECTED_DIRS:
        base = home / d
        if resolved == base or base in resolved.parents:
            return True
    return is_secret_path(resolved) or is_secret_path(path)


def writable_path(path: str, cwd: str | None = None) -> bool:
    """Ohne Root beschreibbar (vorhandene Datei bzw. nächster vorhandener Ordner)?"""
    resolved = _resolve(path, cwd)
    if resolved is None:
        return False
    if resolved.exists():
        return os.access(resolved, os.W_OK)
    parent = resolved.parent
    while not parent.exists() and parent != parent.parent:
        parent = parent.parent
    return parent.is_dir() and os.access(parent, os.W_OK | os.X_OK)
