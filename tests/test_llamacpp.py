"""Eigene Modelle über llama.cpp: offiziellen Build holen (GPU-Variante, sonst Vulkan), GGUF von Hugging Face nach
~/models laden, vorhandene Dateien übernehmen, Profil mit Port/Kontext/Bildmodul anlegen – ohne config.yaml."""

import asyncio
import io
import json
import os
import shlex
import socket
import tarfile
import threading
import time
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

import pytest
from fastapi.testclient import TestClient
from test_llm_memory import write_gguf

from orbwise import llamacpp
from orbwise import models as mdl
from orbwise.config import ProfileConfig, ServerConfig

WORKS = ('#!/bin/sh\ncase "$1" in\n --list-devices) printf "Available devices:\\n  Vulkan0: Fake GPU\\n";;\n'
         ' --help) printf -- "-c, --ctx-size N\\n--ctx-checkpoints N\\n";;\n *) echo "version: 9999 (fake)";;\nesac\n')
BROKEN = '#!/bin/sh\necho "llama-server: error while loading shared libraries: libamdhip64.so.7: cannot open" >&2\nexit 127\n'
REPO = "tiny/Tiny-GGUF"
META = {"qwen3.block_count": 64, "qwen3.attention.head_count": 40, "qwen3.attention.head_count_kv": 8,
        "qwen3.attention.key_length": 128, "qwen3.attention.value_length": 128, "qwen3.context_length": 32768}


def _zip(files: dict) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, text in files.items():
            z.writestr(name, text)
    return buf.getvalue()


def _tgz(files: dict) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as t:
        for name, text in files.items():
            data = text.encode()
            info = tarfile.TarInfo(name)
            info.size = len(data)
            t.addfile(info, io.BytesIO(data))
        link = tarfile.TarInfo("llama-b/libllama.so")  # Bibliotheks-Verweis wie in echten Builds
        link.type, link.linkname = tarfile.SYMTYPE, "libllama.so.0"
        t.addfile(link)
    return buf.getvalue()


class Fake(BaseHTTPRequestHandler):
    """GitHub-Release + Hugging Face (Dateiliste und Downloads mit Range)."""
    tag = "b9999"
    files: dict[str, bytes] = {}
    assets: dict[str, bytes] = {}
    slow = b"x" * (4 << 20)

    def log_message(self, *a):
        pass

    def _send(self, body: bytes, code=200, ctype="application/octet-stream", extra=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = urlparse(self.path).path
        base = f"http://127.0.0.1:{self.server.server_port}"
        if path == "/repos/ggml-org/llama.cpp/releases":
            assets = [{"name": n, "browser_download_url": f"{base}/dl/{n}"} for n in Fake.assets]
            assets.append({"name": f"llama-{Fake.tag}-bin-ubuntu-arm64.tar.gz", "browser_download_url": f"{base}/dl/x"})
            # wie bei GitHub heute: das neueste Release hat keine Programme (nur nightly-tag.txt)
            rels = [{"tag_name": "v0.6.0", "assets": [{"name": "nightly-tag.txt", "browser_download_url": "x"}]},
                    {"tag_name": Fake.tag, "prerelease": True, "assets": assets}]
            return self._send(json.dumps(rels).encode(), ctype="application/json")
        if path.startswith("/dl/"):
            return self._send(Fake.assets[path[4:]])
        if path == f"/api/models/{REPO}/tree/main":
            tree = [{"type": "file", "path": p, "size": len(b)} for p, b in Fake.files.items()]
            return self._send(json.dumps(tree).encode(), ctype="application/json")
        if path == "/slow.bin":
            self.send_response(200)
            self.send_header("Content-Length", str(len(Fake.slow)))
            self.end_headers()
            try:
                for i in range(0, len(Fake.slow), 1 << 20):
                    self.wfile.write(Fake.slow[i:i + (1 << 20)])
                    self.wfile.flush()
                    time.sleep(0.3)
            except (BrokenPipeError, ConnectionResetError):
                pass
            return
        prefix = f"/{REPO}/resolve/main/"
        if path.startswith(prefix) and path[len(prefix):] in Fake.files:
            data = Fake.files[path[len(prefix):]]
            rng = self.headers.get("Range")
            if rng:
                start = int(rng.split("=")[1].split("-")[0])
                if start >= len(data):
                    return self._send(b"", 416)
                return self._send(data[start:], 206, extra={"Content-Range": f"bytes {start}-{len(data) - 1}/{len(data)}"})
            return self._send(data)
        self._send(b"{}", 404, "application/json")


def release(tag="b9999"):
    Fake.tag = tag
    Fake.assets = {
        f"llama-{tag}-bin-ubuntu-rocm-10.0-x64.tar.gz": _tgz({"build/bin/llama-server": BROKEN}),
        f"llama-{tag}-bin-ubuntu-vulkan-x64.tar.gz": _tgz({"llama-b/llama-server": WORKS, "llama-b/libllama.so.0": "lib"}),
        f"llama-{tag}-bin-ubuntu-x64.tar.gz": _tgz({"build/bin/llama-server": WORKS}),
    }


@pytest.fixture
def fake(tmp_path, monkeypatch):
    release()
    model = tmp_path / "src.gguf"
    write_gguf(model, META)
    Fake.files = {"Tiny-Q4_K_M.gguf": model.read_bytes(), "mmproj-F16.gguf": b"proj" * 100,
                  "Q8/Tiny-Q8_0-00001-of-00002.gguf": model.read_bytes(), "Q8/Tiny-Q8_0-00002-of-00002.gguf": b"2" * 500}
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Fake)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_port}"
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setattr(llamacpp, "RELEASE_API", f"{base}/repos/ggml-org/llama.cpp/releases")
    monkeypatch.setattr(mdl, "HF_API", f"{base}/api")
    yield base
    srv.shutdown()


