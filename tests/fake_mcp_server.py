"""Kleiner MCP-Server über stdio für die Tests: zwei Werkzeuge, dazu eine Anfrage an den Client und Protokollzeilen
auf stdout, wie echte Server sie gelegentlich schicken. `python fake_mcp_server.py [crash]`."""

import json
import sys

TOOLS = [
    {"name": "echo", "description": "Gibt den Text  zurück.\nZweite Zeile.",
     "inputSchema": {"type": "object", "required": ["text"],
                     "properties": {"text": {"type": "string", "description": "Der Text"},
                                    "times": {"type": ["integer", "null"]},
                                    "tags": {"type": "array", "items": {"type": "string"}},
                                    "opts": {"type": "object"}}},
     "annotations": {"readOnlyHint": True}},
    {"name": "delete-all", "description": "Löscht alles.", "inputSchema": {"type": "object", "properties": {}}},
]


def send(msg):
    sys.stdout.write(json.dumps(msg) + "\n")
    sys.stdout.flush()


for line in sys.stdin:
    msg = json.loads(line)
    method, rid = msg.get("method"), msg.get("id")
    if method == "initialize":
        if "crash" in sys.argv:
            sys.stderr.write("Lizenzschlüssel fehlt\n")
            sys.exit(3)
        print("Server gestartet (kein JSON)", flush=True)
        send({"jsonrpc": "2.0", "id": rid, "result": {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}},
                                                      "serverInfo": {"name": "fake", "version": "1"}}})
    elif method == "notifications/initialized":
        send({"jsonrpc": "2.0", "id": "srv-1", "method": "roots/list"})  # Anfrage an den Client: muss beantwortet werden
    elif method == "tools/list":
        cursor = (msg.get("params") or {}).get("cursor")
        if not cursor:
            send({"jsonrpc": "2.0", "id": rid, "result": {"tools": TOOLS[:1], "nextCursor": "seite2"}})
        else:
            send({"jsonrpc": "2.0", "id": rid, "result": {"tools": TOOLS[1:]}})
    elif method == "tools/call":
        params = msg["params"]
        args = params.get("arguments") or {}
        if params["name"] == "echo":
            text = " ".join([args["text"]] * int(args.get("times") or 1))
            send({"jsonrpc": "2.0", "id": rid, "result": {"content": [
                {"type": "text", "text": text + (" " + ",".join(args["tags"]) if args.get("tags") else "")},
                {"type": "image", "mimeType": "image/png", "data": "…"}]}})
        elif params["name"] == "delete-all":
            send({"jsonrpc": "2.0", "id": rid, "result": {"isError": True, "content": [
                {"type": "text", "text": "Zugriff verweigert"}]}})
        else:
            send({"jsonrpc": "2.0", "id": rid, "error": {"code": -32602, "message": "Unknown tool"}})
    elif rid is not None and method:
        send({"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": "Method not found"}})
