"""Modelle direkt von Hugging Face: Link oder hf.co-Name einfügen, suchen, Quantisierung mit Größe/Passung wählen –
Orbwise lädt dann die GGUF-Datei und startet sie mit llama.cpp (Ablauf siehe test_llamacpp.py)."""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient

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
    # geteilte Dateien: llama.cpp lädt alle Teile
    assert n(f"https://huggingface.co/{REPO}/blob/main/BF16/Qwen3.6-27B-BF16-00001-of-00002.gguf") == f"hf.co/{REPO}:BF16"
    assert n("Qwen3:8B") == "qwen3:8b" and n("; rm -rf /") is None and n("") is None
    assert mdl.label_of(f"hf.co/{REPO}:UD-Q4_K_XL") == "Qwen3.6-27B UD-Q4_K_XL"
    assert mdl.slug(f"hf.co/{REPO}:Q4_K_M") == "hf.co-unsloth-qwen3.6-27b-gguf-q4-k-m"


def test_search_all_words(hf):
    assert [r["repo"] for r in mdl.hf_search("qwen3.6 27b")] == [REPO, "bartowski/Qwen_Qwen3.6-27B-GGUF"]
    assert FakeHF.searches == ["qwen3.6 27b", "qwen3.6"]  # HF kennt nur Teilstrings → längstes Wort, dann filtern
    assert mdl.hf_search("   ") == []


def test_quants_fit_vram(hf, tmp_path):
    models = tmp_path / "models"
    (models / "unsloth_Qwen3.6-27B-GGUF").mkdir(parents=True)
    (models / "unsloth_Qwen3.6-27B-GGUF" / "Qwen3.6-27B-Q2_K.gguf").write_bytes(b"x")
    items = mdl.hf_quants(REPO, 24, models)
    assert [x["quant"] for x in items] == ["Q2_K", "UD-Q4_K_XL", "Q8_0", "BF16"]  # mmproj kommt mit, nicht als Variante
    q2, q4, q8, bf16 = items
    assert q2["installed"] and not q4["installed"] and q2["fit"] == "ok" and q8["fit"] == "tight"
    assert q4["tag"] == f"hf.co/{REPO}:UD-Q4_K_XL" and q4["download_gb"] == 18.5  # 17,6 GB + Bildmodul 0,9 GB
    assert q4["mmproj"]["path"] == "mmproj-F16.gguf" and q4["vram_gb"] == 20.0
    assert bf16["parts"] == 1 and bf16["file"].startswith("BF16/")
    assert [x["quant"] for x in items if x.get("recommended")] == ["UD-Q4_K_XL"]  # größte, die ganz passt
    assert [x["quant"] for x in mdl.hf_quants(REPO, 16) if x.get("recommended")] == ["Q2_K"]
    with pytest.raises(ValueError):
        mdl.hf_quants("../etc", 8)


def test_hf_search_api(cfg, hf, monkeypatch):
    monkeypatch.setenv("ORBWISE_SKIP_WARMUP", "1")
    monkeypatch.setenv("ORBWISE_FAKE_LLM", "1")
    monkeypatch.setattr(mdl, "detect_gpu", lambda: {"vendor": "amd", "name": "x", "vram_gb": 24.0})
    from orbwise.server import create_app
    with TestClient(create_app(cfg), base_url="http://localhost:8765") as client:
        found = client.get("/api/models/hf/search", params={"q": "qwen3.6 27b"}).json()["results"]
        assert found[0]["repo"] == REPO
        link = f"https://huggingface.co/{REPO}/blob/main/Qwen3.6-27B-Q8_0.gguf"
        assert client.get("/api/models/hf/search", params={"q": link}).json()["results"] == \
            [{"repo": REPO, "tag": f"hf.co/{REPO}:Q8_0"}]
        quants = client.get("/api/models/hf/quants", params={"repo": REPO}).json()
        assert quants["gpu"]["vram_gb"] == 24 and len(quants["quants"]) == 4
        assert client.get("/api/models/hf/quants", params={"repo": "x/gibtsnicht"}).status_code == 404
        assert client.post("/api/models/pull", json={"tag": "; rm -rf /"}).status_code == 400


def test_runner_crash_is_explained():
    from orbwise.llm import ollama_error
    body = '{"error":"llama-server process has terminated: signal: segmentation fault (core dumped)"}'
    text = ollama_error(500, body, f"hf.co/{REPO}:UD-Q4_K_XL")
    assert "abgestürzt" in text and "segmentation fault" in text and "llama-server betreiben" in text
    assert ollama_error(500, '{"error":"model not found"}', "x") == "Ollama antwortet mit 500: model not found"