@pytest.fixture
def setup(cfg, tmp_path):
    cfg.llm.models_dir = str(tmp_path / "models")
    return cfg


AMD = {"vendor": "amd", "name": "RX 7900 XT", "vram_gb": 20.0}


# ---------------------------------------------------------------- Build
def test_asset_choice_by_gpu():
    names = ["cudart-llama-b1-bin-ubuntu-cuda-12.8-x64.tar.gz", "cudart-llama-b1-bin-ubuntu-cuda-13.4-x64.tar.gz",
             "llama-b1-bin-ubuntu-arm64.tar.gz", "llama-b1-bin-ubuntu-cuda-12.8-x64.tar.gz",
             "llama-b1-bin-ubuntu-cuda-13.4-x64.tar.gz", "llama-b1-bin-ubuntu-openvino-2026.4.1-x64.tar.gz",
             "llama-b1-bin-ubuntu-rocm-10.0-x64.tar.gz", "llama-b1-bin-ubuntu-vulkan-arm64.tar.gz",
             "llama-b1-bin-ubuntu-vulkan-x64.tar.gz", "llama-b1-bin-ubuntu-x64.tar.gz", "llama-b1-bin-win-cuda-12.4-x64.zip",
             "llama-b1-ui.tar.gz"]  # echte Namen (Release b11541)
    assets = [{"name": n} for n in names]

    def pick(gpu):
        return [(f, a["name"]) for f, a in llamacpp.pick_assets(assets, gpu)]
    assert pick(AMD) == [("rocm", "llama-b1-bin-ubuntu-rocm-10.0-x64.tar.gz"),
                         ("vulkan", "llama-b1-bin-ubuntu-vulkan-x64.tar.gz"), ("cpu", "llama-b1-bin-ubuntu-x64.tar.gz")]
    cuda = llamacpp.pick_assets(assets, {"vendor": "nvidia"})[0][1]
    assert cuda["name"] == "llama-b1-bin-ubuntu-cuda-12.8-x64.tar.gz"  # nicht das cudart-Paket
    assert llamacpp.cudart_for(assets, cuda)["name"] == "cudart-llama-b1-bin-ubuntu-cuda-12.8-x64.tar.gz"
    assert [f for f, _ in pick({"vendor": "none"})] == ["cpu"]
    rel, seen = llamacpp.pick_release([{"tag_name": "v0.6.0", "assets": [{"name": "nightly-tag.txt"}]},
                                       {"tag_name": "b1", "assets": assets}], AMD)
    assert rel["tag_name"] == "b1" and seen == ["v0.6.0", "b1"]
    assert llamacpp.pick_release({"tag_name": "x", "assets": []}, AMD) == (None, ["x"])


def test_cuda_build_gets_its_runtime(setup, fake):
    Fake.assets = {"llama-b9999-bin-ubuntu-cuda-12.8-x64.tar.gz": _tgz({"build/bin/llama-server": WORKS.replace(
                       "Vulkan0", "CUDA0")}),
                   "cudart-llama-b9999-bin-ubuntu-cuda-12.8-x64.tar.gz": _tgz({"libcudart.so.12": "rt"})}
    m = llamacpp.install(setup, {"vendor": "nvidia", "vram_gb": 12}, lambda _: None)
    assert m["flavor"] == "cuda" and (llamacpp.root(setup) / "current" / "libcudart.so.12").exists()


