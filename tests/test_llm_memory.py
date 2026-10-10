"""Kontext im Speicher (VRAM/RAM) anzeigen und das Kontextfenster in der Oberfläche ändern."""

import json

import httpx
import pytest
from conftest import run

from orbwise.config import LLMConfig, ProfileConfig, ServerConfig
from orbwise.llm import LLMError, OllamaLLM
from orbwise.llm_memory import kv_per_token_from_info, parse_llama_log, recommend, summary
from orbwise.llm_router import LLMRouter

GB = 1024 ** 3
LOG = """altes Zeug
llama_model_loader: loaded meta data with 30 key-value pairs
print_info: n_ctx_train      = 40960
load_tensors: offloaded 49/49 layers to GPU
load_tensors:        ROCm0 model buffer size =  6825.32 MiB
load_tensors:   CPU_Mapped model buffer size =   417.66 MiB
llama_context: n_ctx         = 16384
llama_kv_cache_unified:      ROCm0 KV buffer size =   512.00 MiB
llama_context:      ROCm0 compute buffer size =   300.00 MiB
"""


def test_llama_log_shows_where_the_context_lives():
    info = parse_llama_log("früherer Start\nllama_kv_cache_unified: ROCm0 KV buffer size = 9999.00 MiB\n" + LOG)
    s = summary(info)
    assert info["ctx"] == 16384 and info["kv_vram"] == 512 * 1024 ** 2 and info["kv_ram"] == 0  # nur der letzte Start
    assert s["kv_vram_share"] == 1.0 and info["kv_per_token"] == 32768 and info["model_ram_offload"] == 0
    assert parse_llama_log("nichts dergleichen") is None


def test_recommendation_grows_with_free_vram_and_shrinks_on_spill():
    info = parse_llama_log(LOG)
    roomy = recommend(info, {"vram_total": 12 * GB, "vram_used": 8 * GB})
    assert roomy["verdict"] == "increase" and roomy["max_ctx"] == 32768  # 40k trainiert → höchste Stufe darunter
    assert all(o["ctx"] <= 40960 for o in roomy["options"])
    tight = recommend(info, {"vram_total": 8 * GB, "vram_used": 7.9 * GB})
    assert tight["verdict"] == "ok" and tight["max_ctx"] == 16384
    spill = parse_llama_log(LOG.replace("ROCm0 KV buffer size =   512.00", "CPU KV buffer size =   256.00")
                            + "llama_kv_cache_unified: ROCm0 KV buffer size = 256.00 MiB\n")
    r = recommend(spill, {"vram_total": 8 * GB, "vram_used": 7.9 * GB})
    assert r["verdict"] == "reduce" and r["max_ctx"] == 8192
    assert summary(spill)["kv_vram_share"] == 0.5


def test_ollama_memory_estimate():
    info = {"qwen3.block_count": 36, "qwen3.attention.head_count": 32, "qwen3.attention.head_count_kv": 8,
            "qwen3.attention.key_length": 128, "qwen3.attention.value_length": 128, "qwen3.context_length": 40960}
    per_token = kv_per_token_from_info(info)
    assert per_token == 36 * 8 * 256 * 2  # 144 KiB pro Token (f16)

    def handler(req):
        if req.url.path == "/api/ps":
            return httpx.Response(200, json={"models": [{"name": "qwen3:8b", "size": 8 * GB,
                                                         "size_vram": 6 * GB, "context_length": 16384}]})
        return httpx.Response(200, json={"model_info": info})

    llm = OllamaLLM(LLMConfig(model="qwen3:8b"), transport=httpx.MockTransport(handler))
    m = run(llm.memory_info())
    assert m["ctx"] == 16384 and m["ctx_train"] == 40960 and m["kv_ram"] > 0 and m["model_ram_offload"] > 0
    assert recommend(m, {"vram_total": 8 * GB, "vram_used": 7.5 * GB})["verdict"] == "reduce"


