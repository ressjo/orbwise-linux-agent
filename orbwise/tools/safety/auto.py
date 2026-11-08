"""Auto-Modi: was ohne Rückfrage laufen darf.

- „Dateien bearbeiten“ (file_edit_ok): nur lesende Teile und Dateiänderungen im eigenen Home.
- „Auto“ (auto_shell_ok): alles ohne Root – außer Löschen, Ausschalten, Senden ins Netz, Startdateien/Autostart/
  Zugangsdaten. Die Prüfungen je Programm stehen in AUTO_RULES.
- auto_full_check: die gröbere Vorprüfung für „Auto“ (nur Root und Pushen fragen).
"""

from __future__ import annotations

import os
import re
from collections.abc import Callable
from dataclasses import dataclass

from ...lang import T
from .classify import BLOCK_PATTERNS, WARN_PATTERNS, blocked_reason, warning
from .credentials import SECRET_VARS, prints_secrets, reads_secret
from .readonly import is_read_only
from .shellwords import (
    SHELLS,
    changed_dir,
    plain_words,
    scan_text,
    segments,
    strip_heredocs,
    strip_wrappers,
    substitutions,
    tokens,
    walk,
    words,
)

DELETE_CMDS = {"rm", "rmdir", "unlink", "shred", "trash", "trash-put", "srm", "wipe"}
POWER_CMDS = {"shutdown", "reboot", "poweroff", "halt"}
SEND_CMDS = {"ssh", "scp", "sftp", "mail", "mailx", "sendmail", "mutt", "nc", "ncat", "telnet", "ftp"}
_SEND_FLAGS = re.compile(r"^(-d\S*|--data\S*|-F\S*|--form\S*|-T\S*|--upload-file|--post-data|--post-file|"
                         r"--body-data|--body-file|--method)(=.*)?$", re.I)

_ROOT_WORD = re.compile(r"(?<![\w./-])(sudo|su|pkexec|doas|run0)(?![\w.-])")
# Holen sich Root-Rechte selbst (rufen sudo intern auf) – wie sudo behandeln
_SELF_ROOT = re.compile(r"(?<![\w./-])(yay|paru|pikaur|trizen|aura)(?![\w.-])|(?<![\w./-])makepkg\b[^;&|\n]*"
                        r"(?:\s-\w*[si]\w*\b|--syncdeps|--install)")
_GIT_PUSH = re.compile(r"(?<![\w./-])git\b[^;&|\n]*?\spush\b")

_UNPARSABLE = ("Befehl konnte nicht sicher analysiert werden", "the command could not be analysed safely")


# ---------------------------------------------------------------- „Dateien bearbeiten“

def _never_an_edit(cmd: str) -> bool:
    """Leer, Befehlsersetzung, Token-Variablen, Blockliste oder Warnmuster: nie ohne Rückfrage."""
    if not cmd or "$(" in cmd or "`" in cmd or SECRET_VARS.search(cmd) or re.search(r"\benviron\b", cmd):
        return True
    return any(p.search(cmd) for p, _ in BLOCK_PATTERNS) or any(p.search(cmd) for p, _ in WARN_PATTERNS)


def file_edit_ok(command: str, cwd: str | None = None) -> bool:
    """Auto-Modus „Dateien bearbeiten“: besteht der Befehl nur aus lesenden Teilen und Dateiänderungen im eigenen
    Home (anlegen, schreiben, kopieren, verschieben) – ohne Root, Löschen, Zugangsdaten oder Befehlsersetzung?"""
    from ..filepolicy import shell_edits_ok

    cmd = command.strip()
    if _never_an_edit(cmd):
        return False
    try:
        toks = tokens(cmd)
    except ValueError:
        return False
    cwd = cwd or os.path.expanduser("~")
    edits: list[tuple[str, list[str], str]] = []
    redirects: list[tuple[str, str]] = []
    for seg in walk(toks):
        if seg.root or any(reads_secret([f], cwd, recursive=False) for f in seg.inputs) or seg.bare_env:
            return False
        if not seg.core:
            continue
        redirects += [(target, cwd) for target in seg.redirects]
        if seg.name in ("cd", "pushd"):
            cwd = changed_dir(cwd, seg.args)
            continue
        if prints_secrets(seg.name, seg.args, cwd):
            return False
        if not is_read_only(seg.name, seg.args):
            edits.append((seg.name, seg.args, cwd))
    return bool(edits or redirects) and shell_edits_ok(edits, redirects)


# ---------------------------------------------------------------- „Auto“: Vorprüfung

