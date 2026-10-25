"""Skills: was Orbwise kann – einrichten, an- und abschalten, ohne config.yaml anzufassen.

Ein Skill ist eine Fähigkeit mit ihren Werkzeuggruppen: Verbindungen (Mail, Paperless, Portainer …) brauchen Daten,
eingebaute (Routinen, Erinnerungen …) nur einen Schalter. Eingerichtet wird im Installer (`orbwise skills setup`) und
unter Einstellungen → Skills.

Gespeichert wird getrennt:
- Werte ohne Geheimnis und die Schalter in state.json (Abschnitt "skills"),
- Tokens, Passwörter und API-Keys im Schlüsselbund (keystore.py) – nie im Klartext in config.yaml.

`apply(cfg)` legt beides über die Werte aus config.yaml (Werte aus Oberfläche/CLI gelten vor config.yaml,
Umgebungsvariablen $ORBWISE_* bleiben als Notbehelf). Die Werkzeuge lesen weiter ganz normal ihre Config-Abschnitte.
`migrate_plaintext` zieht Geheimnisse, die noch in config.yaml stehen, einmalig in den Schlüsselbund um.
"""

from __future__ import annotations

import copy
import logging
import re
import shutil
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import keystore, state
from .config import env
from .lang import T

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Field:
    key: str
    label: tuple[str, str]
    type: str = "text"  # text | url | password | path | bool | number | select | list | hosts
    required: bool = False
    help: tuple[str, str] = ("", "")
    placeholder: str = ""
    advanced: bool = False  # hinter „Mehr“
    options: tuple[str, ...] = ()

    @property
    def secret(self) -> bool:
        return self.type == "password"


@dataclass(frozen=True)
class Skill:
    id: str
    name: tuple[str, str]
    icon: str
    desc: tuple[str, str]
    groups: tuple[str, ...]
    section: str = ""  # Config-Abschnitt der Felder ("" = nur Schalter)
    fields: tuple[Field, ...] = ()
    kind: str = "connect"  # connect (mit Daten) | builtin (nur Schalter)
    default_on: bool = True
    extra: bool = False  # Werkzeuge stehen in registry.DEFAULT_OFF – „an“ heißt tools.extra
    hint: tuple[str, str] = ("", "")  # Verweis, z. B. auf den Bildmodus
    test_fills: tuple[str, ...] = ()  # Felder, die der Test selbst ermittelt (Telegram: Chat-ID nach „/start“)
    test: Callable[[Any], Awaitable[dict]] | None = field(default=None, compare=False)


# ---------------------------------------------------------------- Verbindungstests (vorhandene Status-Funktionen)
async def _t_web(cfg):
    from .tools.web import search_status
    return await search_status(cfg)


async def _t_mail(cfg):
    from .tools.mail import mail_status
    return await mail_status(cfg)


async def _t_paperless(cfg):
    from .tools.paperless import paperless_status
    return await paperless_status(cfg)


async def _t_ha(cfg):
    from .tools.homeassistant import ha_status
    return await ha_status(cfg)


async def _t_portainer(cfg):
    from .tools.portainer import portainer_status
    return await portainer_status(cfg)


async def _t_calendar(cfg):
    from .tools.calendar_tools import calendar_status
    return await calendar_status(cfg)


async def _t_trilium(cfg):
    from .tools.trilium import trilium_status
    return await trilium_status(cfg)


async def _t_obsidian(cfg):
    from .tools.obsidian import obsidian_status
    return await obsidian_status(cfg)


async def _t_telegram(cfg):
    from .telegram import check_bot
    return await check_bot(cfg.telegram.secret, want_chat=not cfg.telegram.chat_id)


async def _t_github(cfg):
    from . import github
    tok, source = github.token_info(cfg)
    if not tok:
        return {"enabled": False, "online": False}
    u = await github.user(cfg)
    return {"enabled": True, "online": True, "login": u.get("login"), "source": source}


