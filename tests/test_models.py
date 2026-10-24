import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from fastapi.testclient import TestClient

from orbwise import models as mdl
from orbwise.config import LLMConfig
from orbwise.llm_router import LLMRouter


def test_recommend_and_fit():
    assert mdl.recommend(16) == "qwen3:14b" and mdl.recommend(8) == "qwen3:8b"
    assert mdl.recommend(24) == "qwen3:30b" and mdl.recommend(0) == "qwen3:4b"
    p14 = mdl.BY_TAG["qwen3:14b"]
    assert mdl.fit(p14, 16) == "ok" and mdl.fit(p14, 9) == "tight" and mdl.fit(p14, 6) == "big"
    assert mdl.slug("qwen3:14b") == "qwen3-14b" and mdl.slug("hf.co/x/y:Q4_K_M") == "hf.co-x-y-q4-k-m"
    items = mdl.preset_list(16, {"qwen3:8b"})
    assert next(i for i in items if i["tag"] == "qwen3:8b")["installed"]
    assert [i["tag"] for i in items if i["recommended"]] == ["qwen3:14b"]


def test_interactive_choice():
    answers = iter(["", "3", "unsinn ! name", "my-model:7b"])

    def ask(prompt):
        return next(answers)

    out = []
    assert mdl.choose_interactive(8, ask=ask, out=out.append) == "qwen3:8b"
    assert mdl.choose_interactive(8, ask=ask, out=out.append) == mdl.PRESETS[2].tag
    assert mdl.choose_interactive(8, ask=ask, out=out.append) == "my-model:7b"


def test_added_models_become_profiles(tmp_path):
    state = tmp_path / "state.json"
    assert mdl.register_model(state, "qwen3:14b", activate=True) == "qwen3-14b"
    mdl.register_model(state, "llama3.1:8b")
    router = LLMRouter(LLMConfig(model="qwen3:8b"), state_path=state)
    assert {"standard", "qwen3-14b", "llama3.1-8b"} <= set(router.profiles)
    assert router.active == "qwen3-14b" and router.profile.model == "qwen3:14b" and router.profile.label == "Qwen 3 14B"
    assert mdl.unregister_model(state, "qwen3-14b") == "qwen3:14b"
    assert "active_profile" not in json.loads(state.read_text())
    assert mdl.unregister_model(state, "gibtsnicht") is None


class FakeOllama(BaseHTTPRequestHandler):
    pulled: list = []
    deleted: list = []

    def log_message(self, *a):
        pass

    def _json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/api/tags":
            return self._json({"models": [{"name": "qwen3:8b", "size": 5_200_000_000},
                                          {"name": "qwen3:14b", "size": 9_300_000_000},
                                          {"name": "llama3.1:8b", "size": 4_900_000_000}]})
        self._json({}, 404)

    def do_DELETE(self):
        req = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        FakeOllama.deleted.append(req.get("model"))
        self._json({})

    def do_POST(self):
        req = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        if self.path != "/api/pull":
            return self._json({})
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson")
        self.end_headers()
        if req["model"] == "gibts:nicht":
            self.wfile.write(b'{"error":"pull model manifest: file does not exist"}\n')
            return
        if req["model"] == "langsam:1b":  # großer Download – wird im Test abgebrochen
            try:
                for i in range(200):
                    line = {"status": "downloading", "completed": i, "total": 200}
                    self.wfile.write((json.dumps(line) + "\n").encode())
                    self.wfile.flush()
                    time.sleep(0.05)
            except (BrokenPipeError, ConnectionResetError):
                FakeOllama.aborted = True
            return
        FakeOllama.pulled.append(req["model"])
        for line in ({"status": "pulling manifest"}, {"status": "downloading", "completed": 50, "total": 100},
                     {"status": "success"}):
            self.wfile.write((json.dumps(line) + "\n").encode())


@pytest.fixture
def ollama():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), FakeOllama)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_port}"
    srv.shutdown()


