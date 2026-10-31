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
TEMPLATE = "{% if tools %}{{ tools | tojson }}{% endif %}{% for m in messages %}{{ m.content }}{% endfor %}"
MODELS = [{"id": REPO, "downloads": 90000, "likes": 300, "lastModified": "2026-10-20T10:00:00.000Z",
           "tags": ["gguf", "license:apache-2.0", "image-text-to-text"], "pipeline_tag": "image-text-to-text",
           "gguf": {"total": 27_000_000_000, "architecture": "qwen35", "context_length": 262144,
                    "chat_template": TEMPLATE}},
          {"id": "bartowski/Qwen_Qwen3.6-27B-GGUF", "downloads": 40000},
          {"id": "someone/Qwen3.6-35B-A3B-GGUF", "downloads": 50000}]
GATED = "meta-llama/Llama-4-GGUF"
# „Gerade beliebt“: doppelte Hochlader, ein Embedding-Modell, eins zu groß, eins ohne Angaben
TRENDING = [
    {"id": "unsloth/Gemma-4-12B-it-GGUF", "trendingScore": 900, "pipeline_tag": "image-text-to-text",
     "gguf": {"total": 12_000_000_000, "architecture": "gemma4", "context_length": 131072}, "gated": "manual"},
    {"id": "bartowski/Gemma-4-12B-it-GGUF", "trendingScore": 800, "pipeline_tag": "image-text-to-text",
     "gguf": {"total": 12_000_000_000, "architecture": "gemma4"}},
    {"id": "nomic-ai/nomic-embed-text-v2-GGUF", "trendingScore": 700, "pipeline_tag": "feature-extraction",
     "gguf": {"total": 500_000_000}},
    {"id": "unsloth/Huge-480B-GGUF", "trendingScore": 600, "gguf": {"total": 480_000_000_000}},
    {"id": "x/no-info-GGUF", "trendingScore": 500},
    {"id": "Qwen/Qwen3.6-8B-GGUF", "trendingScore": 400, "pipeline_tag": "text-generation",
     "gguf": {"total": 8_000_000_000, "architecture": "qwen3", "chat_template": TEMPLATE}},
]


class FakeHF(BaseHTTPRequestHandler):
    searches: list = []
    queries: list = []
    auth: list = []

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
        multi = parse_qs(u.query)
        q = {k: v[0] for k, v in multi.items()}
        FakeHF.queries.append({**q, "expand": multi.get("expand[]", [])})
        FakeHF.auth.append(self.headers.get("Authorization", ""))
        if u.path == "/api/models":
            assert q.get("filter") == "gguf"
            if "search" not in q:  # Vorschläge
                assert q.get("sort") == "trendingScore"
                return self._json(TRENDING)
            FakeHF.searches.append(q.get("search"))
            term = q.get("search", "").lower()
            return self._json([m for m in MODELS if term in m["id"].lower()])  # Teilstring wie bei HF
        if u.path == f"/api/models/{REPO}/tree/main":
            return self._json(TREE)
        if u.path == f"/api/models/{REPO}":
            return self._json(MODELS[0])
        if u.path.startswith(f"/api/models/{GATED}"):
            return self._json({"error": "Access to model is restricted"}, 401 if not FakeHF.auth[-1] else 403)
        if u.path == "/api/whoami-v2":
            ok = self.headers.get("Authorization") == "Bearer hf_" + "a" * 30
            return self._json({"name": "jo"} if ok else {"error": "Invalid credentials"}, 200 if ok else 401)
        self._json({"error": "Repository not found"}, 404)


@pytest.fixture
def hf(monkeypatch):
    srv = ThreadingHTTPServer(("127.0.0.1", 0), FakeHF)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setattr(mdl, "HF_API", f"http://127.0.0.1:{srv.server_port}/api")
    FakeHF.searches, FakeHF.queries, FakeHF.auth = [], [], []
    for name in ("HF_TOKEN", "HUGGING_FACE_HUB_TOKEN"):
        monkeypatch.delenv(name, raising=False)
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
    assert "abgestürzt" in text and "segmentation fault" in text and "Mit llama.cpp neu laden" in text
    assert ollama_error(500, '{"error":"model not found"}', "x") == "Ollama antwortet mit 500: model not found"


def test_search_shows_details_and_sorts(hf):
    found = mdl.hf_search("qwen3.6 27b", sort="likes")
    top = found[0]
    assert top["params_b"] == 27.0 and top["arch"] == "qwen35" and top["context"] == 262144
    assert top["tools"] and top["vision"] and top["license"] == "apache-2.0" and top["updated"] == "2026-10-20"
    assert top["est_gb"] == 16.4 and not top["gated"]  # ~4,85 Bit je Gewicht
    assert FakeHF.queries[0]["sort"] == "likes" and "gguf" in FakeHF.queries[0]["expand"]
    assert "chat_template" not in top  # die Vorlage selbst geht nicht an die Oberfläche


