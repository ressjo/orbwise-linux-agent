"""Coding-Modus nach OpenCode: Hintergrund-Jobs, Rückgängig je Antwort, Unteragenten, Rückfragen, Projektzone,
AGENTS.md im Systemprompt."""

import asyncio
import json
import shutil

import pytest
from conftest import run
from fastapi.testclient import TestClient

from orbwise import server, snapshot
from orbwise.agent import Agent
from orbwise.tools import jobs

WS = "ws://localhost:8765/ws"
ORIGIN = {"Origin": "http://localhost:8765"}


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    p = tmp_path / "proj"
    p.mkdir()
    (p / "app.py").write_text("x = 1\n", encoding="utf-8")
    return p


@pytest.fixture
def coding(cfg, llm, memory, project):
    agent = Agent(cfg, llm, memory)
    memory.switch_mode("coding")
    memory.set_project(str(project))
    return agent


def collect(agent, text, approve=True, events=None):
    events = [] if events is None else events

    async def emit(ev):
        events.append(ev)

    async def confirm(*a):
        return approve

    run(agent.run(text, emit, confirm))
    return events


def tool(name, **args):
    return f"/tool {name} " + json.dumps(args)


def test_agents_md_and_environment_in_the_system_prompt(coding, project):
    (project / "AGENTS.md").write_text("Tests mit `make check`.")
    system = coding.epoch_system()
    assert "Projektanweisungen (AGENTS.md)" in system and "make check" in system
    assert "Git-Repository: nein" in system and str(project) in system


@pytest.mark.skipif(not shutil.which("git"), reason="git fehlt")
def test_each_answer_can_be_undone_and_redone(cfg, coding, project, monkeypatch):
    coding.auto_mode = "files"
    collect(coding, tool("read", path="app.py"))  # erst lesen, dann ändern
    events = collect(coding, tool("edit", path="app.py", oldString="x = 1", newString="x = 2"))
    change = next(e for e in events if e["type"] == "changes")
    assert change["files"] == [{"path": "app.py", "add": 1, "del": 1, "status": "modified"}]
    assert (project / "app.py").read_text() == "x = 2\n"
    snap = coding.memory.conversation.meta["snapshots"][str(change["turn"])]
    assert snapshot.restore(str(project), snap["before"], ["app.py"]) == ["app.py"]
    assert (project / "app.py").read_text() == "x = 1\n"
    # eine Antwort ohne Dateiänderung meldet nichts
    assert not any(e["type"] == "changes" for e in collect(coding, tool("read", path="app.py")))


@pytest.mark.skipif(not shutil.which("git"), reason="git fehlt")
def test_revert_endpoint_restores_and_redoes(cfg, project, monkeypatch):
    monkeypatch.setenv("ORBWISE_FAKE_LLM", "1")
    monkeypatch.setenv("ORBWISE_SKIP_WARMUP", "1")
    client = TestClient(server.create_app(cfg), base_url="http://localhost:8765")
    with client, client.websocket_connect(WS, headers=ORIGIN) as ws:
        client.post("/api/mode", json={"mode": "coding"})
        chat = client.get("/api/history").json()["chat"]["id"]
        client.post(f"/api/chats/{chat}/project", json={"path": str(project)})
        ws.send_json({"type": "auto_mode", "mode": "files"})
        ws.send_json({"type": "user_message", "text": tool("write", path="neu.txt", content="hallo\n")})
        for _ in range(300):
            if ws.receive_json()["type"] == "assistant_end":
                break
        assert (project / "neu.txt").exists()
        msg = [m for m in client.get("/api/history").json()["messages"] if m.get("changes")]
        assert msg and msg[-1]["changes"]["files"][0]["path"] == "neu.txt"
        r = client.post("/api/chats/active/revert", json={"turn": msg[-1]["turn"]})
        assert r.status_code == 200 and r.json()["undone"] and not (project / "neu.txt").exists()
        r = client.post("/api/chats/active/revert", json={"turn": msg[-1]["turn"], "redo": True})
        assert r.status_code == 200 and (project / "neu.txt").read_text() == "hallo\n"
        assert client.post("/api/chats/active/revert", json={"turn": 99}).status_code == 404
        notes = client.app.state.hub.agent.memory.conversation.meta["pending_notes"]
        assert "rückgängig" in notes[0] and "wiederhergestellt" in notes[1]


