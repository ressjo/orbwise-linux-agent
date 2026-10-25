"""llm_phase: die Aktivität zeigt, was das Modell zwischen den Werkzeugen tut (einlesen, denken, schreiben …)."""

from types import SimpleNamespace

from conftest import run

from orbwise.agent import Agent
from orbwise.llm import ContextOverflow, FakeLLM


def collect(agent, text, **kw):
    events = []

    async def emit(ev):
        events.append(ev)

    async def confirm(*a):
        return True

    run(agent.run(text, emit, confirm, **kw))
    return events


def phases(events):
    return [(e["phase"], e["id"]) for e in events if e["type"] == "llm_phase"]


def test_phases_of_a_run_with_a_tool(cfg, llm, memory):
    events = collect(Agent(cfg, llm, memory), "/tool system_info {}")
    seq = phases(events)
    names = [p for p, _ in seq]
    assert names == ["prompt", "tool_args", "done", "prompt", "writing", "done"]
    assert len({i for _, i in seq[:3]}) == 1 and seq[0][1] != seq[3][1]  # eine Zeile je Modellschritt
    ph = [e for e in events if e["type"] == "llm_phase"]
    assert ph[1]["name"] == "system_info" and ph[2]["calls"] == ["system_info"]
    assert ph[2]["prompt_ms"] == 842 and ph[2]["tps"] == 42.0 and "seconds" in ph[2]
    assert "eta_s" not in ph[0] and ph[3]["eta_s"] >= 0  # Einlese-Geschwindigkeit wurde im ersten Schritt gelernt
    # Phasen kommen vor dem Inhalt: „antwortet“ steht vor dem ersten Token
    first_token = next(i for i, e in enumerate(events) if e["type"] == "token")
    assert events[first_token - 1].get("phase") == "writing"


def test_thinking_phase_is_throttled(cfg, llm, memory):
    events = collect(Agent(cfg, llm, memory), "Hallo", think=True)
    thinking = [e for e in events if e.get("phase") == "thinking"]
    reasoning = [e for e in events if e["type"] == "reasoning"]
    assert thinking and len(thinking) < len(reasoning) and thinking[0]["tokens"] == 1


def test_retry_when_the_context_overflows(cfg, memory):
    class Overflowing(FakeLLM):
        failed = False

        async def chat_stream(self, messages, tools=None, think=None):
            if not self.failed:
                self.failed = True
                raise ContextOverflow("zu groß", n_ctx=8192, n_prompt=9000)
            async for ev in super().chat_stream(messages, tools, think):
                yield ev

    events = collect(Agent(cfg, Overflowing(delay=0), memory), "Hallo")
    names = [p for p, _ in phases(events)]
    assert names == ["prompt", "retry", "prompt", "writing", "done"]
    retry = next(e for e in events if e.get("phase") == "retry")
    assert retry["n_prompt"] == 9000 and retry["n_ctx"] == 8192


def test_compacting_is_shown(cfg, llm, memory):
    for i in range(12):
        memory.conversation.add({"role": "user", "content": f"Frage {i} " + "x" * 2000})
        memory.conversation.add({"role": "assistant", "content": f"Antwort {i}"})
    agent = Agent(cfg, llm, memory)
    events = []

    async def emit(ev):
        events.append(ev)

    assert run(agent.compact_now(emit))
    rows = [e for e in events if e["type"] == "llm_phase"]
    start = next(e for e in rows if e["phase"] == "compress")
    done = next(e for e in rows if e.get("compress"))
    assert start["id"] == done["id"] and done["after"] < done["before"] and done["seconds"] >= 0
    assert any(e["type"] == "compacted" and e["summary"] for e in events)
    assert any(e.get("prewarm") for e in rows)  # danach gleich vorgewärmt – die nächste Frage wartet nicht


def test_prompt_phase_reports_only_the_new_part(cfg, llm, memory):
    agent = Agent(cfg, llm, memory)
    agent.llm = SimpleNamespace(profile=SimpleNamespace(backend="ollama"), active="local")
    agent.last_context = {"used": 5000}
    agent._last_prompt["local"], agent._prefill_tps["local"] = 4000, 500.0
    info = agent._prompt_phase()
    assert info == {"phase": "prompt", "tokens": 5000, "new": 1000, "eta_s": 2.0}  # nur der neue Teil zählt
