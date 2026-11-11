"""Prüf-Schranke im Coding-Modus: Prüfbefehl erkennen, vor „fertig“ prüfen, rot zurück ans Modell, ehrliche
Statuszeile; Syntaxprüfung direkt nach edit/write."""

import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from conftest import run

from orbwise import verify
from orbwise.agent import Agent

needs_git = pytest.mark.skipif(not shutil.which("git"), reason="git fehlt")


def _proj(tmp_path, ok=False):
    p = tmp_path / "proj"
    (p / "tests").mkdir(parents=True)
    (p / "m.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    (p / "tests" / "test_m.py").write_text(
        "import sys, pathlib\nsys.path.insert(0, str(pathlib.Path(__file__).parent.parent))\nfrom m import f\n\n\n"
        f"def test_f():\n    assert f() == {1 if ok else 2}\n", encoding="utf-8")
    return p


# ---------------------------------------------------------------- Erkennung und Skript
def test_detect_and_project_file(tmp_path, cfg):
    p = _proj(tmp_path)
    names = [n for n, _c in verify.detect(p, ["m.py"])]
    assert names == ["ruff", "pytest"]
    assert [n for n, _c in verify.detect(p, [])] == ["pytest"]  # ruff nur für geänderte .py
    (p / "go.mod").write_text("module x\n")
    (p / "package.json").write_text('{"scripts": {"test": "jest", "lint": "eslint ."}}')
    assert "go-test" in [n for n, _c in verify.detect(p, [])]
    assert "npm-test" not in [n for n, _c in verify.detect(p, [])]  # ohne node_modules nicht
    (p / "node_modules").mkdir()
    assert {"npm-lint", "npm-test"} <= {n for n, _c in verify.detect(p, [])}
    # festgelegt im Projekt geht vor, dann die Config
    cfg.coding.verify_command = "make check"
    assert verify.checks_for(cfg, p, []) == [("check", "make check")]
    (p / ".orbwise.yaml").write_text("check:\n  unit: pytest -q tests/unit\n  types: mypy src\n")
    assert verify.checks_for(cfg, p, []) == [("unit", "pytest -q tests/unit"), ("types", "mypy src")]


def test_script_runs_and_parses(tmp_path):
    p = _proj(tmp_path)
    checks = [("pytest", f"{sys.executable} -m pytest -q -x -p no:cacheprovider"), ("echo", "echo hallo"),
              ("missing", "command -v gibtsnicht-xyz >/dev/null && gibtsnicht-xyz")]
    out = subprocess.run(["bash", "-c", verify.script(checks, 60)], cwd=p, capture_output=True, text=True).stdout
    res = {r["name"]: r for r in verify.parse(out)}
    assert res["pytest"]["status"] == "fail" and "1 failed" in res["pytest"]["output"]
    assert res["echo"]["status"] == "ok" and res["missing"]["status"] == "skip"
    line = verify.summary(list(res.values()))
    assert line.startswith("⚠ Prüfung weiterhin rot: pytest (") and "1 failed" in line
    assert "Schwäche oder lösche keine Tests" in verify.nudge(list(res.values()))
    assert verify.summary([{"name": "ruff", "status": "ok", "output": ""}]) == "✓ Geprüft: ruff"


def test_syntax_check_parses_only(tmp_path):
    assert "Zeile 2" in verify.check_syntax(Path("a.py"), "def f(:\n    pass\n") or "SyntaxError" in verify.check_syntax(Path("a.py"), "def f(:\n")
    assert verify.check_syntax(Path("a.py"), "x = 1\n") == ""
    assert "JSON" in verify.check_syntax(Path("a.json"), '{"a": }')
    assert "YAML" in verify.check_syntax(Path("a.yaml"), "a: [1, 2\n")
    assert "TOML" in verify.check_syntax(Path("a.toml"), "a = \n")
    if shutil.which("bash"):
        f = tmp_path / "x.sh"
        f.write_text("if then fi\n")
        assert verify.check_syntax(f, f.read_text())
    # ausgeführt wird nichts: ein Python-Schnipsel mit Nebenwirkung bleibt folgenlos
    marker = tmp_path / "ran"
    verify.check_syntax(Path("a.py"), f"open({str(marker)!r}, 'w').write('x')\n")
    assert not marker.exists()


