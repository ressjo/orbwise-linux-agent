"""Werkzeug-Auswahl, wenn nicht alle Werkzeuge ins Kontextfenster passen.

Mit allen Integrationen belegen die Werkzeugbeschreibungen fast 14k Token. Das Modell bekommt deshalb eine
Grundausstattung plus die Gruppen, die zur Frage passen (Stichwörter, Deutsch/Englisch), in dieser Runde benutzt
oder per load_tools nachgeladen wurden.

Zwei Stufen (siehe context_plan.py):
- ab 16k: Grundausstattung = ganze Gruppen (CORE_GROUPS, ~4k Token); weitere Gruppen kommen dazu und bleiben.
- unter 16k: Grundausstattung = wenige einzelne Werkzeuge (SMALL_CORE, ~1,5k Token); alles andere per Stichwort –
  bei großen Gruppen zuerst nur der lesende Teil, der ändernde (WRITE_TOOLS) erst, wenn die Bitte danach klingt.
  Auswahl-Einheiten sind dann Gruppen („paperless“) und ändernde Teile („paperless:write“).
"""

from __future__ import annotations

import re

# Ab 16k immer dabei (zusammen ca. 4k Token)
CORE_GROUPS = {"files", "shell", "web", "apps", "memory_tools", "reminder_tools", "power", "weather", "briefing",
               "system", "todo_tools", "toolload"}

# Unter 16k immer dabei (zusammen ca. 1,5k Token) – der Rest kommt per Stichwort oder load_tools
SMALL_CORE = {"run_shell", "read_file", "find_files", "list_directory", "web_search", "recall", "remember",
              "date_info", "load_tools"}

# Kurzbeschreibung je nachladbarer Gruppe (für load_tools – das Modell weiß so, was es gibt)
GROUP_LABELS = {
    "de": {"packages": "Pakete, Updates", "sysadmin": "Prozesse, Dienste, Logs, Netzwerk, Speicherplatz",
           "homeassistant": "Smart Home: Licht, Heizung, Geräte", "mail": "E-Mails", "routine_tools": "Routinen",
           "paperless": "Dokumente, Rechnungen, Verträge", "trilium": "Notizen",
           "obsidian": "Notizen", "vision": "Bildschirm, Bilder", "telegram_tools": "Dateien aufs Handy",
           "calendar_tools": "Kalender, Termine", "portainer": "Docker-Container, Stacks (Portainer)",
           "ssh": "andere Rechner per SSH (NAS, Server)", "image_tools": "Bilder erzeugen",
           # nur im kleinen Fenster nachzuladen (ab 16k immer dabei)
           "weather": "Wetter", "power": "Ausschalten, Neustart, Standby, Sperren", "apps": "Programme öffnen",
           "briefing": "Tagesüberblick", "reminder_tools": "Erinnerungen, Timer",
           "files": "Dateien schreiben/ändern/öffnen", "web": "Webseiten öffnen/lesen", "system": "Systeminfo",
           "todo_tools": "Aufgabenliste", "memory_tools": "Fakten löschen"},
    "en": {"packages": "packages, updates", "sysadmin": "processes, services, logs, network, disk space",
           "homeassistant": "smart home: lights, heating, devices", "mail": "e-mail", "routine_tools": "routines",
           "paperless": "documents, invoices, contracts", "trilium": "notes", "obsidian": "notes",
           "vision": "screen, images", "telegram_tools": "files to the phone", "calendar_tools": "calendar, events",
           "portainer": "docker containers, stacks (Portainer)", "ssh": "other computers via SSH (NAS, server)",
           "image_tools": "generate images",
           "weather": "weather", "power": "shut down, reboot, suspend, lock", "apps": "open programs",
           "briefing": "daily briefing", "reminder_tools": "reminders, timers", "files": "write/edit/open files",
           "web": "open/read web pages", "system": "system info", "todo_tools": "task list",
           "memory_tools": "delete facts"},
}
# Was der ändernde Teil einer Gruppe kann (load_tools nennt es, wenn nur der lesende Teil geladen ist)
WRITE_LABELS = {
    "de": {"paperless": "einordnen, sortieren", "mail": "aufräumen, senden", "calendar_tools": "Termine ändern",
           "sysadmin": "Dienste steuern, beenden, aufräumen", "obsidian": "Notizen anlegen",
           "trilium": "Notizen anlegen", "packages": "installieren, entfernen",
           "portainer": "aktualisieren, starten/stoppen, installieren"},
    "en": {"paperless": "classify, sort", "mail": "tidy up, send", "calendar_tools": "change events",
           "sysadmin": "control services, kill, clean up", "obsidian": "create notes", "trilium": "create notes",
           "packages": "install, remove", "portainer": "update, start/stop, install"},
}

