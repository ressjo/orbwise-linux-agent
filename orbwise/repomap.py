"""Repo-Map für den Coding-Modus: Datei → Symbole (Klassen, Funktionen, Methoden, Typen) mit Zeile und Signatur.
Statt vieler read/grep-Runden sieht das Modell mit einem Aufruf, wo was definiert ist – das hilft kleinen
Kontextfenstern und großen Codebasen am meisten.

Quelle: universal-ctags, falls installiert (viele Sprachen); sonst eingebaut – Python über `ast`, JS/TS, Go, Rust,
Java/Kotlin/C#, C/C++, Ruby und PHP über Muster. Dateiliste über `git ls-files` (beachtet .gitignore), sonst ein
Verzeichnislauf ohne SKIP_DIRS. Ergebnisse werden je Projekt nach Änderungszeit zwischengespeichert."""

from __future__ import annotations

import ast
import hashlib
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

from .tools.files import SKIP_DIRS

MAX_FILES = 20_000
MAX_BYTES = 400_000  # größere Dateien (generiert, minifiziert) auslassen
CODE_SUFFIXES = {".py", ".pyi", ".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".go", ".rs", ".java", ".kt", ".kts",
                 ".cs", ".c", ".h", ".cc", ".cpp", ".cxx", ".hpp", ".hh", ".rb", ".php", ".swift", ".scala", ".sh"}

# Muster je Sprache: (Art, Regex mit Gruppe „name“ und optional „sig“) – zeilenweise
_JS = [("class", r"^\s*(?:export\s+)?(?:default\s+)?(?:abstract\s+)?class\s+(?P<name>[A-Za-z_$][\w$]*)"),
       ("function", r"^\s*(?:export\s+)?(?:default\s+)?(?:async\s+)?function\s*\*?\s*(?P<name>[A-Za-z_$][\w$]*)\s*(?P<sig>\([^)]*\))?"),
       ("const", r"^\s*export\s+(?:const|let|var)\s+(?P<name>[A-Za-z_$][\w$]*)\s*(?::[^=]+)?=\s*(?:async\s*)?(?P<sig>\([^)]*\))?"),
       ("function", r"^\s*(?:const|let)\s+(?P<name>[A-Za-z_$][\w$]*)\s*=\s*(?:async\s*)?(?P<sig>\([^)]*\))\s*(?::[^=]+)?=>"),
       ("interface", r"^\s*(?:export\s+)?interface\s+(?P<name>[A-Za-z_$][\w$]*)"),
       ("type", r"^\s*(?:export\s+)?type\s+(?P<name>[A-Za-z_$][\w$]*)\s*(?:<[^>]*>)?\s*="),
       ("enum", r"^\s*(?:export\s+)?(?:const\s+)?enum\s+(?P<name>[A-Za-z_$][\w$]*)"),
       ("method", r"^\s{2,}(?:public\s+|private\s+|protected\s+|static\s+|async\s+|readonly\s+|override\s+)*"
                  r"(?P<name>(?!if\b|for\b|while\b|switch\b|catch\b|return\b|function\b)[A-Za-z_$][\w$]*)\s*(?P<sig>\([^)]*\))\s*(?::\s*[^{]+)?\{\s*$")]
