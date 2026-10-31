"""`orbwise update`: neuen Stand per Git holen, Abhängigkeiten aktualisieren, laufenden Server neu starten.

Konfiguration (~/.config/orbwise), Stimmen und Gedächtnis (~/.local/share/orbwise) liegen außerhalb des
Projektordners und werden dabei nicht angefasst.
"""

from __future__ import annotations

import os
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


def _git(root: Path, *args: str, runner: Runner = subprocess.run) -> subprocess.CompletedProcess:
    return runner(["git", "-C", str(root), *args], capture_output=True, text=True)


def version(root: Path | None = None, runner: Runner = subprocess.run) -> str:
    root = root or project_root()
    if not (root / ".git").exists() or not shutil.which("git"):
        return T("unbekannt (kein Git-Checkout)", "unknown (not a git checkout)")
    r = _git(root, "log", "-1", "--format=%h %s (%cd)", "--date=format:%d.%m.%Y", runner=runner)
    return r.stdout.strip() or T("unbekannt", "unknown")


def server_running(port: int) -> bool:
    try:
        return httpx.get(f"http://127.0.0.1:{port}/api/status", timeout=2,
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
