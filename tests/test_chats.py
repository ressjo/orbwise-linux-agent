import json

import pytest
from conftest import run
from fastapi.testclient import TestClient

from orbwise.agent import Agent
from orbwise.memory import Memory


async def noop(e):
    pass


async def deny(*a):
    return False


def ask(cfg, llm, memory, text):
    return run(Agent(cfg, llm, memory).run(text, noop, deny))


def test_chats_create_title_star_sort_switch(cfg, llm, memory):
    first = memory.conversation.chat_id
    ask(cfg, llm, memory, "Wie wird das Wetter morgen in Freiburg und brauche ich einen Regenschirm dafür?")
    assert memory.conversation.meta["title"].startswith("Wie wird das Wetter morgen")
    assert memory.conversation.meta["title"].endswith("…")

    second = memory.new_chat().chat_id
    assert second != first
    assert memory.new_chat().chat_id == second  # leerer Chat wird wiederverwendet
    ask(cfg, llm, memory, "Installiere htop")

    chats = memory.chats.list()
    assert [c["id"] for c in chats] == [second, first]  # neueste zuerst
    memory.star_chat(first, True)
    chats = memory.chats.list()
    assert chats[0]["id"] == first and chats[0]["starred"]
    assert chats[1]["active"] and chats[1]["messages"] == 2
    assert [c["id"] for c in memory.chats.list("htop")] == [second]

    # Fortsetzen: nach dem Wechsel sieht das Modell den alten Verlauf
    memory.switch_chat(first)
    ask(cfg, llm, memory, "Und übermorgen?")
    sent = json.dumps(llm.calls[-1], ensure_ascii=False)
    assert "Regenschirm" in sent and "htop" not in sent
    memory.rename_chat(first, "Wetter")
    assert memory.conversation.meta["title"] == "Wetter"

    # Neustart: aktiver Chat und Stern bleiben erhalten
    again = Memory(cfg.memory, llm)
    assert again.conversation.chat_id == first and again.conversation.meta["starred"]
    again.close()


def test_forget_chat_removes_everything_but_facts(cfg, llm, memory):
    ask(cfg, llm, memory, "Mein geheimes Projekt heißt Falkenauge")
    run(memory.remember("Alex mag Kaffee"))
    secret = memory.conversation.chat_id
    memory.new_chat()
    ask(cfg, llm, memory, "Wie heißt die Hauptstadt von Frankreich")
    other = memory.conversation.chat_id
    day = memory.journal.days()[0]
    memory.summaries.write(day, "Zusammenfassung mit Falkenauge")

    assert "Falkenauge" in memory.journal.read(day) and "<!-- chat:" not in memory.journal.read(day)
    hits = run(memory.index.search("Falkenauge Projekt", k=10))
    assert any("Falkenauge" in h.text for h in hits)

    days = memory.forget_chat(secret)
    assert days == [day]
    text = memory.journal.read(day)
    assert "Falkenauge" not in text and "Hauptstadt von Frankreich" in text
    assert memory.summaries.read(day) is None  # wird aus dem Rest neu erstellt
    assert day in memory.days_needing_summary(include_today=True)
    assert not any("Falkenauge" in h.text for h in run(memory.index.search("Falkenauge Projekt", k=10)))
    assert "Alex mag Kaffee" in memory.facts_text()
    assert not memory.chats.exists(secret) and memory.conversation.chat_id == other

    # aktiven Chat löschen → neuer leerer Chat; letzter Eintrag des Tages weg → Tagesdatei weg
    memory.forget_chat(other)
    assert memory.conversation.chat_id not in (secret, other) and not memory.conversation.history
    assert memory.journal.read(day) is None


def test_rebuild_index_keeps_chat_sources(cfg, llm, memory):
    ask(cfg, llm, memory, "Notiere Zebrastreifen")
    chat = memory.conversation.chat_id
    run(memory.rebuild_index())
    memory.forget_chat(chat)
    assert not any("Zebrastreifen" in h.text for h in run(memory.index.search("Zebrastreifen", k=10)))


