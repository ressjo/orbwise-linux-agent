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
