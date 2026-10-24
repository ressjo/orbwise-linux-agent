import json
import socket
import sys
from pathlib import Path

import httpx
import pytest
from conftest import run

from orbwise.agent import Agent
from orbwise.config import Config, LLMConfig, ProfileConfig, ServerConfig
from orbwise.llm import LLMError, OpenAICompatLLM, to_openai_messages
from orbwise.llm_router import LLMRouter, ManagedServer

HERE = Path(__file__).parent


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# ---------------------------------------------------------------- Config

def test_legacy_config_becomes_standard_profile():
    cfg = Config.model_validate({"llm": {"model": "qwen3:8b", "base_url": "http://nas:11434"}})
    profiles = cfg.llm.resolved_profiles()
    assert list(profiles) == ["standard"]
    p = profiles["standard"]
    assert p.backend == "ollama" and p.model == "qwen3:8b" and p.base_url == "http://nas:11434"
    assert p.temperature == 0.6 and p.think is False


def test_profiles_config():
    cfg = Config.model_validate({"llm": {"active": "llama", "profiles": {
        "qwen": {"model": "qwen3:8b"},
        "llama": {"backend": "openai", "base_url": "http://127.0.0.1:8080/v1/", "model": "llama",
                   "num_ctx": 8192, "embed_on_cpu": True,
                   "server": {"command": "~/llama/scripts/start_llama_server.sh -np 1",
                              "env": {"HSA_OVERRIDE_GFX_VERSION": "10.3.0"}}}}}})
    ps = cfg.llm.resolved_profiles()
    assert ps["qwen"].base_url == "http://localhost:11434"
    assert ps["llama"].base_url == "http://127.0.0.1:8080/v1" and ps["llama"].num_ctx == 8192
    assert ps["llama"].server.env["HSA_OVERRIDE_GFX_VERSION"] == "10.3.0"
    with pytest.raises(ValueError):
        Config.model_validate({"llm": {"profiles": {"x": {"backend": "magic"}}}})


def test_example_config_profiles_parse():
    import yaml
    data = yaml.safe_load((HERE.parent / "orbwise" / "config.example.yaml").read_text())
    Config.model_validate(data).llm.resolved_profiles()


# ---------------------------------------------------------------- Nachrichtenformat

def test_to_openai_messages():
    msgs = [
        {"role": "system", "content": "S"},
        {"role": "user", "content": "U"},
        {"role": "assistant", "content": "", "tool_calls": [
            {"function": {"name": "a", "arguments": {"x": 1}}},
            {"function": {"name": "b", "arguments": "{\"y\": 2}"}}]},
        {"role": "tool", "content": "ra", "tool_name": "a"},
        {"role": "tool", "content": "rb", "tool_name": "b"},
        {"role": "assistant", "content": "fertig"},
    ]
    out = to_openai_messages(msgs)
    calls = out[2]["tool_calls"]
    assert [c["function"]["name"] for c in calls] == ["a", "b"]
    assert json.loads(calls[0]["function"]["arguments"]) == {"x": 1} and calls[1]["function"]["arguments"] == '{"y": 2}'
    assert out[3] == {"role": "tool", "tool_call_id": calls[0]["id"], "content": "ra"}
    assert out[4]["tool_call_id"] == calls[1]["id"]
    assert out[5] == {"role": "assistant", "content": "fertig"}


# ---------------------------------------------------------------- OpenAI-Client

def sse(*chunks):
    return "".join(f"data: {json.dumps(c)}\n\n" for c in chunks) + "data: [DONE]\n\n"


def client_with(handler, **profile) -> OpenAICompatLLM:
    p = ProfileConfig(backend="openai", base_url="http://llm/v1", model="llama", temperature=0.6, think=False,
                      **profile)
    return OpenAICompatLLM(p, transport=httpx.MockTransport(handler))


def collect(llm, messages, tools=None):
    async def go():
        return [e async for e in llm.chat_stream(messages, tools)]
    return run(go())


def test_server_props_context_and_slots():
    def handler(req):
        assert req.url.path == "/props"  # ohne /v1 – eigener Endpunkt des llama-servers
        return httpx.Response(200, json={"default_generation_settings": {"n_ctx": 16384}, "total_slots": 4})

    llm = client_with(handler)
    assert run(llm.server_context()) == 16384
    assert run(llm.server_props())["total_slots"] == 4  # doctor rät dann zu -np 1

    for broken in (lambda req: httpx.Response(404), lambda req: httpx.Response(200, text="kein json"),
                   lambda req: httpx.Response(200, json=[1, 2])):
        assert run(client_with(broken).server_props()) == {}
        assert run(client_with(broken).server_context()) is None


