"""MoE-Modelle: Experten-Gewichte in den RAM (--n-cpu-moe) – Tensor-Tabelle lesen, Anzahl wählen, Startbefehl, API."""

import json
import struct

import pytest
from fastapi.testclient import TestClient

from orbwise import llamacpp
from orbwise import llm_router as router_mod
from orbwise import models as mdl
from orbwise.config import LLMConfig, ProfileConfig, ServerConfig
from orbwise.llm_memory import estimate_from_gguf, gguf_layout, moe_layers_of
from orbwise.llm_router import LLMRouter

GIB = 1024 ** 3
LAYERS = 4
ATTN = int(0.1 * GIB)     # je Schicht: Attention u. Ä. (bleibt auf der GPU)
EXPERT = int(0.3 * GIB)   # je Schicht drei Experten-Tensoren (up, down, gate) → 0,9 GiB


def write_gguf(path, moe: bool = True, ctx: int = 32768):
    """GGUF mit Tensor-Tabelle; die Daten sind „sparse“ (große Datei, kaum Platz auf der Platte)."""
    def s(text):
        b = text.encode()
        return struct.pack("<Q", len(b)) + b
    arch = "qwen3moe" if moe else "qwen3"
    meta = {f"{arch}.block_count": LAYERS, f"{arch}.attention.head_count": 4, f"{arch}.attention.head_count_kv": 1,
            f"{arch}.attention.key_length": 64, f"{arch}.attention.value_length": 64, f"{arch}.context_length": ctx}
    if moe:
        meta[f"{arch}.expert_count"] = 8
    kvs = [s("general.architecture") + struct.pack("<I", 8) + s(arch)]
    kvs += [s(k) + struct.pack("<II", 4, v) for k, v in meta.items()]
    tensors, offset = [], 0
    for i in range(LAYERS):
        tensors.append((f"blk.{i}.attn_q.weight", ATTN))
        names = ("ffn_up_exps", "ffn_down_exps", "ffn_gate_exps") if moe else ("ffn_up", "ffn_down", "ffn_gate")
        tensors += [(f"blk.{i}.{n}.weight", EXPERT) for n in names]
    infos = b""
    for name, size in tensors:
        infos += s(name) + struct.pack("<I", 2) + struct.pack("<QQ", 1, 1) + struct.pack("<IQ", 0, offset)
        offset += size
    head = b"GGUF" + struct.pack("<IQQ", 3, len(tensors), len(kvs)) + b"".join(kvs) + infos
    head += b"\0" * (-len(head) % 32)
    with open(path, "wb") as f:
        f.write(head)
        f.seek(len(head) + offset - 1)
        f.write(b"\0")
    return path


def test_layout_finds_experts_per_layer(tmp_path):
    moe = gguf_layout([str(write_gguf(tmp_path / "moe.gguf"))])
    assert moe["layers"] == LAYERS and moe["experts"] == 8
    assert moe["expert_bytes"] == [3 * EXPERT] * LAYERS
    assert moe["total"] > LAYERS * (ATTN + 3 * EXPERT)
    dense = gguf_layout([str(write_gguf(tmp_path / "dense.gguf", moe=False))])
    assert dense["experts"] == 0 and dense["expert_bytes"] == []
    assert llamacpp.moe_layout(tmp_path / "dense.gguf") is None


def test_pick_moe_moves_just_enough_layers(tmp_path):
    gguf = write_gguf(tmp_path / "moe.gguf")
    assert llamacpp.pick_moe(gguf, 32768, vram_gb=16) == 0  # passt ganz
    # 4 GB: Platz ≈ 4·0,95 − 1 = 2,8 GiB, Modell ≈ 4 GiB → zwei Schichten Experten (je 0,9 GiB) in den RAM
    assert llamacpp.pick_moe(gguf, 32768, vram_gb=4) == 2
    assert llamacpp.pick_moe(gguf, 32768, vram_gb=4, extra_bytes=int(0.6 * GIB)) == 3  # Bildmodul braucht Platz
    assert llamacpp.pick_moe(gguf, 32768, vram_gb=1.2) is None  # passt nicht einmal ohne Experten
    assert llamacpp.pick_moe(write_gguf(tmp_path / "dense.gguf", moe=False), 32768, vram_gb=4) is None


