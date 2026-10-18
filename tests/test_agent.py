import asyncio

from conftest import run

from orbwise.agent import Agent, ThinkFilter


def make_agent(cfg, llm, memory):
    return Agent(cfg, llm, memory)


async def collect(agent, text, approve=None):
    events = []
    confirms = []

    async def emit(e):
        events.append(e)

    async def confirm(call_id, name, args, reason):
        confirms.append((name, args, reason))
        return approve

    answer = await agent.run(text, emit, confirm)
    return answer, events, confirms


def test_plain_answer_logged_to_journal(cfg, llm, memory):
    agent = make_agent(cfg, llm, memory)
    answer, events, _ = run(collect(agent, "Hallo Jarvis"))
    assert "Hallo Jarvis" in answer
    assert any(e["type"] == "token" for e in events)
    assert [e["type"] for e in events][-2:] == ["assistant_end", "state"] and events[-1]["state"] == "idle"
    day = memory.journal.days()[0]
    assert "Hallo Jarvis" in memory.journal.read(day)


def test_safe_tool_runs_without_confirmation(cfg, llm, memory, tmp_path):
    (tmp_path / "rechnung_2026.pdf").write_text("x")
    agent = make_agent(cfg, llm, memory)
    answer, events, confirms = run(collect(agent, f'/tool find_files {{"query": "rechnung", "path": "{tmp_path}"}}'))
    assert not confirms
    result = next(e for e in events if e["type"] == "tool_result")
    assert result["status"] == "ok" and "rechnung_2026.pdf" in result["text"]
    assert answer.startswith("Erledigt")


def test_risky_tool_requires_confirmation(cfg, llm, memory, tmp_path):
    target = tmp_path / "x.txt"
    agent = make_agent(cfg, llm, memory)
    cmd = f'/tool run_shell {{"command": "touch {target}"}}'
    _, events, confirms = run(collect(agent, cmd, approve=False))
    assert confirms and confirms[0][0] == "run_shell"
    assert not target.exists()
    assert next(e for e in events if e["type"] == "tool_result")["status"] == "denied"

    _, events, _ = run(collect(agent, cmd, approve=True))
    assert target.exists()


def test_blocked_command_never_runs(cfg, llm, memory):
    agent = make_agent(cfg, llm, memory)
    _, events, confirms = run(collect(agent, '/tool run_shell {"command": "rm -rf ~"}', approve=True))
    assert not confirms
    assert next(e for e in events if e["type"] == "tool_result")["status"] == "blocked"


def test_remember_tool_and_context(cfg, llm, memory):
    agent = make_agent(cfg, llm, memory)
    run(collect(agent, '/tool remember {"fact": "Der Nutzer heißt Alex."}'))
    msgs = agent.build_messages([])
    # Im laufenden Chat bleibt der System-Prompt gleich (sonst müsste der Server alles neu einlesen) –
    # den neuen Fakt kennt das Modell aus seinem eigenen Werkzeug-Aufruf
    assert "Der Nutzer heißt Alex." not in msgs[0]["content"]
    assert "Der Nutzer heißt Alex." in str(msgs[1:])
    memory.new_chat()  # neuer Chat (ebenso nach einer Komprimierung): Fakt steht im System-Prompt
    assert "Der Nutzer heißt Alex." in agent.build_messages()[0]["content"]


def test_history_consistent_after_cancel(cfg, llm, memory):
    agent = make_agent(cfg, llm, memory)

    async def scenario():
        async def emit(e):
            pass

        async def confirm(*a):
            await asyncio.sleep(10)
            return True

        task = asyncio.create_task(agent.run('/tool run_shell {"command": "touch /tmp/orbwise-x"}', emit, confirm))
        await asyncio.sleep(0.2)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    run(scenario())
    hist = memory.conversation.history
    assert hist[-2]["role"] == "tool" and hist[-2]["content"] == "[abgebrochen]"
    assert hist[-1]["role"] == "assistant"


def test_think_filter():
    f = ThinkFilter()
    out = "".join(f.feed(t) for t in ["Hal", "lo <thi", "nk>geheim</th", "ink> Welt"]) + f.flush()
    assert out == "Hallo  Welt"