def test_install_falls_back_to_vulkan_and_updates(setup, fake):
    lines = []
    m = llamacpp.install(setup, AMD, lines.append)
    assert m["flavor"] == "vulkan" and m["tag"] == "b9999"  # ROCm-Build fand seine Bibliotheken nicht
    assert any("rocm" in x and "libamdhip64" in x for x in lines)
    base = llamacpp.root(setup)
    assert (base / "current").resolve().name == "b9999-vulkan" and llamacpp.installed(setup)
    assert (base / "current" / "libllama.so").resolve().name == "libllama.so.0"
    assert llamacpp.install(setup, AMD, lines.append) == m  # schon da: nichts laden
    release("b10000")
    m2 = llamacpp.install(setup, AMD, lines.append, update=True)
    assert m2["tag"] == "b10000" and m2["previous"] == "b9999-vulkan"
    assert (base / "current").resolve().name == "b10000-vulkan" and (base / "b9999-vulkan").is_dir()
    release("b10001")
    llamacpp.install(setup, AMD, lines.append, update=True)
    assert not (base / "b9999-vulkan").exists()  # nur der vorige Build bleibt als Rückfall
    llamacpp.install(setup, AMD, lines.append, update=True)
    assert "aktuell" in lines[-1] or "up to date" in lines[-1]


# ---------------------------------------------------------------- Download
def test_fetch_resumes_and_survives_cancel(setup, fake, tmp_path):
    target = tmp_path / "dl" / "Tiny-Q4_K_M.gguf"
    target.parent.mkdir()
    data = Fake.files["Tiny-Q4_K_M.gguf"]
    (target.parent / (target.name + ".part")).write_bytes(data[:100])
    seen = []

    async def progress(n):
        seen.append(n)
    asyncio.run(llamacpp.fetch(f"{fake}/{REPO}/resolve/main/Tiny-Q4_K_M.gguf", target, progress))
    assert target.read_bytes() == data and seen[-1] == len(data)
    slow = tmp_path / "dl" / "slow.bin"
    with pytest.raises(TimeoutError):
        asyncio.run(asyncio.wait_for(llamacpp.fetch(f"{fake}/slow.bin", slow), 0.5))
    assert not slow.exists() and (slow.parent / "slow.bin.part").stat().st_size > 0  # Teil bleibt zum Fortsetzen


def test_download_quant_with_parts_and_vision(setup, fake):
    items = mdl.hf_quants(REPO, 20, llamacpp.models_dir(setup))
    q8 = next(x for x in items if x["quant"] == "Q8_0")
    assert q8["parts"] == 2 and q8["mmproj"]["path"] == "mmproj-F16.gguf" and not q8["installed"]
    seen = []

    async def progress(done, total):
        seen.append((done, total))
    first, mmproj, paths = asyncio.run(llamacpp.download_quant(setup, REPO, q8, progress, fake))
    assert first.name == "Tiny-Q8_0-00001-of-00002.gguf" and mmproj.name == "mmproj-F16.gguf" and len(paths) == 3
    assert first.parent == llamacpp.models_dir(setup) / "tiny_Tiny-GGUF"
    assert seen[-1][0] == seen[-1][1] == sum(len(b) for p, b in Fake.files.items() if "Q4" not in p)
    assert next(x for x in mdl.hf_quants(REPO, 20, llamacpp.models_dir(setup)) if x["quant"] == "Q8_0")["installed"]