def test_project_files_are_free_from_files_mode_outside_asks(coding, project, tmp_path):
    coding.auto_mode = "files"
    hidden = project / ".github"
    hidden.mkdir()
    collect(coding, tool("read", path="app.py"))  # eine bestehende Datei ersetzt write erst nach dem Lesen
    for path in ("app.py", ".github/ci.yml", "neu/datei.txt"):
        ev = collect(coding, tool("write", path=path, content="x"), approve=False)
        assert next(e for e in ev if e["type"] == "tool_call")["risk"] == "safe", path
    for path in (str(tmp_path / "draussen.txt"), ".git/config", ".env"):
        ev = collect(coding, tool("write", path=path, content="x"), approve=False)
        assert next(e for e in ev if e["type"] == "tool_call")["risk"] != "safe", path
    coding.auto_mode = "read"
    ev = collect(coding, tool("edit", path="app.py", oldString="x", newString="y"), approve=False)
    call = next(e for e in ev if e["type"] == "tool_call")
    assert call["risk"] == "confirm" and "-x" in call["preview"]  # Diff fürs Bestätigungsfenster


def test_project_zone_never_while_root(coding, monkeypatch):
    from orbwise import agent_safety
    monkeypatch.setattr(agent_safety, "running_as_root", lambda: True)
    coding.auto_mode = "files"
    ev = collect(coding, tool("write", path="app.py", content="x"), approve=False)
    assert next(e for e in ev if e["type"] == "tool_call")["risk"] == "confirm"


def test_background_job_reports_back_without_polling(coding, project):
    async def scenario():
        events = []

        async def emit(ev):
            events.append(ev)

        async def confirm(*a):
            return True
        coding.auto_mode = "auto"
        await coding.run(tool("shell", command="sleep 0.2; echo fertig-xyz", background=True), emit, confirm)
        started = next(e for e in events if e["type"] == "tool_result")
        assert "Hintergrund als Job" in started["text"]
        jid = next(e for e in events if e["type"] == "job" and e["phase"] == "start")["id"]
        for _ in range(50):
            if jobs.JOBS[jid].status != "running":
                break
            await asyncio.sleep(0.1)
        assert jobs.JOBS[jid].rc == 0
        await coding.run(tool("read", path="app.py"), emit, confirm)
        # zwischen zwei Fragen fertig geworden → steht in der Kontext-Notiz der nächsten Frage
        note = [m for m in coding.memory.conversation.history if m["role"] == "user"][-1]["note"]
        assert "fertig-xyz" in note and "Exit-Code 0" in note
        assert any(e["type"] == "job" and e["phase"] == "done" for e in events)
    jobs.take_notes()
    run(scenario())


def test_job_limit_and_stop(project):
    async def scenario():
        started = [await jobs.start("sleep 30", str(project), limit=2) for _ in range(2)]
        with pytest.raises(RuntimeError):
            await jobs.start("sleep 30", str(project), limit=2)
        for job in started:
            jobs.stop(job.id)
        for job in started:
            await asyncio.wait_for(job.task, 5)
        assert all(j.status == "stopped" for j in started)
    run(scenario())
    jobs.take_notes()


def test_explore_subagent_reads_but_never_writes(coding, project):
    task = tool("read", path="app.py")
    events = collect(coding, tool("subagent", agent="explore", description="Datei ansehen", prompt=task))
    subs = [e for e in events if e.get("sub")]
    assert any(e["type"] == "tool_call" and e["name"] == "read" for e in subs)
    result = next(e for e in events if e["type"] == "tool_result" and not e.get("sub"))
    assert "[explore: Datei ansehen]" in result["text"] and "x = 1" in result["text"]
    # Schreiben ist für explore nicht verfügbar
    task = tool("write", path="app.py", content="kaputt")
    collect(coding, tool("subagent", agent="explore", description="Schreibversuch", prompt=task))
    assert (project / "app.py").read_text() == "x = 1\n"
    # der Verlauf des Unteragenten landet nicht im Chat
    assert not any(m.get("tool_name") == "read" for m in coding.memory.conversation.history)


def test_question_waits_for_the_answer(coding):
    asked = []

    async def ask(call_id, questions):
        asked.append(questions)
        return [["Variante B"]]
    coding.services["ask"] = ask
    events = collect(coding, tool("question", questions=[{"question": "Welche Variante?", "header": "Weg",
                                                          "options": [{"label": "Variante A"},
                                                                      {"label": "Variante B"}]}]))
    assert asked[0][0]["options"][1]["label"] == "Variante B"
    result = next(e for e in events if e["type"] == "tool_result")
    assert "Welche Variante? → Variante B" in result["text"]


