"""Feste Prüfungen für den Code-Check (laufen in der Sandbox): Tests, Linter und Typprüfung des Projekts, dazu
Sicherheits-Scanner (bandit, semgrep, gitleaks) und shellcheck. Ihre Funde sind echt – das Modell bewertet und behebt
sie, statt zu raten.

build_script() erzeugt ein bash-Skript, das je Prüfung einen Abschnitt ausgibt; parse() zerlegt die Ausgabe wieder.
Fehlt ein Werkzeug (älteres Sandbox-Image), wird die Prüfung übersprungen statt zu scheitern."""

from __future__ import annotations

import re
import shlex

TIMEOUT = 600
OUTPUT_TAIL = 6000  # Zeichen je Prüfung (das Ende ist meist das Wichtige)

LABELS = {
    "ruff": ("Python-Linter (ruff)", "Python linter (ruff)"),
    "pytest": ("Python-Tests", "Python tests"),
    "bandit": ("Python-Sicherheit (bandit)", "Python security (bandit)"),
    "npm-install": ("Node-Abhängigkeiten", "Node dependencies"),
    "npm-lint": ("Linter (npm run lint)", "Linter (npm run lint)"),
    "npm-typecheck": ("Typprüfung (npm run typecheck)", "Type check (npm run typecheck)"),
    "tsc": ("TypeScript (tsc)", "TypeScript (tsc)"),
    "npm-test": ("Tests (npm test)", "Tests (npm test)"),
    "make-test": ("Tests (make test)", "Tests (make test)"),
    "cargo-test": ("Rust-Tests (cargo test)", "Rust tests (cargo test)"),
    "go-test": ("Go (vet + test)", "Go (vet + test)"),
    "shellcheck": ("Shell-Skripte (shellcheck)", "Shell scripts (shellcheck)"),
    "gitleaks": ("Geheimnisse im Code (gitleaks)", "Secrets in code (gitleaks)"),
    "semgrep": ("Sicherheitsmuster (semgrep)", "Security patterns (semgrep)"),
}
# Diese Prüfungen betreffen Sicherheit – ihre Funde gehen an den Sicherheits-Durchgang
SECURITY = {"bandit", "gitleaks", "semgrep"}
HIGH = {"pytest", "npm-test", "make-test", "cargo-test", "go-test", "gitleaks", "tsc", "npm-typecheck"}


def label(name: str, en: bool = False) -> str:
    de, eng = LABELS.get(name, (name, name))
    return eng if en else de


