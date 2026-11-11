"""MCP-Anbindung (Model Context Protocol): Werkzeuge fremder Server einbinden, ohne für jeden Dienst eigenen Code.

Zwei Wege zum Server:
- stdio: Orbwise startet ein Programm (`command`) und spricht über dessen stdin/stdout – JSON-RPC, eine Nachricht je
  Zeile. So laufen die meisten lokalen Server (`npx -y @modelcontextprotocol/server-…`, `uvx …`).
- HTTP („Streamable HTTP“): `url` – jede Anfrage ist ein POST; die Antwort kommt als JSON oder als Ereignis-Strom.

Beim Start von Orbwise werden die Server aus `mcp.servers` verbunden, ihre Werkzeuge abgefragt (`tools/list`) und als
`mcp_<server>_<werkzeug>` in die Werkzeugliste eingetragen (Gruppe „mcp“). Für sie gilt:
- Sie fragen vor jedem Aufruf nach – außer den in `allow` genannten (oder im Auto-Modus „Auto“). Was ein fremder
  Server über sich sagt („nur lesend“), zählt dafür nicht.
- Ihr Ergebnis ist fremder Inhalt wie eine Webseite: es kann versteckte Anweisungen enthalten (siehe agent_safety).
- Was an sie geht, prüft der Datenabfluss-Wächter wie eine Websuche.

Bewusst klein gehalten: nur Werkzeuge (keine Ressourcen, Prompts, Sampling); Anfragen des Servers an Orbwise werden
abgelehnt. Kein zusätzliches Paket nötig.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import shlex
from typing import Any

import httpx

from .tools.registry import CONFIRM, REGISTRY, SAFE, ToolContext, ToolSpec

log = logging.getLogger(__name__)

PROTOCOL = "2025-06-18"
CLIENT = {"name": "orbwise", "version": "1"}
CONNECT_TIMEOUT = 30.0   # Start eines Servers (npx lädt beim ersten Mal erst herunter)
CALL_TIMEOUT = 120.0     # ein Werkzeug-Aufruf
MAX_TOOLS = 60           # je Server – mehr sprengt jeden Prompt
MAX_DESCRIPTION = 400    # Zeichen je Werkzeugbeschreibung im Prompt
GROUP = "mcp"
_TYPES = {"string": str, "integer": int, "number": float, "boolean": bool, "array": list, "object": dict}


class McpError(RuntimeError):
    pass


# ---------------------------------------------------------------- Verbindungen
class _Client:
    """Gemeinsames: Handschlag, Werkzeuge abfragen, aufrufen. Unterklassen liefern request()/notify()/close()."""

    async def request(self, method: str, params: dict | None = None, timeout: float = CALL_TIMEOUT) -> dict:
        raise NotImplementedError

    async def notify(self, method: str, params: dict | None = None) -> None:
        raise NotImplementedError

    async def close(self) -> None:
        pass

    async def initialize(self) -> dict:
        result = await self.request("initialize", {"protocolVersion": PROTOCOL, "capabilities": {},
                                                   "clientInfo": CLIENT}, timeout=CONNECT_TIMEOUT)
        await self.notify("notifications/initialized")
        return result

    async def list_tools(self) -> list[dict]:
        tools, cursor = [], None
        for _page in range(20):
            result = await self.request("tools/list", {"cursor": cursor} if cursor else {}, timeout=CONNECT_TIMEOUT)
            tools += [t for t in result.get("tools") or [] if isinstance(t, dict) and t.get("name")]
            cursor = result.get("nextCursor")
            if not cursor:
                break
        return tools

    async def call_tool(self, name: str, arguments: dict) -> str:
        result = await self.request("tools/call", {"name": name, "arguments": arguments})
        text = content_text(result.get("content"))
        if not text and result.get("structuredContent") is not None:
            text = json.dumps(result["structuredContent"], ensure_ascii=False)
        if result.get("isError"):
            return "Fehler: " + (text or "der MCP-Server meldet einen Fehler ohne Text.")
        return text or "(kein Ergebnis)"


def content_text(content: Any) -> str:
    """Die Textteile eines Ergebnisses; Bilder, Audio und eingebettete Dateien werden nur genannt."""
    parts = []
    for item in content if isinstance(content, list) else []:
        if not isinstance(item, dict):
            continue
        kind = item.get("type")
        if kind == "text":
            parts.append(str(item.get("text") or ""))
        elif kind == "resource" and isinstance(item.get("resource"), dict) and item["resource"].get("text"):
            parts.append(str(item["resource"]["text"]))
        elif kind:
            parts.append(f"[{kind}{': ' + str(item.get('mimeType')) if item.get('mimeType') else ''} – nicht anzeigbar]")
    return "\n".join(p for p in parts if p)


def _result(message: dict) -> dict:
    if message.get("error"):
        err = message["error"]
        raise McpError(str(err.get("message") if isinstance(err, dict) else err)[:500])
    result = message.get("result")
    return result if isinstance(result, dict) else {}


class StdioClient(_Client):
    """Ein gestartetes Programm; JSON-RPC, eine Nachricht je Zeile. Antworten ordnet ein Lesevorgang den wartenden
    Anfragen zu; Anfragen des Servers (roots, sampling …) bekommen „nicht unterstützt“."""

    def __init__(self, command: str | list[str], env: dict | None = None, cwd: str | None = None):
        self.argv = shlex.split(command) if isinstance(command, str) else [str(a) for a in command]
        self.env, self.cwd = dict(env or {}), cwd
        self.proc: asyncio.subprocess.Process | None = None
        self._pending: dict[int, asyncio.Future] = {}
        self._next = 0
        self._reader: asyncio.Task | None = None
        self._lock = asyncio.Lock()
        self.stderr_tail = ""

    async def start(self) -> None:
        if not self.argv:
            raise McpError("kein Befehl angegeben")
        argv = [os.path.expanduser(a) for a in self.argv]
        try:
            self.proc = await asyncio.create_subprocess_exec(
                *argv, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                env={**os.environ, **{k: str(v) for k, v in self.env.items()}}, cwd=self.cwd, start_new_session=True,
                limit=16 * 1024 * 1024)
        except (OSError, ValueError) as e:
            raise McpError(f"'{self.argv[0]}' lässt sich nicht starten: {e}") from e
        self._reader = asyncio.create_task(self._read())
        self._errors = asyncio.create_task(self._read_stderr())

    async def _read_stderr(self) -> None:
        assert self.proc and self.proc.stderr
        while line := await self.proc.stderr.readline():
            self.stderr_tail = (self.stderr_tail + line.decode(errors="replace"))[-800:]

    async def _read(self) -> None:
        assert self.proc and self.proc.stdout
        try:
            while line := await self.proc.stdout.readline():
                try:
                    msg = json.loads(line)
                except ValueError:
                    continue  # manche Server schreiben Protokollzeilen nach stdout
                if not isinstance(msg, dict):
                    continue
                if "method" in msg and "id" in msg:  # Anfrage des Servers an uns
                    await self._send({"jsonrpc": "2.0", "id": msg["id"],
                                      "error": {"code": -32601, "message": "not supported by this client"}})
                elif "id" in msg:
                    fut = self._pending.pop(msg["id"], None)
                    if fut and not fut.done():
                        fut.set_result(msg)
        finally:  # Programm beendet: niemand soll ewig warten
            for fut in self._pending.values():
                if not fut.done():
                    fut.set_exception(McpError("der MCP-Server hat sich beendet"
                                               + (f": {self.stderr_tail.strip()[-300:]}" if self.stderr_tail.strip()
                                                  else "")))
            self._pending.clear()

    async def _send(self, message: dict) -> None:
        if not self.proc or not self.proc.stdin or self.proc.returncode is not None:
            raise McpError("der MCP-Server läuft nicht (mehr)")
        async with self._lock:
            try:
                self.proc.stdin.write(json.dumps(message, ensure_ascii=False).encode() + b"\n")
                await self.proc.stdin.drain()
            except (BrokenPipeError, ConnectionError) as e:
                raise McpError("der MCP-Server hat sich beendet") from e

    async def request(self, method: str, params: dict | None = None, timeout: float = CALL_TIMEOUT) -> dict:
        self._next += 1
        rid = self._next
        fut = asyncio.get_running_loop().create_future()
        self._pending[rid] = fut
        try:
            await self._send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params or {}})
            return _result(await asyncio.wait_for(fut, timeout))
        except asyncio.TimeoutError:
            raise McpError(f"keine Antwort auf {method} nach {int(timeout)} s") from None
        finally:
            self._pending.pop(rid, None)

    async def notify(self, method: str, params: dict | None = None) -> None:
        await self._send({"jsonrpc": "2.0", "method": method, **({"params": params} if params else {})})

    async def close(self) -> None:
        proc, self.proc = self.proc, None
        for task in (self._reader, getattr(self, "_errors", None)):
            if task:
                task.cancel()
        if proc and proc.returncode is None:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(proc.pid, 15)
            with contextlib.suppress(asyncio.TimeoutError, ProcessLookupError):
                await asyncio.wait_for(proc.wait(), 3)
            if proc.returncode is None:
                with contextlib.suppress(ProcessLookupError, PermissionError):
                    os.killpg(proc.pid, 9)


class HttpClient(_Client):
    """Streamable HTTP: jede Nachricht ein POST. Die Antwort ist JSON oder ein Ereignis-Strom, in dem die Antwort mit
    unserer Nummer steht. Der Server kann eine Sitzung vergeben (Mcp-Session-Id) – sie geht dann immer mit."""

    def __init__(self, url: str, headers: dict | None = None, transport: httpx.AsyncBaseTransport | None = None):
        self.url = url
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(CALL_TIMEOUT, connect=15), transport=transport,
                                         headers={"Accept": "application/json, text/event-stream",
                                                  **{str(k): str(v) for k, v in (headers or {}).items()}})
        self._next = 0
        self._session: str | None = None
        self._version: str | None = None

    def _headers(self) -> dict:
        out = {}
        if self._session:
            out["Mcp-Session-Id"] = self._session
        if self._version:
            out["MCP-Protocol-Version"] = self._version
        return out

    async def _post(self, message: dict, timeout: float) -> httpx.Response:
        try:
            resp = await self._client.post(self.url, json=message, headers=self._headers(), timeout=timeout)
        except httpx.HTTPError as e:
            raise McpError(f"{self.url} nicht erreichbar ({type(e).__name__})") from e
        if resp.status_code in (401, 403):
            raise McpError(f"der Server lehnt die Anmeldung ab ({resp.status_code}) – Zugangsdaten in headers prüfen")
        if resp.status_code >= 400:
            raise McpError(f"der Server antwortet mit {resp.status_code}: {resp.text[:200]}")
        if resp.headers.get("mcp-session-id"):
            self._session = resp.headers["mcp-session-id"]
        return resp

    async def request(self, method: str, params: dict | None = None, timeout: float = CALL_TIMEOUT) -> dict:
        self._next += 1
        rid = self._next
        resp = await self._post({"jsonrpc": "2.0", "id": rid, "method": method, "params": params or {}}, timeout)
        if "text/event-stream" in resp.headers.get("content-type", ""):
            for line in resp.text.splitlines():
                if not line.startswith("data:"):
                    continue
                try:
                    msg = json.loads(line[5:].strip())
                except ValueError:
                    continue
                if isinstance(msg, dict) and msg.get("id") == rid and "method" not in msg:
                    return self._done(method, _result(msg))
            raise McpError(f"keine Antwort auf {method} im Ereignis-Strom")
        try:
            msg = resp.json()
        except ValueError:
            raise McpError(f"der Server antwortet auf {method} nicht mit JSON") from None
        return self._done(method, _result(msg if isinstance(msg, dict) else {}))

    def _done(self, method: str, result: dict) -> dict:
        if method == "initialize":
            self._version = str(result.get("protocolVersion") or PROTOCOL)
        return result

    async def notify(self, method: str, params: dict | None = None) -> None:
        await self._post({"jsonrpc": "2.0", "method": method, **({"params": params} if params else {})}, 15)

    async def close(self) -> None:
        await self._client.aclose()


# ---------------------------------------------------------------- Werkzeuge eintragen
def tool_name(server: str, tool: str) -> str:
    """mcp_<server>_<werkzeug> – nur Zeichen, die jede Modell-Schnittstelle als Werkzeugname annimmt, höchstens 64."""
    clean = re.sub(r"[^A-Za-z0-9_]+", "_", f"mcp_{server}_{tool}").strip("_")
    return clean[:64]


def _parameters(schema: Any) -> tuple[dict, dict[str, type]]:
    """inputSchema des Servers → (Parameter für den Prompt, Typen fürs Umwandeln). Unbekanntes bleibt Text."""
    schema = schema if isinstance(schema, dict) else {}
    props, types = {}, {}
    for key, prop in (schema.get("properties") or {}).items():
        prop = prop if isinstance(prop, dict) else {}
        kind = prop.get("type")
        if isinstance(kind, list):  # ["string", "null"]
            kind = next((k for k in kind if k != "null"), "string")
        kind = kind if kind in _TYPES else "string"
        entry: dict[str, Any] = {"type": kind}
        if prop.get("description"):
            entry["description"] = str(prop["description"])[:200]
        if isinstance(prop.get("enum"), list):
            entry["enum"] = prop["enum"][:20]
        if kind == "array":
            items = prop.get("items") if isinstance(prop.get("items"), dict) else {}
            entry["items"] = {"type": items.get("type") if items.get("type") in _TYPES else "string"}
        props[str(key)] = entry
        types[str(key)] = _TYPES[kind]
    required = [str(r) for r in schema.get("required") or [] if str(r) in props]
    return {"type": "object", "properties": props, "required": required}, types


class McpManager:
    """Verbindet die Server aus der Config, trägt ihre Werkzeuge ein und nimmt sie beim Beenden wieder heraus."""

    def __init__(self, cfg: Any):
        self.cfg = cfg
        self.clients: dict[str, _Client] = {}
        self.state: dict[str, dict] = {}   # Name → {ok, error, tools: [...]} für doctor und Oberfläche
        self.names: list[str] = []         # eingetragene Werkzeugnamen

    def servers(self) -> dict[str, Any]:
        mcp = getattr(self.cfg, "mcp", None)
        return {n: s for n, s in (getattr(mcp, "servers", None) or {}).items() if getattr(s, "enabled", True)}

    async def start(self) -> list[str]:
        """Alle Server verbinden (nebeneinander; ein Fehler betrifft nur den einen) → eingetragene Werkzeugnamen."""
        servers = self.servers()
        if servers:
            await asyncio.gather(*(self._connect(name, spec) for name, spec in servers.items()))
        return list(self.names)

    async def _connect(self, name: str, spec: Any) -> None:
        client: _Client | None = None
        try:
            if getattr(spec, "url", ""):
                client = HttpClient(spec.url, getattr(spec, "headers", None))
            elif getattr(spec, "command", ""):
                client = StdioClient(spec.command, getattr(spec, "env", None))
                await client.start()
            else:
                raise McpError("weder command noch url angegeben")
            await client.initialize()
            tools = (await client.list_tools())[:MAX_TOOLS]
        except Exception as e:  # noqa: BLE001 – ein kaputter Server darf den Start nicht aufhalten
            if client is not None:
                with contextlib.suppress(Exception):
                    await client.close()
            reason = str(e) if isinstance(e, McpError) else f"{type(e).__name__}: {e}"
            self.state[name] = {"ok": False, "error": reason[:400], "tools": []}
            log.warning("MCP-Server %s: %s", name, reason)
            return
        self.clients[name] = client
        allow = set(getattr(spec, "allow", None) or [])
        registered = []
        for tool in tools:
            registered.append(self._register(name, client, tool, "*" in allow or tool["name"] in allow))
        self.state[name] = {"ok": True, "error": "", "tools": registered}
        log.info("MCP-Server %s verbunden: %d Werkzeuge", name, len(registered))

    def _register(self, server: str, client: _Client, tool: dict, allowed: bool) -> str:
        from . import agent_safety
        name, remote = tool_name(server, tool["name"]), tool["name"]
        parameters, types = _parameters(tool.get("inputSchema"))
        description = " ".join(str(tool.get("description") or remote).split())[:MAX_DESCRIPTION]

        async def call(ctx: ToolContext, **arguments) -> str:  # noqa: ARG001
            try:
                return await client.call_tool(remote, arguments)
            except McpError as e:
                return f"Fehler: MCP-Server {server}: {e}"

        def risk(ctx: ToolContext, args: dict) -> tuple[str, str]:  # noqa: ARG001
            return (SAFE, "") if allowed else (CONFIRM, f"Werkzeug des MCP-Servers „{server}“")

        REGISTRY[name] = ToolSpec(name=name, description=f"[MCP {server}] {description}", func=call,
                                  parameters=parameters, risk=risk, param_types=types, group=GROUP)
        agent_safety.TAINT_SOURCES.add(name)  # Ergebnis ist fremder Inhalt – wie eine Webseite
        self.names.append(name)
        return name

    async def stop(self) -> None:
        from . import agent_safety
        for name in self.names:
            REGISTRY.pop(name, None)
            agent_safety.TAINT_SOURCES.discard(name)
        self.names = []
        clients, self.clients = self.clients, {}
        for client in clients.values():
            with contextlib.suppress(Exception):
                await client.close()

    def status(self) -> list[dict]:
        return [{"name": n, **s} for n, s in self.state.items()]
