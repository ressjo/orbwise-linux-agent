"""Kontext-Optimierung: stabiler Prompt-Anfang (KV-Cache), eingefrorene Kontext-Notizen, kalibrierte Schätzung."""

from datetime import datetime

from conftest import run

from orbwise import prompts
from orbwise.agent import Agent
from orbwise.memory.index import Hit


def hit(text: str, id_: int = 1) -> Hit:
    return Hit(id=id_, kind="journal", day="2026-09-20", text=text, created=0.0, score=1.0)


def test_context_note_is_frozen_once_sent(cfg, memory, llm):
    agent = Agent(cfg, llm, memory)
    conv = memory.conversation
    conv.add({"role": "user", "content": "Wie heißt mein NAS?"})
    agent._turn_stamp = "Freitag, 3. Oktober 2026, 14:03 Uhr"
    first = agent.build_messages([hit("NAS Tresor unter 10.0.0.5")])
    conv.add({"role": "assistant", "content": "Tresor."})
    conv.add({"role": "user", "content": "Und welche IP hat es?"})
    agent._turn_stamp = "Freitag, 3. Oktober 2026, 14:07 Uhr"
    second = agent.build_messages([hit("Router unter 10.0.0.1", 2)])
    # Was einmal gesendet wurde, bleibt Byte für Byte gleich – der Server liest nur das Neue ein
    assert second[:len(first)] == first
    assert "14:0" not in first[0]["content"] and "10.0.0" not in first[0]["content"]  # nichts Wechselndes im System
    assert first[-1]["content"].startswith("[Kontext – nicht vom Nutzer geschrieben: Freitag, 3. Oktober 2026, 14:03")
    assert "NAS Tresor unter 10.0.0.5" in first[-1]["content"] and first[-1]["content"].endswith("Wie heißt mein NAS?")
    assert "Router unter 10.0.0.1" in second[-1]["content"] and "14:07" in second[-1]["content"]
    # gespeicherter Inhalt bleibt sauber (die Notiz steht getrennt daneben)
    assert [m["content"] for m in conv.history if m["role"] == "user"] == ["Wie heißt mein NAS?",
                                                                            "Und welche IP hat es?"]
    assert prompts.strip_context_note(second[-1]["content"]) == "Und welche IP hat es?"
    # erneut gebaut (z. B. nächster Werkzeugschritt): identisch, auch wenn sich Uhrzeit/Treffer ändern würden
    agent._turn_stamp = "Freitag, 3. Oktober 2026, 15:00 Uhr"
    assert agent.build_messages([hit("ganz anders", 3)]) == second


def test_agent_answer_ignores_context_note(cfg, memory, llm):
    agent = Agent(cfg, llm, memory)

    async def emit(ev):
        pass

    async def confirm(*a):
        return False

    answer = run(agent.run("Hallo Orbwise", emit, confirm))
    assert "Hallo Orbwise" in answer and "Kontext" not in answer
    assert memory.conversation.history[0]["content"] == "Hallo Orbwise"


def test_token_estimate_learns_from_server(cfg, memory, llm):
    agent = Agent(cfg, llm, memory)
    base = agent.context_budget()
    agent.learn_tokens(4000, 5000)  # Server zählt mehr Token als geschätzt → Budget vorsichtiger
    assert agent.token_ratio() == 1.25 and agent.context_budget() < base
    for _ in range(20):
        agent.learn_tokens(4000, 1000)  # weit darunter → Faktor sinkt, aber nicht unter 0,5
    assert agent.token_ratio() >= 0.5 and agent.context_budget() > base
    agent.learn_tokens(100, 5000)  # zu kleine Stichprobe → ignoriert
    assert agent.token_ratio() >= 0.5
    for _ in range(6):  # Gemeldet (Llama): Server las 21k, die Anzeige blieb bei ~7k (Faktor war auf 1,3 begrenzt)
        agent.learn_tokens(7000, 21000)
    assert agent.token_ratio() > 2.8


def test_context_event_reports_cache_and_condensed(cfg, memory, llm):
    agent = Agent(cfg, llm, memory)
    events = []

    async def emit(ev):
        events.append(ev)

    async def confirm(*a):
        return False

    original = llm.chat_stream

    async def with_cache(messages, tools=None, **kw):
        async for ev in original(messages, tools, **kw):
            if ev["type"] == "done":
                ev = {**ev, "stats": {**ev.get("stats", {}), "prompt_total": 900, "prompt_cached": 850}}
            yield ev

    llm.chat_stream = with_cache
    memory.conversation.running_summary = "- Nutzer richtet Llama ein"
    run(agent.run("Wie geht's?", emit, confirm))
    ctx = [e for e in events if e["type"] == "context"][-1]
    assert ctx["real"] == 900 and ctx["cached"] == 850 and ctx["summarized"] is True
    assert ctx["compact_at"] < ctx["window"] and agent.last_context["real"] == 900
    assert datetime.now().strftime("%H:") in agent._turn_time