def test_stream_text_and_timings():
    seen = {}

    def handler(req):
        seen["body"] = json.loads(req.content)
        seen["auth"] = req.headers.get("authorization")
        return httpx.Response(200, text=sse(
            {"choices": [{"delta": {"role": "assistant"}}]},
            {"choices": [], "prompt_progress": {"total": 800, "cache": 200, "processed": 500}},
            {"choices": [{"delta": {"content": "Guten "}}]},
            {"choices": [{"delta": {"content": "Abend."}}]},
            {"choices": [], "timings": {"predicted_n": 5, "predicted_per_second": 19.244,
                                        "prompt_n": 800, "prompt_per_second": 300.0, "prompt_ms": 2666.7}}))

    events = collect(client_with(handler, api_key="geheim"), [{"role": "user", "content": "Hi"}], [{"type": "function"}])
    assert [e["text"] for e in events if e["type"] == "token"] == ["Guten ", "Abend."]
    done = events[-1]
    assert done["message"] == {"role": "assistant", "content": "Guten Abend."}
    assert done["stats"] == {"tokens": 5, "tps": 19.2, "prompt_tokens": 800, "prompt_total": 800,
                             "prompt_tps": 300.0, "prompt_ms": 2667}
    assert [e for e in events if e["type"] == "prompt_progress"] == [
        {"type": "prompt_progress", "total": 800, "cache": 200, "processed": 500}]
    assert seen["auth"] == "Bearer geheim"
    body = seen["body"]
    assert body["stream"] and body["tools"] and body["chat_template_kwargs"] == {"enable_thinking": False}
    assert body["return_progress"] is True


def test_stream_fragmented_tool_calls():
    handler = lambda req: httpx.Response(200, text=sse(  # noqa: E731
        {"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "c1", "function": {"name": "find_", "arguments": ""}}]}}]},
        {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"name": "files", "arguments": "{\"que"}}]}}]},
        {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": "ry\": \"pdf\"}"}}]}}]},
        {"choices": [{"delta": {"tool_calls": [{"index": 1, "id": "c2", "function": {"name": "system_info", "arguments": "{}"}}]}}]},
        {"choices": [{"delta": {}, "finish_reason": "tool_calls"}], "usage": {"completion_tokens": 20, "prompt_tokens": 10}}))
    done = collect(client_with(handler), [{"role": "user", "content": "x"}])[-1]
    assert done["message"]["tool_calls"] == [
        {"function": {"name": "find_files", "arguments": {"query": "pdf"}}},
        {"function": {"name": "system_info", "arguments": {}}}]
    assert done["stats"]["tokens"] == 20


def test_errors():
    llm = client_with(lambda req: httpx.Response(500, text="kaputt"))
    with pytest.raises(LLMError, match="500"):
        collect(llm, [{"role": "user", "content": "x"}])

    def refuse(req):
        raise httpx.ConnectError("refused")

    with pytest.raises(LLMError, match="nicht erreichbar"):
        collect(client_with(refuse), [{"role": "user", "content": "x"}])


# ---------------------------------------------------------------- Router

def make_llm_cfg(port: int, server: dict | None = None) -> LLMConfig:
    llama = {"backend": "openai", "base_url": f"http://127.0.0.1:{port}/v1", "model": "llama", "embed_on_cpu": True}
    if server:
        llama["server"] = server
    return LLMConfig.model_validate({"active": "qwen", "profiles": {"qwen": {"model": "qwen3:8b"}, "llama": llama}})


def ollama_mock(calls):
    def handler(req):
        calls.append((req.url.path, json.loads(req.content) if req.content else None))
        if req.url.path == "/api/ps":
            return httpx.Response(200, json={"models": [{"name": "qwen3:8b"}]})
        if req.url.path == "/api/embed":
            return httpx.Response(200, json={"embeddings": [[0.1, 0.2]]})
        if req.url.path == "/api/tags":
            return httpx.Response(200, json={"models": [{"name": "bge-m3:latest"}, {"name": "qwen3:8b"}]})
        return httpx.Response(200, json={})
    return handler


