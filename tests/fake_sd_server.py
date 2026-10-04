"""Minimaler „sd-server“ (stable-diffusion.cpp) für Tests: /sdcpp/v1/capabilities, img_gen (asynchron), jobs, cancel.
Schreibt Fortschrittszeilen wie das Original auf stdout (landen im Log) und liefert ein kleines PNG."""

import base64
import json
import sys
import threading
import time
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = int(sys.argv[sys.argv.index("--listen-port") + 1])
ARGS = sys.argv[1:]
JOBS: dict[str, dict] = {}


def png(w=4, h=4) -> bytes:
    def chunk(kind, data):
        return (len(data).to_bytes(4, "big") + kind + data
                + zlib.crc32(kind + data).to_bytes(4, "big"))
    raw = b"".join(b"\0" + b"\xff\x88\x00" * w for _ in range(h))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", w.to_bytes(4, "big") + h.to_bytes(4, "big") + b"\x08\x02\0\0\0")
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


def work(job_id: str, body: dict) -> None:
    job = JOBS[job_id]
    job["status"] = "generating"
    steps = body["sample_params"]["sample_steps"]
    for i in range(1, steps + 1):
        if job["status"] == "cancelled":
            return
        print(f"  |{'=' * i}>| {i}/{steps} - 0.05s/it", flush=True)
        time.sleep(0.25)
    if "fail" in body["prompt"]:
        job.update(status="failed", error={"code": "generation_failed", "message": "kaputt"})
        return
    images = [{"index": i, "b64_json": base64.b64encode(png()).decode()} for i in range(body["batch_count"])]
    job.update(status="completed", result={"output_format": "png", "images": images})
    with open(sys.argv[0] + ".last.json", "w") as f:
        json.dump({"body": body, "args": ARGS}, f)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _json(self, code, obj):
        data = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == "/sdcpp/v1/capabilities":
            return self._json(200, {"model": {"name": "qwen"}})
        if self.path.startswith("/sdcpp/v1/jobs/"):
            job = JOBS.get(self.path.rsplit("/", 1)[1])
            return self._json(200, job) if job else self._json(404, {})
        self._json(404, {})

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        if self.path == "/sdcpp/v1/img_gen":
            job_id = f"job_{len(JOBS)}"
            JOBS[job_id] = {"id": job_id, "status": "queued", "result": None, "error": None}
            threading.Thread(target=work, args=(job_id, body), daemon=True).start()
            return self._json(202, {"id": job_id, "status": "queued"})
        if self.path.endswith("/cancel"):
            job = JOBS.get(self.path.split("/")[-2])
            if job:
                job.update(status="cancelled", error={"code": "cancelled", "message": "job cancelled by client"})
            return self._json(200, {})
        self._json(404, {})


print("sd-server listening", flush=True)
ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
