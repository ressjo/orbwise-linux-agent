"""Chat-Aktionen: eigene Nachricht bearbeiten, Antwort neu generieren, Chat als Markdown exportieren."""

import json

import pytest
from fastapi.testclient import TestClient

from orbwise.memory.context import Conversation


def test_truncate_only_in_current_epoch(tmp_path):
    conv = Conversation(tmp_path / "c.json", "c1")
    for i in range(6):
        conv.add({"role": "user" if i % 2 == 0 else "assistant", "content": f"m{i}"})
    conv.start_epoch([4, 5], "Zusammenfassung")
    with pytest.raises(ValueError):
        conv.truncate(2)  # schon zusammengefasst
    conv.truncate(4)
    assert [m["content"] for m in conv.history] == ["m0", "m1", "m2", "m3"]


def _wait_done(ws):
    ended = False
    while True:
        ev = json.loads(ws.receive_text())
        ended = ended or ev.get("type") == "assistant_end"
        if ended and ev.get("type") == "state" and ev.get("state") == "idle":
            return


@pytest.fixture
def client(cfg, monkeypatch):
    monkeypatch.setenv("ORBWISE_FAKE_LLM", "1")
    monkeypatch.setenv("ORBWISE_SKIP_WARMUP", "1")
    from orbwise.server import create_app
    with TestClient(create_app(cfg), base_url="http://localhost:8765") as c:
        yield c


def _ask(ws, text):
    ws.send_text(json.dumps({"type": "user_message", "text": text}))
    _wait_done(ws)


def test_edit_regenerate_and_export(client):
    with client.websocket_connect("ws://localhost:8765/ws", headers={"Origin": "http://localhost:8765"}) as ws:
        _ask(ws, "Erste Frage")
        _ask(ws, "Zweite Frage")
        h = client.get("/api/history").json()
        assert h["turns"] == 2
        assert [(m["role"], m["turn"]) for m in h["messages"]] == [("user", 1), ("assistant", 1),
                                                                    ("user", 2), ("assistant", 2)]
        assert all(m["editable"] for m in h["messages"])

        # neu generieren: gleiche Frage, alte Antwort ersetzt
        assert client.post("/api/chats/active/rewind", json={"turn": 2}).json() == {"ok": True, "turn": 2}
        _wait_done(ws)
        msgs = client.get("/api/history").json()["messages"]
        assert [m["content"] for m in msgs if m["role"] == "user"] == ["Erste Frage", "Zweite Frage"]
        assert len(msgs) == 4

        # bearbeiten: Frage 1 ersetzt, alles danach verworfen
        client.post("/api/chats/active/rewind", json={"turn": 1, "text": "Neue erste Frage"})
        _wait_done(ws)
        msgs = client.get("/api/history").json()["messages"]
        assert [m["content"] for m in msgs if m["role"] == "user"] == ["Neue erste Frage"]
        assert len(msgs) == 2 and "Neue erste Frage" in msgs[1]["content"]

        assert client.post("/api/chats/active/rewind", json={"turn": 9}).status_code == 404
        assert client.post("/api/chats/active/rewind", json={"turn": 1, "text": "  "}).status_code == 400

        chat_id = client.get("/api/history").json()["chat"]["id"]
        r = client.get(f"/api/chats/{chat_id}/export")
        assert r.status_code == 200 and "attachment" in r.headers["content-disposition"]
        assert r.headers["content-type"].startswith("text/markdown")
        text = r.text
        assert text.startswith("# Erste Frage")  # der Titel bleibt beim Bearbeiten
        assert "## Du" in text and "Neue erste Frage" in text and "Zweite Frage" not in text
        assert client.get("/api/chats/gibtsnicht/export").status_code == 404


def test_export_shows_tool_calls(client):
    with client.websocket_connect("ws://localhost:8765/ws", headers={"Origin": "http://localhost:8765"}) as ws:
        _ask(ws, '/tool list_directory {"path": "~"}')
    chat_id = client.get("/api/history").json()["chat"]["id"]
    assert "🔧 list_directory: `~`" in client.get(f"/api/chats/{chat_id}/export").text
