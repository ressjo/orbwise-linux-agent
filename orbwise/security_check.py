"""`orbwise doctor --security`: Wie steht es auf DIESEM Rechner um die Schutzmechanismen? Die Regeln sind überall
gleich – ob sie greifen, hängt von der Umgebung ab (Käfig vorhanden? sudo mit Passwort? nur auf localhost?).

Jede Zeile ist eine Tatsache, die hier geprüft wurde: ok (Schutz greift), warn (Schutz fehlt oder ist geschwächt –
mit dem, was daraus folgt) oder info. Nichts davon ändert etwas."""

from __future__ import annotations

import getpass
import grp
import os
import stat
from typing import Any

from .lang import T

LOCAL = {"127.0.0.1", "localhost", "::1"}
MODE_NAMES = {"off": ("Aus", "Off"), "read": ("Nur lesen", "Read only"), "files": ("Lesen + Dateien", "Read + edit files"),
              "auto": ("Auto", "Auto")}


def _in_group(name: str) -> bool:
    try:
        group = grp.getgrnam(name)
    except KeyError:
        return False
    return group.gr_gid in os.getgroups() or getpass.getuser() in group.gr_mem


def report(cfg: Any) -> list[dict]:
    """→ [{level: ok|warn|info, text}] in fester Reihenfolge."""
    from . import askpass, jail, web_auth
    from .agent_safety import running_as_root
    out: list[dict] = []

    def add(level: str, de: str, en: str) -> None:
        out.append({"level": level, "text": T(de, en)})

    # Wer läuft, und wer kommt heran
    if running_as_root():
        add("warn", "Orbwise läuft als root: jede Stufe fragt dann wie „Nur lesen“ – und jeder bestätigte Befehl läuft "
                    "als root. Besser als normaler Benutzer starten.",
            "Orbwise runs as root: every level then asks like \"Read only\" – and every confirmed command runs as "
            "root. Better start it as a normal user.")
    else:
        add("ok", "Läuft als normaler Benutzer – Root gibt es nur über sudo.",
            "Runs as a normal user – root only through sudo.")
    host = str(getattr(cfg, "host", "127.0.0.1"))
    if host in LOCAL:
        add("ok", f"Oberfläche nur auf diesem Rechner erreichbar ({host}).",
            f"Web UI reachable from this machine only ({host}).")
    else:
        add("warn", f"Oberfläche lauscht auf {host}: aus dem Netz erreichbar – wer das Zugangs-Token hat, bedient "
                    "Orbwise. Eine Sandbox mit Netz erreicht sie ebenfalls.",
            f"Web UI listens on {host}: reachable from the network – whoever has the access token controls Orbwise. "
            "A sandbox with network on reaches it too.")
    token = web_auth.token_path()
    try:
        mode = stat.S_IMODE(token.stat().st_mode)
        if mode & 0o077:
            add("warn", f"Zugangs-Token {token} ist für andere lesbar (Rechte {mode:o}) – chmod 600.",
                f"Access token {token} is readable by others (mode {mode:o}) – chmod 600.")
        else:
            add("ok", "Zugangs-Token nur für dich lesbar.", "Access token readable by you only.")
    except OSError:
        add("info", "Zugangs-Token noch nicht angelegt (entsteht beim ersten Start).",
            "Access token not created yet (written on first start).")

    # Stufe und Käfig
    saved = ""
    try:
        saved = (cfg.memory.dir.parent / "auto_mode").read_text(encoding="utf-8").strip()
    except OSError:
        pass
    mode = saved if saved in MODE_NAMES else "read"
    name = T(*MODE_NAMES[mode])
    if mode == "auto":
        add("warn", f"Auto-Stufe „{name}“: alles außer Root, Pushen, Mails und Zugangsdaten läuft ohne Rückfrage – auch Löschen, "
                    "Netz und was eine Webseite dem Modell einredet.",
            f"Auto level \"{name}\": everything but root, pushing, mail and credentials runs without asking – deleting, network "
            "and whatever a web page talks the model into included.")
    else:
        add("ok", f"Auto-Stufe „{name}“.", f"Auto level \"{name}\".")
    ok, why = jail.available()
    if ok:
        add("ok", "Käfig (bubblewrap) funktioniert: in „Lesen + Dateien“ laufen Skripte und Builds ohne Netz und "
                  "schreiben nur im Projekt.",
            "Cage (bubblewrap) works: in \"Read + edit files\" scripts and builds run without network and write only "
            "in the project.")
    else:
        add("warn", f"Kein Käfig ({why}) – in „Lesen + Dateien“ fragen Skripte und Builds stattdessen jedes Mal.",
            f"No cage ({why}) – in \"Read + edit files\" scripts and builds ask every time instead.")

    # Root
    priv = getattr(cfg.tools, "privilege_cmd", "dashboard")
    if priv == "sudo":
        add("warn", "privilege_cmd: sudo – Root läuft ohne Passwort (NOPASSWD). Die Bestätigungskarte ist dann die "
                    "einzige Schranke.",
            "privilege_cmd: sudo – root runs without a password (NOPASSWD). The confirmation card is then the only "
            "barrier.")
    elif not running_as_root() and askpass.sudo_needs_no_password():
        add("warn", "sudo verlangt für dich kein Passwort (NOPASSWD): der Passwortdialog erscheint nie. Orbwise fragt "
                    "bei sudo mit einer Karte – ruft aber ein Skript sudo auf, läuft es in „Auto“ ohne Rückfrage "
                    "als root.",
            "sudo asks you for no password (NOPASSWD): the password dialog never appears. Orbwise asks for sudo "
            "with a card – but when a script calls sudo, it runs as root without asking in \"Auto\".")
    elif not running_as_root():
        add("ok", "sudo verlangt ein Passwort: Root gibt es nur über den Passwortdialog, der den Befehl zeigt.",
            "sudo asks for a password: root only through the password dialog, which shows the command.")
    if _in_group("docker"):
        add("warn", "Du bist in der Gruppe „docker“: `docker run -v /:/host …` ist Root ohne Passwort – Orbwise "
                    "erkennt das nicht als Root-Befehl.",
            "You are in the \"docker\" group: `docker run -v /:/host …` is root without a password – Orbwise does "
            "not recognise that as a root command.")

    # Was von außen hereinkommt
    servers = {n: s for n, s in (getattr(getattr(cfg, "mcp", None), "servers", None) or {}).items()
               if getattr(s, "enabled", True)}
    wide = sorted(n for n, s in servers.items() if "*" in (getattr(s, "allow", None) or []))
    if wide:
        add("warn", f"MCP-Server mit allow: \"*\" ({', '.join(wide)}): alle ihre Werkzeuge laufen ohne Rückfrage.",
            f"MCP servers with allow: \"*\" ({', '.join(wide)}): all their tools run without asking.")
    elif servers:
        add("ok", f"{len(servers)} MCP-Server – ihre Werkzeuge fragen, außer den ausdrücklich erlaubten.",
            f"{len(servers)} MCP server(s) – their tools ask, except the ones explicitly allowed.")
    tg = getattr(cfg, "telegram", None)
    if tg is not None and getattr(tg, "token", "") and getattr(tg, "chat_id", 0):
        add("info", "Telegram ist eingerichtet: Aufträge und Bestätigungen kommen auch vom Handy (nur aus deinem Chat).",
            "Telegram is set up: requests and confirmations also come from your phone (only from your chat).")
    return out


def render(lines: list[dict]) -> str:
    mark = {"ok": "✔", "warn": "⚠", "info": "ℹ"}
    return "\n".join(f"  {mark[x['level']]} {x['text']}" for x in lines)
