"""MCP-Anbindung: Server über stdio und HTTP verbinden, Werkzeuge eintragen und aufrufen; Freigaben, fremde Inhalte
und Datenabfluss gelten wie für eigene Werkzeuge."""

import json
import sys
from pathlib import Path

import httpx
import pytest
from conftest import run

from orbwise import agent_safety, mcp
from orbwise.agent import Agent
from orbwise.config import McpServerConfig
from orbwise.tools.leakguard import outbound_text
from orbwise.tools.registry import CONFIRM, REGISTRY, SAFE, ToolContext, coerce_args, missing_args

SERVER = f"{sys.executable} {Path(__file__).parent / 'fake_mcp_server.py'}"


@pytest.fixture
def clean():
    yield
    for name in [n for n in REGISTRY if n.startswith("mcp_")]:
        REGISTRY.pop(name)
        agent_safety.TAINT_SOURCES.discard(name)


def test_stdio_server_lists_and_calls_tools(cfg, clean):
    cfg.mcp.servers = {"demo": McpServerConfig(command=SERVER, allow=["echo"])}

    async def go():
        manager = mcp.McpManager(cfg)
        names = await manager.start()
        try:
            assert names == ["mcp_demo_echo", "mcp_demo_delete_all"]  # beide Seiten der Liste, Namen bereinigt
            echo, delete = REGISTRY["mcp_demo_echo"], REGISTRY["mcp_demo_delete_all"]
            assert echo.group == "mcp" and echo.description == "[MCP demo] Gibt den Text zurück. Zweite Zeile."
            assert echo.parameters["required"] == ["text"]
            assert echo.parameters["properties"]["times"] == {"type": "integer"}
            ctx = ToolContext(cfg=cfg, memory=None)
            args = coerce_args(echo, {"text": "hallo", "times": "2", "tags": ["a", "b"], "erfunden": 1})
            assert args == {"text": "hallo", "times": 2, "tags": ["a", "b"]} and not missing_args(echo, args)
            assert missing_args(echo, {}) == ["text"]
            out = await echo.func(ctx, **args)
            assert out == "hallo hallo a,b\n[image: image/png – nicht anzeigbar]"
            assert await delete.func(ctx) == "Fehler: Zugriff verweigert"  # isError des Servers
            # nur was in allow steht, läuft ohne Rückfrage – „readOnlyHint“ des Servers zählt nicht
            assert echo.assess(ctx, {})[0] == SAFE and delete.assess(ctx, {}) == (CONFIRM, "Werkzeug des MCP-Servers „demo“")
            assert manager.status() == [{"name": "demo", "ok": True, "error": "", "tools": names}]
            assert "mcp_demo_echo" in agent_safety.TAINT_SOURCES
        finally:
            await manager.stop()
        assert "mcp_demo_echo" not in REGISTRY and "mcp_demo_echo" not in agent_safety.TAINT_SOURCES
    run(go())


def test_broken_servers_are_reported_and_do_not_block_the_others(cfg, clean):
    cfg.mcp.servers = {"ok": McpServerConfig(command=SERVER), "weg": McpServerConfig(command="gibt-es-nicht-xyz --mcp"),
                       "stirbt": McpServerConfig(command=SERVER + " crash"), "leer": McpServerConfig(),
                       "aus": McpServerConfig(command=SERVER, enabled=False)}

    async def go():
        manager = mcp.McpManager(cfg)
        try:
            names = await manager.start()
            state = {s["name"]: s for s in manager.status()}
        finally:
            await manager.stop()
        return names, state
    names, state = run(go())
    assert names == ["mcp_ok_echo", "mcp_ok_delete_all"] and state["ok"]["ok"] and "aus" not in state
    assert not state["weg"]["ok"] and "lässt sich nicht starten" in state["weg"]["error"]
    assert not state["stirbt"]["ok"] and "Lizenzschlüssel fehlt" in state["stirbt"]["error"]  # stderr des Servers
    assert state["leer"]["error"] == "weder command noch url angegeben"