async def _t_weather(cfg):
    from .tools.weather import geocode
    if not cfg.weather.location:
        return {"enabled": False, "online": False}
    place = await geocode(cfg.weather.location)
    return {"enabled": True, "online": True, "place": place.get("name") or cfg.weather.location}


async def _t_ssh(cfg):
    from .tools.ssh import ssh_status
    st = ssh_status()
    return {"enabled": True, **st} if isinstance(st, dict) else {"enabled": True, "online": bool(st)}


VERIFY = Field("verify_ssl", ("Zertifikat prüfen", "Verify certificate"), "bool", advanced=True,
               help=("Aus bei selbst signierten Zertifikaten im Heimnetz.", "Off for self-signed certificates at home."))

SKILLS: tuple[Skill, ...] = (
    Skill("web", ("Websuche", "Web search"), "globe",
          ("Suchen und Webseiten lesen. Ohne Key über DuckDuckGo.", "Search and read web pages. Without a key via "
           "DuckDuckGo."), ("web",), "tools", (
              Field("brave_api_key", ("Brave-API-Key", "Brave API key"), "password",
                    help=("api-dashboard.search.brave.com → API Keys (kostenloses Kontingent). Leer = DuckDuckGo.",
                          "api-dashboard.search.brave.com → API Keys (free tier). Empty = DuckDuckGo.")),
              Field("searxng_url", ("Eigenes SearXNG", "Own SearXNG"), "url", advanced=True,
                    placeholder="http://searxng.local:8080")), test=_t_web),
    Skill("mail", ("E-Mail", "E-mail"), "mail",
          ("Mails lesen, sortieren, Anhänge an Paperless – Proton Mail Bridge oder jedes IMAP-Postfach.",
           "Read and sort mail, attachments to Paperless – Proton Mail Bridge or any IMAP mailbox."), ("mail",),
          "mail", (
              Field("username", ("Benutzer", "User"), required=True, placeholder="name@proton.me",
                    help=("E-Mail-Adresse bzw. Bridge-Benutzername.", "E-mail address or Bridge user name.")),
              Field("password", ("Passwort", "Password"), "password", required=True,
                    help=("Bei Proton: das Bridge-Passwort aus der Bridge-App, nicht dein Proton-Passwort.",
                          "With Proton: the Bridge password from the Bridge app, not your Proton password.")),
              Field("host", ("IMAP-Server", "IMAP server"), placeholder="127.0.0.1",
                    help=("Proton Mail Bridge: 127.0.0.1", "Proton Mail Bridge: 127.0.0.1")),
              Field("port", ("IMAP-Port", "IMAP port"), "number", placeholder="1143"),
              Field("security", ("Verschlüsselung", "Encryption"), "select", options=("starttls", "ssl", "none")),
              Field("send_enabled", ("Mails senden erlauben", "Allow sending mail"), "bool",
                    help=("Jede Mail zeigt Orbwise vor dem Senden zur Bestätigung.",
                          "Orbwise shows every mail for confirmation before sending.")),
              Field("smtp_host", ("SMTP-Server", "SMTP server"), advanced=True, help=("Leer = wie IMAP.",
                                                                                     "Empty = same as IMAP.")),
              Field("smtp_port", ("SMTP-Port", "SMTP port"), "number", advanced=True, placeholder="1025"),
              Field("smtp_security", ("SMTP-Verschlüsselung", "SMTP encryption"), "select", advanced=True,
                    options=("starttls", "ssl", "none")),
              Field("from_address", ("Absender", "Sender"), advanced=True, help=("Leer = Benutzer.", "Empty = user.")),
              Field("archive_folder", ("Archiv-Ordner", "Archive folder"), advanced=True, placeholder="Archive"),
          ), test=_t_mail),
    Skill("paperless", ("Paperless-ngx", "Paperless-ngx"), "file-text",
          ("Dokumente suchen, Fragen dazu beantworten, sortieren und ablegen.",
           "Search documents, answer questions about them, sort and file them."), ("paperless",), "paperless", (
              Field("url", ("Adresse", "Address"), "url", required=True, placeholder="http://paperless.local:8000"),
              Field("token", ("API-Token", "API token"), "password", required=True,
                    help=("In Paperless: Profil (oben rechts) → API-Auth-Token.",
                          "In Paperless: profile (top right) → API auth token.")), VERIFY), test=_t_paperless),
    Skill("homeassistant", ("Home Assistant", "Home Assistant"), "home",
          ("Licht, Heizung, Rollos und Sensoren steuern.", "Control lights, heating, covers and sensors."),
          ("homeassistant",), "homeassistant", (
              Field("url", ("Adresse", "Address"), "url", required=True, placeholder="http://homeassistant.local:8123"),
              Field("token", ("Zugriffstoken", "Access token"), "password", required=True,
                    help=("Profil → Sicherheit → Langlebige Zugriffstokens → Token erstellen.",
                          "Profile → Security → Long-lived access tokens → Create token.")), VERIFY), test=_t_ha),
    Skill("portainer", ("Portainer (Docker)", "Portainer (Docker)"), "box",
          ("Container und Stacks ansehen, aktualisieren, neu starten, Apps installieren.",
           "View, update and restart containers and stacks, install apps."), ("portainer",), "portainer", (
              Field("url", ("Adresse", "Address"), "url", required=True, placeholder="https://portainer.local:9443"),
              Field("token", ("Access-Token", "Access token"), "password", required=True,
                    help=("Konto (oben rechts) → Access tokens → Add access token.",
                          "Account (top right) → Access tokens → Add access token.")),
              Field("environment", ("Umgebung", "Environment"), help=("Leer = die erste.", "Empty = the first one.")),
              VERIFY), test=_t_portainer),
    Skill("calendar", ("Kalender", "Calendar"), "calendar",
          ("Termine abfragen, freie Zeit finden, Termine anlegen – iCloud oder jeder CalDAV-Server.",
           "Look up events, find free time, create events – iCloud or any CalDAV server."), ("calendar_tools",),
          "calendar", (
              Field("url", ("CalDAV-Adresse", "CalDAV address"), "url", required=True,
                    placeholder="https://caldav.icloud.com",
                    help=("iCloud: https://caldav.icloud.com", "iCloud: https://caldav.icloud.com")),
              Field("username", ("Benutzer", "User"), required=True),
              Field("password", ("Passwort", "Password"), "password", required=True,
                    help=("iCloud: ein app-spezifisches Passwort (appleid.apple.com → Anmeldung und Sicherheit).",
                          "iCloud: an app-specific password (appleid.apple.com → Sign-In and Security).")),
              Field("default_calendar", ("Standardkalender", "Default calendar"), advanced=True,
                    help=("Für neue Termine; leer = der erste.", "For new events; empty = the first one.")),
          ), test=_t_calendar),
    Skill("trilium", ("Trilium", "Trilium"), "notebook",
          ("Notizen suchen, lesen und anlegen.", "Search, read and create notes."), ("trilium",), "trilium", (
              Field("url", ("Adresse", "Address"), "url", required=True, placeholder="http://trilium.local:8080"),
              Field("token", ("ETAPI-Token", "ETAPI token"), "password", required=True,
                    help=("Optionen → ETAPI → Neues ETAPI-Token erstellen.", "Options → ETAPI → Create new ETAPI token.")),
              VERIFY), test=_t_trilium),
    Skill("obsidian", ("Obsidian", "Obsidian"), "notebook",
          ("Notizen im Vault durchsuchen, lesen und anlegen.", "Search, read and create notes in the vault."),
          ("obsidian",), "obsidian", (
              Field("vault", ("Vault-Ordner", "Vault folder"), "path", required=True, placeholder="~/Obsidian"),
              Field("inbox", ("Ordner für neue Notizen", "Folder for new notes"), advanced=True, placeholder="Inbox"),
          ), test=_t_obsidian),
    Skill("telegram", ("Telegram", "Telegram"), "send",
          ("Vom Handy schreiben und sprechen, Erinnerungen und Dateien aufs Handy.",
           "Write and talk from your phone, reminders and files to your phone."), ("telegram_tools",), "telegram", (
              Field("token", ("Bot-Token", "Bot token"), "password", required=True,
                    help=("In Telegram @BotFather schreiben → /newbot → Token kopieren.",
                          "Message @BotFather in Telegram → /newbot → copy the token.")),
              Field("chat_id", ("Chat-ID", "Chat ID"), "number", required=True,
                    help=("Schreib deinem Bot „/start“ und klick auf „Verbindung testen“ – Orbwise trägt sie ein.",
                          "Send your bot “/start” and click “Test connection” – Orbwise fills it in.")),
          ), test=_t_telegram, test_fills=("chat_id",),
          hint=("Aktiv nach einem Neustart von Orbwise.", "Active after restarting Orbwise.")),
    Skill("github", ("GitHub", "GitHub"), "github",
          ("Im Coding-Modus Repos klonen und Branches pushen.", "Clone repositories and push branches in coding "
           "mode."), ("github_tools",), "github", (
              Field("token", ("Token", "Token"), "password",
                    help=("Leer = Anmeldung der GitHub-CLI (gh auth login) wird genutzt.",
                          "Empty = the GitHub CLI login (gh auth login) is used.")),
              Field("clone_dir", ("Ordner für Klone", "Folder for clones"), "path", advanced=True,
                    placeholder="~/Projekte"),
          ), test=_t_github),
    Skill("ssh", ("SSH (NAS, Server)", "SSH (NAS, servers)"), "terminal",
          ("Befehle auf anderen Rechnern ausführen. Passwörter fragt Orbwise jedes Mal im Dashboard.",
           "Run commands on other machines. Orbwise asks for passwords in the dashboard every time."), ("ssh",),
          "ssh", (Field("hosts", ("Kurznamen", "Short names"), "hosts",
                        help=("Optional, z. B. „nas“ → 192.168.1.10. Andere Adressen gehen auch direkt.",
                              "Optional, e.g. “nas” → 192.168.1.10. Other addresses work directly too.")),),
          test=_t_ssh),
    Skill("weather", ("Wetter", "Weather"), "cloud",
          ("Wetter und Vorhersage (Open-Meteo, ohne Konto).", "Weather and forecast (Open-Meteo, no account)."),
          ("weather",), "weather", (
              Field("location", ("Standardort", "Default location"), placeholder="Freiburg"),), test=_t_weather),
    # ------------------------------------------------ eingebaut: nur Schalter
    Skill("routines", ("Routinen", "Routines"), "repeat",
          ("Aufgaben zu festen Zeiten („werktags um 8 Linux-News“).", "Tasks at set times (“weekdays at 8, Linux "
           "news”)."), ("routine_tools",), kind="builtin"),
    Skill("reminders", ("Erinnerungen und Timer", "Reminders and timers"), "bell",
          ("„Erinnere mich in 20 Minuten …“", "“Remind me in 20 minutes …”"), ("reminder_tools",), kind="builtin"),
    Skill("packages", ("Pakete und Updates", "Packages and updates"), "package",
          ("Systemupdate, Pakete suchen, installieren und entfernen.", "System update, search, install and remove "
           "packages."), ("packages",), kind="builtin"),
    Skill("apps", ("Programme starten", "Start programs"), "app",
          ("Installierte Programme öffnen.", "Open installed programs."), ("apps",), kind="builtin"),
    Skill("vision", ("Bildschirm und Bilder", "Screen and images"), "eye",
          ("Fragen zum Bildschirm und zu Fotos – braucht ein Modell mit Bildmodul.",
           "Questions about the screen and photos – needs a model with an image module."), ("vision",),
          kind="builtin"),
    Skill("briefing", ("Morgen-Briefing", "Morning briefing"), "sun",
          ("„Guten Morgen“ mit Wetter, Terminen, Erinnerungen …", "“Good morning” with weather, events, reminders …"),
          ("briefing",), kind="builtin", hint=("Inhalt im Planer einstellen.", "Choose the content in the planner.")),
    Skill("image", ("Bilder erzeugen", "Generate images"), "image",
          ("Lokale Bilder mit Qwen-Image.", "Local images with Qwen-Image."), ("image_tools",), kind="builtin",
          hint=("Einrichten im Bildmodus (oben links „Bild“).", "Set up in image mode (“Image” at the top left).")),
    Skill("sysadmin", ("Erweiterte Systemwerkzeuge", "Extended system tools"), "cpu",
          ("Eigene Werkzeuge für Prozesse, Dienste, Logs, Netzwerk und Speicherplatz. Aus: das Modell nutzt die "
           "Shell (spart Platz im Prompt).", "Dedicated tools for processes, services, logs, network and disk "
           "space. Off: the model uses the shell (saves prompt space)."), ("sysadmin",), kind="builtin",
          default_on=False, extra=True),
)
BY_ID = {s.id: s for s in SKILLS}
SECTIONS = sorted({s.section for s in SKILLS if s.section})
# Umgebungsvariablen, die Geheimnisse ersetzen können (siehe die Accessoren in config.py)
SECRET_ENV = {"tools.brave_api_key": "BRAVE_API_KEY", "paperless.token": "PAPERLESS_TOKEN",
              "homeassistant.token": "HA_TOKEN", "portainer.token": "PORTAINER_TOKEN",
              "calendar.password": "CALENDAR_PASSWORD", "trilium.token": "TRILIUM_TOKEN",
              "mail.password": "MAIL_PASSWORD", "telegram.token": "TELEGRAM_TOKEN", "github.token": "GITHUB_TOKEN"}