# ---------------------------------------------------------------- Profil
def test_profile_context_port_and_quoting(setup, tmp_path):
    d = llamacpp.models_dir(setup) / "mein modell"
    d.mkdir(parents=True)
    model = d / "Big-Model-UD-Q4_K_XL.gguf"
    write_gguf(model, META)  # 64 Schichten · 8 KV-Köpfe · 256 → 256 KiB je Token
    (d / "mmproj-BF16.gguf").write_bytes(b"p")
    (d / "mmproj-F16.gguf").write_bytes(b"p")
    blocker = socket.socket()
    blocker.bind(("127.0.0.1", llamacpp.FIRST_PORT + 1))  # belegter Port wird übersprungen
    try:
        taken = {"llama": ProfileConfig(backend="openai", base_url="http://127.0.0.1:8090/v1")}
        state = tmp_path / "state.json"
        name = llamacpp.add_model(setup, state, model, None, AMD, taken, downloaded=False)
    finally:
        blocker.close()
    p = mdl.added_profiles(state)[name]
    cmd = p["server"]["command"]
    assert name == "big-model-ud-q4-k-xl" and p["label"] == "Big-Model UD-Q4_K_XL"
    assert "--port 8092" in cmd and p["base_url"] == "http://127.0.0.1:8092/v1"
    assert "-c 32768" in cmd  # passt bis 64k, aber das Modell kennt nur 32k
    assert f"--api-key {p['api_key']}" in cmd and "--jinja" in cmd and "-np 1" in cmd
    words = shlex.split(cmd)
    assert Path(words[words.index("--mmproj") + 1]).name == "mmproj-F16.gguf"  # F16 vor BF16
    prof = ProfileConfig.model_validate(p)
    assert llamacpp.profile_paths(prof) == [model.resolve()]  # Leerzeichen im Pfad übersteht das Quoting
    assert p["server"]["env"]["LD_LIBRARY_PATH"].endswith("llama.cpp/current")
    small = llamacpp.pick_context(model, 0, 8.0)
    assert small[0] == 24576 and small[1]  # 6 GiB KV-Cache passen in 8 GB, Embeddings dann auf die CPU
    with pytest.raises(ValueError):
        llamacpp.add_model(setup, state, model, None, AMD, {name: prof}, downloaded=False)  # schon ein Modell


def test_scan_and_forget(setup, tmp_path):
    base = llamacpp.models_dir(setup)
    for rel in ("a/X-Q4_K_M.gguf", "b/Big-Q8_0-00001-of-00002.gguf", "b/Big-Q8_0-00002-of-00002.gguf",
                "c/Half-Q4_0-00001-of-00002.gguf", "a/mmproj-F16.gguf", "1/2/3/4/deep.gguf"):
        (base / rel).parent.mkdir(parents=True, exist_ok=True)
        (base / rel).write_bytes(b"x" * 1000)
    used = {"x": ProfileConfig(backend="openai", server=ServerConfig(command=f"llama-server -m {base}/a/X-Q4_K_M.gguf"))}
    items = {x["rel"]: x for x in llamacpp.scan(setup, used, 20)}
    assert set(items) == {"a/X-Q4_K_M.gguf", "b/Big-Q8_0-00001-of-00002.gguf"}  # unvollständig/zu tief: nein
    assert items["a/X-Q4_K_M.gguf"]["profile"] == "x" and items["a/X-Q4_K_M.gguf"]["mmproj"].endswith("mmproj-F16.gguf")
    assert items["b/Big-Q8_0-00001-of-00002.gguf"]["parts"] == 2 and items["b/Big-Q8_0-00001-of-00002.gguf"]["profile"] is None

    state = tmp_path / "state.json"
    own = llamacpp.add_model(setup, state, base / "b/Big-Q8_0-00001-of-00002.gguf", None, AMD, {}, downloaded=False)
    assert llamacpp.forget(setup, state, own) == 0 and (base / "b/Big-Q8_0-00002-of-00002.gguf").exists()
    got = llamacpp.add_model(setup, state, base / "b/Big-Q8_0-00001-of-00002.gguf", None, AMD, {}, downloaded=True)
    assert llamacpp.forget(setup, state, got) == 2000 and not (base / "b").exists()  # selbst geladen → weg


# ---------------------------------------------------------------- Oberfläche/API
def _until_done(ws, tag):
    while True:
        ev = ws.receive_json()
        if ev["type"] == "model_pull" and ev["tag"] == tag and (ev.get("done") or ev.get("error") or ev.get("cancelled")):
            return ev


