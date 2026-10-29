"""Code-Check: feste Prüfungen (echt), Reviewer-Durchgänge mit frischem Blick, skeptische Gegenprüfung – nur
belegte Funde landen im Bericht und im Auftrag zum Beheben."""

import json
import subprocess
from pathlib import Path

import pytest
from conftest import run

from orbwise import review_checks, sandbox, workspace
from orbwise.agent import Agent
from orbwise.agent_review import clean_findings, confirmed, json_list


def test_json_list_finds_the_answer():
    assert json_list('Ich habe geprüft.\n```json\n[{"title": "a", "line": 3}]\n```') == [{"title": "a", "line": 3}]
    assert json_list("Erst [1,2] dann [{\"x\": [1]}]") == [{"x": [1]}]
    assert json_list("nichts gefunden: []") == []
    assert json_list("kaputt [{") == []


def test_findings_are_normalised_and_only_confirmed_ones_stay():
    raw = [{"severity": "Hoch", "file": "a.py", "line": "7", "title": "Division durch 0", "why": "b kann 0 sein"},
           {"severity": "irgendwas", "file": "b.py", "title": "Name", "why": "unklar"}, {"nix": 1}]
    found = clean_findings(raw, "bugs")
    assert [(f["severity"], f["line"]) for f in found] == [("high", 7), ("low", 0)]
    verdicts = [{"id": 1, "verdict": "bestätigt", "reason": "Test zeigt ZeroDivisionError"},
                {"id": 2, "verdict": "verworfen", "reason": "Name passt"}]
    kept = confirmed(found, verdicts)
    assert len(kept) == 1 and kept[0]["proof"].startswith("Test zeigt")
    assert confirmed(found, []) == []  # ohne Antwort des Prüfers: nichts gilt als belegt


def test_parse_splits_the_sections():
    out = ("@@CHECK ruff\n@@CMD ruff check .\nall good\n@@RC 0\n"
           "@@CHECK pytest\n@@CMD python3 -m pytest\nF\n1 failed\n@@RC 1\n"
           "@@CHECK bandit\n@@SKIP bandit fehlt im Sandbox-Image\n"
           "@@CHECK semgrep\n@@CMD check_semgrep\nFailed to download config: network\n@@RC 2\n@@END\n")
    checks = review_checks.parse(out)
    assert [(c["name"], c["status"]) for c in checks] == [("ruff", "ok"), ("pytest", "fail"), ("bandit", "skip"),
                                                         ("semgrep", "skip")]
    assert checks[1]["cmd"] == "python3 -m pytest" and "1 failed" in checks[1]["output"]


@pytest.fixture
def project(tmp_path):
    p = tmp_path / "proj"
    (p / "tests").mkdir(parents=True)
    (p / "calc.py").write_text("def add(a, b):\n    return a - b\n")
    (p / "tests" / "test_calc.py").write_text("from calc import add\n\n\ndef test_add():\n    assert add(1, 2) == 3\n")
    subprocess.run(["git", "init", "-q"], cwd=p, check=True)
    subprocess.run(["git", "add", "-A"], cwd=p, check=True)
    return p


def test_check_script_finds_a_failing_test(project):
    out = subprocess.run(["bash", "-c", review_checks.build_script(["calc.py"])], cwd=project, capture_output=True,
                         text=True, env={"PATH": "/usr/bin:/bin", "PYTHONPATH": str(project)}).stdout
    checks = {c["name"]: c for c in review_checks.parse(out)}
    if "pytest" in checks:  # pytest im Test-Python vorhanden
        assert checks["pytest"]["status"] == "fail"


def scripted_subagent(answers):
    """run_subagent ersetzen: Reviewer-Durchgänge und Gegenprüfung antworten wie vorgegeben."""
    seen = []

    async def fake(self, ctx, kind, description, task):
        seen.append(task)
        if "skeptisch" in task or "sceptically" in task:
            return answers["verify"]
        for focus, text in answers.items():
            if focus in ("verify",):
                continue
            if focus == "bugs" and "Fehler und Logik" in task or focus == "security" and "Sicherheit:" in task \
                    or focus == "structure" and "Struktur und" in task:
                return text
        return "[]"
    return fake, seen


@pytest.fixture
def caged(project, monkeypatch, cfg, memory):
    """Sandbox ohne Container: Befehle laufen direkt im Testprojekt."""
    sb = {"enabled": True, "path": str(project), "name": "orbwise-test", "project": str(project)}
    memory.switch_mode("coding")
    memory.set_project(str(project))
    memory.conversation.meta["sandbox"] = sb

    async def ensure(cfg, sb, progress=None):
        return "bash"
    monkeypatch.setattr(sandbox, "ensure_running", ensure)
    monkeypatch.setattr(sandbox, "exec_argv", lambda binary, name, workdir, command, timeout=None: ["bash", "-c", command])
    monkeypatch.setattr(workspace, "changes", lambda state: [{"path": "calc.py", "status": "modified", "add": 1, "del": 1}])
    monkeypatch.setattr(workspace, "diff", lambda state, path: "-    return a + b\n+    return a - b")
    monkeypatch.setenv("PYTHONPATH", str(project))
    return sb