def state_path(cfg) -> Path:
    return Path(cfg.memory.dir).expanduser().parent / "state.json"


def _stored(path: Path) -> dict:
    data = state.load(path).get("skills")
    return data if isinstance(data, dict) else {}


def _base(cfg) -> dict:
    """Die Abschnitte, wie sie aus config.yaml kamen – Grundlage jedes apply (sonst summierten sich Änderungen)."""
    base = getattr(cfg, "_skills_base", None)
    if base is None:
        base = {sec: copy.deepcopy(getattr(cfg, sec)) for sec in SECTIONS}
        cfg._skills_base = base
    return base


def _with_values(section_obj, values: dict):
    data = section_obj.model_dump()
    data.update(values)
    return type(section_obj).model_validate(data)


def _section_values(skill: Skill, entry: dict, secrets: bool = True) -> dict:
    vals = {}
    stored = entry.get("values") if isinstance(entry.get("values"), dict) else {}
    for f in skill.fields:
        if f.secret:
            v = keystore.get(f"{skill.section}.{f.key}") if secrets else ""
            if v:
                vals[f.key] = v
        elif f.key in stored:
            vals[f.key] = stored[f.key]
    return vals


def apply(cfg, path: Path | None = None) -> None:
    """Gespeicherte Skill-Werte, Geheimnisse und Schalter auf das (laufende) Config-Objekt legen."""
    base = _base(cfg)
    stored = _stored(path or state_path(cfg))
    sections = {sec: copy.deepcopy(obj) for sec, obj in base.items()}
    for skill in SKILLS:
        if not skill.section:
            continue
        vals = _section_values(skill, stored.get(skill.id) or {})
        if vals:
            try:
                sections[skill.section] = _with_values(sections[skill.section], vals)
            except ValueError as e:  # kaputte Werte nicht über die Config legen
                log.warning("Skill %s: gespeicherte Werte ungültig (%s)", skill.id, e)
    tools = sections.get("tools") or copy.deepcopy(cfg.tools)
    disabled, extra = list(base["tools"].disabled), list(base["tools"].extra)
    for skill in SKILLS:
        entry = stored.get(skill.id) or {}
        if "enabled" not in entry:
            continue
        on = bool(entry["enabled"])
        for g in skill.groups:
            if skill.extra:
                if on and g not in extra:
                    extra.append(g)
                elif not on and g in extra:
                    extra.remove(g)
            elif not on and g not in disabled:
                disabled.append(g)
            elif on and g in disabled:
                disabled.remove(g)
    tools.disabled, tools.extra = disabled, extra
    sections["tools"] = tools
    for sec, obj in sections.items():
        setattr(cfg, sec, obj)


