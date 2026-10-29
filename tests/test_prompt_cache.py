"""Prompt-Cache von llama-server im RAM (--cache-ram): aus dem Server-Log gelesen, nur vom laufenden Server."""

from orbwise.llm_memory import prompt_cache

LOG = """===== Starte: llama-server -m alt.gguf
srv  update:  - cache state: 5 prompts, 9000.000 MiB (limits: 9000.000 MiB, 0 tokens, 100000 est)
===== Starte: llama-server -m neu.gguf --cache-ram 8192
srv  update:  - cache state: 1 prompts, 300.500 MiB (limits: 8192.000 MiB, 0 tokens, 90000 est)
srv  update:    - prompt 0x55d1:    4000 tokens, checkpoints:  1,   300.500 MiB
srv  log_server_r: request: POST /v1/chat/completions 127.0.0.1 200
srv  update:  - cache state: 2 prompts, 1234.567 MiB (limits: 8192.000 MiB, 0 tokens, 90000 est)
srv  update:    - prompt 0x55d1:    4000 tokens, checkpoints:  1,   300.500 MiB
srv  update:    - prompt 0x77aa:   12345 tokens, checkpoints:  2,   934.067 MiB
srv  log_server_r: request: GET /props 127.0.0.1 200
"""


def test_latest_state_of_the_running_server():
    assert prompt_cache(LOG) == {"prompts": 2, "mib": 1234.567, "limit_mib": 8192.0, "tokens": 16345}


def test_nothing_reported_yet():
    assert prompt_cache("===== Starte: llama-server -m x.gguf\nsrv  main: server is listening\n") is None
    assert prompt_cache(LOG.split("===== Starte: llama-server -m neu")[0] + "===== Starte: neu\n") is None
    assert prompt_cache("") is None


def test_memory_endpoint_reports_the_prompt_cache(cfg, tmp_path, monkeypatch):
    """/api/llm/memory liest den Stand aus dem Log (nur bei einem eigenen llama-server)."""
    from fastapi.testclient import TestClient

    from orbwise import llm_router
    from orbwise.config import LLMConfig, ProfileConfig, ServerConfig
    from orbwise.server import create_app
    log = tmp_path / "orbwise-llm.log"
    log.write_text(LOG)
    monkeypatch.setattr(llm_router, "_log_path", lambda: log)

    async def no_start(self, progress=None):  # kein echter llama-server – sein Start schriebe ins Log
        return None
    monkeypatch.setattr(llm_router.LLMRouter, "start", no_start)
    monkeypatch.setenv("ORBWISE_SKIP_WARMUP", "1")
    monkeypatch.delenv("ORBWISE_FAKE_LLM", raising=False)
    cfg.llm = LLMConfig(profiles={"big": ProfileConfig(backend="openai", base_url="http://127.0.0.1:9/v1", model="a",
                                                       server=ServerConfig(command="llama-server -m /m/a.gguf"))},
                        active="big")
    with TestClient(create_app(cfg), base_url="http://localhost:8765") as client:
        data = client.get("/api/llm/memory").json()
    assert data["prompt_cache"] == {"prompts": 2, "mib": 1234.567, "limit_mib": 8192.0, "tokens": 16345}