PATTERNS: dict[str, list[tuple[str, str]]] = {
    "js": _JS,
    "go": [("func", r"^func\s+(?:\((?P<recv>[^)]*)\)\s*)?(?P<name>\w+)\s*(?P<sig>\([^)]*\))"),
           ("type", r"^type\s+(?P<name>\w+)\s+(?:struct|interface|func|\w)")],
    "rs": [("fn", r"^\s*(?:pub(?:\([^)]*\))?\s+)?(?:async\s+)?(?:unsafe\s+)?fn\s+(?P<name>\w+)\s*(?:<[^>]*>)?\s*(?P<sig>\([^)]*\))?"),
           ("struct", r"^\s*(?:pub(?:\([^)]*\))?\s+)?struct\s+(?P<name>\w+)"),
           ("enum", r"^\s*(?:pub(?:\([^)]*\))?\s+)?enum\s+(?P<name>\w+)"),
           ("trait", r"^\s*(?:pub(?:\([^)]*\))?\s+)?trait\s+(?P<name>\w+)"),
           ("impl", r"^\s*impl(?:<[^>]*>)?\s+(?P<name>[\w:<>, ]+?)\s*(?:for\s+(?P<sig>[\w:<>]+))?\s*\{")],
    "java": [("class", r"^\s*(?:public\s+|private\s+|protected\s+|internal\s+)?(?:abstract\s+|final\s+|sealed\s+|static\s+|data\s+|open\s+|partial\s+)*"
                       r"(?:class|interface|enum|record|object)\s+(?P<name>\w+)"),
             ("method", r"^\s+(?:public|private|protected|internal|override|static|final|abstract|async|virtual|suspend|fun)\b[\w\s<>\[\],?]*?\s"
                        r"(?P<name>\w+)\s*(?P<sig>\([^)]*\))\s*(?:throws [\w., ]+)?\s*(?::\s*[\w<>?, ]+)?\s*[{=]?\s*$")],
    "c": [("function", r"^(?!\s)(?:static\s+|inline\s+|extern\s+|virtual\s+|const\s+)*[\w:<>*&\s]+?[\s*&](?P<name>[A-Za-z_][\w:~]*)\s*(?P<sig>\([^;{)]*\))\s*(?:const\s*)?\{?\s*$"),
          ("class", r"^\s*(?:class|struct)\s+(?P<name>\w+)\s*(?::[^{]*)?\{")],
    "rb": [("class", r"^\s*(?:class|module)\s+(?P<name>[\w:]+)"),
           ("def", r"^\s*def\s+(?P<name>[\w.?!=]+)\s*(?P<sig>\([^)]*\))?")],
    "php": [("class", r"^\s*(?:abstract\s+|final\s+)?(?:class|interface|trait|enum)\s+(?P<name>\w+)"),
            ("function", r"^\s*(?:public\s+|private\s+|protected\s+|static\s+)*function\s+(?P<name>\w+)\s*(?P<sig>\([^)]*\))")],
    "sh": [("function", r"^\s*(?:function\s+)?(?P<name>[\w-]+)\s*\(\)\s*\{?")],
}
LANG = {".js": "js", ".jsx": "js", ".mjs": "js", ".cjs": "js", ".ts": "js", ".tsx": "js", ".go": "go", ".rs": "rs",
        ".java": "java", ".kt": "java", ".kts": "java", ".cs": "java", ".scala": "java", ".swift": "java",
        ".c": "c", ".h": "c", ".cc": "c", ".cpp": "c", ".cxx": "c", ".hpp": "c", ".hh": "c",
        ".rb": "rb", ".php": "php", ".sh": "sh"}
_COMPILED = {k: [(kind, re.compile(rx)) for kind, rx in v] for k, v in PATTERNS.items()}


# ---------------------------------------------------------------- Dateien
def files(project: str | Path) -> list[str]:
    """Quellcode-Dateien relativ zum Projekt (git ls-files, sonst Verzeichnislauf)."""
    root = Path(project)
    out: list[str] = []
    if (root / ".git").exists() and shutil.which("git"):
        try:
            r = subprocess.run(["git", "-C", str(root), "ls-files", "-co", "--exclude-standard", "-z"],
                               capture_output=True, timeout=20)
            if r.returncode == 0:
                out = [f for f in r.stdout.decode("utf-8", "replace").split("\0") if f]
        except (OSError, subprocess.TimeoutExpired):
            out = []
    if not out:
        for dirpath, dirnames, names in os.walk(root):
            dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS and not d.startswith("."))
            rel = Path(dirpath).relative_to(root)
            out.extend(str(rel / n) if str(rel) != "." else n for n in sorted(names))
            if len(out) > MAX_FILES * 3:
                break
    return [f for f in out if Path(f).suffix.lower() in CODE_SUFFIXES][:MAX_FILES]


