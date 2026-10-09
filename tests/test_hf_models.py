"""Modelle direkt von Hugging Face: Link oder hf.co-Name einfügen, suchen, Quantisierung mit Größe/Passung wählen –
Ollama lädt dann hf.co/<nutzer>/<repo>:<quant>."""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient
from test_models import FakeOllama, ollama  # noqa: F401 – Fixture

from orbwise import models as mdl

GB = 1_000_000_000
REPO = "unsloth/Qwen3.6-27B-GGUF"
TREE = [
    {"type": "file", "path": "README.md", "size": 100},
    {"type": "file", "path": "Qwen3.6-27B-Q2_K.gguf", "size": 10, "lfs": {"size": int(10.5 * GB)}},
    {"type": "file", "path": "Qwen3.6-27B-UD-Q4_K_XL.gguf", "size": 10, "lfs": {"size": int(17.6 * GB)}},
    {"type": "file", "path": "Qwen3.6-27B-Q8_0.gguf", "size": 10, "lfs": {"size": int(28.7 * GB)}},
    {"type": "file", "path": "mmproj-F16.gguf", "size": 10, "lfs": {"size": int(0.9 * GB)}},
    {"type": "file", "path": "BF16/Qwen3.6-27B-BF16-00001-of-00002.gguf", "size": 10, "lfs": {"size": 30 * GB}},
]
MODELS = [{"id": REPO, "downloads": 90000, "likes": 300},
          {"id": "bartowski/Qwen_Qwen3.6-27B-GGUF", "downloads": 40000},
          {"id": "someone/Qwen3.6-35B-A3B-GGUF", "downloads": 50000}]


class FakeHF(BaseHTTPRequestHandler):
    searches: list = []

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
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        if u.path == "/api/models":
            FakeHF.searches.append(q.get("search"))
            assert q.get("filter") == "gguf"
            term = q.get("search", "").lower()
            return self._json([m for m in MODELS if term in m["id"].lower()])  # Teilstring wie bei HF
        if u.path == f"/api/models/{REPO}/tree/main":
            return self._json(TREE)
        self._json({"error": "Repository not found"}, 404)


@pytest.fixture
def hf(monkeypatch):
    srv = ThreadingHTTPServer(("127.0.0.1", 0), FakeHF)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setattr(mdl, "HF_API", f"http://127.0.0.1:{srv.server_port}/api")
    FakeHF.searches = []
    yield
    srv.shutdown()


def test_normalize_links_and_names():
    n = mdl.normalize_tag
    assert n(f"https://huggingface.co/{REPO}") == f"hf.co/{REPO}"
    assert n(f"https://huggingface.co/{REPO}/tree/main") == f"hf.co/{REPO}"
    assert n(f"https://huggingface.co/{REPO}/blob/main/Qwen3.6-27B-UD-Q4_K_XL.gguf") == f"hf.co/{REPO}:UD-Q4_K_XL"
    assert n(f"huggingface.co/{REPO}/resolve/main/x/Qwen3.6-27B-Q8_0.gguf?download=true") == f"hf.co/{REPO}:Q8_0"
    assert n(f"hf.co/{REPO}:Q4_K_M") == f"hf.co/{REPO}:Q4_K_M"  # Groß/Klein bleibt
    assert n(f"https://huggingface.co/{REPO}/blob/main/BF16/Qwen3.6-27B-BF16-00001-of-00002.gguf") is None
    assert n("Qwen3:8B") == "qwen3:8b" and n("; rm -rf /") is None and n("") is None
    assert mdl.label_of(f"hf.co/{REPO}:UD-Q4_K_XL") == "Qwen3.6-27B UD-Q4_K_XL"
    assert mdl.slug(f"hf.co/{REPO}:Q4_K_M") == "hf.co-unsloth-qwen3.6-27b-gguf-q4-k-m"


def test_search_all_words(hf):
    assert [r["repo"] for r in mdl.hf_search("qwen3.6 27b")] == [REPO, "bartowski/Qwen_Qwen3.6-27B-GGUF"]
    assert FakeHF.searches == ["qwen3.6 27b", "qwen3.6"]  # HF kennt nur Teilstrings → längstes Wort, dann filtern
    assert mdl.hf_search("   ") == []


def test_quants_fit_vram(hf):
    items = mdl.hf_quants(REPO, 20, {f"hf.co/{REPO.lower()}:q2_k"})
    assert [x["quant"] for x in items] == ["Q2_K", "UD-Q4_K_XL", "Q8_0"]  # ohne mmproj und geteilte Dateien
    q2, q4, q8 = items
    assert q2["fit"] == "ok" and q2["installed"] and q8["fit"] == "big"
    assert q4["tag"] == f"hf.co/{REPO}:UD-Q4_K_XL" and q4["vram_gb"] > 17.6
    assert [x["quant"] for x in items if x.get("recommended")] == ["UD-Q4_K_XL"]  # größte, die ganz passt
    assert [x["quant"] for x in mdl.hf_quants(REPO, 16) if x.get("recommended")] == ["Q2_K"]
    with pytest.raises(ValueError):
        mdl.hf_quants("../etc", 8)


def test_hf_via_web_api(cfg, hf, ollama, monkeypatch):  # noqa: F811
    monkeypatch.setenv("ORBWISE_SKIP_WARMUP", "1")
    monkeypatch.delenv("ORBWISE_FAKE_LLM", raising=False)
    monkeypatch.setattr(mdl, "detect_gpu", lambda: {"vendor": "amd", "name": "x", "vram_gb": 24.0})
    cfg.llm.base_url = ollama
    monkeypatch.setattr(FakeOllama, "pulled", [])
    from orbwise.server import create_app
    with TestClient(create_app(cfg), base_url="http://localhost:8765") as client:
        found = client.get("/api/models/hf/search", params={"q": "qwen3.6 27b"}).json()["results"]
        assert found[0]["repo"] == REPO
        link = f"https://huggingface.co/{REPO}/blob/main/Qwen3.6-27B-Q8_0.gguf"
        assert client.get("/api/models/hf/search", params={"q": link}).json()["results"] == \
            [{"repo": REPO, "tag": f"hf.co/{REPO}:Q8_0"}]
        quants = client.get("/api/models/hf/quants", params={"repo": REPO}).json()
        assert quants["gpu"]["vram_gb"] == 24 and len(quants["quants"]) == 3
        assert client.get("/api/models/hf/quants", params={"repo": "x/gibtsnicht"}).status_code == 404
        with client.websocket_connect("ws://localhost:8765/ws", headers={"Origin": "http://localhost:8765"}) as ws:
            ws.receive_json()
            r = client.post("/api/models/pull", json={"tag": f"https://huggingface.co/{REPO}/blob/main/Qwen3.6-27B-UD-Q4_K_XL.gguf"})
            assert r.json()["tag"] == f"hf.co/{REPO}:UD-Q4_K_XL"
            while True:
                ev = ws.receive_json()
                if ev["type"] == "model_pull" and (ev.get("done") or ev.get("error")):
                    break
            assert ev.get("done"), ev
        profiles = {p["name"]: p for p in client.get("/api/models").json()["profiles"]}
        p = profiles[ev["profile"]]
        assert p["model"] == f"hf.co/{REPO}:UD-Q4_K_XL" and p["label"] == "Qwen3.6-27B UD-Q4_K_XL"
        assert client.post("/api/models/pull", json={"tag": "https://huggingface.co/a/b/blob/main/x-00001-of-00002.gguf"}
                           ).status_code == 400
    assert FakeOllama.pulled == [f"hf.co/{REPO}:UD-Q4_K_XL"]