def _venv(project, body):
    bin_ = project / ".venv" / "bin"
    bin_.mkdir(parents=True)
    (project / ".venv" / "pyvenv.cfg").write_text("home = /usr/bin\n")
    (bin_ / "python").write_text(f"#!/bin/sh\n{body}\n")
    (bin_ / "python").chmod(0o755)


def test_checks_use_the_projects_virtual_environment(tmp_path):
    p = _proj(tmp_path, ok=True)
    assert "python3 -m pytest" in dict(verify.detect(p, []))["pytest"]  # ohne .venv: System-Python
    _venv(p, f'exec {sys.executable} "$@"')  # „venv“ mit pytest: reicht an das Python der Tests weiter
    (p / ".venv" / "bin" / "ruff").write_text("#!/bin/sh\necho ruff aus der venv\n")
    (p / ".venv" / "bin" / "ruff").chmod(0o755)
    checks = verify.detect(p, ["m.py"])
    cmds = dict(checks)
    assert cmds["pytest"].startswith(".venv/bin/python -c 'import pytest' 2>/dev/null && .venv/bin/python -m pytest")
    assert cmds["ruff"].startswith("command -v .venv/bin/ruff >/dev/null && .venv/bin/ruff check")
    out = subprocess.run(["bash", "-c", verify.script(checks, 60)], cwd=p, capture_output=True, text=True).stdout
    res = {r["name"]: r for r in verify.parse(out)}
    assert res["pytest"]["status"] == "ok" and "1 passed" in res["pytest"]["output"]
    assert res["ruff"]["status"] == "ok" and "ruff aus der venv" in res["ruff"]["output"]
    # eine venv ohne pytest: übersprungen statt rot
    q = tmp_path / "q"
    (q / "tests").mkdir(parents=True)
    (q / "tests" / "test_a.py").write_text("def test_a():\n    pass\n")
    _venv(q, "exit 1")  # venv ohne pytest
    out = subprocess.run(["bash", "-c", verify.script(verify.detect(q, []), 60)], cwd=q, capture_output=True,
                         text=True).stdout
    assert [r["status"] for r in verify.parse(out)] == ["skip"]


def test_tests_of_the_changed_files_run_first(tmp_path):
    p = _proj(tmp_path)  # m.py + tests/test_m.py (rot)
    (p / "pkg").mkdir()
    (p / "pkg" / "__init__.py").write_text("")
    (p / "pkg" / "store.py").write_text("def get():\n    return 1\n")
    (p / "pkg" / "speed_test.py").write_text("def measure():\n    return 1\n")  # heißt nur so – kein Test
    (p / "other.py").write_text("y = 1\n")
    (p / "tests" / "test_store_roundtrip.py").write_text("def test_a():\n    assert True\n")
    (p / "tests" / "test_api.py").write_text("import sys, pathlib\nsys.path.insert(0, str(pathlib.Path(__file__).parent.parent))\n"
                                        "from pkg import store\n\n\ndef test_b():\n    assert store.get() == 1\n")
    (p / "tests" / "test_slow.py").write_text(
        "import pathlib\n\n\ndef test_slow():\n    pathlib.Path('ran-all').write_text('x')\n")
    assert verify.targeted_tests(p, ["m.py"]) == ["tests/test_m.py"]
    # Name mit allen Teilen vor Import; geänderte Tests selbst zuerst; Dateien ohne Tests nicht
    assert verify.targeted_tests(p, ["pkg/store.py", "tests/test_slow.py", "pkg/speed_test.py"]) == [
        "tests/test_slow.py", "tests/test_store_roundtrip.py", "tests/test_api.py"]
    assert verify.targeted_tests(p, ["pkg/__init__.py"]) == ["tests/test_api.py"]  # Paket: wer es importiert
    assert verify.targeted_tests(p, ["other.py"]) == [] and verify.targeted_tests(p, ["README.md"]) == []
    assert verify.targeted_tests(p, ["m.py", "tests/conftest.py"]) == []  # conftest betrifft alle
    cmd = dict(verify.detect(p, ["m.py"]))["pytest"]
    assert cmd.endswith("-m pytest -q --maxfail=5 -p no:cacheprovider tests/test_m.py && "
                        "python3 -m pytest -q --maxfail=5 -p no:cacheprovider")
    assert " && python3 -m pytest" not in dict(verify.detect(p, ["other.py"]))["pytest"].split("2>/dev/null && ", 1)[1]
    # rot im gezielten Lauf: die ganze Suite startet gar nicht erst; grün: danach läuft alles
    checks = [("pytest", cmd.replace("python3", sys.executable))]
    out = subprocess.run(["bash", "-c", verify.script(checks, 60)], cwd=p, capture_output=True, text=True).stdout
    assert verify.parse(out)[0]["status"] == "fail" and not (p / "ran-all").exists()
    (p / "m.py").write_text("def f():\n    return 1 + 1\n")  # andere Länge – sonst gilt der .pyc-Cache noch
    out = subprocess.run(["bash", "-c", verify.script(checks, 60)], cwd=p, capture_output=True, text=True).stdout
    assert verify.parse(out)[0]["status"] == "ok" and (p / "ran-all").exists()