def test_new_profile_for_a_big_moe_model_keeps_context(tmp_path, cfg):
    gguf = write_gguf(tmp_path / "Big-MoE-Q6_K.gguf")
    small_gpu = {"vram_gb": 4, "gpus": [{"vram_gb": 4}]}
    p = llamacpp.make_profile(cfg, "big", gguf, None, small_gpu, 8090)
    assert " --n-cpu-moe 2" in p["server"]["command"] and " -c 32768 " in p["server"]["command"]
    assert p["embed_on_cpu"] is True
    big_gpu = {"vram_gb": 24, "gpus": [{"vram_gb": 24}]}
    assert "--n-cpu-moe" not in llamacpp.make_profile(cfg, "big", gguf, None, big_gpu, 8090)["server"]["command"]
    dense = write_gguf(tmp_path / "Dense-Q6_K.gguf", moe=False)
    assert "--n-cpu-moe" not in llamacpp.make_profile(cfg, "d", dense, None, small_gpu, 8090)["server"]["command"]


def test_memory_estimate_counts_experts_in_ram(tmp_path):
    gguf = str(write_gguf(tmp_path / "moe.gguf"))
    full = estimate_from_gguf(gguf, 8192, "llama-server -m x -ngl 99")
    moved = estimate_from_gguf(gguf, 8192, "llama-server -m x -ngl 99 --n-cpu-moe 2")
    assert full["model_vram"] - moved["model_vram"] == 2 * 3 * EXPERT
    assert moved["model_ram"] == 2 * 3 * EXPERT and full["model_ram"] == 0
    assert moe_layers_of("x --n-cpu-moe 2 -c 8192") == 2 and moe_layers_of("x -c 8192") == 0


def _router(tmp_path, saved: dict | None = None):
    state = tmp_path / "state.json"
    if saved is not None:
        state.write_text(json.dumps(saved))
    cfg = LLMConfig(base_url="http://127.0.0.1:9", profiles={
        "moe": ProfileConfig(backend="openai", base_url="http://127.0.0.1:8099/v1", model="moe", num_ctx=8192,
                             server=ServerConfig(command="llama-server -m moe.gguf -ngl 99 -c 8192")),
        "olla": ProfileConfig(backend="ollama", model="qwen3")}, active="olla")
    return LLMRouter(cfg, state_path=state), state


def test_router_sets_and_remembers_moe(tmp_path):
    import asyncio
    router, state = _router(tmp_path)
    asyncio.run(router.set_moe("moe", 3))
    assert router.profiles["moe"].server.command.endswith(" --n-cpu-moe 3")
    assert json.loads(state.read_text())["moe"]["moe"] == 3 and not router.moe_auto("moe")
    asyncio.run(router.set_moe("moe", 2, auto=True))
    assert router.profiles["moe"].server.command.count("--n-cpu-moe") == 1 and router.moe_auto("moe")
    router.follow_moe("moe", 4)  # Automatik nach Kontext-Änderung
    assert " --n-cpu-moe 4" in router.profiles["moe"].server.command
    asyncio.run(router.set_moe("moe", 0))
    assert "--n-cpu-moe" not in router.profiles["moe"].server.command
    router.follow_moe("moe", 5)  # nicht mehr automatisch → bleibt aus
    assert "--n-cpu-moe" not in router.profiles["moe"].server.command
    with pytest.raises(Exception):
        asyncio.run(router.set_moe("olla", 2))  # Ollama: geht nicht
    # gemerkte Einstellung gilt nach einem Neustart
    again, _ = _router(tmp_path, {"moe": {"moe": {"auto": 2}}})
    assert " --n-cpu-moe 2" in again.profiles["moe"].server.command and again.moe_auto("moe")


