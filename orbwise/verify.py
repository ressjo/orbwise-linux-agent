"""Prüf-Schranke im Coding-Modus: Bevor das Modell nach Änderungen „fertig“ meldet, führt Orbwise die schnellen
Prüfungen des Projekts aus (Tests, Linter, Typprüfung). Rot geht an das Modell zurück, bis es grün ist oder die
Runden aufgebraucht sind – dann steht ehrlich unter der Antwort, was noch rot ist.

Welche Prüfungen: `check:` in `.orbwise.yaml` im Projekt (ein Befehl oder eine Liste), sonst `coding.verify_command`,
sonst erkannt (eine schnelle Teilmenge der Code-Check-Prüfungen aus review_checks.py – ohne Installieren und ohne
Sicherheits-Scanner). Das Skript läuft über den normalen Shell-Weg des Agenten (Sandbox, Käfig, Bestätigung) und gibt
je Prüfung einen Abschnitt im Format von review_checks aus; review_checks.parse liest ihn wieder.

check_syntax() prüft eine einzelne Datei direkt nach edit/write – nur Parsen, nichts wird ausgeführt."""

from __future__ import annotations

import ast
import json
import re
import shlex
import shutil
import subprocess
import tomllib
from pathlib import Path

import yaml

from . import review_checks

TAIL = 1500  # Zeichen Ausgabe je Prüfung (das Ende ist meist das Wichtige)


# ---------------------------------------------------------------- welche Prüfungen
def project_checks(project: str | Path) -> list[tuple[str, str]] | None:
    """`check:` aus `.orbwise.yaml` → [(Name, Befehl)] oder None (nicht festgelegt)."""
    p = Path(project) / ".orbwise.yaml"
    try:
        data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return None
    check = data.get("check") if isinstance(data, dict) else None
    if isinstance(check, str) and check.strip():
        return [("check", check.strip())]
    if isinstance(check, list):
        items = [str(c).strip() for c in check if str(c).strip()]
        return [(f"check{i + 1}" if len(items) > 1 else "check", c) for i, c in enumerate(items)] or None
    if isinstance(check, dict):
        return [(str(k), str(v).strip()) for k, v in check.items() if str(v).strip()] or None
    return None


def _scripts(project: Path) -> dict:
    try:
        return (json.loads((project / "package.json").read_text(encoding="utf-8")) or {}).get("scripts") or {}
    except (OSError, ValueError, AttributeError):
        return {}


def detect(project: str | Path, changed: list[str]) -> list[tuple[str, str]]:
    """Schnelle Prüfungen nach den Dateien im Projekt. Werkzeuge, die dort fehlen, überspringt das Skript selbst."""
    root = Path(project)
    out: list[tuple[str, str]] = []
    py = [f for f in changed if f.endswith(".py")]
    if py:
        out.append(("ruff", "command -v ruff >/dev/null && ruff check --no-cache --output-format concise "
                            + " ".join(shlex.quote(f) for f in py[:200])))
    if (root / "tests").is_dir() or (root / "test").is_dir() or any(root.glob("test_*.py")) or any(root.glob("*/test_*.py")):
        if any(root.rglob("*.py")):
            out.append(("pytest", "python3 -c 'import pytest' 2>/dev/null && python3 -m pytest -q -x --maxfail=5 "
                                  "-p no:cacheprovider"))
    scripts = _scripts(root)
    if scripts and (root / "node_modules").is_dir():
        for key, name in (("lint", "npm-lint"), ("typecheck", "npm-typecheck")):
            if scripts.get(key):
                out.append((name, f"npm run -s {key}"))
        if not scripts.get("typecheck") and (root / "tsconfig.json").is_file() and (root / "node_modules/.bin/tsc").exists():
            out.append(("tsc", "node_modules/.bin/tsc --noEmit"))
        test = str(scripts.get("test") or "")
        if test and "no test specified" not in test:
            out.append(("npm-test", "npm test --silent"))
    if (root / "go.mod").is_file():
        out.append(("go-test", "command -v go >/dev/null && go vet ./... && go test ./..."))
    if (root / "Cargo.toml").is_file():
        out.append(("cargo-check", "command -v cargo >/dev/null && cargo check --quiet"))
    makefile = root / "Makefile"
    if makefile.is_file() and not scripts:
        text = makefile.read_text(encoding="utf-8", errors="replace")
        for target in ("check", "test"):
            if re.search(rf"^{target}:", text, re.M):
                out.append((f"make-{target}", f"make -s {target}"))
                break
    return out


def checks_for(cfg, project: str | Path, changed: list[str]) -> list[tuple[str, str]]:
    own = project_checks(project)
    if own:
        return own
    if (cfg.coding.verify_command or "").strip():
        return [("check", cfg.coding.verify_command.strip())]
    return detect(project, changed)