def test_router_embed_on_cpu_and_state(tmp_path):
    calls = []
    cfg = make_llm_cfg(9)
    router = LLMRouter(cfg, state_path=tmp_path / "state.json", transport=httpx.MockTransport(ollama_mock(calls)))
    assert router.active == "qwen"
    run(router.embed(["a"]))
    assert "options" not in calls[-1][1]
    # Umschalten ohne verwalteten Server (Server „läuft extern“ – hier nicht nötig)
    router.active = "llama"
    router._build_client()
    run(router.embed(["a"]))
    assert calls[-1][1]["options"] == {"num_gpu": 0}
    router._write_state()
    assert LLMRouter(cfg, state_path=tmp_path / "state.json").active == "llama"


def test_unknown_profile(tmp_path):
    router = LLMRouter(make_llm_cfg(9), state_path=tmp_path / "s.json")
    with pytest.raises(LLMError, match="Unbekanntes Profil"):
        run(router.activate("gibtsnicht"))


def server_cfg(port: int, delay: float = 0.0, timeout: float = 20) -> dict:
    return {"command": f"{sys.executable} {HERE / 'fake_llama_server.py'} {port} {delay}", "startup_timeout": timeout}


def test_managed_server_lifecycle(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    port = free_port()
    server = ManagedServer(ServerConfig(**server_cfg(port, delay=1.5)), f"http://127.0.0.1:{port}/v1")
    progress = []

    async def prog(t):
        progress.append(t)

    async def scenario():
        started = await server.ensure_running(prog)
        assert started and await server.healthy() and server.running()
        again = await server.ensure_running(prog)  # läuft schon → kein zweiter Start
        assert again is False
        pid = server.proc.pid
        await server.stop()
        assert not server.running()
        return pid

    pid = run(scenario())
    with pytest.raises(ProcessLookupError):
        import os
        os.kill(pid, 0)
    assert progress and "gestartet" in progress[0]


def test_managed_server_start_failure(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    server = ManagedServer(ServerConfig(command="echo 'GPU nicht gefunden' >&2; exit 3", startup_timeout=10),
                           f"http://127.0.0.1:{free_port()}/v1")
    with pytest.raises(LLMError, match="Exit-Code 3") as err:
        run(server.ensure_running())
    assert "GPU nicht gefunden" in str(err.value)


def test_switch_starts_server_unloads_ollama_and_agent_works(cfg, llm, memory, tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    port = free_port()
    llm_cfg = make_llm_cfg(port, server_cfg(port))
    llm_cfg.profiles["llama"].unload_ollama = True
    calls = []
    router = LLMRouter(llm_cfg, state_path=tmp_path / "state.json")
    # Ollama-Aufrufe abfangen, echte HTTP-Aufrufe zum Fake-llama-server zulassen
    router.ollama = type(router.ollama)(llm_cfg, transport=httpx.MockTransport(ollama_mock(calls)))
    cfg.llm = llm_cfg

    async def scenario():
        await router.activate("llama")
        assert router.active == "llama" and router.servers["llama"].running()
        assert router.context_size == 12288  # vom Server (/props) übernommen
        assert ("/api/generate", {"model": "qwen3:8b", "keep_alive": 0}) in calls
        events = []

        async def emit(e):
            events.append(e)

        async def confirm(*a):
            return True

        agent = Agent(cfg, router, memory)
        answer = await agent.run("Hallo Llama", emit, confirm)
        stats = [e for e in events if e["type"] == "llm_stats"]
        # Tool-Aufruf über den Fake-Server: list_updates → Ergebnis → Antwort
        answer2 = await agent.run("Mach ein Systemupdate-Check", emit, confirm)
        tool_calls = [e["name"] for e in events if e["type"] == "tool_call"]
        await router.activate("qwen")
        stopped = not router.servers["llama"].running()
        await router.close()
        return answer, stats, answer2, tool_calls, stopped

    answer, stats, answer2, tool_calls, stopped = run(scenario())
    assert answer.startswith("Llama meldet: Hallo Llama")
    assert stats and stats[0]["tps"] == 19.2
    assert tool_calls == ["list_updates"] and answer2.startswith("Llama meldet")
    assert stopped
    assert json.loads((tmp_path / "state.json").read_text())["active_profile"] == "qwen"


def test_commented_example_profile_is_valid():
    import re

    import yaml
    text = (HERE.parent / "orbwise" / "config.example.yaml").read_text()
    block = text[text.index("  # active: llama"):text.index("memory:")]
    enabled = text.replace(block, re.sub(r"^  # ?", "  ", block, flags=re.M))
    cfg = Config.model_validate(yaml.safe_load(enabled))
    ps = cfg.llm.resolved_profiles()
    assert cfg.llm.active == "llama" and ps["llama"].backend == "openai" and ps["llama"].server
    assert "--api-key" in ps["llama"].server.command and ps["llama"].api_key


def test_stream_reasoning_not_part_of_answer(caplog):
    OpenAICompatLLM._warned_reasoning = False
    handler = lambda req: httpx.Response(200, text=sse(  # noqa: E731
        {"choices": [{"delta": {"reasoning_content": "Hmm, der Nutzer "}}]},
        {"choices": [{"delta": {"reasoning_content": "will …"}}]},
        {"choices": [{"delta": {"content": "Hallo."}}]}))
    with caplog.at_level("WARNING"):
        events = collect(client_with(handler), [{"role": "user", "content": "Hi"}])
    assert [e["type"] for e in events] == ["reasoning", "reasoning", "token", "done"]
    assert events[-1]["message"]["content"] == "Hallo."
    assert sum("--reasoning-budget 0" in r.message for r in caplog.records) == 1  # nur einmal gewarnt


def test_agent_ignores_reasoning_events(cfg, memory):
    class ThinkingLLM:
        async def chat_stream(self, messages, tools=None):
            yield {"type": "reasoning", "text": "überlege"}
            yield {"type": "reasoning", "text": " weiter"}
            yield {"type": "token", "text": "Antwort"}
            yield {"type": "done", "message": {"role": "assistant", "content": "Antwort"}, "stats": {}}

    events = []

    async def emit(e):
        events.append(e)

    async def confirm(*a):
        return False

    answer = run(Agent(cfg, ThinkingLLM(), memory).run("Hi", emit, confirm))
    assert answer == "Antwort"  # Denkkette ist nicht Teil der Antwort …
    assert "".join(e["text"] for e in events if e["type"] == "reasoning") == "überlege weiter"  # … wird aber gezeigt
    assert all("überlege" not in e.get("text", "") for e in events if e["type"] == "token")
    assert "überlege" not in json.dumps(memory.conversation.history, ensure_ascii=False)


def test_think_toggle_per_request(cfg, memory):
    bodies = []

    def handler(req):
        bodies.append(json.loads(req.content))
        return httpx.Response(200, text=sse({"choices": [{"delta": {"content": "ok"}}]}))

    llm = client_with(handler)
    collect(llm, [{"role": "user", "content": "x"}])

    async def go(think):
        return [e async for e in llm.chat_stream([{"role": "user", "content": "x"}], None, think=think)]

    run(go(True))
    run(go(False))
    assert [b["chat_template_kwargs"]["enable_thinking"] for b in bodies] == [False, True, False]

    seen = []

    class Spy:
        async def chat_stream(self, messages, tools=None, **kw):
            seen.append(kw)
            yield {"type": "done", "message": {"role": "assistant", "content": "ok"}, "stats": {}}

    async def emit(e):
        pass

    async def confirm(*a):
        return False

    agent = Agent(cfg, Spy(), memory)
    run(agent.run("a", emit, confirm))
    run(agent.run("b", emit, confirm, think=True))
    run(agent.run("c", emit, confirm, think="low"))
    assert seen == [{}, {"think": True}, {"think": True, "effort": "low"}]  # „an“ ohne erfundene Stufe


def test_think_effort_reaches_the_server(cfg):
    bodies = []

    def handler(req):
        bodies.append(json.loads(req.content))
        return httpx.Response(200, text=sse({"choices": [{"delta": {"content": "ok"}}]}))

    llm = client_with(handler)

    async def go(**kw):
        return [e async for e in llm.chat_stream([{"role": "user", "content": "x"}], None, **kw)]

    run(go(think=True, effort="low"))
    run(go(think=False, effort="low"))
    assert bodies[0]["chat_template_kwargs"] == {"enable_thinking": True, "reasoning_effort": "low"}
    assert bodies[1]["chat_template_kwargs"] == {"enable_thinking": False}

    from orbwise.llm import OllamaLLM
    for model, expect in (("gpt-oss:20b", "low"), ("qwen3:8b", True)):
        o = OllamaLLM(LLMConfig(model=model))
        assert o._payload([], None, True, think=True, effort="low")["think"] == expect  # Stufen nur bei gpt-oss


def test_inline_think_tags_become_reasoning(cfg, memory):
    class InlineLLM:
        async def chat_stream(self, messages, tools=None):
            for t in ["<thi", "nk>Ich prüfe", " kurz</think>", "Fertig."]:
                yield {"type": "token", "text": t}
            yield {"type": "done", "message": {"role": "assistant", "content": "<think>Ich prüfe kurz</think>Fertig."},
                   "stats": {}}

    events = []

    async def emit(e):
        events.append(e)

    async def confirm(*a):
        return False

    assert run(Agent(cfg, InlineLLM(), memory).run("x", emit, confirm)) == "Fertig."
    assert "".join(e["text"] for e in events if e["type"] == "reasoning") == "Ich prüfe kurz"


def test_context_overflow_detected_and_agent_retries_smaller(cfg, memory):
    from orbwise.llm import ContextOverflow

    body = json.dumps({"error": {"code": 400, "message": "the request exceeds the available context size",
                                 "type": "exceed_context_size_error", "n_prompt_tokens": 9000, "n_ctx": 8192}})
    with pytest.raises(ContextOverflow) as exc:
        collect(client_with(lambda req: httpx.Response(400, text=body)), [{"role": "user", "content": "x"}])
    assert exc.value.n_ctx == 8192 and exc.value.n_prompt == 9000

    class TightLLM:
        context_size = 8192
        sizes = []

        async def chat_stream(self, messages, tools=None):
            size = sum(len(m.get("content") or "") for m in messages)
            self.sizes.append(size)
            if len(self.sizes) == 1:
                raise ContextOverflow("zu groß", 8192, 9000)
            yield {"type": "done", "message": {"role": "assistant", "content": "passt"}, "stats": {}}

    for i in range(30):
        memory.conversation.add({"role": "user", "content": f"Frage {i} " + "y" * 900})
        memory.conversation.add({"role": "assistant", "content": "Antwort " + "z" * 900})

    async def emit(e):
        pass

    async def confirm(*a):
        return False

    llm = TightLLM()
    assert run(Agent(cfg, llm, memory).run("Und jetzt?", emit, confirm)) == "passt"
    # Der Verlauf ist viel zu groß → vor dem Schritt komprimieren. Auch der Server lehnt dabei ab („zu groß“) →
    # Notfall-Zusammenfassung ohne Modell; der eigentliche Schritt enthält statt 60 Nachrichten nur noch die
    # Zusammenfassung und die Frage – und klappt
    assert len(llm.sizes) == 2 and llm.sizes[-1] < 60 * 900 / 3
    assert len(memory.conversation.epochs) == 2


def test_prompt_total_includes_cache():
    from orbwise.llm import openai_stats
    st = openai_stats({"predicted_n": 5, "predicted_per_second": 20, "prompt_n": 120, "cache_n": 4800}, {}, None, 0)
    assert st["prompt_total"] == 4920 and st["prompt_tokens"] == 120
    assert openai_stats({}, {"prompt_tokens": 777, "completion_tokens": 3}, None, 0)["prompt_total"] == 777


def test_context_event_reports_usage_and_trimming(cfg, memory):
    class LLM:
        context_size = 8192

        async def chat_stream(self, messages, tools=None):
            yield {"type": "done", "message": {"role": "assistant", "content": "ok"},
                   "stats": {"tps": 20.0, "prompt_total": 3456}}

    events = []

    async def emit(e):
        events.append(e)

    async def confirm(*a):
        return False

    agent = Agent(cfg, LLM(), memory)
    run(agent.run("Hallo", emit, confirm))
    ctx_events = [e for e in events if e["type"] == "context"]
    first, last = ctx_events[0], ctx_events[-1]
    assert first["window"] == 8192 and first["budget"] == 8192 - 1024  # kleines Fenster: knappere Antwortreserve
    parts = first["parts"]
    assert parts["tools"] > 0 and parts["system"] > 0 and parts["history"] > 0
    assert first["used"] == sum(parts.values()) and not first["trimmed"]
    assert last["real"] == 3456 and agent.last_context["real"] == 3456

    # sehr langer Verlauf → wird vor dem Schritt zusammengefasst, die Anzeige meldet das
    for i in range(80):
        memory.conversation.add({"role": "user", "content": f"Frage {i} " + "x" * 900})
        memory.conversation.add({"role": "assistant", "content": "y" * 900})
    events.clear()
    run(agent.run("Und?", emit, confirm))
    assert any(e.get("phase") == "compress" for e in events)
    ev = [e for e in events if e["type"] == "context"][-1]
    assert ev["summarized"] and ev["used"] <= ev["budget"] and not ev["trimmed"]
