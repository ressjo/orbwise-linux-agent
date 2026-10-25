"""Erkennt Dateien mit Schlüsseln, Passwörtern und Zugangsdaten.

Solche Dateien liest bzw. verschickt Orbwise nie ohne Weiteres – eine präparierte Mail oder Webseite könnte das
Modell sonst dazu bringen, sie auszulesen und (z. B. per Telegram) nach draußen zu geben. Geprüft wird der
aufgelöste Pfad, ein Symlink wie ~/notiz.txt → ~/.ssh/id_ed25519 hilft also nicht.
"""

from __future__ import annotations

import glob
import os
import re
from pathlib import Path

# Verzeichnisse, deren gesamter Inhalt geheim ist – im eigenen Home und überall sonst (Backups, andere Nutzer)
SECRET_DIRS = (
    ".ssh", ".gnupg", ".password-store", ".local/share/keyrings", ".aws", ".kube", ".azure", ".config/gcloud",
    ".mozilla", ".thunderbird", ".config/chromium", ".config/google-chrome", ".config/BraveSoftware",
    ".config/vivaldi", ".config/microsoft-edge", ".config/opera", ".config/protonmail", ".config/Proton Mail",
    ".config/Signal", ".config/orbwise", ".docker", ".config/gh", ".config/rclone",
)
# Einzelne Dateinamen (egal wo)
SECRET_NAMES = {
    ".netrc", ".pgpass", ".git-credentials", ".env", ".my.cnf", ".pypirc", ".npmrc", "credentials",
    "credentials.json", "secrets.yaml", "secrets.yml", "secrets.json", "id_rsa", "id_dsa", "id_ecdsa",
    "id_ed25519", "shadow", "gshadow", "shadow-", "gshadow-", "sudoers",
}
# Wortbestandteile im Dateinamen, die auf Zugangsdaten hindeuten (z. B. secrets.txt, aws-credentials.csv)
SECRET_WORDS = ("secret", "credential", "passw", "keyring", ".kdbx", "private_key", "private-key", "privkey")
SECRET_SUFFIXES = (".pem", ".key", ".p12", ".pfx", ".kdbx", ".keystore", ".jks", ".ovpn")
SYSTEM_SECRETS = ("/etc/shadow", "/etc/gshadow", "/etc/sudoers", "/etc/ssh", "/etc/NetworkManager/system-connections",
                  "/etc/wpa_supplicant", "/proc/self/environ")
# Für alle lesbar und ohne Passwörter (die stehen in /etc/shadow) – trotz „passw“ im Namen
PUBLIC_FILES = {Path(p) for p in ("/etc/passwd", "/etc/passwd-", "/etc/group", "/etc/group-", "/etc/pam.d/passwd",
                                  "/usr/bin/passwd")}


def _extra_dirs() -> list[Path]:
    """Orbwise-Konfiguration (Tokens, Passwörter) – auch wenn sie per XDG/ORBWISE_CONFIG woanders liegt."""
    from ..config import CONFIG_DIR, config_path
    out = [CONFIG_DIR]
    try:
        out.append(config_path())
    except Exception:  # noqa: BLE001
        pass
    return out


def _within(path: Path, base: Path) -> bool:
    try:
        return path == base or path.is_relative_to(base)
    except ValueError:
        return False


def is_secret_path(path: str | os.PathLike) -> bool:
    raw = str(path).strip()
    if not raw:
        return False
    p = Path(os.path.expandvars(raw)).expanduser()  # auch $HOME/.ssh/…
    candidates = {p.absolute()}
    try:
        candidates.add(p.resolve())  # Symlinks auflösen
    except (OSError, RuntimeError):
        pass
    for c in candidates:
        if c in PUBLIC_FILES:
            continue
        name = c.name.lower()
        if name in SECRET_NAMES or name.endswith(SECRET_SUFFIXES) or name.startswith((".env.", "id_rsa", "id_ed25519")):
            return True
        if any(w in name for w in SECRET_WORDS):
            return True
        if any(w in part.lower() for part in c.parts[:-1] for w in ("keyring", ".password-store")):
            return True
        text = c.as_posix().lower() + "/"
        if any(f"/{d.lower()}/" in text for d in SECRET_DIRS):
            return True
        if any(_within(c, Path(s)) for s in SYSTEM_SECRETS):
            return True
        if c.parts[:2] == ("/", "proc") and name == "environ":
            return True
        for extra in _extra_dirs():
            try:
                if _within(c, extra.expanduser().resolve()):
                    return True
            except (OSError, RuntimeError):
                continue
    return False


def secret_reason() -> str:
    from ..lang import T
    return T("Schlüssel, Passwörter und Zugangsdaten gibt Orbwise nicht heraus",
             "Orbwise never hands out keys, passwords or credentials")


def contains_secrets(path: str | os.PathLike) -> bool:
    """Liegen unterhalb dieses Ordners bekannte Geheimnisse (für rekursive Suchen wie grep -r ~)?"""
    try:
        base = Path(path).expanduser().resolve()
    except (OSError, RuntimeError):
        return True
    if not base.is_dir():
        return False
    if _within(base, Path("/proc")):  # /proc/<pid>/environ – Umgebung samt Tokens
        return True
    home = Path(os.path.expanduser("~")).resolve()
    inside = [home / d for d in SECRET_DIRS] + [Path(s) for s in SYSTEM_SECRETS]
    for extra in _extra_dirs():
        try:
            inside.append(extra.expanduser().resolve())
        except (OSError, RuntimeError):
            continue
    return any(_within(p, base) for p in inside)


_BRACE = re.compile(r"\{([^{}]*,[^{}]*)\}")


def _braces(arg: str, limit: int = 64) -> list[str]:
    """Klammer-Erweiterung der Shell: ~/.ss{h,x}/id → ~/.ssh/id, ~/.ssx/id."""
    out, todo = [], [arg]
    while todo and len(out) + len(todo) <= limit:
        cur = todo.pop()
        m = _BRACE.search(cur)
        if not m:
            out.append(cur)
            continue
        todo += [cur[:m.start()] + alt + cur[m.end():] for alt in m.group(1).split(",")]
    return out + todo


def expand_arg(arg: str, cwd: str, limit: int = 500) -> list[str] | None:
    """Ein Shell-Argument so auflösen, wie bash es täte (~, $VAR, {a,b}, Globs, relativ zu cwd).
    None, wenn das nicht sicher geht (unbekannte Variable, $'…')."""
    out: list[str] = []
    for part in _braces(arg):
        part = os.path.expandvars(part)
        if "$" in part:
            return None
        path = os.path.join(cwd, os.path.expanduser(part))
        out.append(path)
        if glob.has_magic(path):
            out += glob.glob(path, include_hidden=True)[:limit]
    return out
