"""Sampling je Modell und Modus: mit Denken ein Satz, ohne Denken ein anderer – in der Oberfläche einstellbar."""

import json

import pytest
from conftest import run

from orbwise.config import LLMConfig, ProfileConfig
from orbwise.llm import LLMError, OllamaLLM, OpenAICompatLLM, clean_sampling, sampling_for
from orbwise.llm_router import LLMRouter

QWEN = {"think": {"temperature": 1.0, "top_p": 0.95, "top_k": 20, "min_p": 0.0, "presence_penalty": 0.0},
        "fast": {"temperature": 0.7, "top_p": 0.8, "top_k": 20, "min_p": 0.0, "presence_penalty": 1.5}}


def test_clean_and_pick():
    assert clean_sampling({"repetition_penalty": 1.1, "top_k": 20.0, "min_p": ""}) == {"repeat_penalty": 1.1, "top_k": 20}
    for bad in ({"top_p": 3}, {"gibts": 1}, {"temperature": "heiß"}):
        with pytest.raises(ValueError):
            clean_sampling(bad)
    assert sampling_for(QWEN, True, 0.6)["temperature"] == 1.0
    assert sampling_for({"fast": {"top_p": 0.8}}, False, 0.6) == {"top_p": 0.8, "temperature": 0.6}  # Profil-Temperatur
    assert sampling_for({}, False, 0.6) == {"temperature": 0.6}  # wie bisher


def test_payloads():
    p = ProfileConfig(backend="openai", base_url="http://x/v1", model="m", temperature=0.6, sampling=QWEN)
    oa = OpenAICompatLLM(p)
    fast = oa._payload([{"role": "user", "content": "hi"}], None, False, think=False)
    assert fast["presence_penalty"] == 1.5 and fast["top_p"] == 0.8 and fast["temperature"] == 0.7
    assert oa._payload([], None, False, think=True)["top_p"] == 0.95  # llama-server: Felder oben
    ol = OllamaLLM(LLMConfig(model="qwen3:8b", temperature=0.6))
    assert ol._payload([], None, False, think=False)["options"] == {"temperature": 0.6, "num_ctx": 16384}
    ol.sampling = QWEN
    opts = ol._payload([], None, False, think=True)["options"]
    assert opts["top_k"] == 20 and opts["temperature"] == 1.0 and opts["num_ctx"] == 16384  # Ollama: in options


def test_router_remembers(tmp_path):
    cfg = LLMConfig(profiles={"qwen": ProfileConfig(model="qwen3:8b", sampling={"fast": {"repetition_penalty": 1.05}}),
                              "big": ProfileConfig(backend="openai", base_url="http://x/v1", model="m")}, active="qwen")
    state = tmp_path / "state.json"
    router = LLMRouter(cfg, state_path=state)
    assert router.profiles["qwen"].sampling["fast"] == {"repeat_penalty": 1.05}  # aus der config.yaml vereinheitlicht
    run(router.set_sampling("qwen", QWEN["think"], QWEN["fast"]))
    assert router.client.sampling["fast"]["presence_penalty"] == 1.5  # aktiver Ollama-Client sofort
    run(router.set_sampling("big", {"top_k": 40}, None))
    with pytest.raises(LLMError):
        run(router.set_sampling("big", {"top_p": 3}, None))
    assert json.loads(state.read_text())["sampling"]["big"] == {"think": {"top_k": 40}, "fast": {}}
    again = LLMRouter(cfg.model_copy(deep=True), state_path=state)
    assert again.profiles["qwen"].sampling == QWEN and again.profiles["big"].sampling["think"] == {"top_k": 40}


def test_api(cfg, monkeypatch):
    from fastapi.testclient import TestClient

    from orbwise.server import create_app
    monkeypatch.setenv("ORBWISE_SKIP_WARMUP", "1")
    monkeypatch.delenv("ORBWISE_FAKE_LLM", raising=False)
    cfg.llm.base_url = "http://127.0.0.1:9"
    with TestClient(create_app(cfg), base_url="http://localhost:8765") as client:
        data = client.get("/api/models/standard/sampling").json()
        assert data["think"] == {} and data["presets"]["qwen"]["fast"]["presence_penalty"] == 1.5
        assert data["limits"]["top_p"] == [0.0, 1.0]
        r = client.post("/api/models/standard/sampling", json={"think": {"temperature": 1.0}, "fast": {"top_p": 0.8}})
        assert r.json() == {"ok": True, "think": {"temperature": 1.0}, "fast": {"top_p": 0.8}}
        assert client.get("/api/models/standard/sampling").json()["fast"] == {"top_p": 0.8}
        bad = client.post("/api/models/standard/sampling", json={"think": {"top_p": 5}})
        assert bad.status_code == 400 and "top_p" in bad.json()["detail"]
        assert client.get("/api/models/gibtsnicht/sampling").status_code == 404