# ---------------------------------------------------------------- Status für Oberfläche und CLI
def _secret_source(cfg, skill: Skill, f: Field) -> str:
    """Woher das Geheimnis kommt: keyring | file | config | env | gh – "" = fehlt."""
    key = f"{skill.section}.{f.key}"
    if keystore.has(key):
        return keystore.backend()
    if getattr(_base(cfg).get(skill.section), f.key, ""):
        return "config"
    if key in SECRET_ENV and env(SECRET_ENV[key]):
        return "env"
    if key == "github.token":
        from . import github
        return github.token_info(cfg)[1]
    return ""


def _is_on(cfg, skill: Skill) -> bool:
    groups = set(skill.groups)
    if groups & set(cfg.tools.disabled):
        return False
    if skill.extra:
        return bool(groups & set(cfg.tools.extra))
    return True


def _missing(cfg, skill: Skill) -> list[str]:
    sec = getattr(cfg, skill.section) if skill.section else None
    out = []
    for f in skill.fields:
        if not f.required:
            continue
        if f.secret:
            if not (getattr(sec, f.key, "") or _secret_source(cfg, skill, f)):
                out.append(f.key)
        elif getattr(sec, f.key, None) in (None, "", 0):
            out.append(f.key)
    return out


def _value(v: Any) -> Any:
    if isinstance(v, Path):
        return str(v)
    if isinstance(v, dict):
        return {k: (x.model_dump() if hasattr(x, "model_dump") else x) for k, x in v.items()}
    return v


