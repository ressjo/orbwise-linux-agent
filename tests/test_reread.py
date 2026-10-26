"""Warum neu eingelesen? Jeder Aufruf wird mit dem vorigen verglichen – und in großen Fenstern bleibt die
Werkzeugliste fest, damit nicht bei jeder neuen Frage der ganze Chat neu eingelesen werden muss."""

from conftest import run
from test_prefill_speed import Server, _noop, _tools
from test_prompt_size import all_integrations

from orbwise.agent import Agent
from orbwise.reread import compare, fingerprint, with_server

TOOLS = [{"type": "function", "function": {"name": "read_file", "parameters": {}}}]
WEB = [*TOOLS, {"type": "function", "function": {"name": "web_search", "parameters": {}}}]
CHAT = [{"role": "system", "content": "Du bist Orbwise." * 200},
        {"role": "user", "content": "Lies die Datei"},
        {"role": "assistant", "content": "", "tool_calls": [{"function": {"name": "read_file", "arguments": {}}}]},
        {"role": "tool", "content": "x" * 30000, "tool_name": "read_file"}]


def fp(messages, tools=TOOLS, owner=("m", "c1", 0)):
    return fingerprint(owner, messages, tools)


def test_reasons():
    old = fp(CHAT)
    assert compare(old, None)["reason"] == "first"
    grown = compare(fp([*CHAT, {"role": "assistant", "content": "fertig"}, {"role": "user", "content": "und?"}]), old)
    assert grown["reason"] == "appended" and grown["common"] == old.ends[-1] > 10000
    tools = compare(fp(CHAT, WEB), old)
    assert tools["reason"] == "tools" and tools["added"] == ["web_search"] and tools["common"] == 0
    cleared = [*CHAT[:3], {**CHAT[3], "content": "[ausgeblendet]"}, {"role": "user", "content": "und?"}]
    r = compare(fp(cleared), old)
    assert r["reason"] == "cleared" and r["at"] == 3 and 0 < r["common"] < old.ends[-1]
    edited = [CHAT[0], {"role": "user", "content": "Lies die andere Datei"}]
    assert compare(fp(edited), old)["reason"] == "edited"
    assert compare(fp([{**CHAT[0], "content": "anders"}, *CHAT[1:]]), old)["reason"] == "system"
    assert compare(fp(CHAT, owner=("m", "c2", 0)), old)["reason"] == "chat"
    assert compare(fp(CHAT, owner=("m", "c1", 1)), old)["reason"] == "compact"
    assert compare(fp(CHAT, owner=("other", "c1", 0)), old)["reason"] == "model"
    thinking = fingerprint(("m", "c1", 0), CHAT, TOOLS, mode=(True, "high"))
    assert compare(thinking, old)["reason"] == "think"  # die Vorlage rendert mit Denken einen anderen Anfang
    assert compare(fingerprint(("m", "c1", 0), CHAT, TOOLS, mode=(True, "high")), thinking)["reason"] == "appended"
    # Vorwärmen endet mit einer leeren Nutzernachricht – die echte Frage steht dann an ihrer Stelle
    warm = fp([*CHAT, {"role": "user", "content": ""}])
    assert compare(fp([*CHAT, {"role": "user", "content": "Und jetzt?"}]), warm)["reason"] == "appended"


def test_server_that_lost_the_cache():
    diff = {"reason": "appended", "common": 50000}
    assert with_server(diff, {"prompt_tokens": 52000, "prompt_cached": 0})["reason"] == "server"
    assert with_server(diff, {"prompt_tokens": 52000, "prompt_cached": 0}, api_used=True)["reason"] == "api"
    ok = with_server(diff, {"prompt_tokens": 300, "prompt_cached": 49800})
    assert ok["reason"] == "appended" and ok["new"] == 300
    assert with_server({"reason": "tools", "common": 0}, {"prompt_tokens": 60000})["reason"] == "tools"


class ForgetfulServer(Server):
    """Meldet wie llama-server, wie viel aus dem Cache kam – hier: nichts (z. B. Hybrid-Modell ohne Checkpoint)."""

    async def chat_stream(self, messages, tools=None, **kw):
        async for ev in super().chat_stream(messages, tools, **kw):
            if ev["type"] == "done":
                ev["stats"]["prompt_cached"] = 0
            yield ev


def test_steps_report_the_reason(cfg, memory, tmp_path):
    events = []

    async def emit(ev):
        events.append(ev)
    agent = Agent(cfg, ForgetfulServer(tps=800), memory)
    agent._prefill_tps["llama"] = 800.0
    run(agent.run("Hallo", emit, _noop))
    run(agent.run("Und noch was", emit, _noop))
    done = [e for e in events if e.get("type") == "llm_phase" and e.get("phase") == "done" and "reread" in e]
    assert done[0]["reread"]["reason"] == "first"
    # zweite Frage: Prompt nur angehängt, aber der (gespielte) Server nahm nichts aus dem Cache → „server“
    assert done[-1]["reread"]["reason"] == "server" and done[-1]["reread"]["new"] > 0


def test_big_window_keeps_all_tools_on_a_normal_card(cfg, memory, tmp_path):
    all_integrations(cfg, tmp_path)
    agent = Agent(cfg, Server(window=65536), memory)
    agent._prefill_tps["llama"] = agent.all_schema_tokens / 25  # 25 s für alle Werkzeuge: über 10 s, unter 60 s
    agent.choose_tools(set())
    assert agent.memory.conversation.epoch["tools"] is None and "paperless_search" in _tools(agent)
    small = Agent(cfg, Server(window=16384), memory)  # kleines Fenster: weiter die Auswahl
    small._prefill_tps["llama"] = small.all_schema_tokens / 25
    small.choose_tools(set())
    assert "load_tools" in _tools(small)
