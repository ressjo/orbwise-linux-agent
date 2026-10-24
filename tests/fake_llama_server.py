"""Minimaler OpenAI-kompatibler „llama-server“ für Tests: /health, /v1/models, /v1/chat/completions (Streaming)."""

import json
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from orbwise.prompts import strip_context_note

PORT = int(sys.argv[1])
DELAY = float(sys.argv[2]) if len(sys.argv) > 2 and not sys.argv[2].startswith("-") else 0.0
CTX = int(sys.argv[sys.argv.index("-c") + 1]) if "-c" in sys.argv else 12288  # wie llama-server -c
STARTED = time.time()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _json(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        loading = time.time() - STARTED < DELAY
        if self.path == "/health":
            return self._json(503 if loading else 200, {"status": "loading" if loading else "ok"})
        if self.path == "/props":
            return self._json(200, {"default_generation_settings": {"n_ctx": CTX}})
        if self.path == "/v1/models":
            return self._json(200, {"data": [{"id": "llama"}]})
        self._json(404, {})

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        req = json.loads(self.rfile.read(length))
        last = req["messages"][-1]
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()

        def send(obj):
            self.wfile.write(f"data: {json.dumps(obj)}\n\n".encode())
            self.wfile.flush()

        if last["role"] == "user" and "systemupdate" in last["content"].lower() and req.get("tools"):
            send({"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "x", "function": {"name": "list_updates", "arguments": ""}}]}}]})
            send({"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": "{}"}}]}}]})
        else:
            text = "Llama meldet: " + strip_context_note(last.get("content") or "")[:40]
            for word in text.split(" "):
                send({"choices": [{"delta": {"content": word + " "}}]})
        send({"choices": [], "timings": {"predicted_n": 12, "predicted_per_second": 19.24,
                                          "prompt_n": 900, "prompt_per_second": 310.5}})
        self.wfile.write(b"data: [DONE]\n\n")


ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