def test_web_flow(setup, fake, monkeypatch):
    monkeypatch.setenv("ORBWISE_SKIP_WARMUP", "1")
    monkeypatch.delenv("ORBWISE_FAKE_LLM", raising=False)
    monkeypatch.setattr(mdl, "detect_gpu", lambda: dict(AMD))
    setup.llm.base_url = "http://127.0.0.1:9"  # kein Ollama nötig
    mine = llamacpp.models_dir(setup) / "eigen" / "Mine-Q4_K_M.gguf"
    mine.parent.mkdir(parents=True)
    write_gguf(mine, META)
    from orbwise.server import create_app
    with TestClient(create_app(setup), base_url="http://localhost:8765") as client:
        assert client.get("/api/llamacpp").json()["installed"] is False
        local = client.get("/api/models/local").json()
        assert [x["label"] for x in local["items"]] == ["Mine Q4_K_M"] and local["items"][0]["profile"] is None
        with client.websocket_connect("ws://localhost:8765/ws", headers={"Origin": "http://localhost:8765"}) as ws:
            ws.receive_json()
            tag = client.post("/api/models/local", json={"path": str(mine)}).json()["tag"]
            ev = _until_done(ws, tag)
            assert ev.get("done"), ev
            imported = ev["profile"]
            r = client.post("/api/models/pull", json={"tag": f"https://huggingface.co/{REPO}/blob/main/Tiny-Q4_K_M.gguf"})
            assert r.json()["tag"] == f"hf.co/{REPO}:Q4_K_M"
            ev = _until_done(ws, r.json()["tag"])
            assert ev.get("done"), ev
            downloaded = ev["profile"]
        st = client.get("/api/llamacpp").json()
        assert st["installed"] and st["tag"] == "b9999" and st["flavor"] == "vulkan"
        profiles = {p["name"]: p for p in client.get("/api/models").json()["profiles"]}
        a, b = profiles[imported], profiles[downloaded]
        assert a["engine"] == b["engine"] == "llama.cpp" and a["managed"] and a["deletable"]
        assert a["keeps_files"] and not b["keeps_files"] and a["base_url"] != b["base_url"]
        assert a["quant"] == b["quant"] == "Q4_K_M" and a["path"].endswith("Mine-Q4_K_M.gguf")
        assert b["vision"] and not a["vision"] and b["repo"] == REPO and a["fit"] == "ok"
        assert client.get("/api/models").json()["gpu"]["vendor"] == "amd"
        assert client.get("/api/models/local").json()["items"][0]["profile"] == imported
        folder = llamacpp.models_dir(setup) / "tiny_Tiny-GGUF"
        assert (folder / "Tiny-Q4_K_M.gguf").exists() and (folder / "mmproj-F16.gguf").exists()
        assert client.delete(f"/api/models/{downloaded}").json()["freed_gb"] >= 0 and not folder.exists()
        assert client.delete(f"/api/models/{imported}").status_code == 200 and mine.exists()  # eigene Datei bleibt
        assert client.post("/api/models/local", json={"path": "/gibts/nicht.gguf"}).status_code == 400


# ---------------------------------------------------------------- Altes hf.co-Ollama-Modell umstellen
def test_embeddings_fall_back_to_cpu_when_the_gpu_runner_crashes(cfg):
    import httpx

    from orbwise.llm import OllamaLLM
    calls = []

    def handler(req):
        body = json.loads(req.content)
        calls.append(body.get("options"))
        if not body.get("options"):
            return httpx.Response(500, json={"error": "llama-server process has terminated: signal: segmentation fault"})
        return httpx.Response(200, json={"embeddings": [[0.1, 0.2]]})
    llm = OllamaLLM(cfg.llm, transport=httpx.MockTransport(handler))
    assert asyncio.run(llm.embed(["x"])) == [[0.1, 0.2]] and llm.embed_gpu_broken
    asyncio.run(llm.embed(["y"]))
    assert calls == [None, {"num_gpu": 0}, {"num_gpu": 0}]  # danach gleich auf der CPU


def test_prewarm_does_not_retry_a_model_that_fails_to_load(cfg, memory):
    from orbwise.agent import Agent
    from orbwise.llm import LLMError

    class Broken:
        context_size = 16384
        profile = ProfileConfig(backend="ollama", model="kaputt")
        active = "kaputt"
        calls = 0

        async def chat_stream(self, messages, tools=None, **kw):
            Broken.calls += 1
            raise LLMError("Ollama konnte das Modell nicht laden – abgestürzt")
            yield {}
    llm = Broken()
    agent = Agent(cfg, llm, memory)
    assert not asyncio.run(agent.prewarm()) and Broken.calls == 1
    assert not asyncio.run(agent.prewarm()) and Broken.calls == 1  # gesperrt – kein erneuter Absturz
    llm.active = "anderes"  # anderes Modell gewählt → wieder vorwärmen
    asyncio.run(agent.prewarm())
    assert Broken.calls == 2


