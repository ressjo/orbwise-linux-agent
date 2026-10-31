"""Eigenes Backend (Experten): ein Server, den Orbwise nicht startet – Sprachmodell über eine OpenAI-kompatible
Schnittstelle (z. B. Strata, vLLM, LM Studio), Bilder über OpenAI Images API, Automatic1111/Forge oder einen externen
sd-server. Schlüssel liegen im Schlüsselbund, nie in state.json/manifest.json."""

import base64
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from conftest import run
from fastapi.testclient import TestClient

from orbwise import imagegen_setup as setup
from orbwise import keystore, retire
from orbwise.imagegen import ImageEngine, ImageError

PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAADElEQVR4nGP4z8AAAAMBAQDJ/pLvAAAAAElFTkSuQmCC")
B64 = base64.b64encode(PNG).decode()


class FakeBackend(BaseHTTPRequestHandler):
    """OpenAI (/v1/models, /v1/images/generations) und Automatic1111 (/sdapi/v1/…) in einem."""
    requests: list = []
    key = "geheim"

    def log_message(self, *a):
        pass

    def _json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authorized(self):
        return self.headers.get("Authorization") == f"Bearer {FakeBackend.key}"

    def do_GET(self):
        FakeBackend.requests.append(("GET", self.path, self.headers.get("Authorization"), None))
        if not self._authorized():
            return self._json({"error": "unauthorized"}, 401)
        if self.path == "/v1/models":
            return self._json({"data": [{"id": "qwen3.8-flash-next"}]})
        if self.path == "/sdapi/v1/sd-models":
            return self._json([{"title": "juggernautXL.safetensors"}])
        self._json({"error": "not found"}, 404)

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        FakeBackend.requests.append(("POST", self.path, self.headers.get("Authorization"), body))
        if not self._authorized():
            return self._json({"error": "unauthorized"}, 401)
        if self.path == "/v1/images/generations":
            return self._json({"data": [{"b64_json": B64}] * body.get("n", 1)})
        if self.path == "/sdapi/v1/txt2img":
            return self._json({"images": [B64] * body.get("batch_size", 1)})
        self._json({"error": "not found"}, 404)


@pytest.fixture
def backend(monkeypatch):
    srv = ThreadingHTTPServer(("127.0.0.1", 0), FakeBackend)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    FakeBackend.requests = []
    yield f"http://127.0.0.1:{srv.server_port}"
    srv.shutdown()


@pytest.fixture
def app(cfg, monkeypatch, tmp_path):
    from orbwise.server import create_app
    monkeypatch.setenv("ORBWISE_SKIP_WARMUP", "1")
    monkeypatch.delenv("ORBWISE_FAKE_LLM", raising=False)
    cfg.llm.base_url = "http://127.0.0.1:9"  # kein Ollama
    cfg.image.dir = str(tmp_path / "img")
    cfg.image.output_dir = str(tmp_path / "out")
    return create_app(cfg)


# ---------------------------------------------------------------- Sprachmodell
def test_llm_backend_probe_add_and_remove(app, backend, cfg):
    state_file = cfg.memory.dir.parent / "state.json"
    with TestClient(app, base_url="http://localhost:8765") as client:
        bad = client.post("/api/models/external/probe", json={"base_url": backend, "api_key": "falsch"}).json()
        assert not bad["ok"] and "Schlüssel" in bad["error"]
        ok = client.post("/api/models/external/probe", json={"base_url": backend, "api_key": "geheim"}).json()
        assert ok == {"ok": True, "base_url": backend + "/v1", "models": ["qwen3.8-flash-next"], "error": ""}  # /v1 ergänzt
        assert client.post("/api/models/external/probe", json={"base_url": "ftp://x"}).json()["ok"] is False
        assert client.post("/api/models/external", json={"label": "Strata", "base_url": backend}).status_code == 400
        r = client.post("/api/models/external", json={"label": "Strata", "base_url": backend + "/v1", "api_key": "geheim",
                                                      "model": "qwen3.8-flash-next", "context": 131072})
        assert r.json() == {"name": "api-strata"}
        router = app.state.hub.agent.llm
        p = router.profiles["api-strata"]
        assert p.backend == "openai" and p.base_url == backend + "/v1" and p.model == "qwen3.8-flash-next"
        assert p.api_key == "geheim" and p.num_ctx == 131072 and p.server is None  # Orbwise startet dort nichts
        assert keystore.get("llm_api_key:api-strata") == "geheim"
        assert "geheim" not in state_file.read_text()  # Schlüssel nie in state.json
        listed = {m["name"]: m for m in client.get("/api/models").json()["profiles"]}
        assert listed["api-strata"]["engine"] == "OpenAI-API" and listed["api-strata"]["deletable"]
        assert client.delete("/api/models/api-strata").json()["ok"]
        assert "api-strata" not in router.profiles and keystore.get("llm_api_key:api-strata") == ""


def test_an_own_backend_named_strata_survives_the_strata_cleanup(tmp_path):
    state_file = tmp_path / "state.json"
    state_file.write_text(json.dumps({"added_profiles": {
        "api-strata": {"backend": "openai", "base_url": "http://x/v1", "model": "m", "label": "Strata", "api_key_ref": True},
        "strata": {"backend": "openai", "server": {"command": "~/.local/bin/strata launch --port 9"}}}}))
    result = retire.clean_state(state_file)
    assert result["profiles"] == ["strata"]  # nur das alte, von Orbwise gestartete Strata
    assert "api-strata" in json.loads(state_file.read_text())["added_profiles"]