def status(cfg) -> dict:
    """Katalog mit Zustand – Geheimnisse nur als „gesetzt“ und woher, nie der Wert."""
    items = []
    for skill in SKILLS:
        sec = getattr(cfg, skill.section) if skill.section else None
        fields = []
        for f in skill.fields:
            row = {"key": f.key, "label": T(*f.label), "type": f.type, "required": f.required,
                   "help": T(*f.help) if f.help[0] else "", "placeholder": f.placeholder, "advanced": f.advanced,
                   "options": list(f.options)}
            if f.secret:
                row["source"] = _secret_source(cfg, skill, f)
                row["set"] = bool(row["source"])
            else:
                row["value"] = _value(getattr(sec, f.key, None))
            fields.append(row)
        missing = _missing(cfg, skill)
        configured = skill.kind == "builtin" or not missing
        items.append({"id": skill.id, "name": T(*skill.name), "icon": skill.icon, "desc": T(*skill.desc),
                      "kind": skill.kind, "groups": list(skill.groups), "fields": fields, "missing": missing,
                      "configured": configured, "on": _is_on(cfg, skill),
                      "active": configured and _is_on(cfg, skill), "hint": T(*skill.hint) if skill.hint[0] else ""})
    return {"skills": items, "backend": keystore.backend(), "secrets_file": str(keystore.file_path())}


