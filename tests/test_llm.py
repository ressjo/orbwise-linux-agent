"""OpenAICompatLLM gegen einen nachgestellten llama-server: Streaming mit Tool-Calls, Fehler, <think> entfernen."""

import json

import httpx
from conftest import run

from orbwise.config import ProfileConfig
from orbwise.llm import LLMError, OpenAICompatLLM


def make_llm(handler) -> OpenAICompatLLM:
    profile = ProfileConfig(base_url="http://llama/v1", model="m", temperature=0.6, think=False)
    return OpenAICompatLLM(profile, transport=httpx.MockTransport(handler))


def sse(chunks: list[dict]) -> httpx.Response:
    body = "".join(f"data: {json.dumps(c)}\n\n" for c in chunks) + "data: [DONE]\n\n"
    return httpx.Response(200, text=body, headers={"content-type": "text/event-stream"})


def test_stream_with_tool_calls():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return sse([
            {"choices": [{"delta": {"role": "assistant", "content": "Einen "}}]},
            {"choices": [{"delta": {"content": "Moment."}}]},
            {"choices": [{"delta": {"tool_calls": [
                {"index": 0, "id": "c1", "function": {"name": "system_update", "arguments": "{}"}}]}}]},
            {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
        ])

    llm = make_llm(handler)

    async def go():
        return [ev async for ev in llm.chat_stream([{"role": "user", "content": "x"}], [{"type": "function"}])]

    events = run(go())
    assert [e["text"] for e in events if e["type"] == "token"] == ["Einen ", "Moment."]
    final = events[-1]["message"]
    assert final["content"] == "Einen Moment."
    assert final["tool_calls"][0]["function"]["name"] == "system_update"
    assert seen["body"]["stream"] is True and seen["body"]["tools"] and seen["body"]["cache_prompt"] is True


def test_error_is_raised():
    llm = make_llm(lambda r: httpx.Response(404, text='{"error":"model not found"}'))

    async def go():
        return [ev async for ev in llm.chat_stream([{"role": "user", "content": "x"}])]

    try:
        run(go())
    except LLMError as e:
        assert "404" in str(e)
    else:
        raise AssertionError("LLMError erwartet")


def test_chat_strips_think():
    llm = make_llm(lambda r: httpx.Response(200, json={
        "choices": [{"message": {"role": "assistant", "content": "<think>hmm</think>Antwort"}}]}))
    assert run(llm.chat([{"role": "user", "content": "x"}])) == "Antwort"
