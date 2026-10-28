"""Hilfsfunktionen zum Ausführen von Prozessen mit Live-Ausgabe, Timeout und Abbruch."""

from __future__ import annotations

import asyncio
import contextlib
import glob
import os
import re
import signal
import subprocess
import tempfile

from .registry import ToolContext

QUIET_ENV = {"TERM": "dumb", "PAGER": "cat", "SYSTEMD_PAGER": "", "GIT_PAGER": "cat", "NO_COLOR": "1",
             "SYSTEMD_COLORS": "0"}


def clip(text: str, limit: int) -> str:
    """Behält Anfang und Ende, falls die Ausgabe zu lang ist."""
    if len(text) <= limit:
        return text
    head = limit // 4
    tail = limit - head
    return f"{text[:head]}\n… [{len(text) - limit} Zeichen ausgelassen] …\n{text[-tail:]}"


OUTPUTS_KEEP = 100  # so viele gespeicherte große Ausgaben bleiben liegen (älteste zuerst weg)
SAVED_NOTE = re.compile(r"\[Vollständige Ausgabe \([^)]*\): (\S+) – ")  # Hinweis von clip_saved (Pfad)


def outputs_dir() -> str:
    base = os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache")
    path = os.path.join(base, "orbwise", "outputs")
    os.makedirs(path, mode=0o700, exist_ok=True)
    return path


def save_output(text: str, label: str = "ausgabe") -> str | None:
    """Ausgabe vollständig in ~/.cache/orbwise/outputs ablegen (nur für den Nutzer lesbar) – liefert den Pfad.
    Die ältesten Dateien fallen weg, wenn es mehr als OUTPUTS_KEEP werden."""
    try:
        folder = outputs_dir()
        fd, path = tempfile.mkstemp(prefix=f"{re.sub(r'[^a-z0-9-]', '', label.lower())[:20] or 'ausgabe'}-",
                                    suffix=".txt", dir=folder)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        files = sorted(glob.glob(os.path.join(folder, "*.txt")), key=os.path.getmtime)
        for old in files[:-OUTPUTS_KEEP]:
            with contextlib.suppress(OSError):
                os.remove(old)
    except OSError:
        return None
    return path


def clip_saved(text: str, limit: int, label: str = "ausgabe") -> str:
    """Wie clip – ist die Ausgabe zu lang, wird sie aber vollständig in einer Datei abgelegt und der Pfad genannt.
    So geht nichts verloren und das Modell kann gezielt Teile nachlesen (read_file mit offset/limit), statt dass
    die Mitte einfach fehlt (wie Claude Code mit großen Ausgaben umgeht)."""
    if len(text) <= limit:
        return text
    path = save_output(text, label)
    if not path:
        return clip(text, limit)
    lines = text.count("\n") + 1
    note = (f"\n[Vollständige Ausgabe ({len(text)} Zeichen, {lines} Zeilen): {path} – bei Bedarf Teile mit "
            f"read_file bzw. read (path, offset, limit) lesen]")
    return clip(text, max(500, limit - len(note))) + note


def _kill(proc: asyncio.subprocess.Process) -> None:
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        pass


_ASKPASS_RE = re.compile(r"\bsudo\s+(?:-\w+\s+)*-A\b|--sudoflags\s+-A\b")


async def run(ctx: ToolContext, cmd: str | list[str], timeout: float, stream: bool = True,
              cwd: str | None = None, started=None) -> tuple[int | None, str]:
    """started: wird mit der PID des gestarteten Prozesses aufgerufen (Käfig: jail.register)."""
    text = cmd if isinstance(cmd, str) else " ".join(cmd)
    if _ASKPASS_RE.search(text):
        # Root-Rechte über den Passwortdialog der Oberfläche (sudo -A → Orbwise-Askpass); Auto-Modus ohne eigene
        # Karte: dann immer fragen, nie das gemerkte Passwort
        from ..askpass import privileged_env
        with privileged_env(text, use_cache=not getattr(ctx, "fresh_sudo", False)) as extra:
            return await _run(ctx, cmd, timeout, stream, cwd, extra, started)
    return await _run(ctx, cmd, timeout, stream, cwd, {}, started)


async def _run(ctx: ToolContext, cmd: str | list[str], timeout: float, stream: bool, cwd: str | None,
               extra_env: dict, started=None) -> tuple[int | None, str]:
    env = {**os.environ, **QUIET_ENV, **extra_env}
    kwargs = dict(stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                  cwd=cwd or os.path.expanduser("~"), env=env, start_new_session=True)
    if isinstance(cmd, str):
        proc = await asyncio.create_subprocess_shell(cmd, executable="/bin/bash", **kwargs)
    else:
        try:
            proc = await asyncio.create_subprocess_exec(*cmd, **kwargs)
        except FileNotFoundError:
            return 127, f"Programm '{cmd[0]}' ist nicht installiert."
    if started:
        started(proc.pid)

    chunks: list[str] = []

    async def pump() -> None:
        assert proc.stdout
        while True:
            line = await proc.stdout.readline()
            if not line:
                break
            text = line.decode(errors="replace")
            chunks.append(text)
            if stream:
                await ctx.output(text)

    try:
        await asyncio.wait_for(pump(), timeout=timeout)
        rc = await proc.wait()
    except asyncio.TimeoutError:
        _kill(proc)
        chunks.append(f"\n[Abgebrochen: Zeitlimit von {int(timeout)} s überschritten]\n")
        rc = None
    except asyncio.CancelledError:
        _kill(proc)
        raise
    return rc, "".join(chunks)