def test_review_reports_only_proven_findings_and_asks_to_fix_them(cfg, llm, memory, caged, monkeypatch):
    findings = json.dumps([{"severity": "hoch", "file": "calc.py", "line": 2, "title": "add subtrahiert",
                            "why": "return a - b statt a + b", "fix": "a + b"},
                           {"severity": "mittel", "file": "calc.py", "line": 1, "title": "Erfunden", "why": "x"}])
    fake, seen = scripted_subagent({"bugs": "Geprüft.\n" + findings, "security": "[]", "structure": "[]",
                                    "verify": '[{"id": 1, "verdict": "bestätigt", "reason": "add(1,2) == -1"},'
                                              ' {"id": 2, "verdict": "verworfen", "reason": "steht so nicht da"}]'})
    monkeypatch.setattr(Agent, "run_subagent", fake)
    agent = Agent(cfg, llm, memory)
    events = []

    async def emit(ev):
        events.append(ev)

    async def confirm(*a):
        return True
    result = run(agent.code_review(emit, confirm))
    report = memory.conversation.history[-1]["content"]
    assert "add subtrahiert" in report and "Erfunden" not in report and "1 weitere" in report
    assert "calc.py" in seen[0] and "return a - b" in seen[0]  # Reviewer sieht Dateien und Diff, nicht den Chat
    assert "add subtrahiert" in result["fix"] and "`" in result["fix"]  # Auftrag mit Fund und Prüfbefehlen
    steps = [e["args"]["step"] for e in events if e["type"] == "tool_call" and e["name"] == "code_review"]
    assert any("Durchgang" in s for s in steps) and any("gegenprüfen" in s for s in steps)

    # nach dem Beheben: nur die festen Prüfungen erneut
    status = {c["name"]: c["status"] for c in result["checks"]}
    assert status.get("pytest") == "fail" and "Python-Tests" in result["fix"]  # echter Testfehler im Auftrag
    fixed = Path(caged["path"]) / "calc.py"
    fixed.write_text("def add(a, b):\n    return a + b  # behoben\n")  # andere Größe: kein veralteter Bytecode
    for cache in Path(caged["path"]).rglob("__pycache__"):
        __import__("shutil").rmtree(cache)
    again = run(agent.code_recheck(emit, confirm, result, 1, last=True))
    assert again["fix"] == "" and {c["name"]: c["status"] for c in again["checks"]}["pytest"] == "ok"
    assert "grün" in memory.conversation.history[-1]["content"]


def test_review_without_project_says_so(cfg, llm, memory):
    agent = Agent(cfg, llm, memory)

    async def emit(ev):
        pass
    result = run(agent.code_review(emit, None))
    assert result["fix"] == "" and "Projektordner" in memory.conversation.history[-1]["content"]


def test_slash_review_starts_the_check(cfg, monkeypatch):
    from fastapi.testclient import TestClient

    from orbwise.server import create_app
    monkeypatch.setenv("ORBWISE_FAKE_LLM", "1")
    monkeypatch.setenv("ORBWISE_SKIP_WARMUP", "1")
    with TestClient(create_app(cfg), base_url="http://localhost:8765") as client:
        hub = client.app.state.hub
        calls = []

        async def fake_review(full=False):
            calls.append(full)
        hub.review = fake_review
        client.portal.call(hub.submit, "/review")
        client.portal.call(hub.submit, "/review all")
        client.portal.call(hub.submit, "/prüfen")
        import time
        time.sleep(0.2)
        assert calls == [False, True, False]


def test_stop_ends_the_check_with_the_state_so_far(cfg, llm, memory, caged, monkeypatch):
    """„Stoppen“ in der Aktivität während „Sicherheit“: Struktur und Gegenprüfung entfallen, der Fund aus „Fehler“
    steht als nicht gegengeprüft im Bericht, nichts wird automatisch behoben."""
    import asyncio
    seen = []
    agent = Agent(cfg, llm, memory)

    async def fake(self, ctx, kind, description, task):
        seen.append(task)
        if "Sicherheit:" in task:
            assert self.stop_review()
            await asyncio.sleep(30)  # wird abgebrochen
        return json.dumps([{"severity": "hoch", "file": "calc.py", "line": 2, "title": "add subtrahiert",
                            "why": "a - b"}])
    monkeypatch.setattr(Agent, "run_subagent", fake)
    events = []

    async def emit(ev):
        events.append(ev)

    async def confirm(*a):
        return True
    result = run(asyncio.wait_for(agent.code_review(emit, confirm), 20))
    assert result["fix"] == "" and len(seen) == 2  # Fehler + Sicherheit, keine Struktur, keine Gegenprüfung
    report = memory.conversation.history[-1]["content"]
    assert "gestoppt" in report and "nicht gegengeprüft" in report and "add subtrahiert" in report
    stopped = [e for e in events if e["type"] == "tool_result" and e["status"] == "denied"]
    assert len(stopped) == 1 and stopped[0]["text"] == "gestoppt"
    assert not agent.stop_review()  # danach läuft nichts mehr


def test_stop_without_a_running_check(cfg, monkeypatch):
    from fastapi.testclient import TestClient

    from orbwise.server import create_app
    monkeypatch.setenv("ORBWISE_FAKE_LLM", "1")
    monkeypatch.setenv("ORBWISE_SKIP_WARMUP", "1")
    with TestClient(create_app(cfg), base_url="http://localhost:8765") as client:
        assert client.post("/api/review/stop").json() == {"stopped": False}
