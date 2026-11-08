"""Einstufung eines Shell-Befehls für die normale Freigabe:

safe     – nur lesend, wird direkt ausgeführt
confirm  – verändert etwas / unbekannt / Root → Nutzer muss bestätigen
blocked  – zerstörerisch, wird nie ausgeführt
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from ...lang import T
from ..registry import BLOCKED, CONFIRM, SAFE
from .credentials import SECRET_VARS, prints_secrets, reads_secret
from .readonly import is_read_only
from .shellwords import REDIRECTS, changed_dir, scan_text, substitutions, tokens, walk

CRITICAL_PATHS = {"/", "/*", "~", "~/", "~/*", "$HOME", "${HOME}", "/home", "/etc", "/usr", "/boot", "/var",
                  "/bin", "/sbin", "/lib", "/lib64", "/opt", "/root", "/srv", "/dev", "/proc", "/sys", "/mnt",
                  "*", ".", "./", "./*", "..", "../", str(Path.home())}

BLOCK_PATTERNS = [
    (re.compile(r"--no-preserve-root"), lambda: T("Löschen des Wurzelverzeichnisses", "deleting the root directory")),
    (re.compile(r"(^|[\s;&|(])mkfs(\.\w+)?\b"), lambda: T("Formatieren eines Dateisystems", "formatting a file system")),
    (re.compile(r"\bdd\b[^;&|]*\bof=/dev/(sd|nvme|hd|vd|mmcblk|disk)"),
     lambda: T("Überschreiben eines Datenträgers", "overwriting a disk")),
    (re.compile(r">\s*/dev/(sd|nvme|hd|vd|mmcblk)"), lambda: T("Überschreiben eines Datenträgers", "overwriting a disk")),
    (re.compile(r":\(\)\s*\{\s*:\s*\|\s*:\s*&\s*\}\s*;\s*:"), lambda: T("Fork-Bombe", "fork bomb")),
    (re.compile(r"\b(wipefs|shred|blkdiscard)\b[^;&|]*/dev/"), lambda: T("Löschen eines Datenträgers", "wiping a disk")),
    (re.compile(r"\bch(mod|own|grp)\b[^;&|]*\s-\w*R\w*\b[^;&|]*\s/(\s|$|\*)"),
     lambda: T("Rechte des ganzen Systems ändern", "changing permissions of the whole system")),
]

WARN_PATTERNS = [
    (re.compile(r"\b(curl|wget)\b[^;&]*\|\s*(sudo\s+)?(ba|z|da|fi)?sh\b"),
     lambda: T("führt ein Skript direkt aus dem Internet aus", "runs a script straight from the internet")),
    (re.compile(r"\b(fdisk|parted|sgdisk|gdisk|cfdisk)\b"), lambda: T("Partitionierungswerkzeug", "partitioning tool")),
    (re.compile(r"\bpacman\b[^;&|]*\s-R\w*d\w*d"),
     lambda: T("entfernt Pakete ohne Abhängigkeitsprüfung", "removes packages without dependency checks")),
]

# Lesende Befehle, die trotzdem ins Netz gehen (DNS-Anfragen können Daten hinaustragen: dig geheim.example.com)
NET_LOOKUP = {"dig", "host", "nslookup", "whois", "traceroute", "tracepath", "mtr", "getent", "ping", "drill"}


def blocked_reason(cmd: str) -> str | None:
    """Grund, wenn der Befehl nie laufen darf (Formatieren, Datenträger überschreiben …)."""
    return next((reason() for pattern, reason in BLOCK_PATTERNS if pattern.search(cmd)), None)


def warning(text: str) -> str | None:
    return next((reason() for pattern, reason in WARN_PATTERNS if pattern.search(text)), None)


def rm_is_catastrophic(args: list[str]) -> bool:
    recursive = any(a == "--recursive" or (a.startswith("-") and not a.startswith("--") and "r" in a.lower())
                    for a in args)
    targets = {a.rstrip("/") or "/" for a in args if not a.startswith("-")}
    targets |= {a for a in args if not a.startswith("-")}
    return recursive and bool(targets & CRITICAL_PATHS)


def _writes_file(toks: list[str]) -> bool:
    """Leitet der Befehl irgendwo in eine Datei um (nicht /dev/null, nicht &1)?"""
    for i, t in enumerate(toks):
        if t in REDIRECTS:
            target = toks[i + 1] if i + 1 < len(toks) else ""
            if target != "/dev/null" and not target.startswith("&"):
                return True
    return False


class _Blocked(Exception):
    def __init__(self, reason: str):
        self.reason = reason


def _segment_reasons(seg, cwd: str) -> tuple[list[str], str]:
    """Gründe fürs Nachfragen bei einem Teilbefehl und der Ordner danach (cd). _Blocked bei rm -rf /."""
    reasons: list[str] = []
    secrets = T("liest Zugangsdaten (Schlüssel/Passwörter)", "reads credentials (keys/passwords)")
    if any(reads_secret([f], cwd, recursive=False) for f in seg.inputs):  # cat < ~/.ssh/id_rsa
        reasons.append(secrets)
    if not seg.core:
        return reasons + ([secrets] if seg.bare_env else []), cwd
    name, args = seg.name, seg.raw_args
    if name in ("cd", "pushd"):
        cwd = changed_dir(cwd, args)
    if prints_secrets(name, args, cwd):
        reasons.append(secrets)
    if name == "rm" and rm_is_catastrophic(args):
        raise _Blocked(T("rekursives Löschen eines Systemverzeichnisses", "recursive deletion of a system directory"))
    if seg.root:
        reasons.append(T("benötigt Root-Rechte", "needs root privileges"))
    elif name != "printenv" and not is_read_only(name, [a for a in args if a not in REDIRECTS]):
        reasons.append(T(f"'{name}' kann das System verändern", f"'{name}' can change the system"))
    return reasons, cwd


def _whole_command_reasons(cmd: str, toks: list[str]) -> list[str]:
    """Gründe, die am ganzen Befehl hängen: Umleitung in Dateien, Token-Variablen, Befehlsersetzung, Warnmuster."""
    reasons = []
    if _writes_file(toks):
        reasons.append(T("schreibt in eine Datei", "writes to a file"))
    if SECRET_VARS.search(cmd) or re.search(r"\benviron\b", scan_text(cmd)):
        reasons.append(T("liest Zugangsdaten (Schlüssel/Passwörter)", "reads credentials (keys/passwords)"))
    if substitutions(cmd) or "<(" in cmd or ">(" in cmd:
        reasons.append(T("enthält Befehlsersetzung", "contains command substitution"))
    for pattern, reason in WARN_PATTERNS:
        if pattern.search(cmd):
            reasons.append(T("ACHTUNG: ", "WARNING: ") + reason())
    return reasons


def classify_command(command: str, cwd: str | None = None) -> tuple[str, str]:
    cmd = command.strip()
    if not cmd:
        return BLOCKED, T("leerer Befehl", "empty command")
    if (why := blocked_reason(cmd)):
        return BLOCKED, why
    try:
        toks = tokens(cmd)
    except ValueError:
        return CONFIRM, T("Befehl konnte nicht sicher analysiert werden", "the command could not be analysed safely")
    reasons: list[str] = []
    cwd = cwd or os.path.expanduser("~")  # run_shell startet im Home bzw. im Projektordner
    try:
        for seg in walk(toks):
            found, cwd = _segment_reasons(seg, cwd)
            reasons += found
    except _Blocked as e:
        return BLOCKED, e.reason
    reasons += _whole_command_reasons(cmd, toks)
    if reasons:
        return CONFIRM, "; ".join(dict.fromkeys(reasons))
    return SAFE, T("nur lesender Befehl", "read-only command")


def local_read_only(command: str, cwd: str | None = None) -> bool:
    """Rein lesend und lokal (echo, cd, ls, cat …): darf auch nach fremden Inhalten ohne Rückfrage laufen."""
    if classify_command(command, cwd)[0] != SAFE:
        return False
    try:
        toks = tokens(command.strip())
    except ValueError:
        return False
    return not any(os.path.basename(t) in NET_LOOKUP for t in toks)
