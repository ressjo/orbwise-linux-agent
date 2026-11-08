"""Risikobewertung von Shell-Befehlen – wer was ohne Rückfrage ausführen darf.

Aufbau:
- shellwords.py  Befehle zerlegen (Heredocs, Wörter, Teilbefehle, Wrapper wie sudo/env), gemeinsamer Durchlauf walk()
- readonly.py    welche Programme mit welchen Argumenten nur lesen (Regel je Programm + Tabellen)
- credentials.py liest ein Befehl Zugangsdaten (Schlüsseldateien, Umgebung mit Tokens, curl-Uploads)?
- classify.py    normale Freigabe: safe / confirm / blocked
- auto.py        Auto-Modi „Dateien bearbeiten“ und „Auto“ (Prüfungen je Programm in AUTO_RULES)
- privilege.py   sudo durch das eingestellte Werkzeug ersetzen
"""

from .auto import auto_full_check, auto_shell_ok, file_edit_ok
from .classify import classify_command, local_read_only
from .privilege import apply_privilege

__all__ = ["apply_privilege", "auto_full_check", "auto_shell_ok", "classify_command", "file_edit_ok",
           "local_read_only"]