def test_poetry_projects_run_their_checks_through_poetry(tmp_path, monkeypatch):
    p = _proj(tmp_path)
    (p / "poetry.lock").write_text("")
    which = shutil.which
    monkeypatch.setattr(verify.shutil, "which", lambda name: "/usr/bin/poetry" if name == "poetry" else which(name))
    cmd = dict(verify.detect(p, []))["pytest"]
    assert cmd.startswith("poetry run python -c 'import pytest' 2>/dev/null && poetry run python -m pytest")
    # fehlt poetry bzw. pytest darin: übersprungen statt rot
    fake = tmp_path / "bin"
    fake.mkdir()
    (fake / "poetry").write_text("#!/bin/sh\nexit 1\n")
    (fake / "poetry").chmod(0o755)
    out = subprocess.run(["bash", "-c", verify.script([("pytest", cmd)], 30)], cwd=p, capture_output=True, text=True,
                         env={"PATH": f"{fake}:/usr/bin:/bin"}).stdout
    assert [r["status"] for r in verify.parse(out)] == ["skip"]
    _venv(p, "exit 0")  # eine .venv im Projekt geht vor
    assert dict(verify.detect(p, []))["pytest"].startswith(".venv/bin/python")
    monkeypatch.setattr(verify.shutil, "which", lambda name: None if name == "poetry" else which(name))
    (p / ".venv" / "bin" / "python").unlink()
    assert "python3 -m pytest" in dict(verify.detect(p, []))["pytest"]  # ohne poetry im PATH: System-Python