def test_context_setting_is_applied_and_remembered(tmp_path):
    cfg = LLMConfig(profiles={
        "qwen": ProfileConfig(model="qwen3:8b"),
        "bonsai": ProfileConfig(backend="openai", base_url="http://127.0.0.1:8080/v1", model="bonsai",
                                server=ServerConfig(command="~/bonsai/start.sh -np 1", env={"BONSAI_CTX": "8192"})),
        "other": ProfileConfig(backend="openai", base_url="http://127.0.0.1:8081/v1", model="x",
                               server=ServerConfig(command="llama-server -m x.gguf -c 4096 -np 1")),
    }, active="qwen")
    state = tmp_path / "state.json"
    router = LLMRouter(cfg, state_path=state)
    run(router.set_context("bonsai", 16384))  # nicht aktiv: nur merken und Startbefehl anpassen
    run(router.set_context("other", 12288))
    assert router.profiles["bonsai"].server.env["BONSAI_CTX"] == "16384"
    assert router.profiles["other"].server.command == "llama-server -m x.gguf -c 12288 -np 1"
    run(router.set_context("qwen", 24576))  # aktiv (Ollama): neuer Client mit num_ctx
    assert router.client.cfg.num_ctx == 24576 and router.context_size == 24576
    assert json.loads(state.read_text())["context"] == {"bonsai": 16384, "other": 12288, "qwen": 24576}
    again = LLMRouter(cfg.model_copy(deep=True), state_path=state)  # nach Neustart wieder angewendet
    assert again.profiles["bonsai"].server.env["BONSAI_CTX"] == "16384" and again.context_size == 24576
    assert {p["name"]: p["num_ctx"] for p in again.describe()}["other"] == 12288


def test_kv_cache_level_is_applied_and_remembered(tmp_path, monkeypatch):
    from orbwise import bonsai
    from orbwise.llm_memory import cache_type
    monkeypatch.setattr(bonsai, "_server_help", lambda *a, **k: "-fa,   --flash-attn [on|off|auto]  set Flash Attention")
    cfg = LLMConfig(profiles={
        "qwen": ProfileConfig(model="qwen3:8b"),
        "bonsai": ProfileConfig(backend="openai", base_url="http://127.0.0.1:8080/v1", model="bonsai",
                                server=ServerConfig(command="~/bonsai/scripts/start_llama_server.sh -np 1",
                                                    env={"BONSAI_CTX": "8192"})),
        "other": ProfileConfig(backend="openai", base_url="http://127.0.0.1:8090/v1", model="x",
                               server=ServerConfig(command="llama-server -m x.gguf -c 4096 -ctk q4_0 -ctv q4_0 -np 1")),
    }, active="qwen")
    state = tmp_path / "state.json"
    router = LLMRouter(cfg, state_path=state)
    kv = {p["name"]: (p["kv"], p["kv_settable"]) for p in router.describe()}
    assert kv == {"qwen": (None, False), "bonsai": ("f16", True), "other": ("q4_0", True)}
    run(router.set_kv("other", "q8_0"))
    assert router.profiles["other"].server.command == "llama-server -m x.gguf -c 4096 -np 1 -ctk q8_0 -ctv q8_0 -fa on"
    run(router.set_kv("other", "f16"))
    assert router.profiles["other"].server.command == "llama-server -m x.gguf -c 4096 -np 1 -fa on"
    run(router.set_kv("bonsai", "q4_0"))  # Bonsai-Starter: über seine eigene Einstellung
    assert router.profiles["bonsai"].server.env["BONSAI_KV4"] == "1" and "-ctk" not in router.profiles["bonsai"].server.command
    run(router.set_kv("bonsai", "q8_0"))
    b = router.profiles["bonsai"].server
    assert "BONSAI_KV4" not in b.env and b.command.endswith("-ctk q8_0 -ctv q8_0 -fa on") and cache_type(b.command, b.env) == "q8_0"
    with pytest.raises(LLMError):
        run(router.set_kv("qwen", "q8_0"))  # Ollama: gilt für den ganzen Dienst
    with pytest.raises(LLMError):
        run(router.set_kv("other", "q2"))
    assert json.loads(state.read_text())["kv_cache"] == {"other": "f16", "bonsai": "q8_0"}
    again = LLMRouter(cfg.model_copy(deep=True), state_path=state)  # nach Neustart wieder angewendet
    assert {p["name"]: p["kv"] for p in again.describe()}["bonsai"] == "q8_0"
    monkeypatch.setattr(bonsai, "_server_help", lambda *a, **k: "-fa,   --flash-attn   enable Flash Attention")
    run(again.set_kv("other", "q4_0"))  # älterer Build: -fa ohne Wert
    assert again.profiles["other"].server.command == "llama-server -m x.gguf -c 4096 -np 1 -ctk q4_0 -ctv q4_0 -fa"
    assert cache_type("x", {"BONSAI_KV4": "1"}) == "q4_0" and cache_type("x --cache-type-v q8_0", {}) == "q8_0"