class ToolHappyLLM:
    """Ruft immer wieder Tools auf; ohne Tools (Zwischenbilanz) antwortet es mit Text."""

    def __init__(self, same_args=False):
        self.same_args = same_args
        self.calls = 0
        self.final_requests = []

    async def chat_stream(self, messages, tools=None):
        if "Schrittlimit" in (messages[-1].get("content") or ""):  # Zwischenbilanz (Werkzeugliste bleibt gleich)
            self.final_requests.append(messages[-1]["content"])
            yield {"type": "token", "text": "Bisher erledigt: X."}
            yield {"type": "done", "message": {"role": "assistant", "content": "Bisher erledigt: X."}, "stats": {}}
            return
        self.calls += 1
        args = {"note": "gleich"} if self.same_args else {"note": f"n{self.calls}"}
        yield {"type": "done", "message": {"role": "assistant", "content": "", "tool_calls": [
            {"function": {"name": "system_info", "arguments": {}}},
        ] if not self.same_args else [{"function": {"name": "system_info", "arguments": args}}]}, "stats": {}}


def _run_with(cfg, memory, llm):
    agent = Agent(cfg, llm, memory)
    return run(collect(agent, "Mach was Großes"))


def test_step_limit_from_config_ends_with_summary(cfg, memory, monkeypatch):
    from orbwise.agent import Agent as A
    executed = []

    async def fake_exec(self, call, emit, confirm):
        executed.append(call)
        return "system_info", f"ok {len(executed)}", "system_info: ok"

    monkeypatch.setattr(A, "_execute", fake_exec)
    cfg.tools.max_steps = 4

    class Varying(ToolHappyLLM):
        async def chat_stream(self, messages, tools=None):
            if "Schrittlimit" in (messages[-1].get("content") or ""):
                async for ev in super().chat_stream(messages, None):
                    yield ev
                return
            self.calls += 1
            yield {"type": "done", "message": {"role": "assistant", "content": "", "tool_calls": [
                {"function": {"name": "system_info", "arguments": {"i": self.calls}}}]}, "stats": {}}

    llm = Varying()
    answer, events, _ = _run_with(cfg, memory, llm)
    assert llm.calls == 4 and len(executed) == 4
    assert len(llm.final_requests) == 1 and "Schrittlimit" in llm.final_requests[0]
    assert "Bisher erledigt: X." in answer and "mach weiter" in answer
    hist = memory.conversation.history
    assert hist[-1]["role"] == "assistant" and "mach weiter" in hist[-1]["content"]


def test_repeated_identical_calls_are_skipped_and_stopped(cfg, memory, monkeypatch):
    from orbwise.agent import Agent as A
    executed = []

    async def fake_exec(self, call, emit, confirm):
        executed.append(call)
        return "system_info", "ok", "system_info: ok"

    monkeypatch.setattr(A, "_execute", fake_exec)
    cfg.tools.max_steps = 25
    llm = ToolHappyLLM(same_args=True)
    answer, _, _ = _run_with(cfg, memory, llm)
    assert len(executed) == 2          # 3. und 4. identischer Aufruf werden nicht ausgeführt
    assert llm.calls == 4              # nach der 4. Wiederholung Schluss statt bis 25
    assert "mach weiter" in answer
    skipped = [m for m in memory.conversation.history if m["role"] == "tool" and "bereits ausgeführt" in m["content"]]
    assert len(skipped) == 2


def test_cached_prompt_counts_are_not_taken_as_prompt_size():
    """Ollama meldet bei Cache-Treffern nur die neuen Token – das ist keine Prompt-Größe."""
    from orbwise.agent import prompt_size

    assert prompt_size({"prompt_total": 300}, 6000) == 0          # Cache-Treffer
    assert prompt_size({"prompt_total": 5400}, 6000) == 5400      # echte Größe
    assert prompt_size({}, 6000) == 0
    assert prompt_size({"prompt_total": 900, "prompt_cached": 850}, 6000) == 900  # llama-server: volle Größe


def test_auto_read_off_asks_even_for_read_only_shell(cfg, llm, memory):
    agent = Agent(cfg, llm, memory)
    agent.auto_mode = "off"
    asked, events = [], []

    async def emit(ev):
        events.append(ev)

    async def confirm(*a):
        asked.append(a)
        return False

    run(agent.run('/tool run_shell {"command": "ls"}', emit, confirm))
    run(agent.run("/tool system_info {}", emit, confirm))
    assert len(asked) == 1 and "Auto" in asked[0][3]  # nur der Shell-Befehl fragt
    agent.auto_mode = "read"
    run(agent.run('/tool run_shell {"command": "ls"}', emit, confirm))
    assert len(asked) == 1