def script(checks: list[tuple[str, str]], timeout: int) -> str:
    """Kurzes bash-Skript (steht so auch im Bestätigungsfenster): je Prüfung ein Abschnitt @@CHECK/@@CMD/@@RC.
    Ein mit `command -v … &&` eingeleiteter Befehl, dessen Werkzeug fehlt, zählt als übersprungen (Rückgabe 127)."""
    parts = [f"c(){{ n=$1; shift; echo \"@@CHECK $n\"; echo \"@@CMD $*\"; timeout {int(timeout)} bash -c \"$*\" 2>&1 "
             f"| tail -c {TAIL}; rc=${{PIPESTATUS[0]}}; echo; echo \"@@RC $rc\"; }}"]
    for name, cmd in checks:
        guard = re.match(r"^(command -v \S+ >/dev/null|python3 -c 'import \w+' 2>/dev/null) && ", cmd)
        if guard:  # Werkzeug fehlt → Abschnitt als übersprungen
            parts.append(f"if {guard.group(1)}; then c {shlex.quote(name)} {shlex.quote(cmd[guard.end():])}; "
                         f"else echo '@@CHECK {name}'; echo '@@SKIP fehlt'; fi")
        else:
            parts.append(f"c {shlex.quote(name)} {shlex.quote(cmd)}")
    parts.append("echo @@END")
    return "; ".join(parts)


def parse(output: str) -> list[dict]:
    return review_checks.parse(output)


def summary(results: list[dict], en: bool = False) -> str:
    """Einzeiler für unter die Antwort."""
    ran = [r for r in results if r["status"] != "skip"]
    bad = [r for r in ran if r["status"] == "fail"]
    if not ran:
        return ""
    if not bad:
        return ("✓ Checked: " if en else "✓ Geprüft: ") + ", ".join(r["name"] for r in ran)
    names = ", ".join(r["name"] + (f" ({_short(r['output'])})" if _short(r["output"]) else "") for r in bad)
    return (f"⚠ Checks still failing: {names} – details in the activity panel." if en
            else f"⚠ Prüfung weiterhin rot: {names} – Details im Aktivitäts-Panel.")


def _short(output: str) -> str:
    """„2 failed, 10 passed“ o. Ä. aus der letzten passenden Zeile."""
    for line in reversed(output.strip().splitlines()):
        if re.search(r"\b(\d+ (failed|errors?|passed)|Found \d+ errors?|error(s)?\b)", line, re.I):
            return line.strip()[:80]
    return ""


def nudge(results: list[dict], en: bool = False) -> str:
    """Hinweis an das Modell, wenn etwas rot ist."""
    bad = [r for r in results if r["status"] == "fail"]
    body = "\n\n".join(f"### {r['name']}: {r['cmd']}\n{r['output'][-TAIL:]}" for r in bad)
    if en:
        return ("(Orbwise checked your changes automatically – these checks fail:\n\n" + body
                + "\n\nFix the cause in the code. Don't weaken or delete tests just to make them pass. "
                  "Then finish with a short answer.)")
    return ("(Orbwise hat deine Änderungen automatisch geprüft – diese Prüfungen sind rot:\n\n" + body
            + "\n\nBehebe die Ursache im Code. Schwäche oder lösche keine Tests, nur damit sie grün werden. "
              "Danach kurz antworten.)")


# ---------------------------------------------------------------- Syntax direkt nach dem Schreiben
def check_syntax(path: Path, text: str) -> str:
    """Leerer Text, wenn alles parst – sonst eine kurze Meldung. Führt nichts aus."""
    suffix = path.suffix.lower()
    try:
        if suffix == ".py":
            ast.parse(text, filename=str(path))
        elif suffix == ".json":
            json.loads(text)
        elif suffix in (".yaml", ".yml"):
            list(yaml.safe_load_all(text))
        elif suffix == ".toml":
            tomllib.loads(text)
        elif suffix in (".sh", ".bash"):
            return _external(["bash", "-n", str(path)], "bash")
        elif suffix in (".js", ".mjs", ".cjs"):
            return _external(["node", "--check", str(path)], "node")
    except SyntaxError as e:
        return f"SyntaxError Zeile {e.lineno}: {e.msg}"
    except json.JSONDecodeError as e:
        return f"JSON-Fehler Zeile {e.lineno}: {e.msg}"
    except yaml.YAMLError as e:
        mark = getattr(e, "problem_mark", None)
        return f"YAML-Fehler{f' Zeile {mark.line + 1}' if mark else ''}: {getattr(e, 'problem', None) or e}"
    except tomllib.TOMLDecodeError as e:
        return f"TOML-Fehler: {e}"
    return ""


def _external(argv: list[str], tool: str) -> str:
    if not shutil.which(tool):
        return ""
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    if r.returncode == 0:
        return ""
    msg = (r.stderr or r.stdout).strip().splitlines()
    return "Syntaxfehler: " + " ".join(msg[:3])[:300]
