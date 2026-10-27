"""Käfig für Shell-Befehle, die der Auto-Modus ohne Rückfrage ausführt (bubblewrap).

Die Sperrliste in tools/safety.py liest nur den Befehlstext – was `python3 -c …`, ein selbst geschriebenes Skript oder
`npm test` danach tun, sieht sie nicht. Im Käfig ist das egal: das System ist nur lesbar, schreibbar sind nur der
Projektordner (Coding) und ein eigenes /tmp, Zugangsdaten und Tokens sind unsichtbar, Session-Bus und Docker-Socket
fehlen. Das Netz bleibt an (Paketinstallation, Dev-Server, Screenshots); damit ein eingesperrter Prozess Orbwise selbst
nicht fernsteuern kann, lehnt der Server Verbindungen aus dem Käfig ab (peer_is_jailed).

Scheitert ein Befehl am Käfig (Read-only file system), fragt das Werkzeug, ob er ohne Isolation laufen darf.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import time
from pathlib import Path

BWRAP = "bwrap"
# Einzelne Dateien mit Zugangsdaten im Home – im Käfig leer (/dev/null)
SECRET_FILES = (".netrc", ".pgpass", ".git-credentials", ".my.cnf", ".pypirc", ".npmrc", ".env",
                ".config/hub", ".docker/config.json")
DOCKER_SOCKETS = ("/var/run/docker.sock", "/run/docker.sock", "/run/podman/podman.sock")
# Umgebung im Käfig: nur Unkritisches – nie Tokens, Passwörter, Schlüssel
ENV_KEEP = re.compile(r"^(PATH|HOME|USER|LOGNAME|SHELL|LANG|LANGUAGE|LC_\w+|TZ|TERM|EDITOR|VISUAL|"
                      r"PYTHONPATH|VIRTUAL_ENV|NVM_DIR|NODE_PATH|GOPATH|GOROOT|CARGO_HOME|RUSTUP_HOME|JAVA_HOME|"
                      r"PNPM_HOME|BUN_INSTALL|DENO_INSTALL|XDG_DATA_DIRS|XDG_CONFIG_DIRS)$")
ENV_SECRET = re.compile(r"(TOKEN|PASSW|SECRET|API_?KEY|PRIVATE|CREDENTIAL|AUTH)", re.I)
DENIED = re.compile(r"Read-only file system|EROFS", re.I)

_state: dict = {"checked": 0.0, "ok": False, "reason": ""}
_jailed: set[int] = set()  # PIDs der von Orbwise gestarteten bwrap-Prozesse (für peer_is_jailed)


def _hint() -> str:
    from .lang import T
    return T("bubblewrap installieren: sudo pacman -S bubblewrap bzw. sudo apt install bubblewrap",
             "install bubblewrap: sudo pacman -S bubblewrap or sudo apt install bubblewrap")


def _test_argv(binary: str) -> list[str]:
    return [binary, "--die-with-parent", "--new-session", "--unshare-all", "--share-net", "--ro-bind", "/", "/",
            "--dev", "/dev", "--proc", "/proc", "--", "/bin/true"]


def available(force: bool = False) -> tuple[bool, str]:
    """(nutzbar, Grund mit Rat). Ergebnis 10 Minuten zwischengespeichert, Fehlschläge eine Minute."""
    from .lang import T
    if not force and time.monotonic() - _state["checked"] < (600 if _state["ok"] else 60) and _state["checked"]:
        return _state["ok"], _state["reason"]
    binary = shutil.which(BWRAP)
    if not binary:
        ok, reason = False, T("bubblewrap fehlt – ", "bubblewrap is missing – ") + _hint()
    else:
        try:
            r = subprocess.run(_test_argv(binary), capture_output=True, text=True, timeout=20)
            ok, msg = r.returncode == 0, (r.stderr or r.stdout).strip()[-300:]
        except (OSError, subprocess.SubprocessError) as e:
            ok, msg = False, str(e)
        reason = ""
        if not ok:
            reason = T(f"bubblewrap funktioniert hier nicht ({msg})", f"bubblewrap does not work here ({msg})")
            if "namespace" in msg.lower() or "permission" in msg.lower() or "operation not permitted" in msg.lower():
                reason += T(" – vermutlich sind User-Namespaces gesperrt (Ubuntu: AppArmor-Profil für bwrap bzw. "
                            "kernel.apparmor_restrict_unprivileged_userns)",
                            " – user namespaces are probably restricted (Ubuntu: AppArmor profile for bwrap or "
                            "kernel.apparmor_restrict_unprivileged_userns)")
    _state.update(checked=time.monotonic(), ok=ok, reason=reason)
    return ok, reason


def tmp_dir(key: str) -> str:
    """Eigenes /tmp des Käfigs je Chat – bleibt zwischen Befehlen erhalten."""
    base = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / "orbwise" / "jail-tmp"
    path = base / (re.sub(r"[^\w-]", "", key) or "default")
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    return str(path)


def _secret_dirs(home: Path) -> list[str]:
    from .config import CONFIG_DIR
    from .tools.secretpaths import SECRET_DIRS
    out = [str(home / d) for d in SECRET_DIRS if (home / d).is_dir()]
    if CONFIG_DIR.is_dir():
        out.append(str(CONFIG_DIR))
    return sorted(set(out), key=len)


def env() -> dict[str, str]:
    from .tools.proc import QUIET_ENV
    out = {k: v for k, v in os.environ.items() if ENV_KEEP.match(k) and not ENV_SECRET.search(k)}
    return {**out, **QUIET_ENV, "TMPDIR": "/tmp"}


def argv(command: str, cwd: str, writable: list[str], key: str = "") -> list[str]:
    """bwrap-Aufruf, der command in bash im Käfig ausführt."""
    binary = shutil.which(BWRAP) or BWRAP
    home = Path.home()
    out = [binary, "--die-with-parent", "--new-session", "--unshare-all", "--share-net",
           "--ro-bind", "/", "/", "--dev", "/dev", "--proc", "/proc",
           "--bind", tmp_dir(key), "/tmp"]
    run_user = f"/run/user/{os.getuid()}"
    if os.path.isdir(run_user):  # Session-Bus, systemd --user, Podman-API, Wayland/Audio
        out += ["--tmpfs", run_user, "--remount-ro", run_user]
    for path in (cwd, str(home)):  # liegt etwas Benötigtes unter /tmp, verdeckt es das eigene /tmp – wieder einbinden
        real = os.path.realpath(path)
        if real.startswith("/tmp/") and os.path.isdir(real):
            out += ["--ro-bind", real, real]
    for sock in DOCKER_SOCKETS:
        if os.path.exists(sock):
            out += ["--ro-bind", "/dev/null", sock]
    for path in writable:
        real = os.path.realpath(os.path.expanduser(path))
        if os.path.isdir(real):
            out += ["--bind", real, real]
    for d in _secret_dirs(home):  # nach den schreibbaren Ordnern – Geheimes bleibt auch darin unsichtbar
        out += ["--tmpfs", d, "--remount-ro", d]
    for name in SECRET_FILES:
        f = home / name
        if f.is_file():
            out += ["--ro-bind", "/dev/null", str(f)]
    out += ["--clearenv"]
    for k, v in env().items():
        out += ["--setenv", k, v]
    out += ["--chdir", cwd if os.path.isdir(cwd) else str(home), "--", "/bin/bash", "-c", command]
    return out


def denied(output: str) -> bool:
    """Ist der Befehl am Käfig gescheitert (Schreiben außerhalb der erlaubten Orte)?"""
    return bool(DENIED.search(output or ""))


# ---------------------------------------------------------------- Orbwise selbst vor dem Käfig schützen
def register(pid: int) -> None:
    _jailed.add(pid)


def active() -> bool:
    """Lebt gerade ein Käfig-Prozess (auch ein Hintergrund-Job)?"""
    return bool(_alive())


def _alive() -> set[int]:
    for pid in list(_jailed):
        if not os.path.exists(f"/proc/{pid}"):
            _jailed.discard(pid)
    return set(_jailed)


def _children() -> dict[int, list[int]]:
    kids: dict[int, list[int]] = {}
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        try:
            stat = Path(f"/proc/{entry}/stat").read_text()
        except OSError:
            continue
        ppid = int(stat.rsplit(")", 1)[1].split()[1])
        kids.setdefault(ppid, []).append(int(entry))
    return kids


def _jailed_pids(roots: set[int]) -> set[int]:
    kids = _children()
    out, todo = set(), list(roots)
    while todo:
        pid = todo.pop()
        if pid in out:
            continue
        out.add(pid)
        todo += kids.get(pid, [])
    return out


def _socket_inodes(client_port: int, server_port: int) -> set[str]:
    """Inodes der Sockets, die von client_port aus mit server_port verbunden sind (Client-Seite)."""
    out = set()
    for table in ("/proc/net/tcp", "/proc/net/tcp6"):
        try:
            lines = Path(table).read_text().splitlines()[1:]
        except OSError:
            continue
        for line in lines:
            parts = line.split()
            if len(parts) < 10:
                continue
            local, remote = parts[1], parts[2]
            if int(local.rsplit(":", 1)[1], 16) == client_port and int(remote.rsplit(":", 1)[1], 16) == server_port:
                out.add(parts[9])
    return out


def peer_is_jailed(client_port: int | None, server_port: int) -> bool:
    """Kommt diese Verbindung aus einem Käfig? Nur, solange ein Käfig-Prozess lebt – sonst kostenlos.
    Lassen sich die offenen Dateien eines Käfig-Prozesses nicht lesen, gilt die Verbindung im Zweifel als eingesperrt."""
    roots = _alive()
    if not roots or not client_port:
        return False
    inodes = _socket_inodes(client_port, server_port)
    if not inodes:
        return False
    wanted = {f"socket:[{i}]" for i in inodes}
    jailed = _jailed_pids(roots)
    owner = _owner(jailed, wanted)
    if owner is not None:
        return owner
    # Ein Käfig-Prozess war nicht lesbar: nur durchlassen, wenn die Verbindung nachweislich jemand anderem gehört
    others = {int(e) for e in os.listdir("/proc") if e.isdigit()} - jailed
    return _owner(others, wanted) is not True


def _owner(pids: set[int], wanted: set[str]) -> bool | None:
    """True: einer der Prozesse hält den Socket; False: keiner (alle lesbar); None: unklar (nicht lesbar)."""
    unknown = False
    for pid in pids:
        try:
            fds = os.listdir(f"/proc/{pid}/fd")
        except FileNotFoundError:
            continue
        except PermissionError:
            unknown = True
            continue
        for fd in fds:
            try:
                if os.readlink(f"/proc/{pid}/fd/{fd}") in wanted:
                    return True
            except OSError:
                continue
    return None if unknown else False


# ---------------------------------------------------------------- Ausführen (run_shell / shell)
async def run(ctx, command: str, cwd: str, timeout: float, name: str) -> tuple[int | None, str]:
    """Befehl im Käfig. Scheitert er daran, dass er außerhalb schreiben wollte, fragt Orbwise, ob er ohne Isolation
    laufen darf – erst dann läuft er (noch einmal) ungeschützt."""
    from . import prompts
    from .tools import proc
    cage = ctx.jail
    rc, out = await proc.run(ctx, argv(command, cwd, cage["writable"], cage["key"]), timeout=timeout, cwd=cwd,
                             started=register)
    if not rc or not denied(out):
        return rc, out
    if not ctx.confirm:
        return rc, out + "\n" + prompts.text(ctx.cfg, "jail_declined").format(allowed=cage["allowed"])
    reason = prompts.text(ctx.cfg, "jail_escalate").format(allowed=cage["allowed"])
    if ctx.emit:
        await ctx.emit({"type": "state", "state": "confirm"})
    decision = await ctx.confirm(ctx.call_id, name, {"command": command}, reason)
    if isinstance(decision, tuple):
        decision = decision[0]
    if not decision:
        return rc, out + "\n" + prompts.text(ctx.cfg, "jail_declined").format(allowed=cage["allowed"])
    if ctx.emit:
        await ctx.emit({"type": "state", "state": "executing", "tool": name})
    return await proc.run(ctx, command, timeout=timeout, cwd=cwd)
