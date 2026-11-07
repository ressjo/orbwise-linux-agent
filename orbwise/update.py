"""`orbwise update`: neuen Stand per Git holen, Abhängigkeiten aktualisieren, laufenden Server neu starten.

Konfiguration (~/.config/orbwise), Stimmen und Gedächtnis (~/.local/share/orbwise) liegen außerhalb des
Projektordners und werden dabei nicht angefasst.
"""

from __future__ import annotations

import os
import re
import shutil
import signal
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path

import httpx

from .lang import T

Runner = Callable[..., subprocess.CompletedProcess]


def project_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _git(root: Path, *args: str, runner: Runner = subprocess.run, **kw) -> subprocess.CompletedProcess:
    return runner(["git", "-C", str(root), *args], capture_output=True, text=True, **kw)


def version(root: Path | None = None, runner: Runner = subprocess.run) -> str:
    """Für Menschen: „0.3.0 · a1b2c3d Titel (07.11.2026)“ – ohne Git nur die Nummer."""
    root = root or project_root()
    number = package_version(root)
    if not (root / ".git").exists() or not shutil.which("git"):
        return number or T("unbekannt (kein Git-Checkout)", "unknown (not a git checkout)")
    r = _git(root, "log", "-1", "--format=%h %s (%cd)", "--date=format:%d.%m.%Y", runner=runner)
    commit = r.stdout.strip()
    return " · ".join(p for p in (number, commit) if p) or T("unbekannt", "unknown")


_VERSION_RE = re.compile(r'^__version__\s*=\s*"([^"]+)"', re.M)


def package_version(root: Path | None = None) -> str:
    """Versionsnummer (z. B. 0.3.0) – frisch von der Platte, damit sie nach einem git pull sofort stimmt. Quelle ist
    orbwise/__init__.py (pyproject.toml liest sie von dort); ältere Stände hatten sie in pyproject.toml."""
    root = root or project_root()
    for path, pattern in ((root / "orbwise" / "__init__.py", _VERSION_RE),
                          (root / "pyproject.toml", re.compile(r'^version\s*=\s*"([^"]+)"', re.M))):
        try:
            m = pattern.search(path.read_text(encoding="utf-8"))
        except OSError:
            continue
        if m:
            return m.group(1)
    from . import __version__  # installiert ohne Quellordner
    return __version__ if root == project_root() else ""


def details(root: Path | None = None, runner: Runner = subprocess.run) -> dict:
    """Installierter Stand für die Oberfläche: Version, Commit, Datum, Branch."""
    root = root or project_root()
    out = {"version": package_version(root), "git": False, "commit": "", "date": "", "subject": "", "branch": ""}
    if not (root / ".git").exists() or not shutil.which("git"):
        return out
    r = _git(root, "log", "-1", "--format=%h%x1f%cs%x1f%s", runner=runner)
    parts = r.stdout.strip().split("\x1f")
    if len(parts) == 3:
        out.update(git=True, commit=parts[0], date=parts[1], subject=parts[2])
    out["branch"] = _git(root, "rev-parse", "--abbrev-ref", "HEAD", runner=runner).stdout.strip()
    return out


# git fetch ohne Rückfragen (kein Passwort-Prompt, ssh ohne Eingabe) und mit Zeitlimit
FETCH_ENV = {"GIT_TERMINAL_PROMPT": "0", "GIT_SSH_COMMAND": "ssh -o BatchMode=yes -o ConnectTimeout=10"}


def remote_default(root: Path, runner: Runner = subprocess.run) -> str:
    """Hauptbranch des Repos auf dem Server (origin/HEAD) – leer, wenn nicht feststellbar."""
    try:
        r = _git(root, "ls-remote", "--symref", "origin", "HEAD", runner=runner, timeout=45,
                 env={**os.environ, **FETCH_ENV})
    except subprocess.TimeoutExpired:
        return ""
    m = re.search(r"^ref: refs/heads/(\S+)\s+HEAD", r.stdout or "", re.M)
    return m.group(1) if m else ""