# ---------------------------------------------------------------- Bild
@pytest.mark.parametrize("kind,path,base_suffix", [("openai", "/v1/images/generations", "/v1"),
                                                   ("a1111", "/sdapi/v1/txt2img", "")])
def test_image_backend_generates_without_starting_anything(cfg, backend, tmp_path, kind, path, base_suffix):
    cfg.image.dir = str(tmp_path / "img")
    cfg.image.output_dir = str(tmp_path / "out")
    model_id = setup.add_api_model(cfg, "Mein Server", kind, backend + base_suffix, "juggernautXL")
    keystore.set("image_api_key:" + model_id, "geheim")
    engine = ImageEngine(cfg, None, gpu_info=lambda: {"vram_total": 8 * 1024 ** 3})
    assert engine.available() and engine.api()["backend"] == kind and engine.status()["external"] == kind
    saved = run(engine.generate("a red car", negative="blur", size="16:9", steps=12, seed=7, count=2))
    assert len(saved) == 2 and saved[0]["model"] == "Mein Server" and (tmp_path / "out" / saved[0]["file"]).exists()
    assert engine.server is None and not engine.loaded()  # kein sd-server, nichts entladen
    method, called, auth, body = FakeBackend.requests[-1]
    assert called == path and auth == "Bearer geheim"
    if kind == "openai":
        assert body["size"] == "1376x768" and body["n"] == 2 and body["model"] == "juggernautXL" and body["seed"] == 7
    else:
        assert body["width"] == 1376 and body["batch_size"] == 2 and body["steps"] == 12 and body["negative_prompt"] == "blur"
        assert body["override_settings"] == {"sd_model_checkpoint": "juggernautXL"}
    assert not engine.can_edit()
    with pytest.raises(ImageError, match="Qwen-Image"):
        run(engine.generate("x", ref_images=[PNG]))


def test_image_backend_errors_are_clear(cfg, backend, tmp_path):
    cfg.image.dir = str(tmp_path / "img")
    cfg.image.output_dir = str(tmp_path / "out")
    setup.add_api_model(cfg, "ohne Schlüssel", "openai", backend + "/v1")
    engine = ImageEngine(cfg)
    with pytest.raises(ImageError, match="lehnt ab \\(401\\)"):
        run(engine.generate("x"))
    setup.add_api_model(cfg, "weg", "a1111", "http://127.0.0.1:9")
    with pytest.raises(ImageError, match="nicht erreichbar"):
        run(engine.generate("x"))
    with pytest.raises(ValueError):
        setup.add_api_model(cfg, "x", "comfy", "http://x")
    with pytest.raises(ValueError):
        setup.add_api_model(cfg, "x", "openai", "localhost:7860")


def test_image_backend_api(app, backend, cfg):
    with TestClient(app, base_url="http://localhost:8765") as client:
        probe = client.post("/api/image/backend/probe", json={"kind": "a1111", "base_url": backend, "api_key": "geheim"})
        assert probe.json() == {"ok": True, "models": ["juggernautXL.safetensors"], "error": ""}
        assert client.post("/api/image/backend/probe", json={"kind": "openai", "base_url": backend + "/v1"}).json()["ok"] is False
        r = client.post("/api/image/backend", json={"kind": "a1111", "base_url": backend, "api_key": "geheim",
                                                     "label": "Forge im Keller"}).json()
        assert r["id"] == "api-forge-im-keller" and r["available"] and r["external"] == "a1111"
        assert "geheim" not in (setup.image_dir(cfg) / "manifest.json").read_text()
        listed = {m["id"]: m for m in client.get("/api/image/models").json()["models"]}
        assert listed["api-forge-im-keller"]["api"] == "a1111" and listed["api-forge-im-keller"]["active"]
        assert client.post("/api/image/backend", json={"kind": "x", "base_url": backend}).status_code == 400
        gone = client.delete("/api/image/models/api-forge-im-keller").json()
        assert gone["available"] is False and keystore.get("image_api_key:api-forge-im-keller") == ""


def test_external_sd_server_keeps_jobs_and_editing(cfg, tmp_path, monkeypatch):
    import socket
    import subprocess
    import sys
    import time
    from pathlib import Path
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    fake = Path(__file__).parent / "fake_sd_server.py"
    proc = subprocess.Popen([sys.executable, str(fake), "--listen-port", str(port)], stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL)
    try:
        for _ in range(50):
            with socket.socket() as s:
                if s.connect_ex(("127.0.0.1", port)) == 0:
                    break
            time.sleep(0.1)
        cfg.image.dir = str(tmp_path / "img")
        cfg.image.output_dir = str(tmp_path / "out")
        setup.add_api_model(cfg, "sd-server im Netz", "sdcpp", f"http://127.0.0.1:{port}")
        engine = ImageEngine(cfg)
        assert engine.can_edit()  # ein sd-server nimmt Vorlagen an
        saved = run(engine.generate("cat", steps=6, ref_images=[PNG]))
        assert saved and saved[0]["edit"] and engine.server is None
    finally:
        proc.terminate()
        proc.wait(timeout=5)