def test_server_log_shows_last_start_without_key(setup, monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    monkeypatch.setenv("ORBWISE_FAKE_LLM", "1")
    monkeypatch.setenv("ORBWISE_SKIP_WARMUP", "1")
    (tmp_path / "orbwise-llm.log").write_text("===== Starte: alt --api-key aaa\nalte Zeile\n"
                                              "===== Starte: llama-server -m x.gguf --api-key geheim123\nload_tensors: ok\n")
    from orbwise.server import create_app
    with TestClient(create_app(setup), base_url="http://localhost:8765") as client:
        data = client.get("/api/models/log").json()
    assert data["lines"] == ["===== Starte: llama-server -m x.gguf --api-key •••", "load_tensors: ok"]


# ---------------------------------------------------------------- Prompt-Cache-Optionen für den eigenen llama-server
def test_cache_flags_only_what_the_server_knows(tmp_path, monkeypatch):
    import subprocess
    monkeypatch.setattr(llamacpp, "_HELP", {})
    binary = tmp_path / "bin" / "llama-server"
    binary.parent.mkdir()
    binary.write_text("")
    full_help = "-c, --ctx-size N\n--ctx-checkpoints N\n--cache-ram N\n--cache-idle-slots\n"
    calls = []

    def run(cmd, **kw):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout=full_help, stderr="")
    cmd = f"{binary} -m ~/models/q.gguf -np 1"
    # Modell nicht lesbar: llama.cpp-Standard 8 GB, höchstens ein Viertel des Arbeitsspeichers
    assert llamacpp.cache_flags(cmd, run=run, mem_mib=31 * 1024) == "--ctx-checkpoints 32 --cache-ram 7936 --cache-idle-slots"
    assert calls == [[str(binary), "--help"]]
    assert "--cache-ram 2048" in llamacpp.cache_flags(cmd, run=run, mem_mib=8 * 1024)  # 25 % von 8 GB
    assert len(calls) == 1  # Hilfetext nur einmal je Programm
    own = llamacpp.cache_flags(cmd + " --ctx-checkpoints 8 --cache-ram 1000", run=run, mem_mib=31 * 1024)
    assert own == "--cache-idle-slots"  # vom Nutzer gesetzt – bleibt
    assert llamacpp.cache_flags("python3 serve.py", run=run) == ""  # kein llama-server
    old = tmp_path / "alt" / "llama-server"
    old.parent.mkdir()
    assert llamacpp.cache_flags(f"{old} -m x.gguf", mem_mib=16384,
                                run=lambda c, **kw: subprocess.CompletedProcess(c, 0, stdout="-c, --ctx-size N\n")) == ""

    def broken(c, **kw):
        raise OSError("libvulkan fehlt")
    assert llamacpp.cache_flags(f"{tmp_path}/kaputt/llama-server -m x", run=broken) == ""


def test_cache_ram_fits_two_full_chats_of_the_model(tmp_path):
    """Ein Zustand, der größer als --cache-ram ist, legt llama-server gar nicht erst ab – der Cache muss einen langen
    Chat des Modells fassen, sonst fällt er nach jeder Zusammenfassung oder Telegram-Nachricht ganz heraus."""
    from test_llm_memory import write_gguf
    model = tmp_path / "hybrid.gguf"
    # 64 Schichten, jede 4. mit Attention (Hybrid wie Qwen 3.6): 16 × 4 × 512 × 2 B = 64 KiB je Token
    write_gguf(model, {"qwen35.block_count": 64, "qwen35.attention.head_count": 24,
                       "qwen35.attention.head_count_kv": 4, "qwen35.attention.key_length": 256,
                       "qwen35.attention.value_length": 256, "qwen35.full_attention_interval": 4,
                       "qwen35.context_length": 262144})
    cmd = f"llama-server -m {model} -c 65536 -np 1"
    two_chats = int(2 * 65536 * 65536 * 1.2 / 2**20)  # 2 Chats à 64k Token à 64 KiB, mit Zuschlag
    assert llamacpp.cache_ram_mib(cmd, ram=64 * 1024) == two_chats == 9830
    assert llamacpp.cache_ram_mib(cmd + " -ctk q8_0 -ctv q8_0", ram=64 * 1024) < two_chats  # kleinerer KV-Cache
    assert llamacpp.cache_ram_mib(cmd, ram=16 * 1024) == 4096  # höchstens ein Viertel des Arbeitsspeichers
    assert llamacpp.cache_ram_mib(cmd, ram=8 * 1024) == 2048
    tiny = cmd.replace("-c 65536", "--ctx-size=4096")
    assert llamacpp.cache_ram_mib(tiny, ram=64 * 1024) == 4096  # nie weniger als früher
    full = int(2 * 65536 * 262144 * 1.2 / 2**20)  # -c 0 = Kontext aus dem Modell (256k)
    assert llamacpp.cache_ram_mib(f"llama-server -m {model} -c 0", ram=256 * 1024) == full
    assert llamacpp.cache_ram_mib("llama-server -m /fehlt.gguf", ram=64 * 1024) == 8192
    assert llamacpp.cache_ram_mib("llama-server", ram=0) == 8192