def test_question_round_trip_over_the_websocket(cfg, monkeypatch):
    monkeypatch.setenv("ORBWISE_FAKE_LLM", "1")
    monkeypatch.setenv("ORBWISE_SKIP_WARMUP", "1")
    client = TestClient(server.create_app(cfg), base_url="http://localhost:8765")
    with client, client.websocket_connect(WS, headers=ORIGIN) as ws:
        client.post("/api/mode", json={"mode": "coding"})
        ws.send_json({"type": "user_message",
                      "text": tool("question", questions=[{"question": "Sprache?", "options": [{"label": "Python"}]}])})
        for _ in range(300):
            ev = ws.receive_json()
            if ev["type"] == "question_request":
                break
        assert ev["questions"][0]["question"] == "Sprache?"
        ws.send_json({"type": "question_answer", "id": ev["id"], "answers": [["Python"]]})
        for _ in range(300):
            ev = ws.receive_json()
            if ev["type"] == "tool_result":
                break
        assert "Sprache? → Python" in ev["text"]


class EditAndTest:
    """Ändert die Datei und lässt danach jedes Mal denselben Testbefehl laufen – wie ein Modell, das sich an eine
    Lösung herantastet. loop=True: ruft stattdessen nur immer wieder denselben Befehl auf."""

    def __init__(self, rounds: int, loop: bool = False):
        self.rounds, self.loop = rounds, loop

    async def chat_stream(self, messages, tools=None):
        n = sum(1 for m in messages if m["role"] == "tool")
        test = {"name": "shell", "arguments": {"command": "cat app.py"}}
        if self.loop:
            call = test
        elif n == 0:
            call = {"name": "read", "arguments": {"path": "app.py"}}
        elif n <= 2 * self.rounds:
            k = (n + 1) // 2
            call = test if n % 2 == 0 else {"name": "edit", "arguments": {
                "path": "app.py", "oldString": f"x = {k}", "newString": f"x = {k + 1}"}}
        else:
            call = None
        if call and tools is not None and messages[-1]["role"] != "user" or call and n == 0:
            yield {"type": "done", "message": {"role": "assistant", "content": "",
                                                "tool_calls": [{"function": call}]}, "stats": {}}
        else:
            yield {"type": "token", "text": "Fertig."}
            yield {"type": "done", "message": {"role": "assistant", "content": "Fertig."}, "stats": {}}


def test_same_command_after_each_edit_is_not_a_loop(cfg, memory, project):
    cfg.coding.verify = "off"
    agent = Agent(cfg, EditAndTest(rounds=5), memory)
    memory.switch_mode("coding")
    memory.set_project(str(project))
    agent.auto_mode = "auto"
    collect(agent, "Zähl x hoch")
    results = [m["content"] for m in memory.conversation.history if m.get("tool_name") == "shell"]
    assert len(results) == 5 and not any("bereits ausgeführt" in r for r in results)
    assert "x = 6" in results[-1] and (project / "app.py").read_text() == "x = 6\n"


def test_same_command_without_changes_is_still_stopped(cfg, memory, project):
    cfg.coding.verify = "off"
    agent = Agent(cfg, EditAndTest(rounds=0, loop=True), memory)
    memory.switch_mode("coding")
    memory.set_project(str(project))
    agent.auto_mode = "auto"
    collect(agent, "Zeig die Datei")
    results = [m["content"] for m in memory.conversation.history if m.get("tool_name") == "shell"]
    assert len(results) == 4 and ["bereits ausgeführt" in r for r in results] == [False, False, True, True]


