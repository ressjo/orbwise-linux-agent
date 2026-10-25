"""Denk-Wähler: Stufen nur bei Modellen mit echten Stufen, sonst an/aus – oder gar nicht, wenn das Modell nicht denkt."""

import httpx
import pytest
from conftest import run
from fastapi.testclient import TestClient

from orbwise.agent import Agent
from orbwise.config import LLMConfig, ProfileConfig
from orbwise.llm import OllamaLLM, OpenAICompatLLM, template_levels, template_thinking


def test_template_decides_the_caps():
    assert template_thinking("{% if reasoning_effort %}…") == "toggle"  # erwähnt, aber keine Stufen genannt
    assert template_thinking("{% if reasoning_effort == 'low' %}…{% elif reasoning_effort == 'high' %}") == "levels"
    assert template_thinking("{%- if enable_thinking is false %}<think>\n\n</think>") == "toggle"  # Qwen3
    assert template_thinking("{{ message.reasoning_content }}") == "toggle"
    assert template_thinking("<|start_header_id|>{{ role }}") == "none"  # Llama 3
    assert template_thinking("") is None and template_thinking(None) is None


def test_levels_come_from_the_model():
    gpt_oss = '{%- if not reasoning_effort is defined %}{%- set reasoning_effort = "medium" %}{%- endif %}'
    assert template_levels(gpt_oss) == ([], None)  # nennt nur eine Voreinstellung – keine Stufen erfinden
    assert template_levels(gpt_oss, "gpt-oss-20b") == (["low", "medium", "high"], "medium")  # dokumentiert
    own = "{% if reasoning_effort in ['minimal', 'low', 'medium', 'high'] %}{{ reasoning_effort | default('low') }}"
    assert template_levels(own) == (["minimal", "low", "medium", "high"], "low")
    two = "{% if reasoning_effort == 'high' %}lang{% elif reasoning_effort == 'low' %}kurz{% endif %}"
    assert template_levels(two) == (["low", "high"], None)  # genau die zwei – nichts ergänzt
    assert template_levels("{% if reasoning_effort == 'max' %}") == ([], None)  # eine Stufe ist keine Auswahl
    assert template_levels("{% if enable_thinking %}<think>") == ([], None)  # nur an/aus – keine Stufen


def test_ollama_caps_from_show():
    def client(caps, model="qwen3:8b"):
        def handler(req):
            return httpx.Response(200, json={} if caps is None else {"capabilities": caps})
        return OllamaLLM(LLMConfig(model=model), transport=httpx.MockTransport(handler))
    assert run(client(["completion", "tools", "thinking"]).thinking_caps()) == "toggle"
    assert run(client(["completion", "thinking"], "gpt-oss:20b").thinking_caps()) == "levels"
    assert run(client(["completion", "tools"]).thinking_caps()) == "none"
    assert run(client(None).thinking_caps()) is None  # altes Ollama ohne capabilities


def test_llama_server_caps_from_props():
    def client(props, model="qwen"):
        def handler(req):
            assert req.url.path == "/props"
            return httpx.Response(200, json=props)
        p = ProfileConfig(backend="openai", base_url="http://llm/v1", model=model)
        return OpenAICompatLLM(p, transport=httpx.MockTransport(handler))
    assert run(client({"chat_template": "{% if enable_thinking %}"}).thinking_caps()) == "toggle"
    assert run(client({"chat_template": "x"}, model="gpt-oss-120b").thinking_caps()) == "levels"
    assert run(client({}).thinking_caps()) is None


@pytest.mark.parametrize("caps,think,want", [
    ("levels", "low", (True, "low")),
    ("toggle", "low", (True, None)),  # keine erfundene Stufe, kein Stufen-Budget
    ("none", "high", (False, None)),
    (None, "medium", (True, None)),  # unbekannt: nur an/aus – keine Stufen von Orbwise
    ("levels", "max", (True, None)),  # Stufe, die das Modell nicht kennt → seine Voreinstellung
    ("levels", "on", (True, None)),
    ("toggle", True, (True, None)),
])
def test_agent_fits_think_to_the_model(cfg, llm, memory, caps, think, want):
    llm.think_caps = caps
    agent = Agent(cfg, llm, memory)
    agent._set_think(think)
    run(agent._fit_think())
    assert (agent._think, agent._think_level) == want


def test_toggle_model_gets_no_level_and_no_cut(cfg, llm, memory):
    llm.think_caps = "toggle"
    llm.thought_repeat = 200  # lange Denkkette
    agent = Agent(cfg, llm, memory)

    async def nothing(*a, **k):
        return None
    run(agent.run("Wie spät ist es?", nothing, nothing, think="low"))
    assert llm.opts[0]["think"] is True and llm.opts[0]["effort"] is None
    assert len(llm.opts) == 1  # kein Abbruch durch ein Stufen-Budget


def test_model_without_thinking_runs_without(cfg, llm, memory):
    llm.think_caps = "none"
    agent = Agent(cfg, llm, memory)

    async def nothing(*a, **k):
        return None
    run(agent.run("Hallo", nothing, nothing, think="high"))
    assert llm.opts[0]["think"] is False


def test_api_reports_caps(cfg, monkeypatch):
    monkeypatch.setenv("ORBWISE_FAKE_LLM", "1")
    monkeypatch.setenv("ORBWISE_SKIP_WARMUP", "1")
    monkeypatch.setenv("ORBWISE_FAKE_THINK", "toggle")
    from orbwise.server import create_app
    client = TestClient(create_app(cfg), base_url="http://localhost:8765")
    assert client.get("/api/think").json() == {"caps": "toggle", "levels": [], "default": None, "evidence": []}


def test_api_reports_the_models_own_levels(cfg, monkeypatch):
    monkeypatch.setenv("ORBWISE_FAKE_LLM", "1")
    monkeypatch.setenv("ORBWISE_SKIP_WARMUP", "1")
    monkeypatch.delenv("ORBWISE_FAKE_THINK", raising=False)
    from orbwise.server import create_app
    client = TestClient(create_app(cfg), base_url="http://localhost:8765")
    assert client.get("/api/think").json() == {"caps": "levels", "levels": ["low", "medium", "high"],
                                               "default": "medium", "evidence": []}


def test_template_evidence_shows_where_levels_come_from():
    from orbwise.llm import template_evidence
    t = "a\n{%- if reasoning_effort == 'high' %}\nb\n{%- elif reasoning_effort == 'low' %}\n" + "x" * 300
    assert template_evidence(t) == ["{%- if reasoning_effort == 'high' %}", "{%- elif reasoning_effort == 'low' %}"]
    assert template_evidence(None) == []