def test_kv_api(cfg, monkeypatch, tmp_path):
    from fastapi.testclient import TestClient

    from orbwise.server import create_app
    monkeypatch.setenv("ORBWISE_SKIP_WARMUP", "1")
    monkeypatch.delenv("ORBWISE_FAKE_LLM", raising=False)
    cfg.llm.base_url = "http://127.0.0.1:9"
    cfg.llm.profiles = {"qwen": ProfileConfig(model="qwen3:8b"),
                        "other": ProfileConfig(backend="openai", base_url="http://127.0.0.1:8091/v1", model="x",
                                               server=ServerConfig(command="llama-server -m x.gguf -c 4096 -fa on"))}
    cfg.llm.active = "qwen"
    with TestClient(create_app(cfg), base_url="http://localhost:8765") as client:
        assert client.post("/api/models/other/kv", json={"kv": "q8_0"}).json() == {"ok": True, "kv": "q8_0"}
        p = {x["name"]: x for x in client.get("/api/models").json()["profiles"]}
        assert p["other"]["kv"] == "q8_0" and p["other"]["kv_settable"] and not p["qwen"]["kv_settable"]
        r = client.post("/api/models/qwen/kv", json={"kv": "q8_0"})
        assert r.status_code == 400 and "OLLAMA_KV_CACHE_TYPE" in r.json()["detail"]
        assert client.post("/api/models/other/kv", json={"kv": "q3"}).status_code == 400


def test_memory_api_in_demo_mode(cfg, monkeypatch):
    from fastapi.testclient import TestClient

    from orbwise.server import create_app
    monkeypatch.setenv("ORBWISE_FAKE_LLM", "1")
    monkeypatch.setenv("ORBWISE_SKIP_WARMUP", "1")
    with TestClient(create_app(cfg), base_url="http://localhost:8765") as client:
        assert client.get("/api/llm/memory").json() == {"available": False}
        assert client.post("/api/models/demo/context", json={"ctx": 8192}).status_code == 400


def write_gguf(path, meta: dict, vocab: int = 50):
    """Kleine GGUF-Datei: Zahlen-Metadaten plus ein Tokenizer-Array (wird beim Lesen übersprungen)."""
    import struct

    def s(text):
        b = text.encode()
        return struct.pack("<Q", len(b)) + b
    kvs = [s("general.architecture") + struct.pack("<I", 8) + s("qwen3")]
    kvs.append(s("tokenizer.ggml.tokens") + struct.pack("<IIQ", 9, 8, vocab) + b"".join(s(f"t{i}") for i in range(vocab)))
    kvs += [s(k) + struct.pack("<II", 4, v) for k, v in meta.items()]
    path.write_bytes(b"GGUF" + struct.pack("<IQQ", 3, 0, len(kvs)) + b"".join(kvs) + b"\0" * 1000)


def test_estimate_from_gguf_with_compressed_cache(tmp_path):
    from orbwise.llm_memory import cache_bytes, estimate_from_gguf, gguf_metadata
    model = tmp_path / "bonsai.gguf"
    write_gguf(model, {"qwen3.block_count": 48, "qwen3.attention.head_count": 40, "qwen3.attention.head_count_kv": 8,
                       "qwen3.attention.key_length": 128, "qwen3.attention.value_length": 128,
                       "qwen3.context_length": 32768})
    assert gguf_metadata(str(model))["qwen3.block_count"] == 48
    assert cache_bytes("x -ctk q8_0 -ctv q8_0", {}) == 34 / 32 and cache_bytes("x", {"BONSAI_KV4": "1"}) == 18 / 32
    est = estimate_from_gguf(str(model), 16384, "start.sh -np 1", {"BONSAI_KV4": "1"})
    assert est["kv_per_token"] == int(48 * 8 * 256 * 18 / 32) and est["kv_ram"] == 0 and est["ctx_train"] == 32768
    assert estimate_from_gguf(str(model), 16384, "llama-server -nkvo")["kv_vram"] == 0  # Cache bewusst im RAM
    assert estimate_from_gguf(str(tmp_path / "fehlt.gguf"), 16384) is None