def test_npm_and_go_run_the_related_tests_first(tmp_path):
    p = tmp_path / "web"
    (p / "node_modules" / ".bin").mkdir(parents=True)
    (p / "src").mkdir()
    (p / "src" / "math.ts").write_text("export const a = 1;\n")
    (p / "src" / "gone.ts").touch()
    (p / "package.json").write_text('{"scripts": {"test": "vitest run"}}')
    all_ = "npm test --silent"
    assert dict(verify.detect(p, ["src/math.ts"]))["npm-test"] == all_  # vitest nicht installiert: nur alles
    (p / "node_modules" / ".bin" / "vitest").touch()
    assert dict(verify.detect(p, ["src/math.ts", "README.md", "src/fehlt.ts"]))["npm-test"] == \
        "node_modules/.bin/vitest related --run --passWithNoTests src/math.ts && " + all_
    assert dict(verify.detect(p, ["README.md"]))["npm-test"] == all_  # nichts Passendes geändert
    (p / "package.json").write_text('{"scripts": {"test": "jest --ci"}}')
    assert dict(verify.detect(p, ["src/math.ts"]))["npm-test"] == all_
    (p / "node_modules" / ".bin" / "jest").touch()
    assert dict(verify.detect(p, ["src/math.ts"]))["npm-test"] == \
        "node_modules/.bin/jest --findRelatedTests --passWithNoTests src/math.ts && " + all_
    (p / "package.json").write_text('{"scripts": {"test": "mocha"}}')  # anderer Runner: wie bisher
    assert dict(verify.detect(p, ["src/math.ts"]))["npm-test"] == all_
    g = tmp_path / "svc"
    (g / "pkg" / "store").mkdir(parents=True)
    (g / "go.mod").write_text("module x\n")
    (g / "main.go").touch()
    (g / "pkg" / "store" / "store.go").touch()
    assert dict(verify.detect(g, ["pkg/store/store.go", "main.go", "README.md"]))["go-test"] == \
        "command -v go >/dev/null && go test ./ ./pkg/store && go vet ./... && go test ./..."
    assert dict(verify.detect(g, ["README.md"]))["go-test"] == "command -v go >/dev/null && go vet ./... && go test ./..."


def test_summary_says_when_nothing_could_be_checked():
    skipped = [{"name": "ruff", "status": "skip", "output": "fehlt"}, {"name": "pytest", "status": "skip", "output": ""}]
    assert verify.summary(skipped) == "○ Nicht geprüft – hier nicht installiert: ruff, pytest"
    assert verify.summary(skipped, en=True) == "○ Not checked – not installed here: ruff, pytest"
    assert verify.summary([]) == ""
    assert verify.summary([*skipped, {"name": "make", "status": "ok", "output": ""}]) == "✓ Geprüft: make"


needs_node = pytest.mark.skipif(not shutil.which("node"), reason="node fehlt")
TS_OK = "enum E { A, B }\nclass K { constructor(private x: number) {} }\nexport const a: number = E.A;\n"
TS_BAD = "export const a: number = 1;\nfunction f(x: string { return x }\n"


@needs_node
def test_typescript_is_parsed_with_node(tmp_path, monkeypatch):
    monkeypatch.setattr(verify, "_typescript_lib", lambda p: "")  # kein tsc auf dem Rechner
    probe = subprocess.run(["node", "-p", "typeof require('node:module').stripTypeScriptTypes"], capture_output=True,
                           text=True).stdout.strip()
    if probe != "function":
        assert verify.check_syntax(tmp_path / "a.ts", TS_BAD) == ""  # älteres Node: still, kein Fehlalarm
        return
    assert verify.check_syntax(tmp_path / "a.ts", TS_OK) == ""  # enum & Co. sind kein Syntaxfehler
    assert verify.check_syntax(tmp_path / "a.ts", TS_BAD).startswith("Syntaxfehler: ")
    assert verify.check_syntax(tmp_path / "a.mts", TS_BAD).startswith("Syntaxfehler: ")
    assert verify.check_syntax(tmp_path / "a.tsx", "export const C = () => <div>x</span>;\n") == ""  # JSX nur mit tsc


@pytest.mark.skipif(not shutil.which("node") or not shutil.which("tsc"), reason="node oder tsc fehlt")
def test_typescript_and_tsx_are_parsed_with_the_machines_tsc(tmp_path):
    assert verify.check_syntax(tmp_path / "a.ts", TS_OK) == ""
    assert verify.check_syntax(tmp_path / "a.ts", TS_BAD).startswith("Syntaxfehler: Zeile 2: ")
    assert verify.check_syntax(tmp_path / "c.tsx", "export const C = () => <div>x</div>;\n") == ""
    assert "Zeile 1" in verify.check_syntax(tmp_path / "c.tsx", "export const C = () => <div>x</span>;\n")
    # Typfehler sind kein Syntaxfehler – dafür ist die Prüf-Schranke da
    assert verify.check_syntax(tmp_path / "t.ts", "const n: number = 'text';\n") == ""