def test_managed_server_starts_with_cache_flags(tmp_path, monkeypatch):
    import sys

    from orbwise.llm_router import ManagedServer

    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    seen = []

    def flags(command, env=None, **kw):
        seen.append(command)
        return "--ctx-checkpoints 32"
    monkeypatch.setattr(llamacpp, "cache_flags", flags)
    fake_server = Path(__file__).parent / "fake_llama_server.py"
    server = ManagedServer(ServerConfig(command=f"{sys.executable} {fake_server} {port}", startup_timeout=20),
                           f"http://127.0.0.1:{port}/v1")
    try:
        assert asyncio.run(server.ensure_running())
        log = (tmp_path / "orbwise-llm.log").read_text()
        assert f"{fake_server} {port} --ctx-checkpoints 32" in log and seen
    finally:
        asyncio.run(server.stop())


# ---------------------------------------------------------------- Updates: neuere Version, ROCm passend wählen
def test_rocm_build_matching_the_installed_rocm(tmp_path, monkeypatch):
    assets = [{"name": "llama-b1-bin-ubuntu-rocm-6.4-x64.tar.gz"}, {"name": "llama-b1-bin-ubuntu-rocm-7.2-x64.tar.gz"},
              {"name": "llama-b1-bin-ubuntu-rocm-10.1-x64.tar.gz"}, {"name": "llama-b1-bin-ubuntu-vulkan-x64.tar.gz"}]
    pick = lambda rocm: llamacpp.pick_assets(assets, AMD, rocm=rocm)[0][1]["name"]  # noqa: E731
    assert "rocm-7.2" in pick("7.2.4")  # gleiche Haupt- und Nebenversion
    assert "rocm-10.1" in pick("10.1.0")
    assert "rocm-6.4" in pick("6.4.3")
    assert "rocm-7.2" in pick("7.3.0")  # gleiche Hauptversion, nicht neuer als das System
    assert [f for f, _ in llamacpp.pick_assets(assets, AMD, rocm="7.2.4")] == ["rocm", "vulkan"]
    assert llamacpp.asset_rocm("llama-b1-bin-ubuntu-rocm-7.2-x64.tar.gz") == "7.2"
    assert llamacpp.asset_rocm("llama-b1-bin-ubuntu-vulkan-x64.tar.gz") is None
    rocm = tmp_path / "rocm"
    (rocm / ".info").mkdir(parents=True)
    (rocm / ".info" / "version").write_text("7.2.4-71\n")
    assert llamacpp.rocm_version(str(rocm)) == "7.2.4"
    monkeypatch.setenv("ROCM_PATH", str(tmp_path / "keins"))
    assert llamacpp.rocm_version(str(tmp_path / "fehlt")) in (None, llamacpp.rocm_version())  # /opt/rocm je Rechner


def test_rocm_status_only_reports(setup, monkeypatch):
    monkeypatch.setattr(llamacpp, "rocm_version", lambda base=None: "7.2.4")
    root = llamacpp.root(setup)
    root.mkdir(parents=True)
    (root / "manifest.json").write_text(json.dumps({"tag": "b1", "flavor": "rocm", "rocm_build": "10.1"}))
    st = llamacpp.rocm_status(setup, "10.1.0")
    assert st["installed"] == "7.2.4" and st["newer"] and st["mismatch"] and st["guide"].startswith("https://rocm.docs")
    (root / "manifest.json").write_text(json.dumps({"tag": "b1", "flavor": "rocm", "rocm_build": "7.2"}))
    assert not llamacpp.rocm_status(setup, "7.2.4")["newer"] and not llamacpp.rocm_status(setup, None)["mismatch"]