def test_other_chats_found_by_retrieval_even_if_newer(cfg, llm, memory):
    first = memory.conversation.chat_id
    ask(cfg, llm, memory, "Hallo")
    memory.new_chat()
    ask(cfg, llm, memory, "Mein Router hat die Adresse 192.168.1.1")
    memory.switch_chat(first)  # älterer Chat: neuere Einträge anderer Chats dürfen nicht ausgeblendet werden
    hits = run(memory.retrieve("Router Adresse", exclude_after=memory.conversation.window_start()))
    assert any("192.168.1.1" in h.text for h in hits)


def test_migration_from_session_json(cfg, llm):
    cfg.memory.dir.mkdir(parents=True)
    (cfg.memory.dir / "session.json").write_text(json.dumps({
        "history": [{"role": "user", "content": "Alte Frage", "ts": 1000.0},
                    {"role": "assistant", "content": "Alte Antwort", "ts": 1001.0}],
        "running_summary": "früher"}), encoding="utf-8")
    m = Memory(cfg.memory, llm)
    assert m.conversation.history[0]["content"] == "Alte Frage" and m.conversation.running_summary == "früher"
    chats = m.chats.list()
    assert len(chats) == 1 and chats[0]["title"] == "Alte Frage" and chats[0]["legacy"]
    assert not (cfg.memory.dir / "session.json").exists()
    m.close()


@pytest.fixture
def client(cfg, monkeypatch):
    monkeypatch.setenv("ORBWISE_FAKE_LLM", "1")
    monkeypatch.setenv("ORBWISE_SKIP_WARMUP", "1")
    from orbwise.server import create_app
    return TestClient(create_app(cfg), base_url="http://localhost:8765")


def test_chat_api(client):
    with client:
        memory = client.app.state.memory
        hub = client.app.state.hub
        run_chat = lambda text: client.portal.call(hub.agent.run, text, noop, deny)  # noqa: E731
        run_chat("Erster Chat")
        first = memory.conversation.chat_id
        assert client.post("/api/chats").status_code == 200
        run_chat("Zweiter Chat")
        chats = client.get("/api/chats").json()
        assert [c["title"] for c in chats] == ["Zweiter Chat", "Erster Chat"]
        assert client.post(f"/api/chats/{first}/star", json={"starred": True}).json() == {"ok": True}
        assert client.get("/api/chats").json()[0]["id"] == first
        assert client.patch(f"/api/chats/{first}", json={"title": "Umbenannt"}).status_code == 200
        assert client.post(f"/api/chats/{first}/activate").status_code == 200
        h = client.get("/api/history").json()
        assert h["chat"]["title"] == "Umbenannt" and h["messages"][0]["content"] == "Erster Chat"
        assert client.get("/api/chats?q=zweiter").json()[0]["title"] == "Zweiter Chat"
        assert client.post("/api/chats/deadbeef00/activate").status_code == 404
        assert client.post("/api/chats/../../etc/activate").status_code in (404, 405)
        r = client.delete(f"/api/chats/{first}")
        assert r.status_code == 200 and r.json()["days"]
        assert [c["title"] for c in client.get("/api/chats").json() if c["messages"]] == ["Zweiter Chat"]


def test_switch_blocked_while_busy(client):
    with client:
        memory = client.app.state.memory
        agent = client.app.state.hub.agent
        other = memory.chats.create().chat_id
        memory.switch_chat(memory.chats.list()[0]["id"])

        async def hold():
            await agent.lock.acquire()

        client.portal.call(hold)
        try:
            r = client.post(f"/api/chats/{other}/activate")
            assert r.status_code == 409 and "arbeitet" in r.json()["detail"]
        finally:
            client.portal.call(lambda: _release(agent))


async def _release(agent):
    agent.lock.release()