def test_api_sets_moe_and_reports_it(tmp_path, cfg, monkeypatch):
    monkeypatch.setenv("ORBWISE_SKIP_WARMUP", "1")
    monkeypatch.delenv("ORBWISE_FAKE_LLM", raising=False)
    monkeypatch.setattr(mdl, "detect_gpu", lambda: {"vendor": "amd", "vram_gb": 4, "gpus": [{"vram_gb": 4}]})
    monkeypatch.setattr(router_mod, "_help_text", lambda command, env: "--n-cpu-moe N  keep experts of N layers on the CPU")
    gguf = write_gguf(tmp_path / "Big-MoE-Q6_K.gguf")
    cfg.llm.base_url = "http://127.0.0.1:9"
    cfg.llm.profiles = {
        "big": ProfileConfig(backend="openai", base_url="http://127.0.0.1:8099/v1", model="big", num_ctx=32768,
                             server=ServerConfig(command=f"llama-server -m {gguf} -ngl 99 -c 32768")),
        "olla": ProfileConfig(backend="ollama", model="qwen3")}
    cfg.llm.active = "olla"
    state = cfg.memory.dir.parent / "state.json"
    state.parent.mkdir(parents=True, exist_ok=True)
    state.write_text(json.dumps({"local_models": {"big": {"files": [str(gguf)], "downloaded": False}}}))
    from orbwise.server import create_app
    with TestClient(create_app(cfg), base_url="http://localhost:8765") as client:
        info = {p["name"]: p for p in client.get("/api/models").json()["profiles"]}["big"]["moe"]
        assert info["layers"] == LAYERS and info["experts"] == 8 and info["n"] == 0 and info["supported"]
        assert info["auto_n"] == 2 and not info["auto"]
        r = client.post("/api/models/big/moe", json={"mode": "auto"}).json()
        assert r == {"ok": True, "n": 2, "auto": True}
        info = {p["name"]: p for p in client.get("/api/models").json()["profiles"]}["big"]["moe"]
        assert info["n"] == 2 and info["auto"] and info["ram_gb"] == round(2 * 3 * EXPERT / 1e9, 1)
        assert client.post("/api/models/big/moe", json={"mode": "manual", "n": 3}).json()["n"] == 3
        assert client.post("/api/models/big/moe", json={"mode": "manual", "n": 9}).status_code == 400
        assert client.post("/api/models/big/moe", json={"mode": "off"}).json()["n"] == 0
        assert client.post("/api/models/olla/moe", json={"mode": "auto"}).status_code == 400  # kein MoE/llama.cpp
        # Kontext kleiner bei „automatisch“: weniger Experten müssen in den RAM
        client.post("/api/models/big/moe", json={"mode": "auto"})
        client.post("/api/models/big/context", json={"ctx": 8192, "mode": "tools"})
        saved = json.loads(state.read_text())["moe"]["big"]
        assert saved == {"auto": 2}  # 8k statt 32k spart hier nur Megabytes – zwei Schichten bleiben nötig


def test_auto_needs_a_known_gpu(tmp_path, cfg, monkeypatch):
    """Ohne erkannten Grafikspeicher wählt „Automatisch“ nicht blind alle Schichten – es lehnt mit Hinweis ab."""
    monkeypatch.setenv("ORBWISE_SKIP_WARMUP", "1")
    monkeypatch.delenv("ORBWISE_FAKE_LLM", raising=False)
    monkeypatch.setattr(mdl, "detect_gpu", lambda: {"vendor": "", "vram_gb": 0, "gpus": []})
    gguf = write_gguf(tmp_path / "Big-MoE-Q6_K.gguf")
    cfg.llm.base_url = "http://127.0.0.1:9"
    cfg.llm.profiles = {
        "big": ProfileConfig(backend="openai", base_url="http://127.0.0.1:8099/v1", model="big", num_ctx=32768,
                             server=ServerConfig(command=f"llama-server -m {gguf} -ngl 99 -c 32768")),
        "olla": ProfileConfig(backend="ollama", model="qwen3")}
    cfg.llm.active = "olla"
    from orbwise.server import create_app
    with TestClient(create_app(cfg), base_url="http://localhost:8765") as client:
        r = client.post("/api/models/big/moe", json={"mode": "auto"})
        assert r.status_code == 400 and "nicht erkannt" in r.json()["detail"]
        assert client.post("/api/models/big/moe", json={"mode": "manual", "n": 2}).json()["n"] == 2
