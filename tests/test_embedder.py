"""Embeddings über llama.cpp: Modelldatei finden (Pfad, ~/models, altes Ollama), Startbefehl, Anfragen, Fehler."""

import json

import httpx
import pytest
from conftest import run

from orbwise.config import LLMConfig
from orbwise.embedder import Embedder, find_model, ollama_blob
from orbwise.llm import LLMError
from orbwise.llm_router import LLMRouter


def ollama_store(root, tag="bge-m3", owner="library", version="latest"):
    """Verzeichnis wie von Ollama: Manifest → Blob (die GGUF-Datei)."""
    digest = "sha256:" + "ab" * 32
    manifest = root / "manifests" / "registry.ollama.ai" / owner / tag / version
    manifest.parent.mkdir(parents=True)
    manifest.write_text(json.dumps({"layers": [
        {"mediaType": "application/vnd.ollama.image.license", "digest": "sha256:" + "00" * 32},
        {"mediaType": "application/vnd.ollama.image.model", "digest": digest}]}))
    blob = root / "blobs" / digest.replace(":", "-")
    blob.parent.mkdir(parents=True)
    blob.write_bytes(b"GGUF")
    return blob


def test_model_file_is_found_in_order(tmp_path, monkeypatch):
    models = tmp_path / "models"
    monkeypatch.setenv("OLLAMA_MODELS", str(tmp_path / "leer"))
    monkeypatch.setattr("orbwise.embedder.OLLAMA_DIRS", ())
    assert find_model("bge-m3", models) is None and find_model("", models) is None
    blob = ollama_store(tmp_path / "ollama")
    monkeypatch.setenv("OLLAMA_MODELS", str(tmp_path / "ollama"))
    assert find_model("bge-m3", models) == blob and find_model("bge-m3:latest", models) == blob  # altes Ollama
    assert ollama_blob("bge-m3:v2") is None and ollama_blob("nutzer/bge-m3") is None
    (models / "x_bge-m3-GGUF").mkdir(parents=True)
    (models / "x_bge-m3-GGUF" / "mmproj-bge-m3.gguf").write_bytes(b"x")  # Bildmodul zählt nicht
    own = models / "x_bge-m3-GGUF" / "bge-m3-Q8_0.gguf"
    own.write_bytes(b"x")
    assert find_model("bge-m3", models) == own  # ~/models geht vor
    direct = tmp_path / "anders.gguf"
    assert find_model(str(direct), models) is None  # Pfad angegeben, Datei fehlt → nichts anderes nehmen
    direct.write_bytes(b"x")
    assert find_model(str(direct), models) == direct


def test_command_runs_on_cpu_and_only_locally(tmp_path):
    e = Embedder("bge-m3", tmp_path, tmp_path / "llama server", port=8079)
    cmd = e.command(tmp_path / "bge m3.gguf")
    assert "--embedding" in cmd and "-ngl 0" in cmd and "--host 127.0.0.1" in cmd and "--port 8079" in cmd
    assert f"--api-key {e.key}" in cmd and "'" in cmd  # Pfade mit Leerzeichen sind geschützt


def test_embed_batches_truncates_and_keeps_order(tmp_path):
    (tmp_path / "bge-m3.gguf").write_bytes(b"x")
    seen = []

    def handler(req):
        body = json.loads(req.content)
        seen.append((req.url.path, req.headers.get("authorization"), [len(t) for t in body["input"]]))
        data = [{"index": i, "embedding": [float(len(t))]} for i, t in enumerate(body["input"])]
        return httpx.Response(200, json={"data": list(reversed(data))})  # Reihenfolge laut index, nicht laut Liste

    e = Embedder("bge-m3", tmp_path, None, transport=httpx.MockTransport(handler))
    texts = ["a" * (i + 1) for i in range(18)] + ["x" * 50_000, ""]
    out = run(e.embed(texts))
    assert [v[0] for v in out[:18]] == [float(i + 1) for i in range(18)]
    assert out[18] == [12_000.0] and out[19] == [1.0]  # gekürzt; leer → ein Leerzeichen
    assert [len(s[2]) for s in seen] == [16, 4] and seen[0][0] == "/v1/embeddings"
    assert seen[0][1] == f"Bearer {e.key}"
    assert run(e.embed([])) == [] and e.status()["available"] and not e.status()["problem"]
    run(e.close())


def test_errors_are_explained(tmp_path):
    missing = Embedder("bge-m3", tmp_path, None, transport=httpx.MockTransport(lambda r: httpx.Response(200)))
    with pytest.raises(LLMError, match="orbwise model embed"):
        run(missing.embed(["a"]))
    assert not missing.status()["available"] and "bge-m3" in missing.status()["problem"]
    (tmp_path / "bge-m3.gguf").write_bytes(b"x")
    no_binary = Embedder("bge-m3", tmp_path, tmp_path / "fehlt")
    with pytest.raises(LLMError, match="llama.cpp"):
        run(no_binary.embed(["a"]))
    broken = Embedder("bge-m3", tmp_path, None, transport=httpx.MockTransport(lambda r: httpx.Response(500, text="kaputt")))
    with pytest.raises(LLMError, match="500"):
        run(broken.embed(["a"]))
    assert "500" in broken.status()["problem"]
    short = Embedder("bge-m3", tmp_path, None, transport=httpx.MockTransport(
        lambda r: httpx.Response(200, json={"data": [{"index": 0, "embedding": [1.0]}]})))
    with pytest.raises(LLMError, match="jeden Text"):
        run(short.embed(["a", "b"]))


def test_router_uses_the_embedder_and_reports_it(tmp_path):
    (tmp_path / "models").mkdir()
    (tmp_path / "models" / "bge-m3-Q8_0.gguf").write_bytes(b"x")

    def handler(req):
        if not req.url.path.endswith("/embeddings"):  # Statusabfragen ans Sprachmodell
            return httpx.Response(200, json={})
        n = len(json.loads(req.content)["input"])
        return httpx.Response(200, json={"data": [{"index": i, "embedding": [0.5]} for i in range(n)]})
    cfg = LLMConfig(models_dir=str(tmp_path / "models"))
    router = LLMRouter(cfg, state_path=tmp_path / "state.json", transport=httpx.MockTransport(handler))
    assert run(router.embed(["a", "b"])) == [[0.5], [0.5]]
    st = run(router.status())
    assert st["embed_available"] is True and st["embed_problem"] == ""
    run(router.close())