# ---------------------------------------------------------------- Symbole einer Datei
def _python(text: str) -> list[dict]:
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError):
        return _regex(text, [("class", r"^\s*class\s+(?P<name>\w+)"),
                             ("def", r"^\s*(?:async\s+)?def\s+(?P<name>\w+)\s*(?P<sig>\([^)]*\))?")])
    out: list[dict] = []

    def args(fn) -> str:
        a = fn.args
        names = [x.arg for x in a.posonlyargs + a.args]
        if a.vararg:
            names.append("*" + a.vararg.arg)
        names += [x.arg for x in a.kwonlyargs]
        if a.kwarg:
            names.append("**" + a.kwarg.arg)
        return "(" + ", ".join(names) + ")"

    def walk(nodes, parent: str = "", depth: int = 0) -> None:
        for n in nodes:
            if isinstance(n, ast.ClassDef):
                out.append({"line": n.lineno, "kind": "class", "name": n.name, "sig": "", "parent": parent})
                if depth < 2:
                    walk(n.body, n.name, depth + 1)
            elif isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
                kind = "method" if parent else "def"
                out.append({"line": n.lineno, "kind": kind, "name": n.name, "sig": args(n), "parent": parent})
    walk(tree.body)
    return out


def _regex(text: str, patterns) -> list[dict]:
    compiled = [(k, re.compile(rx) if isinstance(rx, str) else rx) for k, rx in patterns]
    out: list[dict] = []
    seen: set[tuple[int, str]] = set()
    for i, line in enumerate(text.splitlines(), 1):
        if len(line) > 400:
            continue
        for kind, rx in compiled:
            m = rx.match(line)
            if not m:
                continue
            name = m.group("name").strip()
            if (i, name) in seen:
                break
            seen.add((i, name))
            gd = m.groupdict()
            sig = re.sub(r"\s+", " ", gd.get("sig") or "").strip()
            if kind == "func" and gd.get("recv"):
                sig = f"({gd['recv'].strip()}) " + sig
            out.append({"line": i, "kind": kind, "name": name, "sig": sig[:120], "parent": ""})
            break
    return out


def symbols(path: Path, text: str) -> list[dict]:
    suffix = path.suffix.lower()
    if suffix in (".py", ".pyi"):
        return _python(text)
    lang = LANG.get(suffix)
    return _regex(text, _COMPILED[lang]) if lang else []


def _ctags(root: Path, rel: list[str]) -> dict[str, list[dict]] | None:
    exe = shutil.which("ctags")
    if not exe or not rel:
        return None
    try:
        probe = subprocess.run([exe, "--version"], capture_output=True, text=True, timeout=5)
        if "Universal Ctags" not in probe.stdout:
            return None
        r = subprocess.run([exe, "--output-format=json", "--fields=+nS", "-f", "-", "-L", "-"], cwd=root,
                           input="\n".join(rel), capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.TimeoutExpired):
        return None
    out: dict[str, list[dict]] = {f: [] for f in rel}
    wanted = {"class", "function", "method", "struct", "interface", "enum", "trait", "type", "typedef",
              "module", "namespace", "func", "member", "constant"}
    for line in r.stdout.splitlines():
        try:
            t = json.loads(line)
        except ValueError:
            continue
        if t.get("_type") != "tag" or t.get("kind") not in wanted or t.get("path") not in out:
            continue
        if t.get("kind") in ("member", "constant") and t.get("scope"):
            continue  # Felder und lokale Konstanten wären zu viel
        out[t["path"]].append({"line": t.get("line", 0), "kind": t["kind"], "name": t.get("name", ""),
                               "sig": (t.get("signature") or "")[:120], "parent": t.get("scope", "") or ""})
    for v in out.values():
        v.sort(key=lambda s: s["line"])
    return out


# ---------------------------------------------------------------- Index mit Cache
def _cache_file(root: Path) -> Path:
    base = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / "orbwise" / "repomap"
    return base / (hashlib.sha1(str(root.resolve()).encode()).hexdigest()[:16] + ".json")