DESKTOP_VARS = ("DISPLAY", "WAYLAND_DISPLAY", "XDG_RUNTIME_DIR", "DBUS_SESSION_BUS_ADDRESS", "XAUTHORITY",
                "XDG_CURRENT_DESKTOP", "XDG_SESSION_TYPE", "DESKTOP_SESSION", "KDE_FULL_SESSION", "XDG_DATA_DIRS")


def _systemd_user_env() -> dict[str, str]:
    try:
        out = subprocess.run(["systemctl", "--user", "show-environment"], capture_output=True, text=True,
                             timeout=3).stdout
    except (OSError, subprocess.SubprocessError):
        return {}
    env = {}
    for line in out.splitlines():
        key, sep, value = line.partition("=")
        if sep and key in DESKTOP_VARS:
            env[key] = value
    return env


def desktop_env() -> dict[str, str]:
    """Umgebung mit Zugriff auf die grafische Sitzung – auch wenn Orbwise z. B. als Dienst gestartet wurde."""
    env = {**os.environ}
    if not (env.get("DISPLAY") or env.get("WAYLAND_DISPLAY")) or not env.get("DBUS_SESSION_BUS_ADDRESS"):
        for key, value in _systemd_user_env().items():
            env.setdefault(key, value)
    runtime = env.setdefault("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
    if not env.get("DBUS_SESSION_BUS_ADDRESS") and os.path.exists(f"{runtime}/bus"):
        env["DBUS_SESSION_BUS_ADDRESS"] = f"unix:path={runtime}/bus"
    if not env.get("WAYLAND_DISPLAY"):
        sockets = sorted(glob.glob(f"{runtime}/wayland-[0-9]"))
        if sockets:
            env["WAYLAND_DISPLAY"] = os.path.basename(sockets[0])
    if not env.get("DISPLAY") and os.path.exists("/tmp/.X11-unix/X0"):
        env["DISPLAY"] = ":0"
    return env


def has_display(env: dict[str, str] | None = None) -> bool:
    env = env if env is not None else desktop_env()
    return bool(env.get("DISPLAY") or env.get("WAYLAND_DISPLAY"))


async def launch(argv: list[str], wait: float = 2.0) -> tuple[bool, str]:
    """Startet ein Programm unabhängig von Orbwise und prüft kurz, ob es sofort mit Fehler endet.
    Liefert (erfolgreich, Fehlermeldung)."""
    with tempfile.TemporaryFile() as err:
        try:
            p = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=err,
                                 start_new_session=True, cwd=os.path.expanduser("~"), env=desktop_env())
        except OSError as e:
            return False, str(e)
        waited = 0.0
        while waited < wait:
            rc = p.poll()
            if rc is not None:
                if rc == 0:
                    return True, ""
                err.seek(0)
                msg = err.read().decode(errors="replace").strip()
                return False, f"Exit-Code {rc}" + (f": {msg[-500:]}" if msg else "")
            await asyncio.sleep(0.1)
            waited += 0.1
    return True, ""  # läuft noch → Programm wurde gestartet


def spawn_detached(argv: list[str]) -> None:
    """Startet ein Programm unabhängig von Orbwise (überlebt dessen Neustart)."""
    subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     start_new_session=True, cwd=os.path.expanduser("~"), env=desktop_env())


ROOT_DENIED_RE = re.compile(
    r"sudo: (?:\d+ )?(?:incorrect password|no password was provided|a password is required|"
    r"no askpass program|a terminal is required)|is not in the sudoers file|Sorry, try again|"
    r"Error executing command as another user|Request dismissed|No authentication agent found|"
    r"pkexec: .*not authorized|Not authorized", re.I)

ROOT_DENIED_HINT = ("\n→ Root-Rechte wurden NICHT erteilt (Passwort abgebrochen, falsch oder nicht eingegeben). "
                    "Nicht mit anderen Befehlen weiterprobieren oder Rechte prüfen – sag dem Nutzer Bescheid.")


# Befehl brauchte Root, lief aber ohne sudo (pacman -S …, apt install …)
NEEDS_ROOT_RE = re.compile(r"unless you are root|are you root\?|must be run as root|need to be root|"
                           r"Could not open lock file .*Permission denied|unable to lock database", re.I)
NEEDS_ROOT_HINT = ("\n→ Der Befehl braucht Root-Rechte: mit sudo voranstellen (Orbwise fragt dann nach dem Passwort) – "
                   "Pakete besser mit install_package installieren (kann auch AUR).")


def root_denied(output: str) -> bool:
    return bool(ROOT_DENIED_RE.search(output or ""))


def format_result(rc: int | None, output: str, limit: int) -> str:
    status = "Zeitüberschreitung" if rc is None else f"Exit-Code {rc}"
    body = clip_saved(output.strip(), limit) or "(keine Ausgabe)"
    hint = ROOT_DENIED_HINT if rc not in (None, 0) and root_denied(output) else ""
    if not hint and rc not in (None, 0) and NEEDS_ROOT_RE.search(output or ""):
        hint = NEEDS_ROOT_HINT
    return f"{status}\n{body}{hint}"
