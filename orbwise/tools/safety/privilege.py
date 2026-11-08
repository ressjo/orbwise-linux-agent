"""sudo im Befehl durch das eingestellte Werkzeug ersetzen (Passwortdialog der Oberfläche, pkexec oder sudo -n)."""

from __future__ import annotations

import re

# Davor: Zeilen-/Befehlsanfang, Leerraum – oder der Anfang des Befehlstexts einer Shell (bash -c 'sudo …', sh -lc "…"),
# sonst bekäme ein sudo darin kein -A und könnte ohne Terminal nicht nach dem Passwort fragen
_SUDO_RE = re.compile(r"(^|[;&|(]\s*|\s|\b(?:ba|z|da|k)?sh(?:\s+-[A-Za-z]+)*?\s+-[A-Za-z]*c[A-Za-z]*\s+['\"]\s*)"
                      r"(?:(?:/[\w.+-]+)*/)?sudo((?:\s+(?:-[ugpCrtUDRTh]\s+[^\s-]\S*|-[A-Za-z]+))*)\s+")
# Optionen mit Wert (z. B. -u root) bleiben erhalten; -n/-S/-A werden durch den gewählten Modus ersetzt
_SUDO_OWN_FLAGS = re.compile(r"\s+-[nSA]+\b")

_PKEXEC_RE = re.compile(r"(^|[;&|(]\s*|\s)pkexec\s+")
_AUR_RE = re.compile(r"(^|[;&|(]\s*)(yay|paru)\s+(?!.*--sudoflags)")


def apply_privilege(command: str, privilege_cmd: str) -> str:
    """Ersetzt 'sudo' durch das konfigurierte Werkzeug: 'dashboard' (früher 'orbwise') → sudo -A (Passwortdialog in
    der Oberfläche), 'pkexec' → grafischer Polkit-Dialog, 'sudo' → sudo -n (nur mit NOPASSWD-Regel)."""
    if privilege_cmd in ("dashboard", "orbwise"):
        command = _PKEXEC_RE.sub(lambda m: f"{m.group(1)}sudo ", command)
        # yay/paru rufen sudo selbst auf – mit -A kommt auch dafür der Passwortdialog der Oberfläche
        command = _AUR_RE.sub(lambda m: f"{m.group(1)}{m.group(2)} --sudoflags -A ", command)
        return _SUDO_RE.sub(lambda m: f"{m.group(1)}sudo -A{_SUDO_OWN_FLAGS.sub('', m.group(2))} ", command)
    if privilege_cmd == "pkexec":
        return _SUDO_RE.sub(lambda m: f"{m.group(1)}pkexec ", command)
    if privilege_cmd == "sudo":
        return _SUDO_RE.sub(lambda m: f"{m.group(1)}sudo -n ", command)
    return command
