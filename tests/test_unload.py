"""Modell von Hand entladen: Server aus bzw. Ollama entladen, Profil bleibt gewählt, die nächste Anfrage lädt es
wieder; der Zustand gilt über einen Neustart, Vorwärmen und Bild-Modus holen es nicht zurück."""

import json

import httpx
import pytest
from conftest import run
from fastapi.testclient import TestClient
from test_models import FakeOllama, ollama  # noqa: F401 – Fixture
from test_profiles import free_port, make_llm_cfg, ollama_mock, server_cfg

from orbwise.agent import Agent
from orbwise.llm import LLMError
from orbwise.llm_router import LLMRouter


def test_unload_managed_server_and_reload_on_next_question(cfg, memory, tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    port = free_port()
    llm_cfg = make_llm_cfg(port, server_cfg(port))
    state = tmp_path / "state.json"
    calls = []
    router = LLMRouter(llm_cfg, state_path=state)
    router.ollama = type(router.ollama)(llm_cfg, transport=httpx.MockTransport(ollama_mock(calls)))
    events = []

    async def progress(text):
        events.append(("progress", text))

    async def done():
        events.append(("done", None))
    router.load_progress, router.load_done = progress, done

    async def scenario():
        await router.activate("bonsai")
        server = router.servers["bonsai"]
        assert server.running() and router.describe()[1]["loaded"]
        await router.unload()
        assert not server.running() and router.unloaded and router.active == "bonsai"
        assert json.loads(state.read_text())["unloaded"] is True
        st = await router.status()
        assert st["unloaded"] and st["online"] and st["profile"] == "bonsai"
        assert not router.describe()[1]["loaded"] and router.describe()[1]["active"]
        # nach einem Neustart bleibt es entladen
        assert LLMRouter(llm_cfg, state_path=state).unloaded
        # Vorwärmen holt es nicht zurück
        assert await Agent(cfg, router, memory).prewarm() is False and not server.running()
        # die nächste Frage lädt es wieder
        answer = [ev async for ev in router.chat_stream([{"role": "user", "content": "Hallo"}])]
        assert answer and server.running() and not router.unloaded
        assert "unloaded" not in json.loads(state.read_text())
        await router.close()

    run(scenario())
    assert events and events[-1] == ("done", None) and any(kind == "progress" for kind, _ in events)


def test_unload_ollama_profile(tmp_path):
    calls = []
    router = LLMRouter(make_llm_cfg(9), state_path=tmp_path / "state.json",
                       transport=httpx.MockTransport(ollama_mock(calls)))
    assert router.active == "qwen"
    run(router.unload())
    assert ("/api/generate", {"model": "qwen3:8b", "keep_alive": 0}) in calls
    before = len(calls)
    assert run(router.status())["unloaded"] and len(calls) == before  # Ollama nicht gefragt
    # Laden über die Modellseite (aktives, entladenes Profil) hebt den Zustand auf
    run(router.activate("qwen"))
    assert not router.unloaded


def test_select_only_when_unloaded(tmp_path):
    router = LLMRouter(make_llm_cfg(9), state_path=tmp_path / "state.json",
                       transport=httpx.MockTransport(ollama_mock([])))
    with pytest.raises(LLMError, match="entladen"):
        run(router.select("bonsai"))
    run(router.unload())
    run(router.select("bonsai"))
    assert router.active == "bonsai" and router.unloaded
    assert LLMRouter(make_llm_cfg(9), state_path=tmp_path / "state.json").active == "bonsai"


def test_image_mode_does_not_restart_unloaded_model(tmp_path):
    from orbwise.imagegen import ImageEngine
    router = LLMRouter(make_llm_cfg(9), state_path=tmp_path / "state.json",
                       transport=httpx.MockTransport(ollama_mock([])))
    started = []

    async def prepare(name, progress):
        started.append(name)
    router._prepare = prepare
    run(router.unload())
    engine = ImageEngine.__new__(ImageEngine)
    engine._idle, engine.server, engine.llm, engine.stopped_llm = None, None, router, True
    run(engine._release_now())
    assert started == []


def test_api_unload_and_startup_keeps_it_unloaded(cfg, ollama, monkeypatch):  # noqa: F811
    monkeypatch.setenv("ORBWISE_SKIP_WARMUP", "1")
    monkeypatch.delenv("ORBWISE_FAKE_LLM", raising=False)
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    cfg.llm.base_url = ollama
    FakeOllama.deleted = []
    from orbwise.server import create_app
    with TestClient(create_app(cfg), base_url="http://localhost:8765") as client:
        assert client.post("/api/models/unload").json()["ok"]
        data = client.get("/api/models").json()
        active = next(p for p in data["profiles"] if p["active"])
        assert data["unloaded"] and active["loaded"] is False
    app = create_app(cfg)
    with TestClient(app, base_url="http://localhost:8765") as client:
        assert app.state.hub.agent.llm.unloaded
        assert client.get("/api/models").json()["unloaded"]
        assert client.post(f"/api/models/{active['name']}/activate").status_code == 200
        assert not client.get("/api/models").json()["unloaded"]