# ---------------------------------------------------------------- Speichern, Entfernen, Testen
def _clean_values(skill: Skill, values: dict) -> tuple[dict, dict]:
    """Formularwerte → (ohne Geheimnisse, Geheimnisse). Leere Geheimnisse = unverändert."""
    plain, secrets = {}, {}
    known = {f.key: f for f in skill.fields}
    for key, v in (values or {}).items():
        f = known.get(key)
        if f is None:
            continue
        if f.secret:
            if isinstance(v, str) and v.strip():
                secrets[key] = v.strip()
            continue
        if f.options and v not in ("", None) and v not in f.options:
            raise ValueError(T(f"{T(*f.label)}: „{v}“ ist keine gültige Auswahl ({', '.join(f.options)}).",
                               f"{T(*f.label)}: “{v}” is not a valid choice ({', '.join(f.options)})."))
        if f.type == "number":
            v = int(v) if str(v).strip().lstrip("-").isdigit() else (None if v in ("", None) else v)
        elif f.type == "bool":
            v = bool(v) if not isinstance(v, str) else v.lower() in ("1", "true", "ja", "yes", "on")
        elif isinstance(v, str):
            v = v.strip()
        if v is None:
            continue
        plain[key] = v
    return plain, secrets


def candidate(cfg, skill_id: str, values: dict | None):
    """Config-Kopie mit ungespeicherten Formularwerten (fehlende Geheimnisse aus dem Speicher) – für den Test."""
    skill = BY_ID[skill_id]
    cand = cfg.model_copy(deep=True)
    if skill.section and values:
        plain, secrets = _clean_values(skill, values)
        setattr(cand, skill.section, _with_values(getattr(cand, skill.section), {**plain, **secrets}))
    return cand


