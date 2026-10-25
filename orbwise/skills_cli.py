"""Skills in der Konsole: `orbwise skills` (Übersicht), `orbwise skills setup` (Auswahl und Daten eingeben – auch im
Installer), `orbwise skills enable|disable <id>`. Gespeichert wird wie in der Oberfläche (skills.save): Werte in
state.json, Geheimnisse im Schlüsselbund."""

from __future__ import annotations

import asyncio
import getpass
from collections.abc import Callable

from . import keystore, skills
from .lang import T

Ask = Callable[[str], str]


def _yes(answer: str, default: bool = False) -> bool:
    a = answer.strip().lower()
    return default if not a else a in ("j", "ja", "y", "yes")


def overview(cfg, out=print) -> None:
    st = skills.status(cfg)
    where = T("Schlüsselbund", "keyring") if st["backend"] == "keyring" else st["secrets_file"] + " (0600)"
    out(T(f"Zugangsdaten: {where}", f"Credentials: {where}"))
    for kind, title in (("connect", T("Verbindungen", "Connections")), ("builtin", T("Eingebaut", "Built in"))):
        out(f"\n{title}:")
        for s in (x for x in st["skills"] if x["kind"] == kind):
            mark = "✔" if s["active"] else ("–" if s["configured"] else "·")
            state = (T("aktiv", "active") if s["active"] else T("aus", "off") if s["configured"]
                     else T("nicht eingerichtet", "not set up"))
            out(f"  {mark} {s['id']:<14} {s['name']:<30} {state}")
    out("\n" + T("Einrichten: orbwise skills setup [name] · auch unter Einstellungen → Skills",
                 "Set up: orbwise skills setup [name] · also under Settings → Skills"))


def choose(cfg, ask: Ask = input, out=print) -> list[str]:
    connect = [s for s in skills.SKILLS if s.kind == "connect"]
    st = {s["id"]: s for s in skills.status(cfg)["skills"]}
    out(T("Welche Skills möchtest du einrichten?", "Which skills do you want to set up?"))
    for i, s in enumerate(connect, 1):
        mark = "✔" if st[s.id]["configured"] and s.fields and any(f.required for f in s.fields) else " "
        out(f"  {i:2}) {mark} {T(*s.name):<22} {T(*s.desc)}")
    answer = ask(T("Auswahl (z. B. 2 5 7 · Enter = keine · a = alle): ",
                   "Choice (e.g. 2 5 7 · Enter = none · a = all): ")).strip().lower()
    if answer in ("a", "alle", "all"):
        return [s.id for s in connect]
    picked = []
    for part in answer.replace(",", " ").split():
        if part.isdigit() and 1 <= int(part) <= len(connect):
            picked.append(connect[int(part) - 1].id)
        elif part in skills.BY_ID:
            picked.append(part)
    return list(dict.fromkeys(picked))


def _ask_field(f: skills.Field, current, is_set: bool, ask: Ask, secret: Ask, out) -> object:
    label = T(*f.label)
    if f.help[0]:
        out(f"   {T(*f.help)}")
    if f.secret:
        hint = T(" (Enter = behalten)", " (Enter = keep)") if is_set else ""
        return secret(f"   {label}{hint}: ").strip()
    if f.type == "bool":
        default = bool(current)
        return _yes(ask(f"   {label} [{'J/n' if default else 'j/N'}]: "), default)
    if f.type == "select":
        value = ask(f"   {label} ({' | '.join(f.options)}) [{current}]: ").strip()
        return value or current
    if f.type == "hosts":
        hosts = dict(current or {})
        out("   " + T("Kurznamen (Enter = fertig):", "Short names (Enter = done):"))
        while True:
            name = ask("   " + T("Kurzname, z. B. nas: ", "short name, e.g. nas: ")).strip()
            if not name:
                return hosts
            host = ask("   " + T("Adresse: ", "address: ")).strip()
            user = ask("   " + T("Benutzer (optional): ", "user (optional): ")).strip()
            port = ask("   Port [22]: ").strip()
            if host:
                hosts[name] = {"host": host, "user": user, "port": int(port) if port.isdigit() else 22}
    shown = current if current not in (None, "", 0) else f.placeholder
    value = ask(f"   {label}{' *' if f.required else ''}" + (f" [{shown}]" if shown else "") + ": ").strip()
    return value if value else (current if current not in (None, 0) else "")


def setup_one(cfg, skill_id: str, ask: Ask = input, secret: Ask = getpass.getpass, out=print) -> bool:
    """Felder abfragen, testen, speichern. → gespeichert?"""
    skill = skills.BY_ID[skill_id]
    out(f"\n— {T(*skill.name)} —\n{T(*skill.desc)}")
    if skill.hint[0]:
        out(T(*skill.hint))
    values: dict = {}
    while True:
        rows = {r["key"]: r for r in next(s for s in skills.status(cfg)["skills"] if s["id"] == skill_id)["fields"]}
        for f in skill.fields:
            if f.advanced or f.key in skill.test_fills:  # z. B. Telegram-Chat-ID: ermittelt der Test
                continue
            current = values.get(f.key, rows[f.key].get("value"))
            values[f.key] = _ask_field(f, current, rows[f.key].get("set", False), ask, secret, out)
        if skill_id == "telegram":
            ask("   " + T("Schreib deinem Bot jetzt in Telegram „/start“ und drück dann Enter … ",
                          "Now send your bot “/start” in Telegram, then press Enter … "))
        if skill.test is None:
            break
        res = asyncio.run(skills.test(cfg, skill_id, values))
        chat = (res.get("details") or {}).get("chat_id")
        if chat:
            values["chat_id"] = chat
        out(("   ✔ " if res["ok"] else "   ✘ " if res["ok"] is False else "   · ") + res["message"])
        if res["ok"] is not False:
            break
        skip_keys, save_keys = (("ü", "u"), ("s",)) if T("de", "en") == "de" else (("s",), ("k",))
        again = ask("   " + T("[N]ochmal eingeben, [ü]berspringen oder trotzdem [s]peichern? ",
                              "[A]gain, [s]kip or [k]eep anyway? ")).strip().lower()[:1]
        if again in skip_keys:
            out("   " + T("Übersprungen.", "Skipped."))
            return False
        if again in save_keys:
            break
    try:
        skills.save(cfg, skill_id, enabled=True, values=values)
    except ValueError as e:
        out(f"   ✘ {e}")
        return False
    where = T("im Schlüsselbund", "in the keyring") if keystore.backend() == "keyring" else str(keystore.file_path())
    out("   " + T(f"Gespeichert – Zugangsdaten {where}.", f"Saved – credentials {where}."))
    return True


def setup(cfg, ids: list[str] | None = None, ask: Ask = input, secret: Ask = getpass.getpass, out=print) -> list[str]:
    ids = ids or choose(cfg, ask, out)
    done = [i for i in ids if i in skills.BY_ID and setup_one(cfg, i, ask, secret, out)]
    if ids:
        out("\n" + T("Später ändern: Einstellungen → Skills oder orbwise skills setup",
                     "Change later: Settings → Skills or orbwise skills setup"))
    return done