def test_latest_versions_are_checked_and_remembered(monkeypatch):
    import httpx
    calls = []

    def handler(req):
        calls.append(req.url.path)
        if req.url.path == "/repos/ROCm/ROCm/releases/latest":
            return httpx.Response(200, json={"tag_name": "rocm-10.1.0"})
        return httpx.Response(200, json=[
            {"tag_name": "v0.6.0", "assets": [{"name": "nightly-tag.txt"}]},
            {"tag_name": "b7001", "assets": [{"name": "llama-b7001-bin-ubuntu-vulkan-x64.tar.gz"}]}])
    client = httpx.Client(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(llamacpp, "_CHECKS", {})
    assert llamacpp.latest_tag(AMD, client) == "b7001" and llamacpp.latest_rocm(client) == "10.1.0"
    assert llamacpp.latest_tag(AMD, client) == "b7001" and len(calls) == 2  # gemerkt – GitHub nicht erneut fragen
    llamacpp.latest_tag(AMD, client, force=True)
    assert len(calls) == 3

    def offline(req):
        raise httpx.ConnectError("offline")
    down = httpx.Client(transport=httpx.MockTransport(offline))
    assert llamacpp.latest_tag(AMD, down, force=True) == "b7001"  # offline: letzter bekannter Stand
    monkeypatch.setattr(llamacpp, "_CHECKS", {})
    assert llamacpp.latest_rocm(down) is None


def test_status_api_reports_update_and_rocm(setup, monkeypatch):
    monkeypatch.setenv("ORBWISE_SKIP_WARMUP", "1")
    monkeypatch.setenv("ORBWISE_FAKE_LLM", "1")
    monkeypatch.setattr(mdl, "detect_gpu", lambda: dict(AMD))
    monkeypatch.setattr(llamacpp, "latest_tag", lambda gpu, client=None, force=False: "b7001")
    monkeypatch.setattr(llamacpp, "latest_rocm", lambda client=None, force=False: "10.1.0")
    monkeypatch.setattr(llamacpp, "rocm_version", lambda base=None: "7.2.4")
    root = llamacpp.root(setup)
    root.mkdir(parents=True)
    (root / "manifest.json").write_text(json.dumps({"tag": "b6900", "flavor": "rocm", "rocm_build": "7.2"}))
    from orbwise.server import create_app
    client = TestClient(create_app(setup), base_url="http://localhost:8765")
    plain = client.get("/api/llamacpp").json()
    assert "latest" not in plain and plain["rocm"]["installed"] == "7.2.4"  # ohne check: kein Netz
    st = client.get("/api/llamacpp?check=1").json()
    assert st["latest"] == "b7001" and st["update"] is True
    assert st["rocm"] == {"installed": "7.2.4", "build": "7.2", "latest": "10.1.0", "newer": True,
                          "mismatch": False, "guide": llamacpp.ROCM_GUIDE}
    (root / "manifest.json").write_text(json.dumps({"tag": "b7001", "flavor": "rocm"}))
    assert client.get("/api/llamacpp?check=1").json()["update"] is False


def test_flavor_switch_downloads_a_missing_build_first(setup, monkeypatch):
    monkeypatch.setenv("ORBWISE_SKIP_WARMUP", "1")
    monkeypatch.setenv("ORBWISE_FAKE_LLM", "1")
    monkeypatch.setattr(mdl, "detect_gpu", lambda: dict(AMD))
    monkeypatch.setattr(llamacpp, "rocm_version", lambda base=None: "7.2.4")
    root = llamacpp.root(setup)
    rocm = root / "b7001-rocm"
    rocm.mkdir(parents=True)
    (rocm / "llama-server").write_text(WORKS)
    (root / "current").symlink_to("b7001-rocm")
    (root / "manifest.json").write_text(json.dumps({"tag": "b7001", "flavor": "rocm", "dir": "b7001-rocm",
                                                    "rocm_build": "7.2"}))
    fetched = []

    def fetch(cfg, gpu, flavor, out=print, **kw):
        fetched.append(flavor)
        target = llamacpp.flavor_dir(cfg, flavor)
        target.mkdir(parents=True)
        (target / "llama-server").write_text(WORKS)
        (target / "asset.txt").write_text("llama-b7001-bin-ubuntu-vulkan-x64.tar.gz")
        return target
    monkeypatch.setattr(llamacpp, "fetch_flavor", fetch)
    from orbwise.server import create_app
    with TestClient(create_app(setup), base_url="http://localhost:8765") as client:
        st = client.get("/api/llamacpp").json()
        assert st["flavors"] == [{"flavor": "rocm", "ready": True}, {"flavor": "vulkan", "ready": False}]
        with client.websocket_connect("ws://localhost:8765/ws", headers={"Origin": "http://localhost:8765"}) as ws:
            ws.receive_json()
            r = client.post("/api/llamacpp/flavor", json={"flavor": "vulkan"}).json()
            assert r["job"] == "llama.cpp"
            ev = _until_done(ws, "llama.cpp")
        assert ev.get("done") and ev["flavor"] == "vulkan" and fetched == ["vulkan"]
        m = llamacpp.manifest(setup)
        assert m["flavor"] == "vulkan" and "rocm_build" not in m and os.readlink(root / "current") == "b7001-vulkan"
        # zurück auf ROCm: liegt schon da – sofort, ohne Download
        r = client.post("/api/llamacpp/flavor", json={"flavor": "rocm"}).json()
        assert "job" not in r and r["flavor"] == "rocm" and fetched == ["vulkan"]
        assert llamacpp.manifest(setup)["preferred_flavor"] == "rocm"
        assert client.post("/api/llamacpp/flavor", json={"flavor": "metal"}).status_code == 400