def test_typescript_from_the_project_is_never_run(tmp_path, monkeypatch):
    """Ein tsc im Projektordner (node_modules) kann der Agent geschrieben haben – es läuft nicht auf dem Rechner."""
    lib = tmp_path / "proj" / "node_modules" / "typescript" / "lib"
    (lib.parent / "bin").mkdir(parents=True)
    lib.mkdir()
    (lib / "typescript.js").write_text("require('fs').writeFileSync(%r, 'x')" % str(tmp_path / "ran"))
    (lib.parent / "bin" / "tsc").write_text("#!/bin/sh\n")
    which = shutil.which
    monkeypatch.setattr(verify.shutil, "which", lambda name: str(lib.parent / "bin" / "tsc") if name == "tsc"
                        else which(name))
    assert verify._typescript_lib(tmp_path / "proj" / "src" / "a.ts") == ""
    verify.check_syntax(tmp_path / "proj" / "src" / "a.tsx", "export const a = 1;\n")
    assert not (tmp_path / "ran").exists()
    assert verify._typescript_lib(tmp_path / "anderswo" / "a.ts") == str(lib / "typescript.js")  # sonst gefunden


# ---------------------------------------------------------------- im Agenten
class Scripted:
    """Schreibt zuerst falschen Code, meldet „fertig“; bekommt die rote Prüfung zurück und korrigiert."""

    def __init__(self, fix=True):
        self.fix, self.calls, self.nudges = fix, 0, 0

    async def chat_stream(self, messages, tools=None):
        last = messages[-1].get("content") or ""
        if "automatisch geprüft" in last:
            self.nudges += 1
        self.calls += 1
        tool_rounds = sum(1 for m in messages if m["role"] == "tool")
        if messages[-1]["role"] == "user" and "automatisch geprüft" not in last:  # Frage
            call = {"name": "read", "arguments": {"path": "m.py"}}
        elif "automatisch geprüft" in last:
            new = "def f():\n    return 2\n" if self.fix else f"def f():\n    return {3 + self.nudges}\n"
            call = {"name": "write", "arguments": {"path": "m.py", "content": new}}
        elif tool_rounds == 1:
            call = {"name": "write", "arguments": {"path": "m.py", "content": "def f():\n    return 1\n# geändert\n"}}
        else:
            call = None
        if call:
            yield {"type": "done", "message": {"role": "assistant", "content": "", "tool_calls": [{"function": call}]}, "stats": {}}
        else:
            yield {"type": "token", "text": "Fertig."}
            yield {"type": "done", "message": {"role": "assistant", "content": "Fertig."}, "stats": {}}


def _agent(cfg, memory, llm, project):
    cfg.coding.verify_command = f"{sys.executable} -m pytest -q -x -p no:cacheprovider"
    agent = Agent(cfg, llm, memory)
    memory.switch_mode("coding")
    memory.set_project(str(project))
    agent.auto_mode = "auto"
    return agent


def _go(agent):
    events = []

    async def emit(e):
        events.append(e)

    async def confirm(*a):
        return True
    return run(agent.run("Mach f richtig", emit, confirm)), events