def build_script(files: list[str]) -> str:
    """bash-Skript für die Arbeitskopie. files: geänderte bzw. zu prüfende Dateien (relativ)."""
    q = " ".join(shlex.quote(f) for f in files) or "''"
    py = " ".join(shlex.quote(f) for f in files if f.endswith(".py")) or "''"
    sh = " ".join(shlex.quote(f) for f in files if f.endswith((".sh", ".bash"))) or "''"
    return f"""set +e
export CI=1 NO_COLOR=1 TERM=dumb PAGER=cat
run() {{ name=$1; shift; echo "@@CHECK $name"; echo "@@CMD $*"; timeout {TIMEOUT} bash -c "$*" > /tmp/orbwise-check.out 2>&1; rc=$?; tail -c {OUTPUT_TAIL} /tmp/orbwise-check.out; echo; echo "@@RC $rc"; }}
skip() {{ echo "@@CHECK $1"; echo "@@SKIP $2"; }}
has() {{ command -v "$1" >/dev/null 2>&1; }}
script() {{ [ -f package.json ] && node -e "const s=(require('./package.json').scripts||{{}})['$1']||''; process.exit(s && !/no test specified/.test(s) ? 0 : 1)" 2>/dev/null; }}
# Prüfungen über einzelne Dateien – als Funktion, damit Dateinamen mit Leerzeichen heil bleiben
existing() {{ for f in "$@"; do [ -f "$f" ] && printf '%s\\0' "$f"; done; }}
check_bandit() {{ existing {py} | xargs -0 -r bandit -q -ll; }}
check_shellcheck() {{ existing {sh} | xargs -0 -r shellcheck -S warning; }}
check_semgrep() {{ existing {q} | xargs -0 -r semgrep scan --config p/default --metrics=off --quiet --error --timeout 30; }}
check_gitleaks() {{ fail=0; for f in {q}; do [ -f "$f" ] || continue; gitleaks detect --no-git --no-banner --redact --exit-code 1 --source "$f" || fail=1; done; return $fail; }}
export -f existing check_bandit check_shellcheck check_semgrep check_gitleaks
HASPY=$(existing {py} | head -c1); HASSH=$(existing {sh} | head -c1); HASFILES=$(existing {q} | head -c1)
if [ -n "$(git ls-files '*.py' 2>/dev/null | head -1)" ] || ls *.py >/dev/null 2>&1; then
  if has ruff; then run ruff "ruff check --no-cache --output-format concise ."; else skip ruff "ruff fehlt im Sandbox-Image"; fi
  if python3 -c "import pytest" 2>/dev/null && {{ [ -d tests ] || [ -d test ] || ls test_*.py */test_*.py >/dev/null 2>&1; }}; then
    run pytest "python3 -m pytest -q -x --maxfail=5 -p no:cacheprovider"
  fi
fi
if [ -n "$HASPY" ]; then
  if has bandit; then run bandit check_bandit; else skip bandit "bandit fehlt im Sandbox-Image"; fi
fi
if [ -f package.json ]; then
  if [ ! -d node_modules ]; then
    if [ -f package-lock.json ]; then run npm-install "npm ci --no-audit --no-fund --loglevel=error"
    else run npm-install "npm install --no-audit --no-fund --loglevel=error"; fi
  fi
  script lint && run npm-lint "npm run -s lint"
  if script typecheck; then run npm-typecheck "npm run -s typecheck"
  elif [ -f tsconfig.json ] && [ -x node_modules/.bin/tsc ]; then run tsc "node_modules/.bin/tsc --noEmit"; fi
  script test && run npm-test "npm test --silent"
fi
if [ -f Makefile ] && grep -qE '^test:' Makefile && [ ! -f package.json ]; then run make-test "make -s test"; fi
if [ -f Cargo.toml ] && has cargo; then run cargo-test "cargo test --quiet"; fi
if [ -f go.mod ] && has go; then run go-test "go vet ./... && go test ./..."; fi
if [ -n "$HASSH" ]; then
  if has shellcheck; then run shellcheck check_shellcheck; else skip shellcheck "shellcheck fehlt im Sandbox-Image"; fi
fi
if [ -n "$HASFILES" ]; then
  if has gitleaks; then run gitleaks check_gitleaks; else skip gitleaks "gitleaks fehlt im Sandbox-Image"; fi
  if has semgrep; then run semgrep check_semgrep; else skip semgrep "semgrep fehlt im Sandbox-Image"; fi
fi
echo "@@END"
"""


_SECTION = re.compile(r"^@@CHECK (\S+)\n(.*?)(?=^@@CHECK |^@@END|\Z)", re.M | re.S)


def parse(output: str) -> list[dict]:
    """→ [{name, status: ok|fail|skip, cmd, output}] in der Reihenfolge des Skripts."""
    out = []
    for m in _SECTION.finditer(output or ""):
        name, body = m.group(1), m.group(2)
        if (skip := re.search(r"^@@SKIP (.*)$", body, re.M)):
            out.append({"name": name, "status": "skip", "cmd": "", "output": skip.group(1).strip()})
            continue
        cmd = re.search(r"^@@CMD (.*)$", body, re.M)
        rc = re.search(r"^@@RC (\d+)\s*$", body, re.M)
        text = re.sub(r"^@@(CMD|RC) .*$\n?", "", body, flags=re.M).strip()
        code = int(rc.group(1)) if rc else 1
        status = "ok" if code == 0 else "fail"
        if name == "semgrep" and code != 0 and re.search(r"(?i)(could not|failed to) (download|fetch|resolve)|"
                                                          r"network|connection", text):
            status = "skip"  # Regeln nicht ladbar (Sandbox ohne Netz) – kein Fund im Code
            text = "semgrep: Regeln nicht ladbar (kein Netz?) – übersprungen"
        if code == 124:
            text += f"\n[Zeitlimit von {TIMEOUT} s überschritten]"
        out.append({"name": name, "status": status, "cmd": cmd.group(1).strip() if cmd else "", "output": text})
    return out
