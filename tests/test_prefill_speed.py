"""Alle Werkzeuge nur, wenn ihr Einlesen kurz ist: Auf langsamen Karten (Bonsai auf 8 GB, ~140 Token/s) kostet ein
Start-Prompt mit allen ~16k Token Werkzeugbeschreibungen über 2 Minuten – nach jedem Start, Modellwechsel und jeder
Komprimierung. Die gelernte Geschwindigkeit wird gemerkt; das Vorwärmen zeigt, was aus dem Cache kam."""

import json

from conftest import run
from test_prompt_size import all_integrations

from orbwise.agent import Agent
from orbwise.config import ProfileConfig


async def _noop(*a):
    return True


class Server:
    def __init__(self, window=65536, tps=None, cached=0):
        self.context_size = window
        self.profile = ProfileConfig(backend="openai", base_url="http://x/v1", model="bonsai", num_ctx=window)
        self.active = "bonsai"
        self.tps, self.cached = tps, cached

    async def chat_stream(self, messages, tools=None, **kw):
        total = int(len(json.dumps([tools, messages], ensure_ascii=False)) / 3)
        stats = {"prompt_total": total, "prompt_tokens": total - self.cached}
        if self.tps:
            stats["prompt_tps"] = self.tps
        yield {"type": "done", "message": {"role": "assistant", "content": "ok"}, "stats": stats}


def _tools(agent):
    return {s["function"]["name"] for s in agent.schemas}


def test_fast_card_keeps_all_tools(cfg, memory, tmp_path):
    all_integrations(cfg, tmp_path)
    agent = Agent(cfg, Server(tps=3000), memory)
    agent._prefill_tps["bonsai"] = 3000.0
    agent.choose_tools(set())
    assert agent.memory.conversation.epoch["tools"] is None  # alle, ohne load_tools
    assert "paperless_search" in _tools(agent) and "load_tools" not in _tools(agent)


def test_slow_card_starts_with_selection(cfg, memory, tmp_path):
    all_integrations(cfg, tmp_path)
    agent = Agent(cfg, Server(), memory)
    agent._prefill_tps["bonsai"] = 140.0
    agent.choose_tools(set())
    names = _tools(agent)
    assert "load_tools" in names and "paperless_search" not in names and "run_shell" in names
    all_tokens = agent.all_schema_tokens
    assert agent.schema_tokens < 0.5 * all_tokens


def test_unknown_speed_is_learned_saved_and_reused(cfg, memory, tmp_path):
    all_integrations(cfg, tmp_path)
    agent = Agent(cfg, Server(tps=140), memory)
    assert run(agent.prewarm()) is True  # unbekannt: erst alle – dabei gemessen
    state = json.loads((cfg.memory.dir.parent / "state.json").read_text())
    assert state["prefill_tps"]["bonsai"] == 140.0
    again = Agent(cfg, Server(), memory)  # nach Neustart sofort bekannt
    again.choose_tools(set())
    assert "load_tools" in _tools(again)


def test_prewarm_reports_cache_share(cfg, memory, tmp_path):
    events = []

    async def emit(ev):
        events.append(ev)
    agent = Agent(cfg, Server(tps=500, cached=1000), memory)
    agent._prefill_tps["bonsai"] = 500.0
    assert run(agent.prewarm(emit))
    start = next(e for e in events if e.get("phase") == "prewarm")
    done = next(e for e in events if e.get("prewarm"))
    assert start["eta_s"] > 0
    assert done["new"] == done["tokens"] - 1000


def test_no_flip_flop_near_threshold(cfg, memory, tmp_path):
    all_integrations(cfg, tmp_path)
    agent = Agent(cfg, Server(), memory)
    tokens = agent.all_schema_tokens
    agent._prefill_tps["bonsai"] = tokens / 9.5  # knapp unter 10 s → alle
    agent.choose_tools(set())
    assert agent.memory.conversation.epoch["tools"] is None
    agent._prefill_tps["bonsai"] = tokens / 11  # etwas langsamer gemessen → bleibt bei allen (Abstand)
    agent.choose_tools(set())
    assert agent.memory.conversation.epoch["tools"] is None