@needs_git
def test_red_check_goes_back_and_ends_green(tmp_path, cfg, memory, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    p = _proj(tmp_path)
    llm = Scripted(fix=True)
    answer, events = _go(_agent(cfg, memory, llm, p))
    assert llm.nudges == 1 and (p / "m.py").read_text() == "def f():\n    return 2\n"
    assert answer.endswith("✓ Geprüft: check")
    hist = memory.conversation.history
    assert any(m.get("verify_nudge") for m in hist) and hist[-1]["content"].endswith("✓ Geprüft: check")
    assert sum(1 for e in events if e["type"] == "tool_call" and e["name"] == "shell") == 2


@needs_git
def test_still_red_after_rounds_says_so(tmp_path, cfg, memory, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    cfg.coding.verify_rounds = 2
    p = _proj(tmp_path)
    llm = Scripted(fix=False)
    answer, _ = _go(_agent(cfg, memory, llm, p))
    assert llm.nudges == 2 and "⚠ Prüfung weiterhin rot: check" in answer


@needs_git
def test_no_check_without_changes_or_when_off(tmp_path, cfg, memory, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    p = _proj(tmp_path, ok=True)

    class ReadOnly(Scripted):
        async def chat_stream(self, messages, tools=None):
            if messages[-1]["role"] == "user":
                yield {"type": "done", "message": {"role": "assistant", "content": "", "tool_calls": [
                    {"function": {"name": "read", "arguments": {"path": "m.py"}}}]}, "stats": {}}
            else:
                yield {"type": "done", "message": {"role": "assistant", "content": "Gelesen."}, "stats": {}}
    answer, events = _go(_agent(cfg, memory, ReadOnly(), p))
    assert answer == "Gelesen." and not any(e.get("name") == "shell" for e in events if e["type"] == "tool_call")
    cfg.coding.verify = "off"
    answer, events = _go(_agent(cfg, memory, Scripted(), p))
    assert "Geprüft" not in answer and not any(e.get("name") == "shell" for e in events if e["type"] == "tool_call")


@needs_git
def test_declined_check_is_not_asked_again(tmp_path, cfg, memory, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    p = _proj(tmp_path)
    agent = _agent(cfg, memory, Scripted(), p)
    agent.auto_mode = "off"
    asked = []

    async def emit(e):
        pass

    async def confirm(call_id, name, args, reason):
        asked.append(name)
        return name != "shell"  # Dateien ja, Prüfung nein
    answer = run(agent.run("Mach f richtig", emit, confirm))
    assert asked.count("shell") == 1 and "Geprüft" not in answer and "rot" not in answer


class Busy:
    """Liest, schreibt falschen Code und arbeitet dann immer weiter (jedes Mal ein anderer Befehl) – bis Orbwise die
    Antwort anhält. stop=True: hört stattdessen nach dem Schreiben ohne Ziel-Marker auf."""

    def __init__(self, stop=False):
        self.stop, self.nudges = stop, 0

    async def chat_stream(self, messages, tools=None):
        last = messages[-1]
        self.nudges += "automatisch geprüft" in (last.get("content") or "")
        n = sum(1 for m in messages if m["role"] == "tool")
        if n == 0:
            call = {"name": "read", "arguments": {"path": "m.py"}}
        elif n == 1:
            call = {"name": "write", "arguments": {"path": "m.py", "content": "def f():\n    return 1\n# geändert\n"}}
        elif self.stop or last["role"] == "user":  # Zwischenbilanz bzw. „mach weiter“
            call = None
        else:
            call = {"name": "shell", "arguments": {"command": f"echo {n}"}}
        if call:
            yield {"type": "done", "message": {"role": "assistant", "content": "", "tool_calls": [{"function": call}]}, "stats": {}}
        else:
            yield {"type": "token", "text": "Zwischenstand."}
            yield {"type": "done", "message": {"role": "assistant", "content": "Zwischenstand."}, "stats": {}}


@needs_git
def test_step_limit_still_reports_the_checks(tmp_path, cfg, memory, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    cfg.coding.max_steps = 4
    llm = Busy()
    answer, events = _go(_agent(cfg, memory, llm, _proj(tmp_path)))
    assert "Zwischenstand." in answer and "⚠ Prüfung weiterhin rot: check" in answer
    assert llm.nudges == 0  # nur gemeldet – die Antwort war am Limit ohnehin zu Ende
    assert memory.conversation.history[-1]["content"].endswith("Details im Aktivitäts-Panel.")


@needs_git
def test_goal_given_up_still_reports_the_checks(tmp_path, cfg, memory, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    llm = Busy(stop=True)
    agent = _agent(cfg, memory, llm, _proj(tmp_path))
    agent.set_goal("Mach f richtig")
    answer, events = _go(agent)
    assert [e["state"] for e in events if e["type"] == "goal"][-1] == "stopped" and agent.goal_state() is None
    assert "⚠ Prüfung weiterhin rot: check" in answer and llm.nudges == 0