KEYWORDS = {
    "packages": r"update|upgrade|paket|package|install|deinstall|uninstall|entfern|pacman|\bapt\b|\baur\b|yay|paru",
    "sysadmin": r"prozess|process|dienst|service|systemd|systemctl|\blogs?\b|journal|netzwerk|network|\bip\b|ping|"
                r"\bports?\b|wlan|wifi|lan\b|speicher|disk|festplatte|platz|space|aufräum|cleanup|clean up|\bcpu\b|"
                r"\bram\b|auslast|langsam|slow|kill|beend|hängt|hang|router|dns|erreichbar|reachable|docker|ssh",
    "homeassistant": r"licht|lampe|light|lamp|heizung|heating|thermostat|rollo|jalousie|blind|shutter|cover|"
                     r"steckdose|plug|schalte|switch|szene|scene|temperatur|temperature|sensor|smart ?home|"
                     r"home assistant|garage|\btür|door|schloss|lock|ventilator|fan|dimm|hell|bright|staubsauger|vacuum",
    "mail": r"\bmail|e-?mail|postfach|mailbox|\binbox|posteingang|nachricht(en)? von|absender|sender|newsletter|"
            r"anhang|anhänge|attachment|ungelesen|unread|spam|archivier|archive",
    "routine_tools": r"routine|jeden (morgen|abend|tag|montag|dienstag|mittwoch|donnerstag|freitag|samstag|sonntag|"
                     r"werktag)|werktags|täglich|wöchentlich|regelmäßig|automatisch|zeitplan|um \d{1,2}([:.]\d\d)? ?uhr|"
                     r"every (day|morning|evening|week|monday|tuesday|wednesday|thursday|friday)|daily|weekly|"
                     r"schedul|at \d{1,2}(:\d\d)? ?(am|pm)",
    "paperless": r"dokument|document|rechnung|invoice|vertrag|contract|\bbrief|letter|paperless|bescheid|"
                 r"versicherung|insurance|quittung|receipt|garantie|warranty|kündig|cancel|steuer|tax|\bpdf|"
                 r"\btags\b|tagge|schlagw|korrespondent|correspondent|dokumenttyp|document type|posteingang|inbox|"
                 r"einordn|sortier|classif",
    "trilium": r"notiz|\bnotes?\b(?![./\w])|trilium|notier|aufschrieb|anleitung|how-?to|wiki|schreib (das |mir )?auf|"
               r"write down",
    "obsidian": r"notiz|\bnotes?\b(?![./\w])|obsidian|notier|aufschrieb|anleitung|how-?to|wiki|protokoll|minutes|vault|"
                r"schreib (das |mir )?auf|write down",
    "vision": r"bildschirm|screen|monitor|fenster|window|siehst du|sieh dir|schau (dir |mal )?|guck|"
              r"fehlermeldung|error message|meldung|dialog|popup|pop-up|foto|photo|bild\b|bilder|image|picture|"
              r"screenshot|was steht da|what does it say|look at|anschau",
    "telegram_tools": r"telegram|handy|smartphone|\bphone|aufs? (telefon|mobil)|schick (mir|sie|es|das|die|den)|"
                      r"send (me|it|this|that)",
    "portainer": r"docker|container|portainer|\bstacks?\b|compose|\bimages?\b|self-?host|watchtower",
    "ssh": r"\bssh\b|\bnas\b|\bserver\b|synology|unraid|truenas|openmediavault|proxmox|raspberry|\bpi\b|"
           r"einlogg|anmelden (auf|am|bei)|log ?in (to|on)|remote|auf (dem|meinem) (nas|server|pi)",
    "image_tools": r"(bild|foto|grafik|illustration|logo|wallpaper|hintergrundbild|poster|icon)\w*\b.*\b(erzeug|"
                   r"generier|mal|zeichne|erstell|mach)|\b(erzeug|generier|zeichne|mal)\w*\b.*\b(bild|foto|logo|illustration|"
                   r"wallpaper|poster|icon|grafik)|generate an? (image|picture)|"
                   r"draw (me )?an?|paint (me )?an?|create an? (image|picture|logo|illustration)",
    "calendar_tools": r"termin|kalender|calendar|meeting|appointment|\bevent|verabred|besprechung|"
                      r"frei(e zeit)?\b|free time|schedule|wann habe ich|when do i",
}

