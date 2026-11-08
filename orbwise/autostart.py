"""Autostart beim Anmelden – standardmäßig aus. Orbwise startet sonst über den Eintrag im Anwendungsmenü (orbwise-open
startet den Server bei Bedarf); wer Routinen oder Telegram auch ohne geöffnetes Fenster braucht, schaltet ihn in den
Einstellungen (Allgemein) oder mit `orbwise autostart on` ein. Der Eintrag liegt in ~/.config/autostart (XDG), damit
Orbwise in der Desktop-Sitzung läuft (nötig, um Dateien und Programme auf dem Desktop zu öffnen)."""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

NAME = "Orbwise (background service)"


def path() -> Path:
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "autostart" / "orbwise.desktop"


def _ours(text: str) -> bool:
    return "orbwise" in text.lower() and "[Desktop Entry]" in text


def enabled() -> bool:
    try:
        text = path().read_text(encoding="utf-8")
    except OSError:
        return False
    flat = text.replace(" ", "").lower()
    return _ours(text) and "hidden=true" not in flat and "x-gnome-autostart-enabled=false" not in flat


def _command() -> str:
    """Wie scripts/orbwise-autostart.desktop: Server starten, Ausgabe ins Protokoll."""
    launcher = Path.home() / ".local" / "bin" / "orbwise"
    exe = str(launcher) if launcher.is_file() else shutil.which("orbwise")
    serve = f'"{exe}" serve' if exe else f'"{sys.executable}" -m orbwise.cli serve'
    state = "${XDG_STATE_HOME:-$HOME/.local/state}"
    return f"""sh -c 'mkdir -p "{state}" && exec {serve} >"{state}/orbwise.log" 2>&1'"""


def content() -> str:
    return (f"[Desktop Entry]\nType=Application\nName={NAME}\nComment=Starts the Orbwise server at login\n"
            f"Exec={_command()}\nTerminal=false\nNoDisplay=true\nX-GNOME-Autostart-enabled=true\n")


def set_enabled(on: bool) -> bool:
    """Eintrag anlegen bzw. löschen (nur einen von Orbwise) → neuer Zustand."""
    p = path()
    if on:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content(), encoding="utf-8")
    else:
        try:
            if _ours(p.read_text(encoding="utf-8")):
                p.unlink()
        except OSError:
            pass
    return enabled()
