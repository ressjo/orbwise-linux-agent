"""Arbeitskopie für die Coding-Sandbox (wie Claude im Web): Der Agent arbeitet in einer eigenen Kopie des Projekts
auf einem eigenen Branch; der Projektordner bleibt unberührt, bis der Nutzer „In Projekt übernehmen“ wählt oder
den Branch pusht.

Git-Projekte werden lokal geklont (eigenes .git – der Container kann das Original-Repo nicht verändern), danach kommt
der aktuelle Arbeitsstand dazu (auch noch nicht Committetes). Andere Ordner werden kopiert (ohne node_modules, .venv …).
Was sich geändert hat, ergibt sich aus Schnappschüssen (snapshot.py) der Kopie und des Projekts beim Anlegen.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

from . import snapshot
from .tools.files import SKIP_DIRS

MAX_COPY_BYTES = 2_000_000_000


class WorkspaceError(RuntimeError):
    pass


def base_dir() -> Path:
    base = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
    return Path(base) / "orbwise" / "workspaces"


def path_for(chat_id: str, project: str) -> Path:
    return base_dir() / re.sub(r"[^\w-]", "", chat_id)[:40] / (Path(project).name or "projekt")


# Die Arbeitskopie kann der Container beschreiben – auch ihr .git. Auf dem Host laufen darin deshalb weder Hooks
# noch core.fsmonitor (beides würde Code des Agenten außerhalb des Containers starten).
SAFE_GIT = ("-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false")


def _git(cwd: str, *args: str, check: bool = True) -> str:
    r = subprocess.run(["git", *SAFE_GIT, *args], cwd=cwd, capture_output=True, text=True, timeout=300,
                       env={**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_CONFIG_NOSYSTEM": "1", "LC_ALL": "C"})
    if check and r.returncode != 0:
        raise WorkspaceError((r.stderr or r.stdout).strip()[-400:])
    return r.stdout


def is_git(project: str) -> bool:
    r = subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=project, capture_output=True, text=True)
    return r.returncode == 0 and Path(r.stdout.strip()).resolve() == Path(project).resolve()


def branch_name(chat_id: str, title: str = "") -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:30] or "arbeit"
    return f"orbwise/{slug}-{chat_id[:6]}"


def _valid_branch(name: str) -> bool:
    return subprocess.run(["git", "check-ref-format", "--branch", name], capture_output=True).returncode == 0


def _copy_tracked(project: Path, ws: Path) -> None:
    """Arbeitsstand des Projekts in die Kopie: alle Dateien laut git (auch neue, nicht ignorierte), Gelöschtes weg."""
    files = _git(str(project), "ls-files", "--cached", "--others", "--exclude-standard", "-z").split("\0")
    present = set()
    for rel in filter(None, files):
        src = project / rel
        if not src.is_file() and not src.is_symlink():
            continue  # im Projekt gelöscht (steht noch im Index)
        present.add(rel)
        dst = ws / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        if src.is_symlink():
            if dst.exists() or dst.is_symlink():
                dst.unlink()
            dst.symlink_to(os.readlink(src))
        else:
            shutil.copy2(src, dst)
    for rel in filter(None, _git(str(ws), "ls-files", "-z").split("\0")):
        if rel not in present and (ws / rel).exists():
            (ws / rel).unlink()


def _copy_plain(project: Path, ws: Path) -> None:
    total = 0

    def ignore(folder: str, names: list[str]) -> set[str]:
        nonlocal total
        skip = {n for n in names if n in SKIP_DIRS}
        for n in names:
            p = os.path.join(folder, n)
            if n not in skip and os.path.isfile(p):
                total += os.path.getsize(p)
        if total > MAX_COPY_BYTES:
            raise WorkspaceError("Der Projektordner ist größer als 2 GB – für die Sandbox ein Git-Repo nutzen "
                                 "(dann wird nur der Code kopiert).")
        return skip
    shutil.copytree(project, ws, symlinks=True, ignore=ignore)


def create(chat_id: str, project: str, title: str = "", identity: dict | None = None, branch: str = "") -> dict:
    """Legt die Arbeitskopie an (oder nimmt die vorhandene) und liefert den Zustand für conv.meta["sandbox"]. branch:
    Name aus einer früheren Sandbox des Chats – so bleibt es auf GitHub bei einem Branch je Chat, auch wenn sich der
    Titel inzwischen geändert hat."""
    wanted = branch.strip()
    src = Path(project).resolve()
    if not src.is_dir():
        raise WorkspaceError(f"Projektordner {project} gibt es nicht.")
    ws = path_for(chat_id, str(src))
    if ws.exists() and is_git(str(src)) and _git_dir(str(ws)) is None:
        # Rest einer früheren Kopie (z. B. Dateien innerer Container ließen sich nicht löschen) – frisch anlegen
        shutil.rmtree(ws, ignore_errors=True)
        if ws.exists():
            raise WorkspaceError(f"Die alte Arbeitskopie {ws} ließ sich nicht ganz löschen – bitte von Hand entfernen.")
    branch = ""
    origin = _git(str(src), "remote", "get-url", "origin", check=False).strip() if is_git(str(src)) else ""
    if not ws.exists():
        ws.parent.mkdir(parents=True, exist_ok=True)
        try:
            if is_git(str(src)):
                _git(str(ws.parent), "clone", "--quiet", "--local", "--no-hardlinks", str(src), str(ws))
                if origin:
                    _git(str(ws), "remote", "set-url", "origin", origin)
                else:
                    _git(str(ws), "remote", "remove", "origin", check=False)
                _copy_tracked(src, ws)
                branch = wanted if wanted and _valid_branch(wanted) else branch_name(chat_id, title)
                _git(str(ws), "checkout", "--quiet", "-B", branch)
                ident = identity or {}
                name = ident.get("name") or _git(str(src), "config", "user.name", check=False).strip()
                email = ident.get("email") or _git(str(src), "config", "user.email", check=False).strip()
                if name:
                    _git(str(ws), "config", "user.name", name)
                if email:
                    _git(str(ws), "config", "user.email", email)
            else:
                _copy_plain(src, ws)
        except Exception:
            shutil.rmtree(ws, ignore_errors=True)
            raise
    else:
        branch = head_branch(str(ws)) or ""
    return {"path": str(ws), "project": str(src), "branch": branch, "origin": origin,
            "base_ws": snapshot.capture(str(ws)), "base_project": snapshot.capture(str(src))}


def _git_dir(ws: str) -> Path | None:
    """Das .git der Arbeitskopie – nur ein echter Ordner (kein Symlink, keine „gitdir:“-Datei, die woandershin zeigt)."""
    gd = Path(ws) / ".git"
    return gd if gd.is_dir() and not gd.is_symlink() else None


def head(ws: str) -> str | None:
    """Inhalt von .git/HEAD als Text – ohne git aufzurufen (dessen Konfiguration kommt aus dem Container)."""
    gd = _git_dir(ws)
    f = gd / "HEAD" if gd else None
    if not f or f.is_symlink() or not f.is_file():
        return None
    try:
        return f.read_text(encoding="utf-8", errors="replace")[:300].strip()
    except OSError:
        return None


def head_branch(ws: str) -> str | None:
    """Ausgecheckter Branch der Arbeitskopie (None: keiner/detached)."""
    m = re.fullmatch(r"ref: refs/heads/(\S+)", head(ws) or "")
    return m.group(1) if m else None


def head_label(ws: str) -> str | None:
    """Branch oder – detached – der kurze Commit-Hash (für die Anzeige)."""
    text = head(ws) or ""
    return head_branch(ws) or (text[:7] if re.fullmatch(r"[0-9a-f]{40,64}", text) else None)


def changes(state: dict) -> list[dict]:
    """Was sich in der Kopie seit dem Anlegen (bzw. dem letzten Übernehmen) geändert hat."""
    now = snapshot.capture(state["path"])
    return snapshot.changed(state["path"], state.get("base_ws") or "", now or "") if now else []


def diff(state: dict, path: str) -> str:
    now = snapshot.capture(state["path"])
    return snapshot.diff(state["path"], state.get("base_ws") or "", now or "", path) if now else ""


def _blob(project: str, tree: str, rel: str) -> bytes | None:
    r = subprocess.run(["git", "--git-dir", str(snapshot.git_dir(project)), "cat-file", "blob", f"{tree}:{rel}"],
                       capture_output=True, timeout=30)
    return r.stdout if r.returncode == 0 else None


def _content(p: Path) -> bytes | None:
    """Inhalt wie git ihn sieht: bei Symlinks das Linkziel (nicht die Datei dahinter)."""
    if p.is_symlink():
        return os.readlink(p).encode()
    return p.read_bytes() if p.is_file() else None


def apply(state: dict, force: bool = False) -> dict:
    """Geänderte Dateien der Kopie ins Projekt übernehmen. Hat sich eine Datei im Projekt seit dem Anlegen selbst
    geändert, wird sie übersprungen (Konflikt) – außer mit force. Danach gilt der neue Stand als Basis."""
    ws, project = Path(state["path"]), Path(state["project"])
    files = changes(state)
    base = state.get("base_project") or ""
    applied, conflicts = [], []
    for f in files:
        rel = f["path"]
        target = (project / rel).resolve()
        if project.resolve() not in target.parents:
            continue
        source = ws / rel
        current = _content(target)
        if current == _content(source):
            applied.append(rel)  # steht schon so im Projekt
            continue
        if not force and base and _blob(str(project), base, rel) != current:
            conflicts.append(rel)  # im Projekt seit dem Anlegen selbst geändert
            continue
        if target.is_symlink() or target.is_file():
            target.unlink()
        if source.is_symlink():  # Links bleiben Links – nie dem Ziel folgen (es könnte auf Dateien des Hosts zeigen)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.symlink_to(os.readlink(source))
        elif source.is_file():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target, follow_symlinks=False)
        applied.append(rel)
    if applied and not conflicts:
        state["base_ws"] = snapshot.capture(str(ws))
        state["base_project"] = snapshot.capture(str(project))
    return {"applied": applied, "conflicts": conflicts}


def discard_chat(chat_id: str) -> None:
    """Alle Arbeitskopien eines Chats löschen (Chat gelöscht)."""
    folder = base_dir() / re.sub(r"[^\w-]", "", chat_id)[:40]
    if chat_id and folder.is_dir():
        shutil.rmtree(folder, ignore_errors=True)


def discard(state: dict) -> None:
    ws = Path(state.get("path") or "")
    if ws and base_dir() in ws.resolve().parents:
        shutil.rmtree(ws.parent if ws.parent != base_dir() else ws, ignore_errors=True)
