"""Ziel-Modus (/goal): arbeitet ohne „mach weiter“, bis das Modell [ZIEL ERREICHT] meldet (oder eine Grenze greift)."""

import json

from conftest import run

from orbwise.agent import Agent, goal_marker


def _scripted(llm, replies):
    """chat_stream liefert der Reihe nach die vorgegebenen Antworten: str = Text, dict = Werkzeugaufruf."""
    seen = []

    async def stream(messages, tools=None, **kw):
        seen.append(messages)
        reply = replies[min(len(seen), len(replies)) - 1]
        if isinstance(reply, dict):
            msg = {"role": "assistant", "content": "", "tool_calls": [{"function": reply}]}
        else:
            msg = {"role": "assistant", "content": reply}
            yield {"type": "token", "text": reply}
        yield {"type": "done", "message": msg, "stats": {}}
    llm.chat_stream = stream
    return seen


def _go(agent, text):
    events = []

    async def emit(ev):
        events.append(ev)

    async def confirm(*a):
        return True
    answer = run(agent.run(text, emit, confirm))
    return answer, events


def test_marker_parsing():
    assert goal_marker("Fertig, alles getestet.\n[ZIEL ERREICHT]") == ("done", "Fertig, alles getestet.")
    assert goal_marker("Welche Farbe?\n[QUESTION]  ") == ("question", "Welche Farbe?")
    assert goal_marker("Zwischenstand") == ("", "Zwischenstand")


def test_goal_keeps_going_until_reached(cfg, llm, memory, tmp_path):
    agent = Agent(cfg, llm, memory)
    agent.set_goal("Baue die Seite")
    find = {"name": "find_files", "arguments": {"query": "x", "path": str(tmp_path)}}
    seen = _scripted(llm, [find, "Erster Teil steht, soll ich weitermachen?", find, "Alles fertig und geprüft.\n[ZIEL ERREICHT]"])
    answer, events = _go(agent, "Baue die Seite")
    assert "Alles fertig und geprüft." in answer and "[ZIEL ERREICHT]" not in answer
    assert agent.goal_state() is None and len(seen) == 4
    nudges = [m for m in memory.conversation.history if m.get("goal_nudge")]
    assert len(nudges) == 1 and "Baue die Seite" in nudges[0]["content"]
    assert [e["state"] for e in events if e["type"] == "goal"] == ["progress", "done"]
    assert "Ziel-Modus – Ziel: Baue die Seite" in json.dumps(seen[-1], ensure_ascii=False)  # Kontext-Notiz


def test_goal_ignores_the_normal_step_limit(cfg, llm, memory, tmp_path):
    cfg.tools.max_steps = 3
    agent = Agent(cfg, llm, memory)
    agent.set_goal("Lange Aufgabe")
    calls = [{"name": "find_files", "arguments": {"query": f"q{i}", "path": str(tmp_path)}} for i in range(6)]
    _scripted(llm, [*calls, "Erledigt.\n[ZIEL ERREICHT]"])
    answer, _ = _go(agent, "Lange Aufgabe")
    assert "mach weiter" not in answer and "Erledigt." in answer


def test_goal_limit_pauses_and_question_keeps_the_goal(cfg, llm, memory, tmp_path):
    cfg.coding.goal_max_steps = 2
    agent = Agent(cfg, llm, memory)
    agent.set_goal("Endlos")
    _scripted(llm, [{"name": "find_files", "arguments": {"query": f"q{i}", "path": str(tmp_path)}} for i in range(5)])
    answer, events = _go(agent, "Endlos")
    assert "/goal weiter" in answer and agent.goal_state() is not None
    assert any(e["type"] == "goal" and e["state"] == "limit" for e in events)
    agent.set_goal("Farbe wählen")
    _scripted(llm, ["Blau oder Grün?\n[FRAGE]"])
    _go(agent, "Farbe wählen")
    assert agent.goal_state()["text"] == "Farbe wählen"


def test_without_goal_nothing_changes(cfg, llm, memory):
    agent = Agent(cfg, llm, memory)
    _scripted(llm, ["Soll ich weitermachen?"])
    answer, events = _go(agent, "Hallo")
    assert answer == "Soll ich weitermachen?" and not any(e["type"] == "goal" for e in events)


def test_goal_command_in_the_hub(cfg, monkeypatch):
    from fastapi.testclient import TestClient

    from orbwise import server
    monkeypatch.setenv("ORBWISE_FAKE_LLM", "1")
    monkeypatch.setenv("ORBWISE_SKIP_WARMUP", "1")
    app = server.create_app(cfg)
    with TestClient(app, base_url="http://localhost:8765") as client:
        with client.websocket_connect("ws://localhost:8765/ws", headers={"Origin": "http://localhost:8765"}) as ws:
            assert ws.receive_json()["goal"] is None
        hub = app.state.hub
        assert run(hub.goal_command("Teste alles")) == "Teste alles" and hub.agent.goal_state()["text"] == "Teste alles"
        assert run(hub.goal_command("")) == "" and hub.agent.goal_state()
        assert run(hub.goal_command("weiter")) and hub.agent.goal_state()["steps"] == 0
        assert run(hub.goal_command("stop")) == "" and hub.agent.goal_state() is None
        assert client.get("/api/history").status_code == 200