# Unter 16k zusätzlich per Stichwort: die Gruppen, die ab 16k immer dabei sind
SMALL_KEYWORDS = {
    "weather": r"wetter|weather|regen|rain|schnee|snow|sonnig|sunny|gewitter|storm|temperatur|temperature|"
               r"\bgrad\b|°|vorhersage|forecast|regenschirm|umbrella|jacke|jacket|draußen|outside|\bwind",
    "power": r"herunterfahr|runterfahr|fahr\b.*\b(runter|herunter)|ausschalt|schalt\b.*\baus\b|shut ?down|"
             r"power off|neustart|neu ?start|reboot|restart|standby|suspend|ruhezustand|hibernat|sperr|"
             r"lock (the )?(screen|computer|pc)|bildschirm aus",
    "apps": r"öffne|öffnen|starte|start|launch|\bopen\b|programm|program|anwendung|application|\bapps?\b|installiert",
    "briefing": r"guten morgen|good morning|briefing|tagesüberblick|überblick|was steht (heute )?an|"
                r"what'?s (on|up) today|tagesplan",
    "reminder_tools": r"erinner|remind|timer|wecker|alarm|countdown|stoppuhr|"
                      r"in \d+ ?(sek|min|std|stunde|sec|minute|hour)",
    "files": r"schreib(?!.*\b(e-?mail|mail|nachricht|notiz|sms|telegram)\b)|\bwrite(?!.*\b(e-?mail|mail|note)\b)|abspeicher|speicher\w* (das|es|die|den|unter|in|als)\b|\bsave\b|anleg|erstell|"
             r"\bcreate\b|änder|ersetz|bearbeit|\bedit|umbenenn|rename|öffne\w*\b.*\b(datei|ordner|pdf|bild|foto|"
             r"dokument)|\bopen\b.*\b(file|folder|pdf|image)|durchsuch|grep|enthält|enthalten|contains|"
             r"in welche[nr]? dateien|which files",
    "web": r"https?://|www\.|\.(de|com|org|net|io|eu)\b|website|webseite|internetseite|seite|\bsite\b|\bpage\b|"
           r"\burl\b|\blink|youtube|artikel|article",
    "system": r"system|rechner|computer|\bpc\b|kernel|version|hardware|uptime|laufzeit|distribution|distro",
    "todo_tools": r"aufgabenliste|todo|to-?do|checkliste|checklist|schritt für schritt|step by step",
    "memory_tools": r"vergiss|vergessen|forget|lösch\w* (die |den |das )?(erinnerung|fakt)",
}

# Unter 16k: diese Werkzeuge einer Gruppe kommen nur mit, wenn die Bitte nach Ändern klingt
WRITE_TOOLS = {
    "paperless": {"paperless_suggest_metadata", "paperless_apply_metadata", "paperless_review_next",
                  "paperless_review_skip", "paperless_upload"},
    "mail": {"mail_manage", "mail_to_paperless", "mail_send"},
    "calendar_tools": {"calendar_add", "calendar_update", "calendar_delete"},
    "sysadmin": {"service_control", "kill_process", "cleanup_system"},
    "obsidian": {"obsidian_create_note", "obsidian_update_note", "obsidian_append"},
    "trilium": {"trilium_create_note", "trilium_update_note", "trilium_append"},
    "packages": {"install_package", "remove_package", "system_update"},
    "portainer": {"portainer_container_action", "portainer_update", "portainer_deploy_stack", "portainer_stack_action"},
}
WRITE_INTENT = re.compile(
    r"sortier|einordn|ordne\b|klassifizier|verschlagwort|tagge|anleg|\bleg\b.*\ban\b|erstell|\bneue[nmrs]? "
    r"(notiz|termin|eintrag)|\btrag\b.*\bein\b|eintrag|hinzufüg|\bfüg\b.*\bhinzu\b|ergänz|änder|bearbeit|schreib|"
    r"notier|\bräum\w*\b.*\bauf\b|"
    r"aktualisier|umbenenn|verschieb|archivier|lösch|entfern|papierkorb|markier|als gelesen|\bsend|schick|"
    r"antworte|weiterleit|hochlad|\blad\b.*\bhoch\b|upload|install|update|upgrade|neustart|neu ?start|restart|"
    r"stopp|\bstop|beend|kill|aufräum|clean ?up|übernehm|anwend|weiter\b|verleg|absag|"
    r"\bcreate|\badd\b|\bnew (note|event|mail)|\bedit|\bchange|\bmove|\bdelete|\bremove|\btrash|"
    r"\bmark (as|it|them|all)\b|\breply|forward|\bapply|\bsort|classify|\bcontinue|cancel|\bwrite\b|"
    r"(note|jot) (it )?down",
    re.I)