def test_http_server_json_and_event_stream(cfg, clean):
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        msg = json.loads(request.content)
        seen.append((msg.get("method"), request.headers.get("mcp-session-id"), request.headers.get("authorization"),
                     request.headers.get("mcp-protocol-version")))
        if request.headers.get("authorization") != "Bearer geheim":
            return httpx.Response(401)
        if "id" not in msg:
            return httpx.Response(202)
        if msg["method"] == "initialize":
            return httpx.Response(200, headers={"Mcp-Session-Id": "s1"}, json={
                "jsonrpc": "2.0", "id": msg["id"], "result": {"protocolVersion": "2025-03-26", "capabilities": {}}})
        if msg["method"] == "tools/list":
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": msg["id"], "result": {"tools": [
                {"name": "search", "inputSchema": {"type": "object", "properties": {"q": {"type": "string"}}}}]}})
        body = ("event: message\ndata: {\"jsonrpc\": \"2.0\", \"method\": \"notifications/progress\"}\n\n"
                f"event: message\ndata: {json.dumps({'jsonrpc': '2.0', 'id': msg['id'], 'result': {'content': [{'type': 'text', 'text': 'Treffer für ' + msg['params']['arguments']['q']}]}})}\n\n")
        return httpx.Response(200, headers={"Content-Type": "text/event-stream"}, text=body)

    async def go():
        client = mcp.HttpClient("https://example.com/mcp", {"Authorization": "Bearer geheim"},
                                transport=httpx.MockTransport(handler))
        await client.initialize()
        tools = await client.list_tools()
        out = await client.call_tool("search", {"q": "orbwise"})
        bad = mcp.HttpClient("https://example.com/mcp", transport=httpx.MockTransport(handler))
        with pytest.raises(mcp.McpError, match="lehnt die Anmeldung ab"):
            await bad.initialize()
        await client.close()
        await bad.close()
        return tools, out
    tools, out = run(go())
    assert [t["name"] for t in tools] == ["search"] and out == "Treffer für orbwise"
    # die Sitzung und die ausgehandelte Protokollversion gehen nach dem Handschlag immer mit
    assert seen[0][:2] == ("initialize", None) and seen[1] == ("notifications/initialized", "s1", "Bearer geheim",
                                                                 "2025-03-26")
    assert seen[3][0] == "tools/call" and seen[3][1] == "s1"


def test_agent_uses_mcp_tools_with_the_usual_safeguards(cfg, llm, memory, clean):
    cfg.mcp.servers = {"demo": McpServerConfig(command=SERVER, allow=["echo"])}

    async def go():
        manager = mcp.McpManager(cfg)
        await manager.start()
        try:
            agent = Agent(cfg, llm, memory)
            agent.reload_tools()
            assert agent.groups_of["mcp_demo_echo"] == "mcp" and agent.tool_allowed("mcp_demo_echo")
            assert "mcp_demo_echo" in {s["function"]["name"] for s in agent._mode_schemas()[0]}
            events, asked = [], []

            async def emit(ev):
                events.append(ev)

            async def confirm(call_id, name, args, reason):
                asked.append((name, reason))
                return False
            await agent.run('/tool mcp_demo_echo {"text": "hallo"}', emit, confirm)
            assert not asked and any("hallo" in e.get("text", "") for e in events if e["type"] == "tool_result")
            assert agent._tainted  # Ergebnis eines fremden Servers: wie eine Webseite
            # danach fragt auch das freigegebene Werkzeug (fremder Inhalt im Chat) – und das andere sowieso
            await agent.run('/tool mcp_demo_echo {"text": "nochmal"}', emit, confirm)
            await agent.run('/tool mcp_demo_delete_all {}', emit, confirm)
            assert [n for n, _ in asked] == ["mcp_demo_echo", "mcp_demo_delete_all"]
            memory.switch_mode("coding")  # auch im Coding-Modus verfügbar
            assert agent.tool_allowed("mcp_demo_echo")
            assert "mcp_demo_echo" in {s["function"]["name"] for s in agent._mode_schemas()[0]}
        finally:
            await manager.stop()
    run(go())
    # was an einen fremden Server geht, prüft der Datenabfluss-Wächter
    assert outbound_text("mcp_demo_echo", {"text": "IBAN DE00", "tags": ["x"], "n": 3}) == 'IBAN DE00 ["x"]'


def test_tool_names_fit_every_model_interface():
    assert mcp.tool_name("my-server", "read.file/v2") == "mcp_my_server_read_file_v2"
    assert len(mcp.tool_name("s" * 40, "t" * 60)) == 64
