"""Agent-Benchmark: jede Aufgabe ist im Ausgangszustand ungelöst und mit ihrer Musterlösung gelöst; der Lauf misst,
was der Agent tut, und entscheidet Rückfragen nach einer festen Regel."""

import shutil
from pathlib import Path

import pytest
from conftest import run

from orbwise import bench
from orbwise.bench_tasks import BY_NAME, COMMON, TASKS

needs_git = pytest.mark.skipif(not shutil.which("git"), reason="git fehlt")


@pytest.mark.parametrize("task", TASKS, ids=lambda t: t.name)
def test_task_is_unsolved_at_first_and_solved_by_its_reference(task, tmp_path):
    project = tmp_path / "p"
    project.mkdir()
    bench._prepare(task, project)
    ok, why = bench.verdict(task, project, "Ich habe nichts gefunden.")
    assert not ok, f"schon im Ausgangszustand gelöst: {why}"
    for rel, content in task.solution.items():
        (project / rel).parent.mkdir(parents=True, exist_ok=True)
        (project / rel).write_text(content, encoding="utf-8")
    ok, why = bench.verdict(task, project, task.answer)
    assert ok, why
    assert task.solution or task.answer  # jede Aufgabe hat eine Musterlösung


def test_checks_need_no_pytest(tmp_path):
    """Orbwises eigene Umgebung hat kein pytest – die Prüfung nimmt den Test-Lauf, den jedes Projekt mitbringt."""
    import subprocess
    import sys
    task = BY_NAME["fix-bug"]
    project = tmp_path / "p"
    project.mkdir()
    bench._prepare(task, project)
    assert "import pytest" not in COMMON["run_tests.py"] and "run_tests.py" in COMMON["AGENTS.md"]
    assert COMMON[".orbwise.yaml"] == "check: python3 run_tests.py\n"  # auch die Prüf-Schranke des Agenten
    # -I -S: ohne site-packages, also sicher ohne pytest
    r = subprocess.run([sys.executable, "-I", "-S", "run_tests.py"], cwd=project, capture_output=True, text=True)
    assert r.returncode == 1 and "FAILED test_stats.py::test_median_even" in r.stdout and "1 failed, 3 passed" in r.stdout
    (project / "stats.py").write_text(task.solution["stats.py"])
    r = subprocess.run([sys.executable, "-I", "-S", "run_tests.py"], cwd=project, capture_output=True, text=True)
    assert r.returncode == 0 and r.stdout.strip().endswith("0 failed, 4 passed")
    # ein Projekt ganz ohne Tests gilt nicht als „grün“; ein Importfehler zählt als Fehlschlag
    (project / "tests" / "test_stats.py").write_text("import gibt_es_nicht\n")
    r = subprocess.run([sys.executable, "-I", "-S", "run_tests.py"], cwd=project, capture_output=True, text=True)
    assert r.returncode == 1 and "FAILED test_stats.py (Import)" in r.stdout
    (project / "tests" / "test_stats.py").unlink()
    assert subprocess.run([sys.executable, "run_tests.py"], cwd=project, capture_output=True).returncode == 1


def test_cheating_does_not_count(tmp_path):
    # Tests abschwächen: die geschützten Tests gelten im Ausgangszustand
    task = BY_NAME["fix-bug"]
    project = tmp_path / "a"
    project.mkdir()
    bench._prepare(task, project)
    (project / "tests" / "test_stats.py").write_text("def test_nichts():\n    assert True\n")
    assert not bench.verdict(task, project, "")[0]
    assert "median" in (project / "tests" / "test_stats.py").read_text()  # zurückgesetzt
    # „ändere nichts“: mit geänderten Dateien nicht gelöst, auch wenn die Antwort stimmt
    task = BY_NAME["find-code"]
    project = tmp_path / "b"
    project.mkdir()
    bench._prepare(task, project)
    (project / "pricing.py").write_text("# aufgeräumt\n")
    ok, why = bench.verdict(task, project, task.answer)
    assert not ok and "pricing.py" in why
    # Tests, die nichts prüfen
    task = BY_NAME["write-tests"]
    project = tmp_path / "c"
    project.mkdir()
    bench._prepare(task, project)
    (project / "tests" / "test_utils.py").write_text(
        "from utils import clamp\n\n\n" + "\n\n".join(f"def test_{i}():\n    assert clamp(5, 0, 10) == 5" for i in range(3)) + "\n")
    ok, why = bench.verdict(task, project, "")
    assert not ok and "kaputtem clamp" in why


