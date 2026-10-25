"""Andere Rechner (NAS, Server) per SSH: anmelden und dort Linux-Befehle ausführen wie mit run_shell.

Benutzername und Passwort werden bei jeder neuen Verbindung im Dashboard abgefragt (Anmeldedialog) – sie stehen
nirgends in der Config, gehen nie an das Sprachmodell und nie auf die Platte. Das System-`ssh` meldet sich über den
Orbwise-Askpass-Helfer an (SSH_ASKPASS_REQUIRE=force) und hält die Verbindung als „Master“ offen
(ControlPersist, Standard 15 Minuten nach dem letzten Befehl) – Folgebefehle brauchen kein Passwort. Danach oder
nach ssh_disconnect wird wieder gefragt.

Befehle werden wie auf dem eigenen Rechner eingestuft (safety.classify_command): lesende laufen ohne Rückfrage,
alles andere muss bestätigt werden – auch im Auto-Modus. `sudo` auf dem Zielrechner fragt das Passwort ebenfalls
im Dashboard ab (auf Wunsch für die Dauer der Verbindung gemerkt, nur im Arbeitsspeicher).
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import os
import re
import shutil
import signal
import subprocess
import tempfile
import time
from dataclasses import dataclass
from typing import Annotated, Any

from ..lang import T
from . import proc
from .registry import BLOCKED, CONFIRM, SAFE, ToolContext, tool
from .safety import classify_command

SSH = "ssh"  # für Tests austauschbar


@dataclass
class Session:
    name: str
    host: str
    port: int
    user: str
    socket: str
    sudo_password: str = ""  # nur auf Wunsch und nur im Arbeitsspeicher
    opened: float = 0.0
    info: str = ""


SESSIONS: dict[str, Session] = {}
_LOCKS: dict[str, asyncio.Lock] = {}

_SUDO = re.compile(r"(?:^|[;&|(]|\b(?:then|do|else)\s)\s*sudo(?=\s)")  # sudo an Befehlsposition
_SUDO_FAILED = re.compile(r"incorrect password|Sorry, try again|\d+ incorrect password attempts|"
                          r"no password was provided", re.I)
_HOST_KEY_CHANGED = re.compile(r"REMOTE HOST IDENTIFICATION HAS CHANGED|Host key verification failed", re.I)
_AUTH_FAILED = re.compile(r"Permission denied|Authentication failed|Too many authentication failures", re.I)


def _enabled(cfg: Any) -> bool:
    return shutil.which(SSH) is not None


def resolve(cfg: Any, host: str, user: str = "", port: int = 0) -> tuple[str, str, int, str]:
    """(Kurzname, Adresse, Port, Benutzer-Vorschlag) – Kurznamen aus ssh.hosts, sonst „user@host:port“ direkt."""
    raw = (host or "").strip()
    known = {k.lower(): (k, v) for k, v in cfg.ssh.hosts.items()}
    if raw.lower() in known:
        name, h = known[raw.lower()]
        return name, h.host, port or h.port, user or h.user
    if "@" in raw:
        user, raw = (user or raw.split("@", 1)[0]), raw.split("@", 1)[1]
    m = re.fullmatch(r"\[(.+)\](?::(\d+))?", raw) or re.fullmatch(r"([^:]+):(\d+)", raw)
    if m:  # host:port bzw. [IPv6]:port
        raw = m.group(1)
        if m.group(2):
            port = port or int(m.group(2))
    for name, h in cfg.ssh.hosts.items():  # Adresse eines Kurznamens → dessen Angaben
        if h.host.lower() == raw.lower():
            return name, h.host, port or h.port, user or h.user
    return raw, raw, port or 22, user


def socket_path(host: str, port: int, user: str) -> str:
    from ..askpass import helper_path
    folder = helper_path().parent
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    digest = hashlib.sha1(f"{user}@{host}:{port}".encode()).hexdigest()[:12]
    return str(folder / f"ssh-{digest}")  # kurz: Unix-Sockets dürfen höchstens ~100 Zeichen lang sein


def _base(s: Session) -> list[str]:
    return [SSH, "-S", s.socket, "-p", str(s.port), "-l", s.user]


def _env(extra: dict | None = None) -> dict:
    env = {**os.environ, **proc.QUIET_ENV, **(extra or {})}
    env.setdefault("DISPLAY", ":0")  # ältere ssh nutzen den Askpass-Helfer nur mit DISPLAY
    return env


async def _call(argv: list[str], timeout: float, env: dict | None = None,
                stdin_text: str | None = None) -> tuple[int | None, str]:
    """Kurzer ssh-Aufruf ohne Live-Ausgabe; stderr landet in einer Datei (ein Master im Hintergrund hält sonst die
    Pipe offen)."""
    with tempfile.TemporaryFile() as err:
        try:
            p = await asyncio.create_subprocess_exec(
                *argv, stdin=subprocess.PIPE if stdin_text is not None else subprocess.DEVNULL,
                stdout=subprocess.DEVNULL, stderr=err, env=_env(env), start_new_session=True)
        except FileNotFoundError:
            return 127, T("ssh ist nicht installiert (Paket openssh).", "ssh is not installed (package openssh).")
        try:
            if stdin_text is not None:
                await asyncio.wait_for(p.communicate(stdin_text.encode()), timeout=timeout)
                rc = p.returncode
            else:
                rc = await asyncio.wait_for(p.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(p.pid, signal.SIGTERM)
            rc = None
        err.seek(0)
        return rc, err.read().decode(errors="replace").strip()


async def alive(s: Session) -> bool:
    rc, _ = await _call([*_base(s), "-O", "check", s.host], timeout=10)
    return rc == 0


def _connect_error(rc: int | None, err: str, target: str) -> str:
    if rc is None:
        return T(f"{target} antwortet nicht (Zeitüberschreitung) – Adresse/Port prüfen.",
                 f"{target} does not answer (timeout) – check address/port.")
    if _HOST_KEY_CHANGED.search(err):
        return T(f"Der Schlüssel von {target} hat sich geändert – möglicher Angriff oder neu installiertes Gerät. "
                 "Nicht automatisch übergangen: der Nutzer muss den alten Eintrag selbst prüfen und entfernen "
                 "(ssh-keygen -R <adresse>).",
                 f"The host key of {target} changed – possible attack or reinstalled device. Not bypassed "
                 "automatically: the user has to check and remove the old entry (ssh-keygen -R <address>).")
    if _AUTH_FAILED.search(err):
        return T(f"Anmeldung an {target} abgelehnt – Benutzername oder Passwort falsch (oder Passwort-Anmeldung "
                 "auf dem Gerät abgeschaltet). Nicht erneut versuchen, ohne den Nutzer zu fragen.",
                 f"Login to {target} refused – wrong user name or password (or password login disabled on the "
                 "device). Don't retry without asking the user.")
    return T(f"Verbindung zu {target} fehlgeschlagen: ", f"Connecting to {target} failed: ") + (err[-400:] or f"Exit {rc}")


async def open_session(ctx: ToolContext, host: str, user: str = "", port: int = 0) -> Session | str:
    """Bestehende Verbindung oder neue Anmeldung (fragt Benutzer + Passwort im Dashboard) → Session oder Fehlertext."""
    from .. import askpass
    name, address, port, user_hint = resolve(ctx.cfg, host, user, port)
    if not address:
        return T("Kein Rechner angegeben.", "No host given.")
    lock = _LOCKS.setdefault(name.lower(), asyncio.Lock())
    async with lock:
        old = SESSIONS.get(name.lower())
        if old and await alive(old):
            return old
        SESSIONS.pop(name.lower(), None)
        broker = askpass.BROKER
        if broker is None or not broker.ready():
            return T("Anmelden geht nur über das Dashboard (Orbwise-Server mit Askpass-Helfer) – siehe orbwise doctor.",
                     "Logging in only works through the dashboard (Orbwise server with askpass helper) – see orbwise doctor.")
        target = f"{address}" + (f":{port}" if port != 22 else "")
        login = await broker.ask_login(T(f"SSH-Anmeldung bei {name}", f"SSH login to {name}"),
                                       command=f"ssh {target}", user=user_hint)
        if not login:
            return T("Anmeldung abgebrochen bzw. nicht möglich (nur am Rechner im Dashboard, nicht per Telegram oder "
                     "Routine). Nicht erneut versuchen – sag dem Nutzer Bescheid.",
                     "Login cancelled or not possible (only in the dashboard on this computer, not via Telegram or a "
                     "routine). Don't retry – tell the user.")
        login_user, password, _ = login
        s = Session(name=name, host=address, port=port, user=login_user,
                    socket=socket_path(address, port, login_user), opened=time.time())
        with contextlib.suppress(OSError):
            os.unlink(s.socket)  # Rest einer abgebrochenen Verbindung
        argv = [*_base(s), "-M", "-f", "-N",
                "-o", f"ControlPersist={max(1, ctx.cfg.ssh.keep_minutes)}m",
                "-o", "StrictHostKeyChecking=accept-new",  # neues Gerät: Schlüssel merken; geänderter: abbrechen
                "-o", "PubkeyAuthentication=no", "-o", "PreferredAuthentications=keyboard-interactive,password",
                "-o", "NumberOfPasswordPrompts=1", "-o", f"ConnectTimeout={ctx.cfg.ssh.connect_timeout}",
                "-o", "ServerAliveInterval=30", s.host]
        with broker.grant_secret(password) as extra:
            password = ""
            rc, err = await _call(argv, timeout=ctx.cfg.ssh.connect_timeout + 30, env=extra)
        if rc != 0:
            return _connect_error(rc, err, f"{login_user}@{target}")
        rc, out = await _run(s, "uname -srm; hostname", timeout=15)
        s.info = " · ".join(x.strip() for x in out.splitlines() if x.strip())[:200] if rc == 0 else ""
        SESSIONS[name.lower()] = s
        return s


async def _run(s: Session, command: str, timeout: float, ctx: ToolContext | None = None,
               stdin_text: str | None = None) -> tuple[int | None, str]:
    """Befehl über die offene Verbindung (BatchMode: ist sie weg, wird nicht still neu nach dem Passwort gefragt)."""
    argv = [*_base(s), "-o", "ControlMaster=no", "-o", "BatchMode=yes", s.host, "--", command]
    try:
        p = await asyncio.create_subprocess_exec(
            *argv, stdin=subprocess.PIPE if stdin_text is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=_env(), start_new_session=True)
    except FileNotFoundError:
        return 127, T("ssh ist nicht installiert.", "ssh is not installed.")
    if stdin_text is not None:
        assert p.stdin
        with contextlib.suppress(BrokenPipeError, ConnectionResetError):
            p.stdin.write(stdin_text.encode())
            await p.stdin.drain()
            p.stdin.close()
    chunks: list[str] = []

    async def pump() -> None:
        assert p.stdout
        while line := await p.stdout.readline():
            text = line.decode(errors="replace")
            chunks.append(text)
            if ctx:
                await ctx.output(text)

    try:
        await asyncio.wait_for(pump(), timeout=timeout)
        rc = await p.wait()
    except asyncio.TimeoutError:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(p.pid, signal.SIGTERM)
        chunks.append(T(f"\n[Abgebrochen: Zeitlimit von {int(timeout)} s überschritten]\n",
                        f"\n[Aborted: time limit of {int(timeout)} s exceeded]\n"))
        rc = None
    except asyncio.CancelledError:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(p.pid, signal.SIGTERM)
        raise
    return rc, "".join(chunks)


# sudo auf dem Zielrechner: Das Passwort kommt über stdin, wird aber nur von `sudo -v` gelesen (frischt den
# Zeitstempel auf) – der eigentliche Befehl bekommt eine leere Eingabe. So landet es nie in dessen stdin (z. B.
# `sudo tee datei`), nie in der Befehlszeile (ps) und nie in der Ausgabe. sudo merkt sich die Anmeldung ohne Terminal
# je Elternprozess – die Shell, die danach den Befehl ausführt.
SUDO_PREFIX = ("IFS= read -r __orbwise_pw; printf '%s\\n' \"$__orbwise_pw\" | sudo -S -p '' -v 2>/dev/null; "
               "__orbwise_rc=$?; unset __orbwise_pw; "
               "if [ $__orbwise_rc -ne 0 ]; then echo 'sudo: incorrect password' >&2; exit 1; fi; ")


def with_sudo(command: str) -> str:
    # In einer Gruppe mit nachfolgendem exit: sonst ersetzt die Shell sich beim letzten Befehl durch ihn (exec) –
    # dann ist sudos Elternprozess ein anderer und die eben bestätigte Anmeldung gilt nicht
    return SUDO_PREFIX + "exec </dev/null; {\n" + command + "\n}; exit $?"


def _risk(ctx: ToolContext, args: dict) -> tuple[str, str]:
    command = str(args.get("command", ""))
    risk, reason = classify_command(command, None)
    if risk == BLOCKED:
        return risk, reason
    where = str(args.get("host") or "")
    if risk == SAFE:
        return SAFE, T(f"nur lesend auf {where}", f"read-only on {where}")
    return CONFIRM, (reason + " · " if reason else "") + T(f"auf {where}", f"on {where}")


@tool(
    "Verbindet per SSH mit einem anderen Rechner (NAS, Server). Benutzer und Passwort fragt Orbwise im Dashboard "
    "ab – NIE im Chat danach fragen. Nur zum Vorab-Verbinden; ssh_run verbindet selbst.",
    enabled=_enabled,
)
async def ssh_connect(
    ctx: ToolContext,
    host: Annotated[str, "Kurzname aus der Config (z. B. 'nas') oder [user@]host[:port]"],
    user: Annotated[str, "Vorschlag für den Anmeldedialog"] = "",
    port: Annotated[int, "Standard 22"] = 0,
) -> str:
    s = await open_session(ctx, host, user, port)
    if isinstance(s, str):
        return s
    return T(f"Verbunden: {s.user}@{s.host} ({s.name})" + (f" – {s.info}" if s.info else "") +
             f". Befehle mit ssh_run(host='{s.name}', command=…). Die Verbindung bleibt "
             f"{ctx.cfg.ssh.keep_minutes} min nach dem letzten Befehl offen.",
             f"Connected: {s.user}@{s.host} ({s.name})" + (f" – {s.info}" if s.info else "") +
             f". Run commands with ssh_run(host='{s.name}', command=…). The connection stays open for "
             f"{ctx.cfg.ssh.keep_minutes} min after the last command.")


@tool(
    "Führt einen Bash-Befehl auf einem ANDEREN Rechner per SSH aus (verbindet bei Bedarf). Root: 'sudo' "
    "voranstellen. Keine interaktiven Programme.",
    risk=_risk,
    enabled=_enabled,
)
async def ssh_run(
    ctx: ToolContext,
    host: Annotated[str, "Kurzname oder Adresse"],
    command: str,
    timeout_seconds: Annotated[int, "Standard 120"] = 0,
) -> str:
    from .. import askpass
    s = await open_session(ctx, host)
    if isinstance(s, str):
        return s
    timeout = min(timeout_seconds if timeout_seconds > 0 else ctx.cfg.tools.shell_timeout, ctx.cfg.tools.update_timeout)
    stdin_text = None
    if _SUDO.search(command):
        password = s.sudo_password
        if not password:
            broker = askpass.BROKER
            login = await broker.ask_login(T(f"sudo auf {s.name} ({s.user})", f"sudo on {s.name} ({s.user})"),
                                           command=command, user=s.user, ask_user=False,
                                           remember_text=T("für diese Verbindung merken",
                                                           "remember for this connection")) if broker else None
            if not login:
                return T("Root-Rechte auf dem Zielrechner wurden NICHT erteilt (Passwort abgebrochen). Nicht mit "
                         "anderen Befehlen weiterprobieren – sag dem Nutzer Bescheid.",
                         "Root privileges on the remote host were NOT granted (password cancelled). Don't try other "
                         "commands – tell the user.")
            password = login[1]
            if login[2]:
                s.sudo_password = password
        command, stdin_text = with_sudo(command), password + "\n"
    rc, out = await _run(s, command, timeout, ctx, stdin_text)
    if rc == 255 and not await alive(s):  # Verbindung weg (Gerät neu gestartet, Netz) – beim nächsten Mal neu anmelden
        SESSIONS.pop(s.name.lower(), None)
        out += T("\n[Die SSH-Verbindung ist getrennt – der nächste Befehl fragt wieder nach der Anmeldung.]",
                 "\n[The SSH connection was closed – the next command asks for the login again.]")
    if stdin_text and rc not in (None, 0) and _SUDO_FAILED.search(out):
        s.sudo_password = ""
    return f"[{s.user}@{s.name}] " + proc.format_result(rc, out, ctx.limit())


@tool(
    "Trennt SSH-Verbindungen.",
    enabled=_enabled,
)
async def ssh_disconnect(
    ctx: ToolContext,
    host: Annotated[str, "Leer = alle"] = "",
) -> str:
    names = [resolve(ctx.cfg, host)[0].lower()] if host else list(SESSIONS)
    closed = []
    for key in names:
        s = SESSIONS.pop(key, None)
        if s:
            await _call([*_base(s), "-O", "exit", s.host], timeout=10)
            closed.append(s.name)
    if not closed:
        return T("Keine offene SSH-Verbindung.", "No open SSH connection.")
    return T("Getrennt: ", "Disconnected: ") + ", ".join(closed)


async def close_all() -> None:
    """Beim Beenden von Orbwise: offene Master-Verbindungen schließen."""
    for key in list(SESSIONS):
        s = SESSIONS.pop(key)
        with contextlib.suppress(Exception):
            await _call([*_base(s), "-O", "exit", s.host], timeout=5)


def ssh_status() -> dict:
    """Für orbwise doctor: ssh vorhanden und neu genug für SSH_ASKPASS_REQUIRE (OpenSSH ≥ 8.4)?"""
    path = shutil.which(SSH)
    if not path:
        return {"ok": False, "error": T("ssh nicht gefunden (Paket openssh)", "ssh not found (package openssh)")}
    try:
        out = subprocess.run([path, "-V"], capture_output=True, text=True, timeout=5).stderr.strip()
    except (OSError, subprocess.SubprocessError) as e:
        return {"ok": False, "error": str(e)}
    m = re.search(r"OpenSSH_(\d+)\.(\d+)", out)
    ok = bool(m) and (int(m.group(1)), int(m.group(2))) >= (8, 4)
    return {"ok": ok, "version": out.split(",")[0],
            "error": "" if ok else T("OpenSSH ab 8.4 nötig (Passwort über das Dashboard)",
                                     "OpenSSH 8.4 or newer needed (password via the dashboard)")}