def auto_full_check(command: str) -> tuple[str, str]:
    """Auto-Modus „Auto“: nur Root und Pushen fragen. → ("ok" | "root" | "push", Grund)."""
    root = "root", T("braucht Root-Rechte", "needs root privileges")
    plain = plain_words(scan_text(command.strip()))
    if _ROOT_WORD.search(plain) or _SELF_ROOT.search(plain):
        return root
    try:  # auch über die Wörter, wie die Shell sie sieht (Anführungszeichen, Pfade)
        if any(strip_wrappers(seg)[1] for seg in segments(tokens(command))):
            return root
    except ValueError:
        pass
    if _GIT_PUSH.search(plain):
        return "push", T("schickt Commits ins Internet (git push)", "pushes commits to the internet (git push)")
    return "ok", ""


# ---------------------------------------------------------------- „Auto“: Prüfung je Teilbefehl

@dataclass(frozen=True)
class Reasons:
    """Gründe fürs Nachfragen in der Sprache der Oberfläche (beim Aufruf erzeugt)."""
    root: str
    secrets: str
    delete: str
    power: str
    send: str
    persist: str

    @classmethod
    def now(cls) -> Reasons:
        still = T(" – fragt auch im Auto-Modus", " – Auto still asks")
        return cls(
            root=T("benötigt Root-Rechte", "needs root privileges") + still,
            secrets=T("liest Zugangsdaten (Schlüssel/Passwörter)", "reads credentials (keys/passwords)") + still,
            delete=T("löscht Dateien", "deletes files") + still,
            power=T("schaltet aus, startet neu oder beendet die Sitzung", "shuts down, reboots or ends the session")
            + still,
            send=T("sendet etwas ins Netz", "sends something over the network") + still,
            persist=T("ändert Startdateien, Autostart, Zugangsdaten oder Orbwise selbst",
                      "changes start-up files, autostart, credentials or Orbwise itself") + still)


@dataclass(frozen=True)
class Call:
    """Ein Teilbefehl, wie die Regeln ihn sehen."""
    args: list[str]
    cwd: str
    depth: int
    why: Reasons


Rule = Callable[[Call], "str | None"]  # Grund fürs Nachfragen oder None


def _inner(text: str, call: Call) -> str | None:
    ok, why = auto_shell_ok(text, call.cwd, call.depth + 1)
    return None if ok else why


def _shell(c: Call) -> str | None:  # bash -c "…": den inneren Befehl genauso prüfen
    if "-c" not in c.args:
        return None
    i = c.args.index("-c") + 1
    return _inner(c.args[i] if i < len(c.args) else "", c)


def _eval(c: Call) -> str | None:
    return _inner(" ".join(c.args), c)


def _xargs(c: Call) -> str | None:  # xargs rm …
    sub = os.path.basename(next((a for a in c.args if not a.startswith("-")), ""))
    if sub in DELETE_CMDS:
        return c.why.delete
    return c.why.send if sub in POWER_CMDS | SEND_CMDS else None


def _gio(c: Call) -> str | None:
    return c.why.delete if c.args[:1] in (["trash"], ["remove"]) else None


def _find(c: Call) -> str | None:
    a = c.args
    runs_delete = any(x in ("-exec", "-execdir", "-ok", "-okdir") and i + 1 < len(a)
                      and os.path.basename(a[i + 1]) in DELETE_CMDS for i, x in enumerate(a))
    return c.why.delete if "-delete" in a or runs_delete else None


def git_sub(args: list[str]) -> str:
    """Unterbefehl von git, auch nach -C ordner / -c key=value."""
    i = 0
    while i < len(args):
        a = args[i]
        if a in ("-C", "-c", "--git-dir", "--work-tree"):
            i += 2
            continue
        if not a.startswith("-"):
            return a
        i += 1
    return ""


def _git(c: Call) -> str | None:
    return {"push": c.why.send, "clean": c.why.delete}.get(git_sub(c.args))


_SYSTEMCTL_POWER = {"poweroff", "reboot", "suspend", "hibernate", "halt", "hybrid-sleep", "suspend-then-hibernate",
                    "kexec", "soft-reboot"}
_SYSTEMCTL_PERSIST = {"enable", "link", "preset", "preset-all", "set-environment"}


def _systemctl(c: Call) -> str | None:
    verb = (words(c.args) or [""])[0]
    return c.why.power if verb in _SYSTEMCTL_POWER else c.why.persist if verb in _SYSTEMCTL_PERSIST else None


def _loginctl(c: Call) -> str | None:
    return c.why.power if any(a.startswith(("terminate", "kill", "poweroff", "reboot")) for a in c.args) else None


def _crontab(c: Call) -> str | None:
    return None if "-l" in c.args else c.why.persist


def _rsync(c: Call) -> str | None:  # Ziel auf einem anderen Rechner (host:/pfad)
    remote = any(re.match(r"^[\w.@-]+:", a) and not a.startswith(("/", "~", ".")) for a in words(c.args))
    return c.why.send if remote else None


def _http_method(args: list[str], i: int, a: str) -> str:
    if a in ("-X", "--request") and i + 1 < len(args):
        return args[i + 1]
    return a[2:] if a.startswith("-X") and len(a) > 2 else ""


