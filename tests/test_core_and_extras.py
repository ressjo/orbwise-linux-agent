"""Kern und Zusätze: Der Kern (Rechner bedienen, Coding, Gedächtnis, Sprache, Freigaben) hängt nicht von den
Anbindungen in orbwise/extras ab – er fasst sie nur dort an, wo er sie einbindet."""

import re
from pathlib import Path

ROOT = Path(__file__).parent.parent / "orbwise"
EXTRAS = {"paperless", "mail", "calendar_tools", "portainer", "trilium", "obsidian", "homeassistant", "telegram_tools",
          "telegram"}
# die Stellen, an denen der Kern Zusätze einbindet – jede mit ihrem Grund
PLUGS = {
    "tools/registry.py": "trägt ihre Werkzeuge ein",
    "skills.py": "Einrichten und Statusprüfung je Skill",
    "cli.py": "orbwise doctor prüft jeden eingerichteten Dienst",
    "tools/briefing.py": "das Morgen-Briefing fragt Mail, Kalender und Paperless ab",
    "webapp/routes/system.py": "Statusseite",
    "webapp/telegram_link.py": "startet den Telegram-Bot",
}
IMPORT = re.compile(r"^\s*(?:from\s+(\.+[\w.]*|orbwise[\w.]*)\s+import\s+(.+)|import\s+(orbwise[\w.]*))", re.M)


def _uses_extras(text: str) -> bool:
    for module, names, plain in IMPORT.findall(text):
        target = module or plain
        if "extras" in target.split("."):
            return True
        if target.strip(".") in ("", "orbwise") and re.search(r"\bextras\b", names):
            return True
    return False


def test_core_touches_the_extras_only_where_it_plugs_them_in():
    users = {str(p.relative_to(ROOT)) for p in ROOT.rglob("*.py")
             if "extras" not in p.relative_to(ROOT).parts and _uses_extras(p.read_text(encoding="utf-8"))}
    assert users == set(PLUGS), f"neu: {sorted(users - set(PLUGS))} · nicht mehr nötig: {sorted(set(PLUGS) - users)}"


def test_extras_are_complete_and_registered():
    from orbwise.tools.registry import load_all_tools
    assert {p.stem for p in (ROOT / "extras").glob("*.py")} - {"__init__"} == EXTRAS
    for name in EXTRAS:  # keiner liegt mehr (auch) im Kern
        assert not (ROOT / "tools" / f"{name}.py").exists() and not (ROOT / f"{name}.py").exists()
    groups = {spec.group for spec in load_all_tools().values()}
    assert EXTRAS - {"telegram"} <= groups  # der Bot selbst (telegram) hat keine Werkzeuge
    tests = {p.stem.removeprefix("test_") for p in (ROOT.parent / "tests" / "extras").glob("test_*.py")}
    assert {"paperless", "mail", "calendar", "portainer", "trilium", "obsidian", "homeassistant", "telegram"} <= tests