class Solver:
    """Löst „fix-bug“ wie ein Modell: lesen, falsch ändern (Edit schlägt fehl), richtig ändern, antworten.
    lazy: antwortet nur, ohne etwas zu tun. wild: versucht stattdessen, alles zu löschen."""

    def __init__(self, lazy=False, wild=False):
        self.lazy, self.wild = lazy, wild

    async def chat_stream(self, messages, tools=None, **_):
        n = sum(1 for m in messages if m["role"] == "tool")
        fixed = BY_NAME["fix-bug"].solution["stats.py"]
        plan = [] if self.lazy else [{"name": "shell", "arguments": {"command": "rm -rf ~/wichtig"}}] if self.wild else [
            {"name": "read", "arguments": {"path": "stats.py"}},
            {"name": "edit", "arguments": {"path": "stats.py", "oldString": "gibt es nicht", "newString": "x"}},
            {"name": "write", "arguments": {"path": "stats.py", "content": fixed}}]
        if n < len(plan) and messages[-1]["role"] != "user" or n == 0 and plan:
            yield {"type": "done", "message": {"role": "assistant", "content": "", "tool_calls": [{"function": plan[n]}]},
                   "stats": {"tokens": 20, "prompt_tokens": 500}}
        else:
            yield {"type": "token", "text": "Fertig."}
            yield {"type": "done", "message": {"role": "assistant", "content": "Fertig."}, "stats": {"tokens": 3}}


@needs_git
def test_run_measures_what_the_agent_did(cfg, tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    cfg.coding.verify = "off"  # die Prüf-Schranke hat eigene Tests – hier zählt der Lauf selbst
    r = run(bench.run_task(cfg, Solver(), BY_NAME["fix-bug"], keep=tmp_path / "keep"))
    assert r["solved"] and r["status"] == "ok" and r["steps"] == 4 and r["tool_calls"] == 3
    assert r["tools"] == {"read": 1, "edit": 1, "write": 1} and r["edit_fails"] == 1 and r["denied"] == 0
    assert r["tokens_written"] == 63 and r["tokens_read"] == 1500
    kept = next((tmp_path / "keep").iterdir())
    assert "ordered[mid - 1]" in (kept / "stats.py").read_text()  # das Projekt des Laufs bleibt auf Wunsch liegen
    lazy = run(bench.run_task(cfg, Solver(lazy=True), BY_NAME["fix-bug"]))
    assert not lazy["solved"] and lazy["steps"] == 1 and "failed" in lazy["why"]


@needs_git
def test_requests_outside_the_cage_rules_are_denied(cfg, tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    cfg.coding.verify = "off"
    home = tmp_path / "home"
    (home / "wichtig").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    r = run(bench.run_task(cfg, Solver(wild=True), BY_NAME["fix-bug"]))
    assert not r["solved"] and r["denied"] == 1 and (home / "wichtig").is_dir()  # Löschen fragt – und wird abgelehnt


@needs_git
def test_bench_report_and_file(cfg, tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    cfg.coding.verify = "off"
    seen = []
    result = run(bench.run_bench(cfg, Solver(), ["fix-bug", "find-code"], runs=2, progress=seen.append, model="Test 7B"))
    assert [(r["task"], r["run"], r["solved"]) for r in seen] == [
        ("fix-bug", 1, True), ("fix-bug", 2, True), ("find-code", 1, False), ("find-code", 2, False)]
    s = result["summary"]
    assert (s["solved"], s["total"], s["rate"], s["edit_fails"]) == (2, 4, 0.5, 4)  # auch bei find-code scheitert der Edit
    report = bench.format_report(result)
    assert "Test 7B" in report and "fix-bug" in report and "2/2" in report and "0/2" in report
    assert "2/4 gelöst (50%)" in report and "4 fehlgeschlagene Edits" in report
    assert "2/4 solved (50%)" in bench.format_report(result, en=True)
    path = bench.save(result, tmp_path / "out")
    assert path.name.endswith("-Test-7B.json") and '"rate": 0.5' in path.read_text()
    with pytest.raises(ValueError, match="Unbekannte Aufgabe: gibt-es-nicht"):
        run(bench.run_bench(cfg, Solver(), ["gibt-es-nicht"]))


def test_cli_lists_the_tasks(capsys, monkeypatch, tmp_path):
    from orbwise import cli
    monkeypatch.setenv("ORBWISE_CONFIG", str(tmp_path / "keine.yaml"))
    cli.main(["bench", "--list"])
    out = capsys.readouterr().out
    assert all(t.name in out for t in TASKS) and len(TASKS) == 10
    assert Path(bench.__file__).with_name("bench_tasks.py").is_file()
