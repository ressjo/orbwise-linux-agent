"""Getrenntes Gedächtnis: Tools-Chats haben eins, jedes Coding-Projekt sein eigenes – plus höhere Schrittgrenze
im Coding-Modus."""

from conftest import run

from orbwise.agent import Agent
from orbwise.memory import Memory, project_id
from orbwise.tools.memory_tools import recall
from orbwise.tools.registry import ToolContext


def _coding(memory, project):
    memory.switch_mode("coding")
    memory.new_chat()
    memory.set_project(str(project))
    return memory.conversation


def _tools(memory):
    memory.switch_mode("tools")
    return memory.conversation


def _recall(cfg, memory, **kw):
    ctx = ToolContext(cfg=cfg, memory=memory)
    return run(recall.fn(ctx, **kw)) if hasattr(recall, "fn") else run(recall(ctx, **kw))


def test_facts_are_separate_per_project_and_from_tools(memory, cfg, tmp_path):
    a, b = tmp_path / "alpha", tmp_path / "beta"
    a.mkdir(), b.mkdir()
    _tools(memory)
    run(memory.remember("Der Nutzer heißt Kim."))
    _coding(memory, a)
    run(memory.remember("Alpha nutzt FastAPI und pytest."))
    assert "FastAPI" in memory.facts_text() and "Kim" not in memory.facts_text()
    assert (cfg.memory.dir / "projects" / project_id(str(a)) / "facts.md").read_text().count("FastAPI") == 1
    _coding(memory, b)
    assert memory.facts_text() == ""
    run(memory.remember("Beta ist ein Rust-Projekt."))
    assert run(memory.forget("FastAPI")) == []  # andere Projekte bleiben unberührt
    _tools(memory)
    assert "Kim" in memory.facts_text() and "FastAPI" not in memory.facts_text() and "Rust" not in memory.facts_text()
    assert [f for f, _ in memory.facts.list()] == ["Der Nutzer heißt Kim."]


def test_conversations_are_found_only_in_their_scope(memory, cfg, tmp_path):
    a, b = tmp_path / "alpha", tmp_path / "beta"
    a.mkdir(), b.mkdir()
    _coding(memory, a)
    run(memory.log_exchange("Wie starte ich den Webserver?", "Mit uvicorn app:main.", []))
    _coding(memory, b)
    run(memory.log_exchange("Wie baue ich den Webserver?", "Mit cargo build.", []))
    _tools(memory)
    run(memory.log_exchange("Wie wird das Wetter beim Webserver-Umzug?", "Sonnig.", []))

    memory.new_chat()
    tools_hits = run(memory.retrieve("Webserver"))
    assert tools_hits and all(h.kind == "journal" for h in tools_hits)
    assert "uvicorn" not in _recall(cfg, memory, query="Webserver")

    _coding(memory, a)
    out = _recall(cfg, memory, query="Webserver")
    assert "uvicorn" in out and "cargo" not in out and "Sonnig" not in out
    # Coding-Protokolle stehen im Projekt-Tagebuch, nicht im allgemeinen (→ keine Tageszusammenfassung)
    assert "uvicorn" not in "".join(memory.journal.read(d) or "" for d in memory.journal.days())
    assert any("uvicorn" in (memory.scope_journal().read(d) or "") for d in memory.scope_journal().days())


def test_old_coding_chats_move_to_their_project_and_rebuild_keeps_it(cfg, llm, tmp_path):
    proj = tmp_path / "alt"
    proj.mkdir()
    mem = Memory(cfg.memory, llm)
    try:
        run(mem.remember("Der Nutzer mag Tee."))
        mem.switch_mode("coding")
        mem.set_project(str(proj))
        chat = mem.conversation.chat_id
        mem.conversation.add({"role": "user", "content": "Datenbank-Migration"})
        mem.conversation.save()
        # So schrieben ältere Versionen: Coding-Austausch im allgemeinen Tagebuch, Index-Art "journal"
        mem.journal.append("Du", "Datenbank-Migration mit alembic?", chat=chat)
        mem.journal.append("Orbwise", "Ja, alembic upgrade head.")
        run(mem.index.add("journal", "2026-01-01", f"chat:{chat}", "Nutzer: Datenbank-Migration mit alembic"))
        (cfg.memory.dir / ".scopes_v1").unlink()
    finally:
        mem.close()

    mem = Memory(cfg.memory, llm)  # Start → Migration
    try:
        pid = project_id(str(proj))
        mem.switch_mode("tools")
        assert run(mem.retrieve("alembic")) == [] and "Tee" in mem.facts_text()
        mem.switch_mode("coding")
        assert [h.kind for h in run(mem.retrieve("alembic"))] == [f"code:{pid}"]
        assert mem.facts_text() == ""  # Fakten bleiben im Tools-Gedächtnis

        run(mem.rebuild_index())
        assert {h.kind for h in run(mem.retrieve("alembic"))} == {f"code:{pid}"}
        mem.switch_mode("tools")
        assert run(mem.retrieve("alembic")) == [] and run(mem.retrieve("Tee"))
    finally:
        mem.close()


def test_coding_has_its_own_higher_step_limit(cfg, llm, memory, tmp_path, monkeypatch):
    executed = []

    async def fake_exec(self, call, emit, confirm):
        executed.append(call)
        return "glob", f"ok {len(executed)}", "glob: ok"

    monkeypatch.setattr(Agent, "_execute", fake_exec)
    cfg.tools.max_steps, cfg.coding.max_steps = 3, 8
    calls = iter(range(100))

    async def stream(messages, tools=None, **kw):
        if tools and len(executed) < 6:
            msg = {"role": "assistant", "content": "", "tool_calls": [
                {"function": {"name": "glob", "arguments": {"pattern": f"*{next(calls)}"}}}]}
        else:
            msg = {"role": "assistant", "content": "Fertig."}
            yield {"type": "token", "text": "Fertig."}
        yield {"type": "done", "message": msg, "stats": {}}
    llm.chat_stream = stream

    async def emit(ev):
        pass

    async def confirm(*a):
        return True

    _coding(memory, tmp_path)
    agent = Agent(cfg, llm, memory)
    assert agent.max_steps() == 8
    answer = run(agent.run("Baue alles", emit, confirm))
    assert len(executed) == 6 and "mach weiter" not in answer

    _tools(memory)
    assert Agent(cfg, llm, memory).max_steps() == 3
