"""Wem gehört eine lokale TCP-Verbindung? Aus /proc gelesen, ohne ss/lsof (Käfig-Erkennung, Askpass-Prüfung)."""

from __future__ import annotations

import os
from pathlib import Path


def socket_inodes(client_port: int, server_port: int) -> set[str]:
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


def holds_socket(pid: int, wanted: set[str]) -> bool | None:
    """True: der Prozess hält einen der Sockets; False: nicht; None: nicht lesbar (fremder Benutzer)."""
    try:
        fds = os.listdir(f"/proc/{pid}/fd")
    except FileNotFoundError:
        return False
    except PermissionError:
        return None
    for fd in fds:
        try:
            if os.readlink(f"/proc/{pid}/fd/{fd}") in wanted:
                return True
        except OSError:
            continue
    return False


def socket_owner(client_port: int, server_port: int) -> int | None:
    """PID des (lesbaren) Prozesses, der die Verbindung client_port → server_port geöffnet hat."""
    inodes = socket_inodes(client_port, server_port)
    if not inodes:
        return None
    wanted = {f"socket:[{i}]" for i in inodes}
    for entry in os.listdir("/proc"):
        if entry.isdigit() and holds_socket(int(entry), wanted):
            return int(entry)
    return None


def parent_pid(pid: int) -> int | None:
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return None
    fields = stat.rsplit(")", 1)[-1].split()  # Name in Klammern kann Leerzeichen enthalten
    return int(fields[1]) if len(fields) > 1 and fields[1].isdigit() else None


def command_line(pid: int) -> list[str]:
    try:
        return [a.decode(errors="replace") for a in Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0") if a]
    except OSError:
        return []


def process_name(pid: int) -> str:
    try:
        return Path(f"/proc/{pid}/comm").read_text().strip()
    except OSError:
        return ""


def effective_uid(pid: int) -> int | None:
    """Effektive UID (aus /proc/<pid>/status – auch bei setuid-Programmen wie sudo lesbar)."""
    try:
        for line in Path(f"/proc/{pid}/status").read_text().splitlines():
            if line.startswith("Uid:"):
                return int(line.split()[2])
    except (OSError, ValueError, IndexError):
        return None
    return None