def test_suggestions_fit_the_memory_without_duplicates(hf):
    repos = [r["repo"] for r in mdl.hf_suggest(16)]
    # Gemma nur vom beliebtesten Hochlader; kein Embedding, nichts zu Großes, nichts ohne Größe
    assert repos == ["unsloth/Gemma-4-12B-it-GGUF", "Qwen/Qwen3.6-8B-GGUF"]
    gemma = mdl.hf_suggest(16)[0]
    assert gemma["gated"] and gemma["fit"] == "ok" and gemma["vision"]
    small = mdl.hf_suggest(6)  # 6 GB: 12B ist zu groß, 8B geht teilweise im Arbeitsspeicher
    assert [r["repo"] for r in small] == ["Qwen/Qwen3.6-8B-GGUF"] and small[0]["fit"] == "tight"


def test_quant_notes_and_context_estimate(hf):
    items = {x["quant"]: x for x in mdl.hf_quants(REPO, 24)}
    assert "verlustfrei" in items["Q8_0"]["note"] and "Kompromiss" in items["UD-Q4_K_XL"]["note"]
    assert "ungenauer" not in items["Q2_K"]["note"] and "schlechter" in items["Q2_K"]["note"]
    assert mdl.quant_note("IQ4_XS").startswith("etwas kleiner") and mdl.quant_note("Q4_K_M").endswith("beste Wahl")
    assert mdl.context_fit(5, 24) == 65536 and mdl.context_fit(5, 24, train=32768) == 32768
    assert mdl.context_fit(20, 24) == 8192 and mdl.context_fit(23.5, 24) == 0 and mdl.context_fit(5, 0) == 0


def test_llamacpp_knows_the_architecture(cfg):
    from orbwise import llamacpp
    assert llamacpp.supports_arch(cfg, "qwen3") is None  # noch kein llama.cpp
    lib = llamacpp.root(cfg) / "current"
    lib.mkdir(parents=True)
    (lib / "llama-server").write_bytes(b"\x7fELF...")
    (lib / "libllama.so").write_bytes(b"\x00llama\x00qwen3\x00qwen3moe\x00gemma3\x00")
    assert llamacpp.supports_arch(cfg, "qwen3moe") is True and llamacpp.supports_arch(cfg, "gemma3") is True
    assert llamacpp.supports_arch(cfg, "gemma4") is False and llamacpp.supports_arch(cfg, "") is None
    assert llamacpp.supports_arch(cfg, "../x") is None


def test_api_suggestions_details_gated_and_token(cfg, hf, monkeypatch):
    monkeypatch.setenv("ORBWISE_SKIP_WARMUP", "1")
    monkeypatch.setenv("ORBWISE_FAKE_LLM", "1")
    monkeypatch.setattr(mdl, "detect_gpu", lambda: {"vendor": "amd", "name": "x", "vram_gb": 24.0, "gpus": [{}]})
    from orbwise.server import create_app
    with TestClient(create_app(cfg), base_url="http://localhost:8765") as client:
        sug = client.get("/api/models/hf/suggest").json()
        assert [r["repo"] for r in sug["results"]][:1] == ["unsloth/Gemma-4-12B-it-GGUF"]
        calls = len(FakeHF.queries)
        client.get("/api/models/hf/suggest")
        assert len(FakeHF.queries) == calls  # eine Stunde gemerkt
        found = client.get("/api/models/hf/search", params={"q": "qwen3.6", "sort": "updated"}).json()["results"]
        assert found[0]["fit"] == "ok" and found[0]["supported"] is None  # ohne llama.cpp: unbekannt
        assert FakeHF.queries[-1]["sort"] == "lastModified"
        data = client.get("/api/models/hf/quants", params={"repo": REPO}).json()
        assert data["info"]["arch"] == "qwen35" and data["info"]["hf_token"] is False
        q4 = next(x for x in data["quants"] if x["quant"] == "UD-Q4_K_XL")
        assert q4["vram_pct"] == 83 and q4["ctx"] == 16384 and q4["note"]
        gated = client.get("/api/models/hf/quants", params={"repo": GATED})
        assert gated.status_code == 403 and "Token" in gated.json()["detail"]
        # Token: Format prüfen, bei HF gegenprüfen, speichern – danach geht es bei jeder Anfrage mit
        assert client.get("/api/hf/token").json() == {"set": False, "source": "saved"}
        assert client.post("/api/hf/token", json={"token": "abc"}).status_code == 400
        assert client.post("/api/hf/token", json={"token": "hf_" + "b" * 30}).status_code == 400  # HF kennt es nicht
        ok = client.post("/api/hf/token", json={"token": "hf_" + "a" * 30}).json()
        assert ok == {"set": True, "source": "saved", "user": "jo"}
        assert client.get("/api/hf/token").json()["set"] is True
        client.get("/api/models/hf/quants", params={"repo": REPO})
        assert FakeHF.auth[-1] == "Bearer hf_" + "a" * 30
        assert client.post("/api/hf/token", json={"token": ""}).json()["set"] is False
        monkeypatch.setenv("HF_TOKEN", "hf_env")
        assert client.get("/api/hf/token").json() == {"set": True, "source": "env"}
