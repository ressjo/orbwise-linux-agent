"""Rückgängig je Antwort im Coding-Modus (wie /undo bei OpenCode): vor und nach jeder Antwort ein Schnappschuss des
Projektordners in einem eigenen Git-Verzeichnis – das Git des Projekts wird nicht angefasst, seine .gitignore gilt
trotzdem. Zurücksetzen stellt nur die Dateien wieder her, die sich in der Antwort geändert haben.

Ablage: ~/.local/share/orbwise/snapshots/<sha1 des Projektpfads>. Ohne git, bei sehr großen Ordnern oder wenn ein
Schnappschuss zu lange dauert, ist die Funktion für diesen Ordner aus (liefert None).
"""

from __future__ import annotations

import contextlib
import hashlib
import logging
import os
import shutil
import subprocess
from pathlib import Path

log = logging.getLogger(__name__)

MAX_FILES = 20_000
TIMEOUT = 10.0
_too_big: set[str] = set()


def base_dir() -> Path:
    base = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
    return Path(base) / "orbwise" / "snapshots"


def git_dir(project: str) -> Path:
    return base_dir() / hashlib.sha1(str(Path(project).resolve()).encode()).hexdigest()[:16]


def available() -> bool:
    return shutil.which("git") is not None


def _git(project: str, *args: str, stdin: str | None = None, check: bool = True) -> str:
    gd = git_dir(project)
    env = {**os.environ, "GIT_DIR": str(gd), "GIT_WORK_TREE": str(Path(project).resolve()),
           "GIT_INDEX_FILE": str(gd / "index"), "GIT_CONFIG_NOSYSTEM": "1", "GIT_TERMINAL_PROMPT": "0",
           "LC_ALL": "C"}
    r = subprocess.run(["git", "-c", "core.autocrlf=false", "-c", "core.quotepath=false", *args],
                       cwd=str(Path(project).resolve()), env=env, input=stdin, capture_output=True, text=True,
                       timeout=TIMEOUT)
    if check and r.returncode != 0:
        raise RuntimeError((r.stderr or r.stdout).strip()[:300])
    return r.stdout


def _init(project: str) -> None:
    gd = git_dir(project)
    if (gd / "HEAD").exists():
        return
    gd.mkdir(parents=True, exist_ok=True)
    _git(project, "init", "--quiet")
    (gd / "project").write_text(str(Path(project).resolve()), encoding="utf-8")
    # die .gitignore des Projekts gilt über GIT_WORK_TREE; dazu das Git-Verzeichnis des Projekts selbst ausnehmen
    (gd / "info").mkdir(exist_ok=True)
    (gd / "info" / "exclude").write_text(".git/\nnode_modules/\n.venv/\n__pycache__/\n", encoding="utf-8")


def capture(project: str) -> str | None:
    """Schnappschuss des Arbeitsstands: Tree-ID oder None (nicht verfügbar / zu groß / Fehler)."""
    key = str(Path(project).resolve())
    if not available() or key in _too_big or not Path(project).is_dir():
        return None
    try:
        _init(project)
        files = _git(project, "ls-files", "--others", "--cached", "--exclude-standard", "-z")
        if files.count("\0") > MAX_FILES:
            _too_big.add(key)
            log.info("Schnappschüsse für %s aus: mehr als %d Dateien", project, MAX_FILES)
            return None
        _git(project, "add", "--all", ".")
        return _git(project, "write-tree").strip() or None
    except subprocess.TimeoutExpired:
        _too_big.add(key)
        log.info("Schnappschüsse für %s aus: dauert zu lange", project)
        return None
    except (OSError, RuntimeError, subprocess.SubprocessError) as e:
        log.warning("Schnappschuss von %s fehlgeschlagen: %s", project, e)
        return None


def changed(project: str, before: str, after: str) -> list[dict]:
    """Geänderte Dateien zwischen zwei Schnappschüssen: [{path, add, del, status}] (Binärdateien: add/del = None)."""
    if not before or not after or before == after:
        return []
    try:
        numstat = _git(project, "diff-tree", "-r", "--numstat", "--no-renames", before, after)
        status = _git(project, "diff-tree", "-r", "--name-status", "--no-renames", before, after)
    except (OSError, RuntimeError, subprocess.SubprocessError):
        return []
    kinds = {}
    for line in status.splitlines():
        kind, _, path = line.partition("\t")
        kinds[path] = {"A": "added", "D": "deleted"}.get(kind[:1], "modified")
    out = []
    for line in numstat.splitlines():
        parts = line.split("\t", 2)
        if len(parts) != 3:
            continue
        add, rem, path = parts
        out.append({"path": path, "add": None if add == "-" else int(add), "del": None if rem == "-" else int(rem),
                    "status": kinds.get(path, "modified")})
    return out


def diff(project: str, before: str, after: str, path: str, max_lines: int = 400) -> str:
    try:
        text = _git(project, "diff", "--no-color", "--no-renames", before, after, "--", path)
    except (OSError, RuntimeError, subprocess.SubprocessError):
        return ""
    lines = text.splitlines()
    return "\n".join(lines[:max_lines] + ([f"… ({len(lines) - max_lines} weitere Zeilen)"] if len(lines) > max_lines
                                          else []))


def restore(project: str, tree: str, paths: list[str]) -> list[str]:
    """Die genannten Dateien auf den Stand von tree bringen; Dateien, die es dort nicht gab, löschen. Liefert die
    tatsächlich wiederhergestellten Pfade."""
    root = Path(project).resolve()
    present = set(_git(project, "ls-tree", "-r", "--name-only", "-z", tree).split("\0")) if paths else set()
    done = []
    for rel in paths:
        target = (root / rel).resolve()
        if root not in target.parents:  # nie außerhalb des Projekts
            continue
        if rel in present:
            blob = subprocess.run(["git", "--git-dir", str(git_dir(project)), "cat-file", "blob", f"{tree}:{rel}"],
                                  capture_output=True, timeout=TIMEOUT)
            if blob.returncode != 0:
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(blob.stdout)
        else:
            with contextlib.suppress(FileNotFoundError):
                target.unlink()
            # leer gewordene Ordner bis zum Projekt aufräumen
            parent = target.parent
            while parent != root and parent.is_dir() and not any(parent.iterdir()):
                parent.rmdir()
                parent = parent.parent
        done.append(rel)
    return done
