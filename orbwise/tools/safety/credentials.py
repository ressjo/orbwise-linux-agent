"""Liest ein Befehl Zugangsdaten? Schlüssel- und Passwortdateien, Umgebung mit Tokens (printenv, ps e, systemctl
show), WLAN-Passwörter (nmcli -s), Dateien, die curl hochlädt."""

from __future__ import annotations

import re

from ..secretpaths import contains_secrets, expand_arg, is_secret_path
from .shellwords import REDIRECTS, first_word, operands

# Befehle, die Dateiinhalte ausgeben – auf Schlüssel/Passwort-Dateien angewandt nur mit Rückfrage
READERS = {"cat", "head", "tail", "grep", "egrep", "fgrep", "rg", "less", "more", "zcat", "xxd", "hexdump",
           "strings", "jq", "diff", "cmp", "sort", "uniq", "cut", "column", "tr", "sed", "awk", "base64", "od",
           "nl", "tac", "bat", "batcat", "view", "vim", "vi", "nano", "cp", "scp", "rsync", "tar", "zip", "curl"}
SECRET_VARS = re.compile(r"\$\{?\w*(TOKEN|PASSW|SECRET|API_?KEY|PRIVATE)\w*", re.I)
RECURSIVE_FLAGS = {"-r", "-R", "--recursive", "--dereference-recursive"}


def reads_secret(targets: list[str], cwd: str, recursive: bool) -> bool:
    """Trifft eines der Ziele (nach cd, ~, $VAR, {a,b} und Globs wie die Shell) eine Geheimnis-Datei – oder
    durchsucht es rekursiv einen Ordner, in dem welche liegen? Unauflösbare Pfade gelten als verdächtig."""
    for arg in targets:
        paths = expand_arg(arg, cwd)
        if paths is None:
            return True
        for p in paths:
            if is_secret_path(p) or (recursive and contains_secrets(p)):
                return True
    return False


# ps-Optionen mit Wert (ps -eo user, ps -C firefox, ps axo user): der Wert ist kein BSD-Schalter
_PS_LONG_VALUE = {"--format", "--pid", "--ppid", "--user", "--User", "--group", "--Group", "--tty", "--sort", "--sid",
                  "--cols", "--rows", "--columns", "--width", "--quick-pid"}
_PS_SHORT_VALUE = set("oOpqugUGtCsk")
_PS_BSD_VALUE = set("oOpUtk")


def _ps_shows_env(args: list[str]) -> bool:
    """BSD-Modifikator „e“ (ps eww, ps auxe) gibt die Umgebung jedes Prozesses aus – Optionswerte zählen nicht."""
    skip = False
    for a in args:
        if skip:
            skip = False
        elif a.startswith("--"):
            skip = a in _PS_LONG_VALUE
        elif a.startswith("-"):
            skip = len(a) > 1 and a[-1] in _PS_SHORT_VALUE
        elif a.isalpha():
            if "e" in a:
                return True
            skip = a[-1] in _PS_BSD_VALUE
    return False


_CURL_DATA = re.compile(r"^(-d|--data(-binary|-raw|-ascii|-urlencode)?|-F|--form(-string)?|--json)(=(.*))?$")
_CURL_FILE = re.compile(r"^(-T|--upload-file|-K|--config|-E|--cert|--key|--cacert|--netrc-file)(=(.*))?$")


def _curl_files(args: list[str]) -> list[str]:
    """Welche lokalen Dateien liest curl? Nur @datei bei -d/-F/--json, -T, -K, Zertifikate/Schlüssel und file://-URLs –
    Optionswerte wie -A "$UA", -H, -o oder -w sind keine Dateien."""
    out: list[str] = []
    for i, a in enumerate(args):
        nxt = args[i + 1] if i + 1 < len(args) else ""
        if (m := _CURL_DATA.match(a)):
            value = m.group(5) if m.group(4) else nxt
            out += [p.split(";")[0] for p in re.findall(r"[@<]([^\s,]+)", value or "")]
        elif (m := _CURL_FILE.match(a)):
            out.append(m.group(3) if m.group(2) else nxt)
        elif a.lower().startswith("file://"):
            out.append(a[7:])
    return [p for p in out if p]


def _nmcli_secrets(args: list[str]) -> bool:  # -s / --show-secrets zeigt WLAN- und VPN-Passwörter
    return any(a == "--show-secrets" or (a.startswith("-") and not a.startswith("--") and "s" in a) for a in args)


def _systemctl_secrets(args: list[str]) -> bool:  # Units und Manager-Umgebung können Environment=…TOKEN enthalten
    sub = first_word(args)
    return sub in ("cat", "show-environment") or (sub == "show" and not any(
        a in ("-p", "--property") or a.startswith("--property=") or (a.startswith("-p") and len(a) > 2) for a in args))


def _reader_secrets(name: str, args: list[str], cwd: str) -> bool:
    if name == "curl":
        return reads_secret(_curl_files(args), cwd, False)
    targets = [a for a in operands(name, args) if a not in REDIRECTS and a != "<"]
    recursive = name == "rg" or (name in ("grep", "egrep", "fgrep") and any(
        a in RECURSIVE_FLAGS or (a.startswith("-") and not a.startswith("--") and set(a[1:]) & {"r", "R"})
        for a in args))
    if recursive and not targets:
        targets = ["."]  # ohne Pfad durchsuchen grep -r und rg das aktuelle Verzeichnis
    return reads_secret(targets, cwd, recursive)


def prints_secrets(name: str, args: list[str], cwd: str) -> bool:
    if name == "printenv":
        return True
    if name == "nmcli":
        return _nmcli_secrets(args)
    if name == "ps":
        return _ps_shows_env(args)
    if name == "systemctl":
        return _systemctl_secrets(args)
    return name in READERS and _reader_secrets(name, args, cwd)
