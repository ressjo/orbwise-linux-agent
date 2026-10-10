"""Kontext-Selbsttest (orbwise context-test): erkennt falsche Fenstergröße, fehlenden Prompt-Cache, stilles Kürzen,
RAM-Auslagerung und eine Obergrenze in der Config – gegen einen simulierten Modell-Server."""

import json
import re

from conftest import run

from orbwise.agent import Agent
from orbwise.config import ProfileConfig
from orbwise.context_test import format_report, run_context_test
from orbwise.llm import ContextOverflow

MIB = 1024 ** 2


class Server:
    """Simulierter llama-server: zählt Token (≈ 2,5 Zeichen), merkt sich den letzten Prompt für den Cache,
    lehnt zu große Prompts ab und beantwortet die Codewort-Frage."""

    def __init__(self, window=8192, reported=None, cache=True, truncate=False, overflow_error=True,
                 kv_ram=0, remembers=True, flash=None):
        self.context_size = window
        self.window = reported or window
        self.profile = ProfileConfig(backend="openai", base_url="http://x/v1", model="bonsai", num_ctx=window)
        self.active = "bonsai"
        self.client = self
        self.cache, self.truncate, self.overflow_error = cache, truncate, overflow_error
        self.kv_ram, self.remembers, self.flash = kv_ram, remembers, flash
        self.last = ""
        self.calls = 0

    async def server_context(self):
        return self.window

    async def memory_info(self):
        kv = self.window * 18 * 1024
        return {"ctx": self.window, "kv_per_token": 18 * 1024, "kv_vram": kv - self.kv_ram, "kv_ram": self.kv_ram,
                "model_vram": 4000 * MIB, "model_ram": 0, "model_ram_offload": 0, "source": "log",
                "flash_attn": self.flash}, ""

    async def chat_stream(self, messages, tools=None, **kw):
        self.calls += 1
        # wie die Chat-Vorlage: Werkzeuge stehen vorn im Systemprompt, danach die Nachrichten
        text = json.dumps(tools or [], ensure_ascii=False) + json.dumps(messages, ensure_ascii=False)
        total = int(len(text) / 2.5)
        if total > self.window:
            if self.overflow_error and not self.truncate:
                raise ContextOverflow("Kontext zu klein", n_ctx=self.window, n_prompt=total)
            total, text = self.window, text[-int(self.window * 2.5):]  # still gekürzt: der Anfang fehlt
        common = 0
        if self.cache:
            while common < min(len(text), len(self.last)) and text[common] == self.last[common]:
                common += 1
        self.last = text
        cached = min(total - 1, int(common / 2.5)) if common else 0
        answer = "OK"
        found = re.search(r"Codewort: (\S+)", text)
        if found and self.remembers:
            answer = found.group(1).strip('\\"')
        yield {"type": "done", "message": {"role": "assistant", "content": answer},
               "stats": {"prompt_tokens": total - cached, "prompt_total": total, "prompt_cached": cached,
                         "prompt_tps": 500.0}}


def _test(cfg, memory, server, quick=False):
    agent = Agent(cfg, server, memory)
    return agent, run(run_context_test(agent, server, quick=quick))


def _status(result) -> dict:
    return {c["id"]: c["status"] for c in result["checks"]}


def test_everything_fine(cfg, memory):
    agent, result = _test(cfg, memory, Server(8192))
    st = _status(result)
    assert st["window"] == st["memory"] == st["cache"] == st["followup"] == st["fill"] == st["overflow"] == "ok"
    assert "budget" not in st
    assert result["small"] is True and result["compact_at"] == agent.compact_limit()
    fill = next(c for c in result["checks"] if c["id"] == "fill")["detail"]
    pct = int(re.search(r"\((\d+) %", fill).group(1))
    assert 80 <= pct <= 100  # bis an den Komprimierungspunkt gefüllt, ohne Überlauf
    assert agent.token_ratio() != 1.0  # echte Zahlen des Servers wurden gelernt
    assert "✔" in format_report(result)


def test_large_window_stage(cfg, memory):
    _, result = _test(cfg, memory, Server(32768), quick=True)
    assert result["small"] is False
    assert "fill" not in _status(result) and "overflow" not in _status(result)  # Schnelltest