def save(cfg, skill_id: str, enabled: bool | None = None, values: dict | None = None,
         path: Path | None = None) -> None:
    """Skill speichern und sofort anwenden. ValueError bei ungültigen Werten."""
    skill = BY_ID.get(skill_id)
    if skill is None:
        raise ValueError(T(f"Unbekannter Skill: {skill_id}", f"Unknown skill: {skill_id}"))
    path = path or state_path(cfg)
    plain, secrets = _clean_values(skill, values or {})
    if skill.section and (plain or secrets):
        _with_values(getattr(cfg, skill.section), {**plain, **secrets})  # prüfen, bevor etwas gespeichert wird

    def change(data: dict) -> None:
        skills = data.setdefault("skills", {})
        entry = skills.setdefault(skill_id, {})
        if values is not None and skill.section:
            entry["values"] = {**(entry.get("values") or {}), **plain}
        if enabled is not None:
            entry["enabled"] = bool(enabled)
        if not entry:
            skills.pop(skill_id, None)
    state.update(path, change)
    for key, v in secrets.items():
        keystore.set(f"{skill.section}.{key}", v)
    apply(cfg, path)


def remove(cfg, skill_id: str, path: Path | None = None) -> None:
    """Gespeicherte Daten und Geheimnisse eines Skills löschen (Werte aus config.yaml bleiben)."""
    skill = BY_ID[skill_id]
    path = path or state_path(cfg)
    state.update(path, lambda data: (data.get("skills") or {}).pop(skill_id, None))
    for f in skill.fields:
        if f.secret:
            keystore.delete(f"{skill.section}.{f.key}")
    apply(cfg, path)


async def test(cfg, skill_id: str, values: dict | None = None) -> dict:
    """Verbindung mit (ungespeicherten) Werten prüfen → {ok, message, details}."""
    skill = BY_ID.get(skill_id)
    if skill is None or skill.test is None:
        return {"ok": None, "message": T("Für diesen Skill gibt es keinen Test.", "There is no test for this skill.")}
    cand = candidate(cfg, skill_id, values)
    missing = [k for k in _missing(cand, skill) if k not in skill.test_fills]
    if missing:
        labels = ", ".join(T(*f.label) for f in skill.fields if f.key in missing)
        return {"ok": False, "message": T(f"Es fehlt noch: {labels}", f"Still missing: {labels}")}
    try:
        st = await skill.test(cand)
    except Exception as e:  # noqa: BLE001 – jede Ausnahme wird zur verständlichen Meldung
        st = {"online": False, "error": str(e)}
    ok = st.get("online")
    return {"ok": ok, "message": _describe(skill, st), "details": {k: v for k, v in st.items()
                                                                   if k in ("chat_id", "login", "place", "bot")}}


def _describe(skill: Skill, st: dict) -> str:
    if st.get("online") is False:
        err = str(st.get("error") or T("nicht erreichbar", "not reachable"))
        err = re.sub(r"\s*(Prüfen mit|Check with) `orbwise doctor`.*$", "", err, flags=re.S)  # Hinweis fürs Modell
        return T("Keine Verbindung: ", "No connection: ") + err[:300]
    if st.get("online") is None:
        return T("Eingerichtet (ohne Verbindungstest).", "Set up (no connection test).")
    bits = []
    for key, de, en in (("version", "Version", "version"), ("count", "Einträge", "items"),
                        ("entities", "Geräte", "entities"), ("folders", "Ordner", "folders"),
                        ("login", "angemeldet als", "signed in as"), ("place", "Ort", "place"),
                        ("bot", "Bot", "bot"), ("chat_id", "Chat-ID", "chat ID")):
        if st.get(key) not in (None, ""):
            bits.append(f"{T(de, en)} {st[key]}")
    if st.get("calendars"):
        bits.append(T("Kalender: ", "calendars: ") + ", ".join(st["calendars"][:4]))
    return T("Verbindung klappt", "Connection works") + (" – " + ", ".join(bits) if bits else ".")


