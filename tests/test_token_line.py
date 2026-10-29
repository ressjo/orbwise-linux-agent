"""Token der Sitzung (dieses Chats): gezählt je Modellschritt, im Chat gespeichert, nach dem Neuladen wieder da."""

from conftest import run

from orbwise.agent import Agent


def test_chat_counts_its_tokens(cfg, llm, memory):
    agent = Agent(cfg, llm, memory)
    events = []

    async def emit(ev):
        events.append(ev)

    async def confirm(*a):
        return True
    run(agent.run("Hallo, wie geht es?", emit, confirm))
    first = agent.chat_usage()
    assert first["read"] == 800 and first["written"] > 0 and first["requests"] == 1  # Demo-Modell: 800 gelesen
    assert [e for e in events if e["type"] == "usage"][-1]["read"] == 800
    run(agent.run("Und sonst?", emit, confirm))
    assert agent.chat_usage()["read"] == 1600 and agent.chat_usage()["requests"] == 2
    memory.conversation.save()
    chat = memory.conversation.chat_id
    memory.new_chat()
    assert agent.chat_usage()["read"] == 0  # neuer Chat zählt neu
    memory.switch_chat(chat)
    assert agent.chat_usage()["read"] == 1600  # gespeichert


def test_history_brings_the_usage_back(cfg, monkeypatch):
    from fastapi.testclient import TestClient

    from orbwise.server import create_app
    monkeypatch.setenv("ORBWISE_FAKE_LLM", "1")
    monkeypatch.setenv("ORBWISE_SKIP_WARMUP", "1")
    with TestClient(create_app(cfg), base_url="http://localhost:8765") as client:
        memory = client.app.state.memory
        memory.conversation.meta["usage"] = {"read": 1200, "cached": 300, "written": 90, "requests": 3}
        assert client.get("/api/history").json()["usage"] == {"read": 1200, "cached": 300, "written": 90,
                                                              "requests": 3}
