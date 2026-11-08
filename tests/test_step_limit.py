"""Schritt-Limit je Antwort in den Einstellungen: Tools/Coding getrennt, auch unbegrenzt (0), bleibt nach Neustart."""

import json

from fastapi.testclient import TestClient

from orbwise import step_limit
from orbwise.agent import Agent
from orbwise.config import Config
from tests.test_agent import ToolHappyLLM, _run_with


def test_unlimited_runs_until_the_model_is_done(cfg, memory, monkeypatch):
    executed = []

    async def fake_exec(self, call, emit, confirm):
        executed.append(call)
        return "system_info", f"ok {len(executed)}", "system_info: ok"

    monkeypatch.setattr(Agent, "_execute", fake_exec)
    cfg.tools.max_steps = 0

    class Long(ToolHappyLLM):  # 40 verschiedene Werkzeug-Runden, dann die Antwort
        async def chat_stream(self, messages, tools=None):
            self.calls += 1
            if self.calls > 40:
                yield {"type": "token", "text": "Fertig."}
                yield {"type": "done", "message": {"role": "assistant", "content": "Fertig."}, "stats": {}}
                return
            yield {"type": "done", "message": {"role": "assistant", "content": "", "tool_calls": [
                {"function": {"name": "system_info", "arguments": {"i": self.calls}}}]}, "stats": {}}

    llm = Long()
    answer, _, _ = _run_with(cfg, memory, llm)
    assert len(executed) == 40 and "Fertig." in answer and "mach weiter" not in answer
    assert Agent(cfg, llm, memory).max_steps() == 0


def test_config_accepts_zero_and_caps_large_values():
    c = Config.model_validate({"tools": {"max_steps": 0}, "coding": {"max_steps": 9999}})
    assert c.tools.max_steps == 0 and c.coding.max_steps == 500
    assert Config.model_validate({"tools": {"max_steps": -3}}).tools.max_steps == 0


def test_api_sets_limits_and_they_survive_a_restart(cfg, monkeypatch):
    monkeypatch.setenv("ORBWISE_FAKE_LLM", "1")
    monkeypatch.setenv("ORBWISE_SKIP_WARMUP", "1")
    from orbwise.server import create_app
    with TestClient(create_app(cfg), base_url="http://localhost:8765") as client:
        assert client.get("/api/steps").json() == {"tools": 25, "coding": 60, "max": 500}
        assert client.post("/api/steps", json={"tools": 0}).json()["tools"] == 0
        assert client.post("/api/steps", json={"coding": 120}).json() == {"tools": 0, "coding": 120, "max": 500}
        assert client.post("/api/steps", json={"tools": "viele"}).status_code == 400
        assert client.post("/api/steps", json={"tools": [1]}).status_code == 400
        assert cfg.tools.max_steps == 0 and cfg.coding.max_steps == 120
    state = cfg.memory.dir.parent / "state.json"
    assert json.loads(state.read_text())["max_steps"] == {"tools": 0, "coding": 120}
    fresh = Config()
    fresh.memory.dir = cfg.memory.dir
    step_limit.apply_saved(fresh, state)
    assert (fresh.tools.max_steps, fresh.coding.max_steps) == (0, 120)