def target_branch(root: Path, branch: str, runner: Runner = subprocess.run) -> str:
    """Welchem Branch folgt das Update? Dem ausgecheckten – außer er ist auf dem Server weg (umbenannt, gelöscht) oder
    enthält nichts, was der Hauptbranch nicht schon hat (z. B. ein alter, längst gemergter Branch): dann dem
    Hauptbranch. Sonst blieben Installationen für immer auf einem alten Stand stehen. Eigene, nicht gemergte Commits
    bleiben, wo sie sind (dann gilt der ausgecheckte Branch weiter)."""
    default = remote_default(root, runner)
    if not default or default == branch:
        return branch
    try:
        fetched = _git(root, "fetch", "--quiet", "origin", default, runner=runner, timeout=45,
                       env={**os.environ, **FETCH_ENV})
        heads = _git(root, "ls-remote", "--heads", "origin", branch, runner=runner, timeout=45,
                     env={**os.environ, **FETCH_ENV})
    except subprocess.TimeoutExpired:
        return branch
    if fetched.returncode != 0:
        return branch
    if heads.returncode == 0 and not heads.stdout.strip():
        return default
    merged = _git(root, "merge-base", "--is-ancestor", "HEAD", f"origin/{default}", runner=runner).returncode == 0
    return default if merged else branch


def check(root: Path | None = None, runner: Runner = subprocess.run, fetch: bool = True) -> dict:
    """Gibt es einen neueren Stand? → details + behind (Anzahl), changes (Commit-Titel), can_update, reason."""
    root = root or project_root()
    info = {**details(root, runner), "behind": 0, "changes": [], "can_update": False, "reason": "", "error": ""}
    if not info["git"]:
        info["reason"] = T("kein Git-Checkout – Update nur über scripts/bootstrap.sh",
                           "not a git checkout – update via scripts/bootstrap.sh")
        return info
    branch = info["branch"]
    if branch in ("", "HEAD"):
        info["reason"] = T("kein Branch ausgecheckt", "no branch checked out")
        return info
    if fetch:
        branch = target_branch(root, branch, runner)
    info["target"] = branch
    if fetch:
        try:
            r = _git(root, "fetch", "--quiet", "origin", branch, runner=runner, timeout=45,
                     env={**os.environ, **FETCH_ENV})
        except subprocess.TimeoutExpired:
            info["error"] = T("Zeitüberschreitung beim Abrufen", "timed out while fetching")
            return info
        if r.returncode != 0:
            info["error"] = (r.stderr or r.stdout).strip().splitlines()[-1:][0] if (r.stderr or r.stdout).strip() \
                else T("Abrufen fehlgeschlagen", "fetch failed")
            return info
    remote = f"origin/{branch}"
    counted = _git(root, "rev-list", "--count", f"HEAD..{remote}", runner=runner)
    if counted.returncode != 0:
        info["error"] = T(f"{remote} nicht gefunden", f"{remote} not found")
        return info
    info["behind"] = int(counted.stdout.strip() or 0)
    if info["behind"]:
        log = _git(root, "log", "--format=%s", "-n", "30", f"HEAD..{remote}", runner=runner).stdout
        info["changes"] = [line for line in log.splitlines() if line.strip()]
    dirty = _git(root, "status", "--porcelain", "--untracked-files=no", runner=runner).stdout.strip()
    ff = _git(root, "merge-base", "--is-ancestor", "HEAD", remote, runner=runner).returncode == 0
    if dirty:
        info["reason"] = T("lokale Änderungen im Projektordner – erst sichern (git stash)",
                           "local changes in the project folder – save them first (git stash)")
    elif not ff:
        info["reason"] = T("der lokale Stand weicht vom Server ab – von Hand zusammenführen",
                           "the local version differs from the server – merge by hand")
    info["can_update"] = bool(info["behind"]) and not dirty and ff
    return info


def server_running(port: int) -> bool:
    try:  # /api/alive braucht kein Zugangs-Token (web_auth.py)
        return httpx.get(f"http://127.0.0.1:{port}/api/alive", timeout=2,
                         headers={"Host": f"localhost:{port}"}).status_code == 200
    except httpx.HTTPError:
        return False


def _is_server(pid: int) -> bool:
    """Wirklich ein `orbwise serve` (Programm orbwise, erstes Argument serve) – nicht etwa eine Shell, ein Editor oder
    `tail -f`, in deren Befehlszeile der Text nur vorkommt."""
    try:
        args = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
    except OSError:
        return True  # ohne /proc: wie bisher dem pgrep-Treffer glauben
    names = [os.path.basename(a.decode(errors="replace")) for a in args]
    return any(n == "orbwise" and i + 1 < len(names) and names[i + 1] == "serve" for i, n in enumerate(names))


