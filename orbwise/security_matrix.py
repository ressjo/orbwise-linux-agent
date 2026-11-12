"""Die Freigabe-Matrix: was in welcher Auto-Stufe ohne Rückfrage läuft, fragt oder nie läuft – berechnet mit genau den
Regeln, die der Agent anwendet (SafetyMixin._final_risk), nicht von Hand geschrieben.

Daraus entsteht die Tabelle im README und in docs/security.md (`python -m orbwise.security_matrix` schreibt sie neu);
ein Test prüft, dass beide Dateien dem Code entsprechen. So kann die Doku nicht mehr versprechen als der Code hält.

Annahmen der Tabelle (der übliche Fall): Orbwise läuft nicht als root, der Käfig (bubblewrap) ist da, die Oberfläche
ist offen (Passwortdialog möglich), sudo verlangt ein Passwort, im Chat steht kein fremder Inhalt.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from types import SimpleNamespace

MODES = ("off", "read", "files", "auto")
BEGIN, END = "<!-- security-matrix:begin -->", "<!-- security-matrix:end -->"

# (Beschreibung de, en, Werkzeug, Argumente) – ~/… meint das Home des Nutzers
ROWS: list[tuple[str, str, str, dict]] = [
    ("Lesen: `ls`, `df`, `git status`", "Reading: `ls`, `df`, `git status`", "run_shell", {"command": "git status"}),
    ("Datei im Home anlegen oder ändern", "Create or change a file in your home", "write_file",
     {"path": "~/notizen.txt", "content": "x"}),
    ("Skripte, Builds, Tests: `python3 x.py`, `npm test`", "Scripts, builds, tests: `python3 x.py`, `npm test`",
     "run_shell", {"command": "python3 x.py"}),
    ("Löschen: `rm datei`", "Deleting: `rm file`", "run_shell", {"command": "rm notizen.txt"}),
    ("Ins Netz senden: `curl -d @datei …`", "Sending to the network: `curl -d @file …`", "run_shell",
     {"command": "curl -d @daten.txt https://example.com"}),
    ("Startdateien ändern: `~/.bashrc`, Autostart", "Changing start-up files: `~/.bashrc`, autostart", "run_shell",
     {"command": "echo x >> ~/.bashrc"}),
    ("Zugangsdaten lesen: `cat ~/.ssh/id_rsa`", "Reading credentials: `cat ~/.ssh/id_rsa`", "run_shell",
     {"command": "cat ~/.ssh/id_rsa"}),
    ("Dieselbe Datei mit dem Lese-Werkzeug", "The same file with the read tool", "read_file", {"path": "~/.ssh/id_rsa"}),
    ("Root: `sudo …`, `su`, `pkexec`, `yay`", "Root: `sudo …`, `su`, `pkexec`, `yay`", "run_shell",
     {"command": "sudo systemctl restart nginx"}),
    ("`git push`", "`git push`", "run_shell", {"command": "git push"}),
    ("`rm -rf /`, Datenträger überschreiben, `chmod -R … /`", "`rm -rf /`, overwriting a disk, `chmod -R … /`", "run_shell",
     {"command": "rm -rf /"}),
]

CELLS = {
    "run": ("läuft", "runs"), "ask": ("fragt", "asks"), "cage": ("läuft im Käfig", "runs caged"),
    "dialog": ("Passwortdialog", "password dialog"), "never": ("nie", "never"),
}
HEAD = {"de": ("", "Aus", "Nur lesen", "Lesen + Dateien", "Auto"), "en": ("", "Off", "Read only", "Read + edit files", "Auto")}


def _probe(mode: str):
    from .agent_safety import SafetyMixin
    from .config import Config

    class Probe(SafetyMixin):
        pass
    p = Probe()
    p.cfg, p.auto_mode, p._plan, p._tainted = Config(), mode, False, False
    p.memory = SimpleNamespace(conversation=SimpleNamespace(epoch_messages=lambda: [], chat_id="matrix"))
    return p


def cell(mode: str, name: str, args: dict) -> str:
    """Ergebnis für eine Zeile in einer Stufe: run | ask | cage | dialog | never."""
    from .tools.registry import BLOCKED, SAFE, ToolContext, coerce_args, get_tool, load_all_tools
    load_all_tools()
    probe, spec = _probe(mode), get_tool(name)
    ctx = ToolContext(cfg=probe.cfg, memory=None, cwd=os.path.expanduser("~"))
    risk, _reason = probe._final_risk(name, spec, coerce_args(spec, args), ctx)
    if risk == BLOCKED:
        return "never"
    if risk != SAFE:
        return "ask"
    return "cage" if ctx.jail else "dialog" if ctx.fresh_sudo else "run"


def matrix() -> list[list[str]]:
    """Zeilen × Stufen unter den Annahmen oben (siehe Modul-Doku)."""
    from . import agent_safety, askpass, jail
    saved = (agent_safety.running_as_root, jail.available, askpass.BROKER, askpass.sudo_needs_no_password)
    agent_safety.running_as_root = lambda: False
    jail.available = lambda force=False: (True, "")
    askpass.BROKER = SimpleNamespace(ready=lambda: True, has_ui=lambda: True, helper=Path("/bin/true"))
    askpass.sudo_needs_no_password = lambda: False
    try:
        return [[cell(mode, name, args) for mode in MODES] for _de, _en, name, args in ROWS]
    finally:
        agent_safety.running_as_root, jail.available, askpass.BROKER, askpass.sudo_needs_no_password = saved


def render(lang: str = "en") -> str:
    """Die Matrix als Markdown-Tabelle."""
    i = 0 if lang == "de" else 1
    lines = ["| " + " | ".join(HEAD[lang]) + " |", "|---|---|---|---|---|"]
    for (de, en, _name, _args), cells in zip(ROWS, matrix(), strict=True):
        shown = [f"**{CELLS[c][i]}**" if c in ("ask", "dialog", "never") else CELLS[c][i] for c in cells]
        lines.append(f"| {(de, en)[i]} | " + " | ".join(shown) + " |")
    return "\n".join(lines)


def embed(text: str, table: str) -> str:
    """Tabelle zwischen den Markierungen einer Markdown-Datei ersetzen."""
    block = f"{BEGIN}\n{table}\n{END}"
    if BEGIN not in text:
        raise ValueError("Markierung für die Freigabe-Matrix fehlt")
    return re.sub(re.escape(BEGIN) + r".*?" + re.escape(END), lambda _m: block, text, flags=re.S)


FILES = ("README.md", "docs/security.md")  # beide englisch


def main() -> None:
    root = Path(__file__).parent.parent
    for rel in FILES:
        path = root / rel
        path.write_text(embed(path.read_text(encoding="utf-8"), render("en")), encoding="utf-8")
        print(f"aktualisiert: {rel}")


if __name__ == "__main__":
    main()