# ---------------------------------------------------------------- Umzug: Klartext aus config.yaml in den Schlüsselbund
_KEY_LINE = re.compile(r"^(\s+)(\w+):\s*(.*?)\s*$")


def _plain_secrets(cfg) -> list[tuple[str, str, str]]:
    """(Abschnitt, Feld, Wert) der Geheimnisse, die in config.yaml stehen (Werte vor apply)."""
    base = _base(cfg)
    out = []
    for skill in SKILLS:
        for f in skill.fields:
            if f.secret:
                v = getattr(base.get(skill.section), f.key, "")
                if isinstance(v, str) and v:
                    out.append((skill.section, f.key, v))
    return out


def plaintext_left(cfg) -> list[str]:
    """Für doctor/Oberfläche: welche Geheimnisse noch im Klartext in config.yaml stehen."""
    return [f"{sec}.{key}" for sec, key, _ in _plain_secrets(cfg)]


def migrate_plaintext(cfg, config_file: Path) -> list[str]:
    """Geheimnisse aus config.yaml in den Schlüsselbund ziehen und dort leeren (Kommentare bleiben).

    Nur, wenn jeder Wert sicher gespeichert ist; vorher config.yaml.bak. → umgezogene Schlüssel."""
    found = _plain_secrets(cfg)
    if not found or not config_file.is_file():
        return []
    text = config_file.read_text(encoding="utf-8")
    lines = text.splitlines(keepends=True)
    targets = {(sec, key): v for sec, key, v in found}
    hits: dict[int, tuple[str, str]] = {}
    section = ""
    for i, line in enumerate(lines):
        top = re.match(r"^(\w+):", line)
        if top:
            section = top.group(1)
            continue
        m = _KEY_LINE.match(line.rstrip("\n"))
        if not m or (section, m.group(2)) not in targets:
            continue
        raw = m.group(3).split(" #", 1)[0].strip().strip("'\"")
        if raw == targets[(section, m.group(2))]:
            hits[i] = (section, m.group(2))
    if not hits:
        return []
    for sec, key in hits.values():
        if not keystore.set(f"{sec}.{key}", targets[(sec, key)]):
            return []
        if keystore.get(f"{sec}.{key}") != targets[(sec, key)]:
            log.warning("Umzug abgebrochen: %s.%s ließ sich nicht sicher speichern", sec, key)
            return []
    shutil.copy2(config_file, config_file.with_name(config_file.name + ".bak"))
    note = T("im Schlüsselbund (Einstellungen → Skills)", "in the keyring (Settings → Skills)") \
        if keystore.backend() == "keyring" else T("in secrets.json (Einstellungen → Skills)",
                                                  "in secrets.json (Settings → Skills)")
    for i, (sec, key) in hits.items():
        indent = _KEY_LINE.match(lines[i].rstrip("\n")).group(1)
        nl = "\n" if lines[i].endswith("\n") else ""
        lines[i] = f'{indent}{key}: ""   # {note}{nl}'
    config_file.write_text("".join(lines), encoding="utf-8")
    for sec, key in hits.values():  # die Basis kennt den Wert nicht mehr – er kommt jetzt aus dem Speicher
        base = _base(cfg)[sec]
        setattr(base, key, "")
    moved = [f"{sec}.{key}" for sec, key in hits.values()]
    log.info("Zugangsdaten aus config.yaml in den sicheren Speicher übernommen: %s", ", ".join(moved))
    return moved
