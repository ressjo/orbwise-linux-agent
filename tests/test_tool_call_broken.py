"""Abgeschnittene Werkzeugaufrufe (llama-server: „Failed to parse tool call arguments“): erkennen, einmal mit dem
Hinweis „in Teilen schreiben“ wiederholen; write mit append."""

import json

import httpx
from conftest import run

from orbwise.agent import Agent
from orbwise.llm import LLMError, OpenAICompatLLM, ToolCallBroken
from orbwise.tools import code
from orbwise.tools.registry import ToolContext

BROKEN = ('{"error":{"code":500,"message":"Failed to parse tool call arguments as JSON: [json.exception.parse_error.101] '
          'parse error at line 1, column 32566: syntax error while parsing value - invalid string: missing closing '
          'quote","type":"server_error"}}')


def _client(handler):
    from orbwise.config import ProfileConfig
    p = ProfileConfig(backend="openai", base_url="http://llm/v1", model="m", think=False)
    return OpenAICompatLLM(p, transport=httpx.MockTransport(handler))


async def _drain(llm):
    async for _ in llm.chat_stream([{"role": "user", "content": "hi"}]):
        pass


def test_broken_tool_call_is_recognised_as_status_and_as_stream_event():
    import pytest
    with pytest.raises(ToolCallBroken):
        run(_drain(_client(lambda r: httpx.Response(500, text=BROKEN))))
    sse = "data: " + json.dumps({"error": json.loads(BROKEN)["error"]}) + "\n\n"
    with pytest.raises(ToolCallBroken):
        run(_drain(_client(lambda r: httpx.Response(200, text=sse, headers={"content-type": "text/event-stream"}))))


def _flaky(llm, fails: int):
    """chat_stream wirft die ersten `fails` Male ToolCallBroken und merkt sich die Nachrichten jedes Aufrufs."""
    original, seen = llm.chat_stream, []

    async def stream(messages, tools=None, **kw):
        seen.append(messages)
        if len(seen) <= fails:
            raise ToolCallBroken("Failed to parse tool call arguments as JSON")
        async for ev in original(messages, tools, **kw):
            yield ev
    llm.chat_stream = stream
    return seen


def test_agent_retries_once_with_a_hint_that_stays_out_of_the_history(cfg, llm, memory):
    agent = Agent(cfg, llm, memory)
    seen = _flaky(llm, 1)
    events = []

    async def emit(ev):
        events.append(ev)

    async def confirm(*a):
        return True
    answer = run(agent.run("Hallo", emit, confirm))
    assert answer and len(seen) == 2
    assert "in Teilen" in seen[1][-1]["content"] and "in Teilen" not in seen[0][-1]["content"]
    assert not any("in Teilen" in (m.get("content") or "") for m in memory.conversation.history if m["role"] == "user")


def test_agent_explains_when_it_breaks_twice(cfg, llm, memory):
    agent = Agent(cfg, llm, memory)
    _flaky(llm, 5)
    events = []

    async def emit(ev):
        events.append(ev)

    async def confirm(*a):
        return True
    run(agent.run("Hallo", emit, confirm))
    text = json.dumps(events, ensure_ascii=False)
    assert "zu langen Werkzeugaufruf" in text and "Failed to parse" not in text


def test_write_appends_in_parts(cfg, memory, tmp_path):
    ctx = ToolContext(cfg=cfg, memory=memory, cwd=str(tmp_path))
    run(code.code_write(ctx, path="gross.tsx", content="teil 1\n"))
    run(code.code_write(ctx, path="gross.tsx", content="teil 2\n", append=True))
    run(code.code_write(ctx, path="neu.txt", content="x", append=True))
    assert (tmp_path / "gross.tsx").read_text() == "teil 1\nteil 2\n" and (tmp_path / "neu.txt").read_text() == "x"
    assert "+teil 2" in code.write_preview(ctx, {"path": "gross.tsx", "content": "teil 2\n", "append": True})
    assert LLMError


def test_error_lines_in_the_stream_are_read_too():
    import pytest
    sse = 'data: {"choices":[{"delta":{"content":"a"}}]}\n\nerror: ' + json.dumps(json.loads(BROKEN)["error"]) + "\n\n"
    with pytest.raises(ToolCallBroken):
        run(_drain(_client(lambda r: httpx.Response(200, text=sse, headers={"content-type": "text/event-stream"}))))
    other = 'error: {"code":500,"message":"something else"}\n\n'
    with pytest.raises(LLMError, match="something else"):
        run(_drain(_client(lambda r: httpx.Response(200, text=other, headers={"content-type": "text/event-stream"}))))