def test_llama_server_memory_falls_back_and_explains(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    model = tmp_path / "m.gguf"
    write_gguf(model, {"llama.block_count": 32, "llama.attention.head_count": 32,
                       "llama.attention.head_count_kv": 8, "llama.embedding_length": 4096})
    cfg = LLMConfig(profiles={"bonsai": ProfileConfig(
        backend="openai", base_url="http://127.0.0.1:8080/v1", model="bonsai", num_ctx=8192,
        server=ServerConfig(command="start.sh", env={"BONSAI_CTX": "8192"}))}, active="bonsai")
    router = LLMRouter(cfg)
    # älterer Start mit Angaben, letzter Start ohne: nicht die alten Zahlen nehmen
    (tmp_path / "orbwise-llm.log").write_text("===== Starte: alt\n" + LOG + "\n===== Starte: neu\nserver listening\n")
    props = {}

    async def server_props():
        return props
    monkeypatch.setattr(router.client, "server_props", server_props)
    info, reason = run(router.memory_info())
    assert info is None and "Modelldatei" in reason
    props["model_path"] = str(model)
    info, reason = run(router.memory_info())
    assert info["source"] == "estimate" and info["ctx"] == 8192 and info["kv_per_token"] == 32 * 8 * 256 * 2
    (tmp_path / "orbwise-llm.log").write_text("===== Starte: neu\n" + LOG)  # Log mit Angaben: genau
    assert run(router.memory_info())[0]["source"] == "log"


def test_new_context_restarts_a_server_that_ran_before_orbwise(tmp_path, monkeypatch):
    """Gemeldet: Kontext umgestellt, oben blieb die alte Größe – der Server lief schon vor Orbwise und wurde nie
    neu gestartet."""
    import socket
    import subprocess
    import sys
    from pathlib import Path

    from orbwise.llm_router import listening_pids

    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    fake = Path(__file__).parent / "fake_llama_server.py"
    command = f"{sys.executable} {fake} {port} -c 8192"
    foreign = subprocess.Popen(command.split())  # „aus einer früheren Sitzung“
    try:
        cfg = LLMConfig(profiles={"bonsai": ProfileConfig(
            backend="openai", base_url=f"http://127.0.0.1:{port}/v1", model="bonsai",
            server=ServerConfig(command=command, startup_timeout=20))}, active="bonsai")
        router = LLMRouter(cfg, state_path=tmp_path / "state.json")
        run(router.start())
        assert router.context_size == 8192 and foreign.pid in listening_pids(port)
        run(router.set_context("bonsai", 16384))
        assert router.context_size == 16384  # neu gestartet mit der neuen Größe
        assert foreign.wait(timeout=5) is not None  # der alte Server ist beendet
    finally:
        foreign.kill() if foreign.poll() is None else None
        run(router.close())


def test_hybrid_models_only_count_attention_layers(tmp_path):
    """Bonsai 2 (Qwen 3.5): nur jede 4. Schicht hat einen KV-Cache – vorher zeigte die Anzeige bei 64k 4,5 GB statt
    ~1,2 GB und warnte „zu groß“."""
    import struct

    from orbwise.llm_memory import estimate_from_gguf, gguf_metadata, kv_per_token_from_info
    base = {"qwen35.block_count": 64, "qwen35.attention.head_count": 24, "qwen35.attention.head_count_kv": 4,
            "qwen35.attention.key_length": 256, "qwen35.attention.value_length": 256}
    model = tmp_path / "bonsai2.gguf"
    write_gguf(model, {**base, "qwen35.full_attention_interval": 4})
    est = estimate_from_gguf(str(model), 65536, "start_llama_server.sh", {"BONSAI_KV4": "1"})
    assert est["kv_per_token"] == int(16 * 4 * 512 * 18 / 32)  # 18 KiB pro Token wie in der Bonsai-Doku
    assert 1.0e9 < est["kv_vram"] < 1.3e9

    # je Schicht angegeben (0 = rekurrente Schicht)
    def s(text):
        b = text.encode()
        return struct.pack("<Q", len(b)) + b
    per_layer = [0, 0, 0, 4] * 16
    kvs = [s(k) + struct.pack("<II", 4, v) for k, v in base.items() if not k.endswith("head_count_kv")]
    kvs.append(s("qwen35.attention.head_count_kv") + struct.pack("<IIQ", 9, 4, len(per_layer))
               + b"".join(struct.pack("<I", x) for x in per_layer))
    arr = tmp_path / "array.gguf"
    arr.write_bytes(b"GGUF" + struct.pack("<IQQ", 3, 0, len(kvs)) + b"".join(kvs) + b"\0" * 100)
    assert gguf_metadata(str(arr))["qwen35.attention.head_count_kv"] == per_layer
    assert kv_per_token_from_info(gguf_metadata(str(arr))) == 16 * 4 * 512 * 2  # 64 KiB pro Token bei f16
    assert kv_per_token_from_info(base) == 64 * 4 * 512 * 2  # ohne Hinweis: alle Schichten (wie bisher)