def test_chat_actions_while_a_routine_works_in_its_own_chat(cfg):
    """Während eine Routine/Telegram-Anfrage in ihrem Chat arbeitet, betreffen Umbenennen, Favorisieren und
    Löschen trotzdem den richtigen Chat – und der offene Chat des Nutzers wird nicht versehentlich überschrieben."""
    from orbwise.memory import Memory

    memory = Memory(cfg.memory, None)
    mine = memory.conversation
    mine.add({"role": "user", "content": "hallo"})
    other = memory.new_chat()
    other.add({"role": "user", "content": "zweiter"})
    mine = memory.switch_chat(mine.chat_id)
    with memory.in_chat("", "📱 Telegram") as conv:
        assert memory.active is mine and memory.conversation is conv
        memory.rename_chat(mine.chat_id, "Mein Chat")
        memory.star_chat(mine.chat_id, True)
        with pytest.raises(RuntimeError):
            memory.forget_chat(conv.chat_id)  # der laufende Chat bleibt
        memory.forget_chat(other.chat_id)
    assert memory.conversation is mine and mine.meta["title"] == "Mein Chat" and mine.meta["starred"]
    mine.save()
    listed = {c["id"]: c for c in memory.chats.list("")}
    assert listed[mine.chat_id]["title"] == "Mein Chat" and other.chat_id not in listed


def test_start_fresh_opens_an_empty_tools_chat(cfg, llm):
    from orbwise.memory import Memory
    m = Memory(cfg.memory, llm)
    m.switch_mode("coding")
    m.conversation.add({"role": "user", "content": "alter Coding-Chat"})
    m.conversation.save()
    old = m.conversation.chat_id
    m.close()
    m = Memory(cfg.memory, llm)  # wie beim Neustart: der letzte Modus/Chat wäre der Coding-Chat
    assert m.mode == "coding" and m.conversation.chat_id == old
    fresh = m.start_fresh()
    assert m.mode == "tools" and not fresh.history
    again = m.start_fresh()
    assert again.chat_id == fresh.chat_id  # kein zweiter leerer Chat
    assert m.chats.exists(old)
    m.close()


def test_server_start_shows_an_empty_tools_chat(cfg, llm, monkeypatch):
    from fastapi.testclient import TestClient

    from orbwise import server
    from orbwise.memory import Memory
    m = Memory(cfg.memory, llm)
    m.switch_mode("coding")
    m.conversation.add({"role": "user", "content": "vom letzten Mal"})
    m.conversation.save()
    m.close()
    monkeypatch.setenv("ORBWISE_FAKE_LLM", "1")
    monkeypatch.setenv("ORBWISE_SKIP_WARMUP", "1")
    with TestClient(server.create_app(cfg), base_url="http://localhost:8765") as client:
        assert client.get("/api/history").json()["messages"] == []
        with client.websocket_connect("ws://localhost:8765/ws", headers={"Origin": "http://localhost:8765"}) as ws:
            hello = ws.receive_json()
        assert hello["type"] == "hello" and hello["mode"] == "tools" and hello["boot"]
        assert hello["name"] == "Orbwise" and hello["wake_phrase"] == "Hey Jarvis"  # Satz des Wake-Word-Modells


def test_chat_list_marks_chats_with_a_sandbox(cfg, llm):
    from orbwise.memory import Memory
    m = Memory(cfg.memory, llm)
    m.switch_mode("coding")
    m.conversation.add({"role": "user", "content": "baue was"})
    m.conversation.meta["sandbox"] = {"enabled": True, "path": "/x"}
    m.conversation.save()
    row = next(c for c in m.chats.list() if c["id"] == m.conversation.chat_id)
    assert row["sandbox"] is True
    m.close()


def test_wake_phrase_from_the_model_name():
    from orbwise.config import Config, wake_phrase
    assert Config().assistant_name == "Orbwise"
    assert wake_phrase("hey_jarvis_v0.1") == "Hey Jarvis" and wake_phrase("/m/hey_mycroft.onnx") == "Hey Mycroft"


def test_history_returns_the_whole_chat_after_a_reload(client):
    with client:
        conv = client.app.state.memory.active
        for i in range(60):  # früher kamen nur die letzten 40 Nachrichten zurück
            conv.add({"role": "user", "content": f"Frage {i}"})
            conv.add({"role": "assistant", "content": f"Antwort {i}"})
        msgs = client.get("/api/history").json()["messages"]
        assert len(msgs) == 120 and msgs[0]["content"] == "Frage 0"