class SubScript:
    """Hauptagent: startet einen explore-Unteragenten und antwortet mit dessen Ergebnis. Unteragent: stellt die
    Aufrufe aus `plan` (Schritt → Liste von Aufrufen; der letzte Eintrag gilt für alle weiteren Schritte), bis er ohne
    Werkzeuge antworten soll. overflow: ab so vielen Zeichen Prompt meldet der „Server“ Kontextüberlauf."""

    def __init__(self, plan, overflow=0, fail_after=0):
        self.plan, self.overflow, self.fail_after = plan, overflow, fail_after
        self.sub_prompts: list[int] = []  # Größe jedes Unteragenten-Prompts in Zeichen
        self.sub_tools: list[bool] = []

    async def chat_stream(self, messages, tools=None):
        sub = "Unteragent" in messages[0]["content"]
        n = sum(1 for m in messages if m["role"] == "assistant")
        calls, text = None, "Fertig."
        if not sub:
            if messages[-1]["role"] == "tool":
                text = messages[-1]["content"]
            else:
                calls = [{"name": "subagent", "arguments": {"agent": "explore", "description": "Suche",
                                                             "prompt": "Finde x"}}]
        else:
            size = len(json.dumps(messages, ensure_ascii=False))
            self.sub_prompts.append(size)
            self.sub_tools.append(tools is not None)
            if self.overflow and size > self.overflow:
                from orbwise.llm import ContextOverflow
                raise ContextOverflow("zu groß", n_ctx=1000, n_prompt=2000)
            if self.fail_after and n >= self.fail_after:
                from orbwise.llm import LLMError
                raise LLMError("Server weg")
            if tools is not None:
                calls = self.plan[min(n, len(self.plan) - 1)]
                text = f"Notiz {n + 1}: x steht in app.py."
            else:
                text = "Ergebnis: x = 1 in app.py."
        msg = {"role": "assistant", "content": text if not calls or sub else ""}
        if calls:
            msg["tool_calls"] = [{"function": c} for c in calls]
        else:
            yield {"type": "token", "text": text}
        yield {"type": "done", "message": msg, "stats": {}}


def _sub_run(cfg, memory, project, llm, budget=0, monkeypatch=None):
    agent = Agent(cfg, llm, memory)
    memory.switch_mode("coding")
    memory.set_project(str(project))
    if budget:
        monkeypatch.setattr(agent, "context_budget", lambda: budget)
        monkeypatch.setattr(agent, "output_chars", lambda: 6000)
    events = collect(agent, "Such x")
    result = next(e for e in events if e["type"] == "tool_result" and not e.get("sub"))["text"]
    reads = sum(1 for e in events if e.get("sub") and e["type"] == "tool_call")
    return result, reads


def test_subagent_stops_repeating_the_same_call(cfg, memory, project):
    llm = SubScript([[{"name": "read", "arguments": {"path": "app.py"}}]])
    result, reads = _sub_run(cfg, memory, project, llm)
    # zweimal ausgeführt, zweimal übersprungen, dann ohne Werkzeuge zusammengefasst – nicht bis zum Schritt-Limit
    assert reads == 2 and llm.sub_tools == [True, True, True, True, False]
    assert "Ergebnis: x = 1 in app.py." in result and "abgebrochen" not in result


def test_subagent_shrinks_old_results_instead_of_overflowing(cfg, memory, project, monkeypatch):
    for i in range(4):
        (project / f"big{i}.py").write_text(f"# Datei {i}\n" + "wert = 'abcdefghij'\n" * 250)
    step = [{"name": "read", "arguments": {"path": f"big{i}.py"}} for i in range(4)]
    llm = SubScript([step, [{"name": "grep", "arguments": {"pattern": "Datei"}}]])
    budget = 6000 + 2000  # Werkzeugbeschreibungen von explore (≈ 2000 geschätzte Token) + 6000 für den Verlauf
    result, reads = _sub_run(cfg, memory, project, llm, budget=budget, monkeypatch=monkeypatch)
    assert reads == 4 and "Ergebnis: x = 1 in app.py." in result and "abgebrochen" not in result
    assert max(llm.sub_prompts) // 3 <= budget  # vier Dateien auf einmal hätten das Fenster gesprengt


def test_subagent_survives_a_server_overflow(cfg, memory, project):
    (project / "big.py").write_text("wert = 'abcdefghij'\n" * 200)
    llm = SubScript([[{"name": "read", "arguments": {"path": "big.py"}}],
                     [{"name": "read", "arguments": {"path": "app.py"}}]], overflow=4000)
    result, _ = _sub_run(cfg, memory, project, llm)
    assert "Ergebnis: x = 1 in app.py." in result and "abgebrochen" not in result
    assert llm.sub_tools[-1] is False and llm.sub_prompts[-1] <= 4000  # gekürzt und ohne Werkzeuge zusammengefasst


def test_subagent_failure_keeps_what_it_found(cfg, memory, project):
    llm = SubScript([[{"name": "read", "arguments": {"path": "app.py"}}],
                     [{"name": "glob", "arguments": {"pattern": "*.py"}}]], fail_after=2)
    result, _ = _sub_run(cfg, memory, project, llm)
    assert "Notiz 1: x steht in app.py." in result and "Unteragent abgebrochen: Server weg" in result
