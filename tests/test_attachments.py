"""Bilder im Chat: hochladen, an der Nutzernachricht speichern; ein Modell mit Bildunterstützung bekommt sie direkt
(OpenAI image_url / Ollama images), sonst steht der Pfad in der Nachricht und look_at_image wird geladen."""

import base64
import json

from conftest import run
from test_prompt_size import all_integrations

from orbwise import attachments
from orbwise.agent import Agent
from orbwise.config import ProfileConfig
from orbwise.llm import ollama_messages, to_openai_messages

PNG = b"\x89PNG\r\n\x1a\n" + b"\0" * 32


async def _noop(*a):
    return True


class Model:
    def __init__(self, vision: bool, window: int = 16384):
        self.vision = vision
        self.context_size = window
        self.profile = ProfileConfig(backend="openai", base_url="http://x/v1", model="m", num_ctx=window)
        self.active = "m"
        self.sent: list[list[dict]] = []
        self.tools: list[set[str]] = []

    async def supports_images(self):
        return self.vision

    async def chat_stream(self, messages, tools=None, **kw):
        self.sent.append(messages)
        self.tools.append({t["function"]["name"] for t in tools or []})
        yield {"type": "done", "message": {"role": "assistant", "content": "Ein Fuchs."}, "stats": {}}


def test_save_and_lookup(cfg):
    name = attachments.save(cfg, PNG)
    assert name.endswith(".png") and attachments.path_of(cfg, name).read_bytes() == PNG
    assert attachments.save(cfg, PNG) == name  # gleiches Bild → gleiche Datei
    assert attachments.path_of(cfg, "../config.yaml") is None
    for bad in (b"<html>", b"GIF8"):
        try:
            attachments.save(cfg, bad)
            raise AssertionError("kein Bild angenommen")
        except ValueError:
            pass
    url = "data:image/png;base64," + base64.b64encode(PNG).decode()
    assert attachments.save_data_url(cfg, url) == name


def test_message_formats(cfg):
    path = attachments.path_of(cfg, attachments.save(cfg, PNG))
    msgs = [{"role": "user", "content": "Was ist das?", "images": [str(path)]}]
    oa = to_openai_messages(msgs)[0]["content"]
    assert oa[0] == {"type": "text", "text": "Was ist das?"}
    assert oa[1]["image_url"]["url"].startswith("data:image/png;base64,")
    assert ollama_messages(msgs)[0]["images"] == [base64.b64encode(PNG).decode()]


def test_vision_model_gets_the_image(cfg, memory):
    name = attachments.save(cfg, PNG)
    llm = Model(vision=True)
    agent = Agent(cfg, llm, memory)
    run(agent.run("Was ist das?", _noop, _noop, images=[name]))
    user = [m for m in llm.sent[0] if m["role"] == "user"][-1]
    assert user["images"] == [str(attachments.path_of(cfg, name))] and "look_at_image" not in user["content"]
    assert memory.conversation.history[0]["attachments"] == [name]
    assert agent.last_context["parts"]["history"] >= 800  # Bild zählt in der Kontext-Anzeige


def test_text_model_uses_vision_tool(cfg, memory, tmp_path):
    all_integrations(cfg, tmp_path)
    cfg.llm.num_ctx = 8192
    name = attachments.save(cfg, PNG)
    llm = Model(vision=False, window=8192)
    agent = Agent(cfg, llm, memory)
    run(agent.run("Was ist das?", _noop, _noop, images=[name]))
    user = [m for m in llm.sent[0] if m["role"] == "user"][-1]
    assert "images" not in user and "look_at_image" in user["content"] and name in user["content"]
    assert "look_at_image" in llm.tools[0]  # auch im kleinen Fenster dabei


def test_upload_api(cfg, monkeypatch):
    from fastapi.testclient import TestClient

    from orbwise.server import create_app
    monkeypatch.setenv("ORBWISE_FAKE_LLM", "1")
    monkeypatch.setenv("ORBWISE_SKIP_WARMUP", "1")
    with TestClient(create_app(cfg), base_url="http://localhost:8765") as client:
        r = client.post("/api/attachments", json={"data": "data:image/png;base64," + base64.b64encode(PNG).decode()})
        assert r.status_code == 200 and r.json()["vision"] is False
        assert client.get(r.json()["url"]).content == PNG
        assert client.post("/api/attachments", json={"data": base64.b64encode(b"hallo").decode()}).status_code == 400
        assert client.get("/api/attachments/..%2Fstate.json").status_code == 404
        with client.websocket_connect("ws://localhost:8765/ws", headers={"Origin": "http://localhost:8765"}) as ws:
            ws.send_text(json.dumps({"type": "user_message", "text": "", "images": [r.json()["name"]]}))
            for _ in range(50):
                ev = json.loads(ws.receive_text())
                if ev.get("type") == "user":
                    assert ev["images"] == [r.json()["name"]] and ev["text"]  # Standardfrage ohne Text
                    break
