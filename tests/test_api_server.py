"""OpenAI-kompatible Schnittstelle (/v1): aus bis zum Einschalten, nur mit Schlüssel, reicht an das geladene Modell
weiter (mit dessen eigenem Schlüssel) und wechselt das Modell nur, wenn das erlaubt ist."""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from fastapi.testclient import TestClient

from orbwise.api_server import resolve_model
from orbwise.config import ProfileConfig


class FakeLlamaServer(BaseHTTPRequestHandler):
    seen: list[dict] = []

    def log_message(self, *args):
        pass

    def do_GET(self):
        self.send_response(404)
        self.end_headers()

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        FakeLlamaServer.seen.append({"path": self.path, "auth": self.headers.get("Authorization"), **body})
        if body.get("stream"):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            for word in ("Hal", "lo"):
                chunk = {"choices": [{"index": 0, "delta": {"content": word}}], "model": body["model"]}
                self.wfile.write(f"data: {json.dumps(chunk)}\n\n".encode())
            self.wfile.write(b"data: [DONE]\n\n")
            return
        out = json.dumps({"object": "chat.completion", "model": body["model"],
                          "choices": [{"index": 0, "message": {"role": "assistant", "content": "Hallo"},
                                       "finish_reason": "stop"}]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)


@pytest.fixture
def client(cfg, monkeypatch):
    srv = ThreadingHTTPServer(("127.0.0.1", 0), FakeLlamaServer)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    FakeLlamaServer.seen = []
    monkeypatch.setenv("ORBWISE_SKIP_WARMUP", "1")
    monkeypatch.delenv("ORBWISE_FAKE_LLM", raising=False)
    base = f"http://127.0.0.1:{srv.server_port}/v1"
    cfg.llm.base_url = "http://127.0.0.1:9"  # kein Ollama nötig
    cfg.llm.profiles = {"local": ProfileConfig(backend="openai", base_url=base, model="m-local", api_key="innen",
                                               label="Lokal Q4"),
                        "other": ProfileConfig(backend="openai", base_url=base, model="m-other", label="Anderes")}
    cfg.llm.active = "local"
    from orbwise.server import create_app
    with TestClient(create_app(cfg), base_url="http://localhost:8765") as c:
        yield c
    srv.shutdown()


def test_resolve_model_by_name_label_or_model():
    profiles = {"a": ProfileConfig(label="Qwen 27B", model="qwen-27b"), "b": ProfileConfig(model="gemma")}
    assert resolve_model(profiles, "b", "a") == "b"
    assert resolve_model(profiles, "qwen 27b", "b") == "a" and resolve_model(profiles, "QWEN-27B", "b") == "a"
    assert resolve_model(profiles, "gpt-4o", "b") == "b" and resolve_model(profiles, "", "a") == "a"


def test_off_until_enabled_and_needs_the_key(client):
    chat = {"messages": [{"role": "user", "content": "Hi"}]}
    st = client.get("/api/apiserver").json()
    assert st["enabled"] is False and st["url"] == "http://127.0.0.1:8765/v1" and st["available"]
    assert client.post("/v1/chat/completions", json=chat).status_code == 503
    st = client.post("/api/apiserver", json={"enabled": True}).json()
    key = st["key"]
    assert st["enabled"] and key.startswith("ow-")
    r = client.post("/v1/chat/completions", json=chat, headers={"Authorization": "Bearer falsch"})
    assert r.status_code == 401 and r.json()["error"]["type"] == "invalid_api_key"
    assert client.get("/v1/models").status_code == 401
    # neuer Schlüssel macht den alten ungültig
    fresh = client.post("/api/apiserver", json={"new_key": True}).json()["key"]
    assert fresh != key and fresh.startswith("ow-")
    assert client.get("/v1/models", headers={"Authorization": f"Bearer {key}"}).status_code == 401
    assert client.get("/v1/models", headers={"Authorization": f"Bearer {fresh}"}).status_code == 200
    # fremde Webseiten kommen nicht durch (Origin), auch mit Schlüssel
    r = client.post("/v1/chat/completions", json=chat,
                    headers={"Authorization": f"Bearer {fresh}", "Origin": "https://evil.example"})
    assert r.status_code == 403


def test_forwards_to_the_loaded_model(client):
    key = client.post("/api/apiserver", json={"enabled": True}).json()["key"]
    auth = {"Authorization": f"Bearer {key}"}
    models = client.get("/v1/models", headers=auth).json()
    assert [m["id"] for m in models["data"]] == ["local"] and models["data"][0]["loaded"]

    r = client.post("/v1/chat/completions", headers=auth,
                    json={"model": "irgendwas", "messages": [{"role": "user", "content": "Hi"}], "temperature": 0.2})
    assert r.status_code == 200 and r.json()["choices"][0]["message"]["content"] == "Hallo"
    assert r.json()["model"] == "local"
    sent = FakeLlamaServer.seen[-1]
    assert sent["path"] == "/v1/chat/completions" and sent["model"] == "m-local" and sent["temperature"] == 0.2
    assert sent["auth"] == "Bearer innen"  # der Schlüssel des Modell-Servers, nicht der des Programms
    assert sent["messages"] == [{"role": "user", "content": "Hi"}]  # roh: kein Systemprompt, kein Gedächtnis

    with client.stream("POST", "/v1/chat/completions", headers=auth,
                       json={"messages": [{"role": "user", "content": "Hi"}], "stream": True}) as r:
        assert r.headers["content-type"].startswith("text/event-stream")
        text = "".join(r.iter_text())
    assert text.count("data: ") == 3 and text.rstrip().endswith("[DONE]")

    r = client.post("/v1/completions", headers=auth, json={"prompt": "def f(", "max_tokens": 8})
    assert r.status_code == 200 and FakeLlamaServer.seen[-1]["path"] == "/v1/completions"
    assert client.post("/v1/chat/completions", headers=auth, json={"model": "x"}).status_code == 400

    stats = client.get("/api/apiserver").json()["stats"]
    assert stats["requests"] == 3 and stats["running"] == 0 and stats["last_model"] == "Lokal Q4"


def test_switches_models_only_when_allowed(client):
    key = client.post("/api/apiserver", json={"enabled": True}).json()["key"]
    auth = {"Authorization": f"Bearer {key}"}
    chat = {"model": "other", "messages": [{"role": "user", "content": "Hi"}]}
    client.post("/v1/chat/completions", headers=auth, json=chat)
    assert FakeLlamaServer.seen[-1]["model"] == "m-local"  # nicht erlaubt → geladenes Modell
    assert client.get("/api/models").json()["active"] == "local"

    client.post("/api/apiserver", json={"allow_switch": True})
    assert [m["id"] for m in client.get("/v1/models", headers=auth).json()["data"]] == ["local", "other"]
    r = client.post("/v1/chat/completions", headers=auth, json=chat)
    assert r.status_code == 200 and r.json()["model"] == "other"
    assert FakeLlamaServer.seen[-1]["model"] == "m-other" and FakeLlamaServer.seen[-1]["auth"] is None
    assert client.get("/api/models").json()["active"] == "other"


def test_demo_mode_has_no_model(cfg, monkeypatch):
    monkeypatch.setenv("ORBWISE_FAKE_LLM", "1")
    monkeypatch.setenv("ORBWISE_SKIP_WARMUP", "1")
    from orbwise.server import create_app
    with TestClient(create_app(cfg), base_url="http://localhost:8765") as c:
        key = c.post("/api/apiserver", json={"enabled": True}).json()["key"]
        r = c.get("/v1/models", headers={"Authorization": f"Bearer {key}"})
        assert r.status_code == 503 and not c.get("/api/apiserver").json()["available"]