def test_retry_always_makes_room_first(cfg, llm, memory):
    agent = Agent(cfg, llm, memory)
    _flaky(llm, 1)
    rooms = []
    original = agent._make_room

    async def spy(*a, **kw):
        rooms.append(1)
        return await original(*a, **kw)
    agent._make_room = spy

    async def emit(ev):
        pass

    async def confirm(*a):
        return True
    run(agent.run("Hallo", emit, confirm))
    assert rooms


# ---------------------------------------------------------------- abgeschnittene Argumente nie im Verlauf
CUT = '{"path": "app/forms/[id]/page.tsx", "content": "zeile 1\\nzeile 2\\nzeile 3\\nzeile 4 ohne en'


def test_stream_marks_cut_arguments_and_sending_repairs_old_chats():
    from orbwise.llm import _json_args
    sse = ("data: " + json.dumps({"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {
        "name": "write", "arguments": CUT}}]}}]}) + "\n\ndata: [DONE]\n\n")

    async def done(llm):
        async for ev in llm.chat_stream([{"role": "user", "content": "hi"}], tools=[]):
            if ev["type"] == "done":
                return ev["message"]
    msg = run(done(_client(lambda r: httpx.Response(200, text=sse, headers={"content-type": "text/event-stream"}))))
    call = msg["tool_calls"][0]
    assert call["broken"] == CUT and call["function"]["arguments"] == {}
    assert _json_args(CUT) == "{}" and _json_args('{"a": 1}') == '{"a": 1}' and _json_args({"b": 2}) == '{"b": 2}'


def test_salvage_write_keeps_whole_lines_only():
    from orbwise.llm import salvage_write
    assert salvage_write(CUT) == ("app/forms/[id]/page.tsx", "zeile 1\nzeile 2\nzeile 3\n", False)
    assert salvage_write('{"path": "a.txt", "append": true, "content": "x\\ny\\u00') == ("a.txt", "x\n", True)
    assert salvage_write('{"path": "a.txt", "content": "nur halb') is None
    assert salvage_write('{"content": "a\\nb') is None


def _cut_once(llm, raw, name="write"):
    """Erster Schritt: abgeschnittener Aufruf; danach normales FakeLLM-Verhalten."""
    original, seen = llm.chat_stream, []

    async def stream(messages, tools=None, **kw):
        seen.append(messages)
        if len(seen) == 1:
            yield {"type": "done", "message": {"role": "assistant", "content": "", "tool_calls": [
                {"function": {"name": name, "arguments": {}}, "broken": raw}]}, "stats": {}}
            return
        async for ev in original(messages, tools, **kw):
            yield ev
    llm.chat_stream = stream
    return seen


def test_cut_write_is_saved_up_to_the_last_line_and_the_model_continues(cfg, llm, memory, tmp_path):
    agent = Agent(cfg, llm, memory)
    memory.switch_mode("coding")
    memory.set_project(str(tmp_path))
    agent.auto_mode = "files"
    seen = _cut_once(llm, CUT)

    async def emit(ev):
        pass

    async def confirm(*a):
        return True
    run(agent.run("Schreib die Seite", emit, confirm))
    assert (tmp_path / "app/forms/[id]/page.tsx").read_text() == "zeile 1\nzeile 2\nzeile 3\n"
    tool = next(m for m in memory.conversation.history if m["role"] == "tool")
    assert "append=true" in tool["content"] and "zeile 3" in tool["content"]
    for m in memory.conversation.history:
        for c in m.get("tool_calls") or []:
            assert isinstance(c["function"]["arguments"], dict) and "broken" not in c
    assert len(seen) >= 2


def test_cut_shell_call_is_retried_without_a_trace(cfg, llm, memory):
    agent = Agent(cfg, llm, memory)
    seen = _cut_once(llm, '{"command": "ls -la /very/lo', name="run_shell")

    async def emit(ev):
        pass

    async def confirm(*a):
        return True
    run(agent.run("Hallo", emit, confirm))
    assert len(seen) == 2 and "in Teilen" in seen[1][-1]["content"]
    assert not any(m.get("tool_calls") for m in memory.conversation.history)


def test_both_modes_keep_room_for_long_output(cfg, llm, memory):
    agent = Agent(cfg, llm, memory)
    agent.model_window = lambda: 49152
    tools = agent.answer_reserve()
    memory.switch_mode("coding")
    assert agent.answer_reserve() == tools >= 12000


def test_output_reserve_for_every_window():
    from orbwise.context_plan import plan_for
    assert plan_for(8192).reserve(False) == 2048 and plan_for(8192).reserve(True) == 1024 + int(8192 * 0.2)
    assert plan_for(49152).reserve(False) == plan_for(49152).reserve(True) == 12288
    assert plan_for(131072).reserve(True) == 16000
