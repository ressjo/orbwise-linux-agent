"""Tools- und Coding-Modus: getrennte Chats, schlankere Werkzeugliste, Projektordner, keine Sprache."""

import pytest
from conftest import run
from fastapi.testclient import TestClient

from orbwise import server
from orbwise.agent import CODING_GROUPS, Agent
from orbwise.memory.chats import ChatStore

WS = "ws://localhost:8765/ws"
ORIGIN = {"Origin": "http://localhost:8765"}


def test_each_mode_has_its_own_chats(tmp_path):
    store = ChatStore(tmp_path)
    tools = store.create()
    tools.add({"role": "user", "content": "Wetter?"})
    tools.save()
    code = store.create(mode="coding")
    code.add({"role": "user", "content": "Bau mir ein Skript"})
    code.save()
    assert store.active_id("tools") == tools.chat_id and store.active_id("coding") == code.chat_id
    assert [c["id"] for c in store.list(mode="tools")] == [tools.chat_id]
    assert [c["id"] for c in store.list(mode="coding")] == [code.chat_id]
    assert store.list(mode="coding")[0]["mode"] == "coding" and store.list(mode="coding")[0]["active"]
    store.set_active(code.chat_id)
    assert store.current_mode() == "coding"
    store.delete(code.chat_id)
    assert store.active_id("coding") == "" and store.active_id("tools") == tools.chat_id


def test_memory_switches_between_the_last_chat_of_each_mode(memory):
    first = memory.conversation
    first.add({"role": "user", "content": "Hallo"})
    coding = memory.switch_mode("coding")
    assert memory.mode == "coding" and coding.chat_id != first.chat_id
    coding.add({"role": "user", "content": "Code"})
    second = memory.new_chat()
    assert second.meta.get("mode") == "coding" and memory.mode == "coding"
    back = memory.switch_mode("tools")
    assert back.chat_id == first.chat_id and memory.mode == "tools"
    assert memory.switch_mode("coding").chat_id == second.chat_id


def test_coding_mode_sends_fewer_tools_and_its_own_prompt(cfg, llm, memory):
    cfg.paperless.url, cfg.paperless.token = "http://pl", "t"
    cfg.tools.extra = ["sysadmin"]
    agent = Agent(cfg, llm, memory)
    tools_names = {s["function"]["name"] for s in agent.all_schemas}
    tools_prompt = agent.system_prompt()
    memory.switch_mode("coding")
    names = {s["function"]["name"] for s in agent._mode_schemas()[0]}
    assert "paperless_search" in tools_names and "paperless_search" not in names and "mail_list" not in names
    assert {"shell", "read", "write", "edit", "grep", "glob", "websearch", "webfetch", "subagent", "question",
            "remember", "todo_write"} <= names
    assert not {"run_shell", "read_file", "edit_file", "web_search"} & names  # die alten bleiben im Tools-Modus
    assert not {"read", "edit", "shell"} & tools_names
    assert {agent.groups_of[n] for n in names} <= CODING_GROUPS
    assert agent._mode_schemas()[1] < agent.all_schema_tokens / 2  # deutlich mehr Platz für Code
    prompt = agent.system_prompt()
    assert "Coding-Agent" in prompt and "vorgelesen" in tools_prompt and "vorgelesen" not in prompt


def test_coding_mode_refuses_other_tools(cfg, llm, memory):
    agent = Agent(cfg, llm, memory)
    memory.switch_mode("coding")
    events = []

    async def emit(ev):
        events.append(ev)

    async def confirm(*a):
        return True

    run(agent.run("/tool weather {}", emit, confirm))
    assert not any(e["type"] == "tool_call" for e in events)  # gar nicht erst ausgeführt


def test_project_folder_is_the_working_directory(cfg, llm, memory, tmp_path):
    project = tmp_path / "projekt"
    project.mkdir()
    (project / "README.md").write_text("Hallo Projekt")
    agent = Agent(cfg, llm, memory)
    memory.switch_mode("coding")
    memory.set_project(str(project))
    agent.auto_mode = "auto"
    results = []

    async def emit(ev):
        if ev["type"] == "tool_result":
            results.append(ev["text"])

    async def confirm(*a):
        return False

    run(agent.run('/tool shell {"command": "pwd"}', emit, confirm))
    run(agent.run('/tool read {"path": "README.md"}', emit, confirm))
    assert str(project) in results[0] and "1: Hallo Projekt" in results[1]
    assert str(project) in agent.system_prompt()


@pytest.fixture
def client(cfg, monkeypatch):
    monkeypatch.setenv("ORBWISE_FAKE_LLM", "1")
    monkeypatch.setenv("ORBWISE_SKIP_WARMUP", "1")
    return TestClient(server.create_app(cfg), base_url="http://localhost:8765")


def receive_until(ws, kind, limit=200):
    for _ in range(limit):
        ev = ws.receive_json()
        if ev["type"] == kind:
            return ev
    raise AssertionError(kind)


def test_server_mode_switch_project_and_no_voice(client, tmp_path):
    with client, client.websocket_connect(WS, headers=ORIGIN) as ws:
        hello = receive_until(ws, "hello")
        assert hello["mode"] == "tools"
        hub = client.app.state.hub
        spoken = []
        hub.speaker.feed = lambda *a: spoken.append(a)
        hub.speaker.say = lambda *a: spoken.append(a)

        r = client.post("/api/mode", json={"mode": "coding"})
        assert r.status_code == 200 and r.json()["mode"] == "coding"
        ev = receive_until(ws, "chat_switched")
        assert ev["mode"] == "coding"
        assert client.post("/api/mode", json={"mode": "x"}).status_code == 400

        bad = client.post(f"/api/chats/{ev['id']}/project", json={"path": str(tmp_path / "gibtsnicht")})
        assert bad.status_code == 400
        ok = client.post(f"/api/chats/{ev['id']}/project", json={"path": str(tmp_path)})
        assert ok.json()["project"] == str(tmp_path)
        assert receive_until(ws, "chat_switched")["project"] == str(tmp_path)

        ws.send_json({"type": "user_message", "text": "Hallo"})
        receive_until(ws, "assistant_end")
        assert not spoken  # im Coding-Modus wird nichts vorgelesen
        client.portal.call(hub.submit, "Hallo per Stimme", "voice")
        assert all(c["mode"] == "coding" for c in client.get("/api/chats").json())
        assert len(client.get("/api/chats").json()) == 1  # Spracheingabe wurde ignoriert, kein zweiter Austausch
        hist = client.get("/api/history").json()["messages"]
        assert [m["content"] for m in hist if m["role"] == "user"] == ["Hallo"]

        client.post("/api/mode", json={"mode": "tools"})
        assert receive_until(ws, "chat_switched")["mode"] == "tools"
        assert all(c["mode"] == "tools" for c in client.get("/api/chats").json())


def test_prompts_ask_for_edit_and_real_error_messages(cfg):
    from orbwise import prompts
    values = dict(name="Orbwise", project="/p", env="", os="Linux", host="h", user="u", home="/home/u", nas="-")
    for lang, keep, cause in (("de", "nie komplett neu schreiben", "erfinde keine Ursache"),
                              ("en", "never rewrite them completely", "invent a cause")):
        cfg.language = lang
        text = prompts.coding_prompt(cfg, **values)
        assert keep in text and cause in text
        assert ("ohne eine Ursache zu erfinden" if lang == "de" else "without inventing a cause") in \
            prompts.base_prompt(cfg, **values)