def _curl(c: Call) -> str | None:
    if any(_SEND_FLAGS.match(a) for a in c.args):
        return c.why.send
    sends = any(_http_method(c.args, i, a).upper() in ("POST", "PUT", "PATCH", "DELETE") for i, a in enumerate(c.args))
    return c.why.send if sends else None


def _always(field: str) -> Rule:
    return lambda c: getattr(c.why, field)


AUTO_RULES: dict[str, Rule] = {
    **dict.fromkeys(SHELLS, _shell), "eval": _eval, "xargs": _xargs,
    **dict.fromkeys(DELETE_CMDS, _always("delete")), "gio": _gio, "find": _find, "git": _git,
    **dict.fromkeys(POWER_CMDS, _always("power")), "systemctl": _systemctl, "loginctl": _loginctl,
    "crontab": _crontab, **dict.fromkeys(SEND_CMDS, _always("send")), "rsync": _rsync, "curl": _curl, "wget": _curl,
}
assert len(AUTO_RULES) == len(SHELLS) + len(DELETE_CMDS) + len(POWER_CMDS) + len(SEND_CMDS) + 11  # keine Doppelten


def write_targets(name: str, args: list[str]) -> list[str]:
    """Ziele, in die ein Befehl schreibt (für den Schutz von Startdateien, Autostart, ~/.ssh …)."""
    paths = words(args)
    if name in ("tee", "touch", "mkdir"):
        return paths
    if name in ("cp", "mv", "install", "ln", "rsync"):
        for i, a in enumerate(args):
            if a in ("-t", "--target-directory") and i + 1 < len(args):
                return [args[i + 1]]
            if a.startswith("--target-directory="):
                return [a.split("=", 1)[1]]
        return paths[-1:] if len(paths) >= 2 else []
    return []


def _before_parsing(cmd: str, cwd: str | None, depth: int, why: Reasons) -> str | None:
    """Prüfungen am ganzen Befehlstext: Blockliste, Warnungen, Root, Zugangsdaten, Befehlsersetzung."""
    still = T(" – fragt auch im Auto-Modus", " – Auto still asks")
    if (reason := blocked_reason(cmd)):
        return reason + still
    scan = scan_text(cmd)
    if (reason := warning(scan)):
        return reason + still
    if _ROOT_WORD.search(scan) or _SELF_ROOT.search(scan):
        return why.root
    if SECRET_VARS.search(cmd) or re.search(r"\benviron\b", scan):
        return why.secrets
    for inner in substitutions(strip_heredocs(cmd)):  # echo "$(rm -rf ~)" führt rm aus
        ok, reason = auto_shell_ok(inner, cwd, depth + 1) if inner.strip() else (True, "")
        if not ok:
            return reason
    return None


def _segment_why(seg, cwd: str, depth: int, why: Reasons) -> tuple[str | None, str]:
    """Grund fürs Nachfragen bei einem Teilbefehl (oder None) und der Ordner danach (cd)."""
    from ..filepolicy import protected_path

    if seg.root:
        return why.root, cwd
    if any(reads_secret([f], cwd, recursive=False) for f in seg.inputs) or seg.bare_env:
        return why.secrets, cwd
    if not seg.core:
        return None, cwd
    if any(t != "/dev/null" and not t.startswith("&") and protected_path(t, cwd) for t in seg.redirects):
        return why.persist, cwd
    name, args = seg.name, seg.args
    if name in ("cd", "pushd"):
        return None, changed_dir(cwd, args)
    if prints_secrets(name, args, cwd):
        return why.secrets, cwd
    rule = AUTO_RULES.get(name)
    if rule and (reason := rule(Call(args, cwd, depth, why))) is not None:
        return reason, cwd
    if any(protected_path(t, cwd) for t in write_targets(name, args)):
        return why.persist, cwd
    return None, cwd


def auto_shell_ok(command: str, cwd: str | None = None, _depth: int = 0) -> tuple[bool, str]:
    """Auto-Modus „Auto“: läuft dieser Befehl ohne Rückfrage? Alles ohne Root – außer Löschen, Ausschalten,
    Senden ins Netz, Startdateien/Autostart/Zugangsdaten. Liefert (ok, Grund fürs Nachfragen)."""
    why = Reasons.now()
    cmd = command.strip()
    if not cmd or _depth > 3:
        return False, T(*_UNPARSABLE)
    if (reason := _before_parsing(cmd, cwd, _depth, why)) is not None:
        return False, reason
    try:
        toks = tokens(cmd)
    except ValueError:
        return False, T(*_UNPARSABLE)
    cwd = cwd or os.path.expanduser("~")
    for seg in walk(toks):
        reason, cwd = _segment_why(seg, cwd, _depth, why)
        if reason is not None:
            return False, reason
    return True, ""