def index(project: str | Path) -> dict[str, list[dict]]:
    """Datei → Symbole; nur Dateien mit neuer Änderungszeit werden neu gelesen."""
    root = Path(project)
    cache_path = _cache_file(root)
    try:
        cache = json.loads(cache_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        cache = {}
    entries = cache.get("files", {}) if isinstance(cache, dict) else {}
    result: dict[str, list[dict]] = {}
    todo: list[str] = []
    stamps: dict[str, list[int]] = {}
    for rel in files(root):
        try:
            st = (root / rel).stat()
        except OSError:
            continue
        if st.st_size > MAX_BYTES:
            continue
        stamp = [st.st_mtime_ns, st.st_size]
        stamps[rel] = stamp
        old = entries.get(rel)
        if old and old.get("stamp") == stamp:
            result[rel] = old["symbols"]
        else:
            todo.append(rel)
    fresh = _ctags(root, todo) if len(todo) > 20 else None  # ctags lohnt bei vielen Dateien
    for rel in todo:
        if fresh is not None and rel in fresh and (fresh[rel] or Path(rel).suffix.lower() not in (".py", ".pyi")):
            result[rel] = fresh[rel]
            continue
        try:
            text = (root / rel).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        result[rel] = symbols(root / rel, text)
    if todo or len(result) != len(entries):
        try:
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            cache_path.write_text(json.dumps({"files": {f: {"stamp": stamps[f], "symbols": s} for f, s in result.items()
                                                        if f in stamps}}), encoding="utf-8")
        except OSError:
            pass
    return result


# ---------------------------------------------------------------- Ausgabe
def _fmt(s: dict) -> str:
    name = f"{s['parent']}.{s['name']}" if s.get("parent") and s["kind"] in ("method", "def", "function", "member") else s["name"]
    kind = {"def": "def", "function": "fn", "func": "func", "method": "def", "class": "class"}.get(s["kind"], s["kind"])
    return f"L{s['line']} {kind} {name}{s.get('sig') or ''}"


def render(idx: dict[str, list[dict]], path: str = "", query: str = "", limit: int = 6000) -> str:
    """Übersicht (Datei: Symbole) bzw. Treffer zu query; auf limit Zeichen gekürzt, mit Hinweis was fehlt."""
    prefix = path.strip().strip("/")
    items = sorted((f, s) for f, s in idx.items() if not prefix or f == prefix or f.startswith(prefix + "/"))
    if query:
        q = query.lower()
        hits = []
        for f, syms in items:
            for s in syms:
                if q in s["name"].lower() or (s.get("parent") and q in f"{s['parent']}.{s['name']}".lower()):
                    exact = s["name"].lower() == q
                    hits.append((not exact, f, s))
        hits.sort(key=lambda h: (h[0], h[1], h[2]["line"]))
        if not hits:
            return f"Kein Symbol mit „{query}“ gefunden" + (f" unter {prefix}" if prefix else "") + " – grep sucht im Text."
        lines = [f"{f}: {_fmt(s)}" for _x, f, s in hits]
        return _cut(lines, limit, len(lines), "Treffer")
    if not items:
        return "Keine Quellcode-Dateien gefunden" + (f" unter {prefix}" if prefix else "") + "."
    if len(items) > 60 and sum(len(f) + 12 * 25 for f, _s in items) > limit:
        # zu groß für Symbole je Datei: erst die Ordner (bis zur zweiten Ebene unter path) mit Anzahl
        depth = (prefix.count("/") + 1 if prefix else 0) + 2
        dirs: dict[str, list[int]] = {}
        for f, syms in items:
            parts = f.split("/")
            key = "/".join(parts[:min(depth, len(parts) - 1)]) or "."
            d = dirs.setdefault(key, [0, 0])
            d[0] += 1
            d[1] += len(syms)
        head = [f"{len(items)} Quelldateien – Ordner (Dateien · Symbole); Details mit path=<Ordner>, Definitionen mit query=<Name>:"]
        return _cut(head + [f"{k}/  ({n} · {m})" if k != "." else f"./  ({n} · {m})" for k, (n, m) in sorted(dirs.items())],
                    limit, len(dirs), "Ordner")
    lines = []
    for f, syms in items:
        top = [s for s in syms if s["kind"] not in ("method", "member")] or syms
        shown = " · ".join(_fmt(s) for s in top[:12]) + (f" · (+{len(syms) - min(len(top), 12)})" if len(syms) > 12 else "")
        lines.append(f"{f}: {shown}" if syms else f)
    return _cut(lines, limit, len(items), "Dateien")


def _cut(lines: list[str], limit: int, total: int, what: str) -> str:  # noqa: ARG001
    out, used = [], 0
    for ln in lines:
        if used + len(ln) + 1 > limit and out:
            rest = len(lines) - len(out)
            out.append(f"[… {rest} weitere {what} – mit path=<Ordner> oder query=<Name> eingrenzen]")
            break
        out.append(ln)
        used += len(ln) + 1
    return "\n".join(out)