def test_wrong_window_is_reported(cfg, memory):
    _, result = _test(cfg, memory, Server(16384, reported=8192), quick=True)
    assert _status(result)["window"] == "fail"
    assert result["status"] == "fail"


def test_missing_prompt_cache(cfg, memory):
    _, result = _test(cfg, memory, Server(8192, cache=False), quick=True)
    st = _status(result)
    assert st["cache"] == "fail" and st["followup"] == "fail"


def test_silent_truncation_and_overflow(cfg, memory):
    _, result = _test(cfg, memory, Server(8192, reported=None, truncate=True))
    st = _status(result)
    assert st["overflow"] == "warn"  # zu großer Prompt ohne Fehler angenommen


def test_model_loses_start_of_full_window(cfg, memory):
    _, result = _test(cfg, memory, Server(8192, remembers=False))
    assert _status(result)["fill"] == "warn"


def test_kv_cache_in_ram(cfg, memory):
    _, result = _test(cfg, memory, Server(8192, kv_ram=300 * MIB), quick=True)
    assert _status(result)["memory"] == "fail"


def test_config_cap_is_flagged(cfg, memory):
    cfg.memory.context_budget_tokens = 6000
    _, result = _test(cfg, memory, Server(65536), quick=True)
    assert _status(result)["budget"] == "warn"


def test_open_chat_untouched(cfg, memory):
    memory.conversation.add({"role": "user", "content": "Hallo"})
    before = list(memory.conversation.history)
    _test(cfg, memory, Server(8192))
    assert memory.conversation.history == before


def test_api_in_demo_mode(cfg, monkeypatch):
    from fastapi.testclient import TestClient

    from orbwise.server import create_app
    monkeypatch.setenv("ORBWISE_FAKE_LLM", "1")
    monkeypatch.setenv("ORBWISE_SKIP_WARMUP", "1")
    with TestClient(create_app(cfg), base_url="http://localhost:8765") as client:
        assert client.post("/api/context/test").status_code == 400


def test_cli_without_running_server(cfg, monkeypatch, capsys):
    import httpx
    import pytest

    from orbwise import cli

    def refuse(*a, **kw):
        raise httpx.ConnectError("nope")
    monkeypatch.setattr(cli, "load_config", lambda: cfg)
    monkeypatch.setattr(httpx, "post", refuse)
    with pytest.raises(SystemExit) as e:
        cli.main(["context-test", "--quick"])
    assert "orbwise serve" in str(e.value)


def test_cache_judged_by_time_without_cache_numbers(cfg, memory):
    """Manche llama-server melden bei max_tokens=1 keine Timings – usage zählt dann die Cache-Token mit."""
    class NoNumbers(Server):
        async def chat_stream(self, messages, tools=None, **kw):
            async for ev in super().chat_stream(messages, tools, **kw):
                st = ev["stats"]
                cold = st["prompt_tokens"] > 0.5 * st["prompt_total"]
                if cold:
                    import asyncio
                    await asyncio.sleep(2.1)
                st["prompt_tokens"] = st["prompt_total"]
                yield ev
    _, result = _test(cfg, memory, NoNumbers(8192), quick=True)
    assert _status(result)["cache"] == "ok"


def test_stats_use_timings_without_generation_speed():
    from orbwise.llm import openai_stats
    st = openai_stats({"prompt_n": 3, "cache_n": 2638, "predicted_n": 1}, {"prompt_tokens": 2641}, None, 0)
    assert st["prompt_tokens"] == 3 and st["prompt_total"] == 2641 and st["prompt_cached"] == 2638


def test_flash_attention_from_the_log(cfg, memory):
    assert "flash" not in _status(_test(cfg, memory, Server(8192), quick=True)[1])  # unbekannt: kein Prüfpunkt
    assert _status(_test(cfg, memory, Server(8192, flash="on"), quick=True)[1])["flash"] == "ok"
    _, result = _test(cfg, memory, Server(8192, flash="off"), quick=True)
    check = next(c for c in result["checks"] if c["id"] == "flash")
    assert check["status"] == "warn" and "-fa on" in check["detail"]
