"""Mehrere Grafikkarten: Erkennung (gemeinsamer Speicher), Kontext mit Puffer je Karte, eigene Aufteilung je Modell
(--tensor-split, gemerkt), zusammengefasste Telemetrie."""

import asyncio
import json
import subprocess
from types import SimpleNamespace

import pytest

from orbwise import metrics
from orbwise import models as mdl
from orbwise.config import LLMConfig, ProfileConfig, ServerConfig
from orbwise.llm import LLMError
from orbwise.llm_router import LLMRouter, gpu_flags, gpu_split_of

SMI = "0, NVIDIA GeForce RTX 3090, 24576\n1, NVIDIA GeForce RTX 3060, 12288\n"


def fake_smi(monkeypatch, out=SMI):
    monkeypatch.setattr(mdl.shutil, "which", lambda name: "/usr/bin/nvidia-smi" if name == "nvidia-smi" else None)
    monkeypatch.setattr(mdl.subprocess, "run", lambda *a, **k: SimpleNamespace(stdout=out))


def test_two_nvidia_gpus_count_together(monkeypatch):
    fake_smi(monkeypatch)
    gpus = mdl.detect_gpus()
    assert [(g["index"], g["vram_gb"]) for g in gpus] == [(0, 24.0), (1, 12.0)]
    gpu = mdl.detect_gpu()
    assert gpu["vram_gb"] == 36.0 and gpu["vendor"] == "nvidia" and len(gpu["gpus"]) == 2
    assert gpu["name"] == "NVIDIA GeForce RTX 3090 + NVIDIA GeForce RTX 3060"
    fake_smi(monkeypatch, "0, RTX 3090, 24576\n1, RTX 3090, 24576\n")
    assert mdl.detect_gpu()["name"] == "2× RTX 3090"


def test_single_gpu_stays_as_before(monkeypatch):
    fake_smi(monkeypatch, "0, RTX 4070, 12282\n")
    gpu = mdl.detect_gpu()
    assert gpu["name"] == "RTX 4070" and gpu["vram_gb"] == 12.0 and len(gpu["gpus"]) == 1


def test_split_flags_round_trip():
    assert gpu_flags(None) == ""
    flags = gpu_flags([70, 30])
    assert "--tensor-split 70,30" in flags and "--main-gpu 0" in flags and "--split-mode layer" in flags
    assert gpu_split_of("llama-server -m x" + flags) == [70, 30]
    one = gpu_flags([0, 100])
    assert one == " --split-mode none --main-gpu 1" and gpu_split_of("x" + one) == [0, 100]
    assert gpu_split_of("llama-server -m x -ngl 99") is None


def run(coro):
    return asyncio.run(coro)


def router(tmp_path, cmd):
    cfg = LLMConfig(profiles={
        "ollama": ProfileConfig(model="qwen3:8b"),
        "big": ProfileConfig(backend="openai", base_url="http://127.0.0.1:8091/v1", model="a",
                             server=ServerConfig(command=cmd)),
    }, active="ollama")
    return LLMRouter(cfg, state_path=tmp_path / "state.json")


def test_split_is_set_remembered_and_reset(tmp_path):
    cmd = "llama-server -m /m/a.gguf -ngl 99 -c 65536"
    r = router(tmp_path, cmd)
    big = next(p for p in r.describe() if p["name"] == "big")
    assert big["gpu_split"] is None and big["gpu_split_settable"]
    run(r.set_gpus("big", [60, 40]))
    server = r.profiles["big"].server
    assert server.command == cmd + " --split-mode layer --tensor-split 60,40 --main-gpu 0"
    assert server.env["CUDA_DEVICE_ORDER"] == "PCI_BUS_ID"  # Reihenfolge wie nvidia-smi
    assert json.loads((tmp_path / "state.json").read_text())["gpu_split"] == {"big": [60, 40]}
    again = router(tmp_path, cmd)  # Neustart: gemerkte Aufteilung wird angewendet
    assert gpu_split_of(again.profiles["big"].server.command) == [60, 40]
    run(r.set_gpus("big", None))  # zurück auf automatisch
    assert r.profiles["big"].server.command == cmd
    assert "big" not in json.loads((tmp_path / "state.json").read_text()).get("gpu_split", {})
    with pytest.raises(LLMError):
        run(r.set_gpus("big", [0, 0]))
    with pytest.raises(LLMError):
        run(r.set_gpus("ollama", [50, 50]))


def test_context_keeps_a_buffer_per_gpu(tmp_path, monkeypatch):
    from orbwise import llamacpp, llm_memory
    model = tmp_path / "m.gguf"
    model.write_bytes(b"\0" * 1024)
    monkeypatch.setattr(llm_memory, "gguf_metadata", lambda path: {})
    monkeypatch.setattr(llm_memory, "estimate_from_gguf",
                        lambda path, ctx: {"kv_vram": ctx * 1024 ** 2})  # 1 MB je Token
    one = llamacpp.pick_context(model, 0, 36.0, 1)[0]
    two = llamacpp.pick_context(model, 0, 36.0, 2)[0]
    single = llamacpp.pick_context(model, 0, 24.0, 1)[0]
    assert single < two <= one  # zwei Karten: mehr Kontext als eine, ein Puffer mehr als „eine große“


def test_telemetry_combines_the_cards(monkeypatch):
    monkeypatch.setattr(metrics.shutil, "which", lambda name: "/usr/bin/" + name)
    out = ("RTX 3090, 80, 20000, 24576, 250.5, 70\n"
           "RTX 3060, 40, 9000, 12288, 120.0, 61\n")
    g = metrics.nvidia_gpu(lambda *a, **k: subprocess.CompletedProcess(a, 0, stdout=out))
    assert g["name"] == "RTX 3090 + RTX 3060" and g["util"] == 80 and g["temp"] == 70
    assert g["vram_total"] == (24576 + 12288) * 1024 ** 2 and g["power"] == pytest.approx(370.5)
    assert len(g["gpus"]) == 2
    single = metrics.nvidia_gpu(lambda *a, **k: subprocess.CompletedProcess(a, 0, stdout=out.splitlines()[0]))
    assert single["name"] == "RTX 3090" and "gpus" not in single


def test_split_api_checks_one_share_per_gpu(cfg, monkeypatch):
    from fastapi.testclient import TestClient

    from orbwise.server import create_app
    monkeypatch.setenv("ORBWISE_SKIP_WARMUP", "1")
    monkeypatch.delenv("ORBWISE_FAKE_LLM", raising=False)
    fake_smi(monkeypatch)
    cfg.llm = LLMConfig(profiles={
        "ollama": ProfileConfig(model="qwen3:8b"),
        "big": ProfileConfig(backend="openai", base_url="http://127.0.0.1:8091/v1", model="a",
                             server=ServerConfig(command="llama-server -m /m/a.gguf -ngl 99")),
    }, active="ollama")
    origin = {"Origin": "http://localhost:8765"}
    with TestClient(create_app(cfg), base_url="http://localhost:8765") as client:
        assert client.post("/api/models/big/gpus", json={"split": [100]}, headers=origin).status_code == 400
        assert client.post("/api/models/ollama/gpus", json={"split": [50, 50]}, headers=origin).status_code == 400
        r = client.post("/api/models/big/gpus", json={"split": [75, 25]}, headers=origin)
        assert r.status_code == 200, r.text
        listed = {p["name"]: p for p in client.get("/api/models").json()["profiles"]}
        assert listed["big"]["gpu_split"] == [75, 25]
        assert client.post("/api/models/big/gpus", json={"split": None}, headers=origin).json()["split"] is None