def test_pull_via_web_api(cfg, ollama, monkeypatch):
    monkeypatch.setenv("ORBWISE_SKIP_WARMUP", "1")
    monkeypatch.delenv("ORBWISE_FAKE_LLM", raising=False)
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    cfg.llm.base_url = ollama
    from orbwise.server import create_app
    with TestClient(create_app(cfg), base_url="http://localhost:8765") as client:
        presets = client.get("/api/models/presets").json()
        assert any(p["tag"] == "qwen3:8b" and p["installed"] for p in presets["presets"])
        with client.websocket_connect("ws://localhost:8765/ws", headers={"Origin": "http://localhost:8765"}) as ws:
            ws.receive_json()
            assert client.post("/api/models/pull", json={"tag": "qwen3:14b"}).status_code == 200
            events = []
            while True:
                ev = ws.receive_json()
                if ev["type"] == "model_pull":
                    events.append(ev)
                    if ev.get("done") or ev.get("error"):
                        break
            assert events[-1] == {"type": "model_pull", "tag": "qwen3:14b", "done": True, "profile": "qwen3-14b"}
            assert any(e.get("status") for e in events[:-1])  # Fortschritt (gedrosselt auf ~2 pro Sekunde)
            names = [p["name"] for p in client.get("/api/models").json()["profiles"]]
            assert "qwen3-14b" in names
            client.post("/api/models/pull", json={"tag": "gibts:nicht"})
            while True:
                ev = ws.receive_json()
                if ev["type"] == "model_pull" and (ev.get("done") or ev.get("error")):
                    break
            assert "does not exist" in ev["error"]
        assert client.post("/api/models/pull", json={"tag": "; rm -rf /"}).status_code == 400
    assert FakeOllama.pulled == ["qwen3:14b"]


def model_pull_events(ws, until):
    while True:
        ev = ws.receive_json()
        if ev["type"] == "model_pull":
            if until(ev):
                return ev


def test_cancel_download_and_delete_models(cfg, ollama, monkeypatch, tmp_path):
    monkeypatch.setenv("ORBWISE_SKIP_WARMUP", "1")
    monkeypatch.delenv("ORBWISE_FAKE_LLM", raising=False)
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    cfg.llm.base_url = ollama
    FakeOllama.deleted, FakeOllama.aborted = [], False
    from orbwise.server import create_app
    with TestClient(create_app(cfg), base_url="http://localhost:8765") as client:
        with client.websocket_connect("ws://localhost:8765/ws", headers={"Origin": "http://localhost:8765"}) as ws:
            ws.receive_json()
            # Download abbrechen
            assert client.post("/api/models/pull", json={"tag": "langsam:1b"}).status_code == 200
            model_pull_events(ws, lambda ev: ev.get("completed"))
            running = client.get("/api/models").json()["pulls"]
            assert running[0]["tag"] == "langsam:1b" and running[0]["total"] == 200
            assert client.delete("/api/models/pull/langsam:1b").status_code == 200
            assert model_pull_events(ws, lambda ev: ev.get("cancelled") or ev.get("done") or ev.get("error")) == \
                {"type": "model_pull", "tag": "langsam:1b", "cancelled": True}
            assert client.get("/api/models").json()["pulls"] == []
            assert "langsam-1b" not in [p["name"] for p in client.get("/api/models").json()["profiles"]]
            assert client.delete("/api/models/pull/langsam:1b").status_code == 404

            # Geladenes Modell löschen
            client.post("/api/models/pull", json={"tag": "llama3.1:8b"})
            model_pull_events(ws, lambda ev: ev.get("done") or ev.get("error"))
        models = {p["name"]: p for p in client.get("/api/models").json()["profiles"]}
        assert models["llama3.1-8b"]["deletable"] and models["llama3.1-8b"]["size_gb"] == 4.9
        assert not models["standard"]["deletable"]  # aus der config.yaml
        assert client.delete("/api/models/standard").status_code in (400, 409)
        assert client.delete("/api/models/gibtsnicht").status_code == 404
        # nutzt ein anderes Profil dasselbe Ollama-Modell, bleiben die Dateien (hier: standard = qwen3:14b)
        mdl.register_model(cfg.memory.dir.parent / "state.json", "qwen3:14b")
        client.post("/api/models/reload")
        assert client.delete("/api/models/qwen3-14b").json()["ok"] and FakeOllama.deleted == []
        assert client.post("/api/models/llama3.1-8b/activate").status_code == 200
        # aktives Modell: wird entladen, ein anderes gewählt (ohne Laden), dann gelöscht
        r = client.delete("/api/models/llama3.1-8b").json()
        assert r["ok"] and r["active"] != "llama3.1-8b"
        listed = client.get("/api/models").json()
        assert listed["unloaded"] and listed["active"] == r["active"]
        assert "llama3.1-8b" not in [p["name"] for p in client.get("/api/models").json()["profiles"]]
        assert FakeOllama.deleted == ["llama3.1:8b"]
        assert "llama3.1:8b" not in mdl.added_models(cfg.memory.dir.parent / "state.json")