# Kurze Zustimmung („ja“, „mach das“): dann zählt, was das Modell zuletzt vorgeschlagen hat
FOLLOW_UP = re.compile(r"^\W*(ja|jo|jep|ok(ay)?|gerne?|bitte|mach (das|es|weiter|mal)|los|weiter|genau|richtig|"
                       r"stimmt|passt|einverstanden|klar|yes|yeah|yep|sure|please|go ahead|do it|continue|right)\b",
                       re.I)

# „Posteingang“ gibt es in Mail und Paperless – wer Paperless nennt, meint nicht das Postfach
MAIL_ONLY = re.compile(r"\bmail|e-?mail|postfach|mailbox|ungelesen|unread|newsletter|absender|sender|spam", re.I)
PAPERLESS_NAMED = re.compile(r"paperless|dokument|document", re.I)

_COMPILED = {g: re.compile(p, re.I) for g, p in KEYWORDS.items()}
_SMALL = {g: re.compile(p, re.I) for g, p in SMALL_KEYWORDS.items()}
KNOWN_GROUPS = CORE_GROUPS | set(KEYWORDS) | set(SMALL_KEYWORDS)


def _matching(joined: str) -> set[str]:
    groups = {g for g, rx in _COMPILED.items() if rx.search(joined)}
    if "mail" in groups and PAPERLESS_NAMED.search(joined) and not MAIL_ONLY.search(joined):
        groups.discard("mail")
    return groups


def relevant_groups(texts: list[str]) -> set[str]:
    """Nachladbare Gruppen (Stichwörter ab 16k), die zu den Texten passen."""
    return _matching("\n".join(t for t in texts if t))


def wants_change(texts: list[str]) -> bool:
    """Klingt die Bitte nach Anlegen, Ändern, Löschen, Senden, Sortieren …?"""
    return bool(WRITE_INTENT.search("\n".join(t for t in texts if t)))


def is_follow_up(text: str) -> bool:
    """Zustimmung oder Fortsetzung („ja“, „mach das“, „weiter“) – sie bezieht sich auf den letzten Vorschlag des
    Modells."""
    return bool(FOLLOW_UP.match(text or ""))


def write_units(groups: set[str]) -> set[str]:
    """Ändernde Teile zu den Gruppen (nur Gruppen, die einen haben)."""
    return {f"{g}:write" for g in groups if g in WRITE_TOOLS}


def units_for(texts: list[str], small: bool) -> set[str]:
    """Auswahl-Einheiten, die zu den Texten passen. Klein: auch die Gruppen der Grundausstattung (per Stichwort)
    und – klingt die Bitte nach Ändern – der ändernde Teil der passenden Gruppen."""
    joined = "\n".join(t for t in texts if t)
    units = _matching(joined)
    if small:
        units |= {g for g, rx in _SMALL.items() if rx.search(joined)}
        if WRITE_INTENT.search(joined):
            units |= write_units(units)
    return units


def unit_of(name: str, group: str, small: bool) -> str:
    """Auswahl-Einheit eines Werkzeugs: seine Gruppe – klein beim ändernden Teil „gruppe:write“."""
    return f"{group}:write" if small and name in WRITE_TOOLS.get(group, ()) else group


def select(schemas: list[dict], groups_of: dict[str, str], units: set[str], small: bool = False) -> list[dict]:
    """Werkzeuge der gewählten Einheiten. Groß: Grundausstattung = CORE_GROUPS (müssen in units stehen), eine
    Gruppe heißt immer die ganze Gruppe. Klein: SMALL_CORE immer, sonst nach Einheit. Unbekannte Gruppen (z. B.
    Plugins) bleiben drin."""
    out = []
    for s in schemas:
        name = s["function"]["name"]
        group = groups_of.get(name, "")
        if group not in KNOWN_GROUPS or (small and name in SMALL_CORE):
            out.append(s)
        elif unit_of(name, group, small) in units or (not small and group in {u.split(":")[0] for u in units}):
            out.append(s)
    return out