def systemd_unit() -> str | None:
    """Läuft Orbwise als systemd-Dienst? Dann den Namen der Unit (aus /proc/self/cgroup)."""
    if not os.environ.get("INVOCATION_ID"):
        return None
    try:
        text = Path("/proc/self/cgroup").read_text(encoding="utf-8")
    except OSError:
        return None
    m = re.search(r"/([^/\n]+\.service)\s*$", text, re.M)
    return m.group(1) if m else None


def restart_self(port: int, root: Path) -> None:
    """Neustart aus dem laufenden Server heraus (nach einem Update aus der Oberfläche): als systemd-Dienst über
    systemctl, sonst startet ein kleiner Helfer `orbwise serve`, sobald der Port frei ist – dann beendet sich dieser
    Prozess sauber (SIGTERM: Modell-Server usw. werden gestoppt)."""
    unit = systemd_unit()
    if unit and shutil.which("systemctl"):
        subprocess.Popen(["systemctl", "--user", "--no-block", "restart", unit], start_new_session=True,
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return
    launcher = Path.home() / ".local/bin/orbwise"
    cmd = [str(launcher)] if launcher.exists() else [str(root / ".venv/bin/orbwise")]
    log_dir = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state"))
    log_dir.mkdir(parents=True, exist_ok=True)
    helper = (
        "import socket, subprocess, sys, time\n"
        "for _ in range(240):\n"
        "    s = socket.socket()\n"
        "    try:\n"
        f"        s.connect(('127.0.0.1', {int(port)}))\n"
        "    except OSError:\n"
        "        break\n"
        "    finally:\n"
        "        s.close()\n"
        "    time.sleep(0.25)\n"
        f"log = open({str(log_dir / 'orbwise.log')!r}, 'ab')\n"
        f"subprocess.Popen({[*cmd, 'serve']!r}, stdout=log, stderr=log, stdin=subprocess.DEVNULL, "
        f"start_new_session=True, cwd={str(Path.home())!r})\n"
    )
    subprocess.Popen([sys.executable, "-c", helper], start_new_session=True, stdin=subprocess.DEVNULL,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    os.kill(os.getpid(), signal.SIGTERM)


def stop_servers(port: int) -> int:
    """Beendet laufende `orbwise serve`-Prozesse (SIGTERM – sie räumen auf wie bei Strg+C) und wartet, bis der Port
    frei ist. Liefert, wie viele es waren."""
    pids = subprocess.run(["pgrep", "-f", "orbwise serve"], capture_output=True, text=True).stdout.split()
    stopped = 0
    for pid in pids:
        if int(pid) != os.getpid() and _is_server(int(pid)):
            try:
                os.kill(int(pid), signal.SIGTERM)
                stopped += 1
            except (ProcessLookupError, PermissionError):
                pass
    for _ in range(40):
        if not server_running(port):
            break
        time.sleep(0.25)
    return stopped


def restart_server(port: int, root: Path, out: Callable[[str], None] = print) -> None:
    """Beendet laufende `orbwise serve`-Prozesse und startet im Hintergrund neu."""
    stop_servers(port)
    launcher = Path.home() / ".local/bin/orbwise"
    cmd = [str(launcher)] if launcher.exists() else [str(root / ".venv/bin/orbwise")]
    log_dir = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state"))
    log_dir.mkdir(parents=True, exist_ok=True)
    with (log_dir / "orbwise.log").open("ab") as log:
        subprocess.Popen([*cmd, "serve"], stdout=log, stderr=log, stdin=subprocess.DEVNULL,
                         start_new_session=True, cwd=str(Path.home()))
    for _ in range(60):
        if server_running(port):
            out(T("✔ Server neu gestartet – Browser-Seite neu laden.", "✔ Server restarted – reload the browser page."))
            out(T("Er läuft jetzt im Hintergrund – beenden mit  orbwise stop  oder in der Oberfläche unten links "
                  "mit „Beenden“.", "It now runs in the background – stop it with  orbwise stop  or with \"Quit\" "
                  "at the bottom left of the interface."))
            return
        time.sleep(0.5)
    out(T("Server startet noch …", "Server is still starting …") + f" (Log: {log_dir / 'orbwise.log'})")


def update(root: Path | None = None, port: int = 8765, runner: Runner = subprocess.run,
           restart: Callable[[int, Path], None] | None = None, out: Callable[[str], None] = print) -> int:
    root = root or project_root()
    if not (root / ".git").exists():
        out(f"{root} " + T("ist kein Git-Checkout (vermutlich ein ZIP-Download) – automatische Updates gehen so nicht.",
                           "is not a git checkout (probably a ZIP download) – automatic updates are not possible."))
        out(T("Einmalig umziehen:  bash scripts/bootstrap.sh   (legt ~/orbwise per Git an und installiert)",
              "Move once:  bash scripts/bootstrap.sh   (creates ~/orbwise via git and installs)"))
        return 1
    if not shutil.which("git"):
        out(T("git ist nicht installiert", "git is not installed") + ":  sudo pacman -S git  /  sudo apt install git")
        return 1

    dirty = _git(root, "status", "--porcelain", "--untracked-files=no", runner=runner).stdout.strip()
    if dirty:
        out(T("Im Projektordner gibt es lokale Änderungen – ich überschreibe nichts:",
              "The project folder has local changes – I won't overwrite anything:"))
        out(dirty)
        out(T("Sichern mit", "Save them with") + f":  git -C {root} stash   " + T("(danach erneut orbwise update)", "(then run orbwise update again)"))
        return 1

    before = _git(root, "rev-parse", "HEAD", runner=runner).stdout.strip()
    branch = _git(root, "rev-parse", "--abbrev-ref", "HEAD", runner=runner).stdout.strip() or "HEAD"
    out(T("Aktueller Stand: ", "Current version: ") + version(root, runner))
    target = target_branch(root, branch, runner) if branch != "HEAD" else branch
    if target != branch:
        out(T(f"Der Branch {branch} wird nicht mehr weitergeführt – wechsle auf {target} …",
              f"Branch {branch} is no longer maintained – switching to {target} …"))
        exists = _git(root, "rev-parse", "--verify", "--quiet", f"refs/heads/{target}", runner=runner).returncode == 0
        switched = _git(root, "checkout", "-q", target, runner=runner) if exists else \
            _git(root, "checkout", "-q", "-b", target, "--track", f"origin/{target}", runner=runner)
        if switched.returncode != 0:
            out(T("Wechsel fehlgeschlagen:", "Switching failed:"))
            out((switched.stderr or switched.stdout).strip())
            return 1
        branch = target
    out(T("Hole Updates", "Fetching updates") + f" ({branch}) …")
    pulled = _git(root, "pull", "--ff-only", "origin", branch, runner=runner)
    if pulled.returncode != 0:
        out(T("Update fehlgeschlagen:", "Update failed:"))
        out((pulled.stderr or pulled.stdout).strip())
        return 1
    after = _git(root, "rev-parse", "HEAD", runner=runner).stdout.strip()

    venv_ok = (root / ".venv/bin/orbwise").exists()
    if before == after:
        out(T("✔ Bereits auf dem neuesten Stand.", "✔ Already up to date."))
        if venv_ok:
            return 0
    else:
        log = _git(root, "log", "--format=  • %s", f"{before}..{after}", runner=runner).stdout.rstrip()
        out(T("Neu:", "New:"))
        out(log or T("  (keine Beschreibung)", "  (no description)"))

    uv = shutil.which("uv")
    if not uv:
        out(T("uv fehlt – bitte scripts/install.sh ausführen.", "uv is missing – please run scripts/install.sh."))
        return 1
    out(T("Aktualisiere Python-Abhängigkeiten …", "Updating Python dependencies …"))
    synced = runner([uv, "sync", "--extra", "voice", "--quiet"], cwd=str(root))
    if synced.returncode != 0:
        out(T("uv sync ist fehlgeschlagen – Details siehe oben.", "uv sync failed – see the details above."))
        return 1
    out(T("✔ Aktualisiert auf: ", "✔ Updated to: ") + version(root, runner))

    if server_running(port):
        out(T("Starte laufenden Orbwise-Server neu …", "Restarting the running Orbwise server …"))
        (restart or restart_server)(port, root)
    else:
        out(T("Starten mit:  orbwise serve --open", "Start with:  orbwise serve --open"))
    return 0


def main_update(port: int) -> None:
    sys.exit(update(port=port))
