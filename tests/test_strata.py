"""Strata als Engine: Terminal wie install.sh einrichten (Prüfsumme + Signatur), Variante passend zum Speicher, Profil
als abgelöster Server (launch --no-tui / --stop, bereit erst bei „loaded“), keine llama.cpp-Optionen, Werkzeug-Test."""

import asyncio
import hashlib
import io
import json
import tarfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest
from fastapi.testclient import TestClient

from orbwise import strata
from orbwise.config import LLMConfig, ProfileConfig, ServerConfig
from orbwise.llm import LLMError
from orbwise.llm_router import LLMRouter, ManagedServer, _server_ctx, is_strata

MANIFEST = ("linux-x86_64 0.1.48 strata-control-0.1.48-linux-x86_64.tar.gz {sha} {size}\n"
            "macos-arm64 0.1.48 strata-control-0.1.48-macos-arm64.tar.gz abc 1\n")


def run(coro):
    return asyncio.run(coro)


def terminal_archive() -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, data in (("bin/strata", b"#!/bin/sh\necho strata\n"), ("VERSION", b"0.1.48\n")):
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("STRATA_DATA_DIR", str(tmp_path / "strata"))
    return tmp_path


def feed(archive: bytes, sha: str | None = None):
    sha = sha or hashlib.sha256(archive).hexdigest()

    def handler(r):
        if r.url.path.endswith("install-manifest.txt"):
            return httpx.Response(200, text=MANIFEST.format(sha=sha, size=len(archive)))
        return httpx.Response(200, content=archive)
    return httpx.MockTransport(handler)


class Ran:
    def __init__(self, code=0):
        self.code, self.calls = code, []

    def __call__(self, argv, **kw):
        self.calls.append(argv)
        return type("R", (), {"returncode": self.code, "stdout": "feed ok" if not self.code else "", "stderr": "bad sig"})()


def test_manifest_and_choice_of_variant():
    line = strata.parse_manifest(MANIFEST.format(sha="3c8f", size=98715291))
    assert line == {"version": "0.1.48", "file": "strata-control-0.1.48-linux-x86_64.tar.gz", "sha256": "3c8f",
                    "size": 98715291}
    assert strata.parse_manifest("windows-x86_64 1 x.zip abc 1") is None
    pick = {o["model"]: o for o in strata.options(20, 31.3)}  # 32 GB zeigt das System als ~31 GB
    assert pick["iq1_m"]["recommended"] and pick["iq1_m"]["fits"] and not pick["q2_0"]["fits"]
    assert next(o for o in strata.options(20, 64) if o["recommended"])["model"] == "iq2_xs"
    assert next(o for o in strata.options(24, 128) if o["recommended"])["model"] == "iq3_s"
    assert not any(o["recommended"] for o in strata.options(20, 16))
    ok, note = strata.gpu_supported({"vendor": "amd", "vram_gb": 20})
    assert ok and "experimentell" in note
    assert not strata.gpu_supported({"vendor": "nvidia", "vram_gb": 8}, {})[0]
    assert not strata.gpu_supported({"vendor": "none", "vram_gb": 0})[0]


def test_nvidia_rtx_4080_with_64_gb(monkeypatch):
    rtx = {"vendor": "nvidia", "name": "NVIDIA GeForce RTX 4080", "vram_gb": 16.0}
    assert strata.gpu_supported(rtx, {"driver": "580.95.05", "compute_cap": 8.9}) == (True, "")
    ok, note = strata.gpu_supported(rtx, {"driver": "550.120", "compute_cap": 8.9})
    assert ok and "CUDA-12" in note
    ok, note = strata.gpu_supported(rtx, {"driver": "470.256", "compute_cap": 8.9})
    assert not ok and "zu alt" in note
    assert not strata.gpu_supported({**rtx, "vram_gb": 12}, {"driver": "580.65", "compute_cap": 6.1})[0]  # GTX
    opts = {o["model"]: o for o in strata.options(16, 62.7)}  # 64 GB zeigt das System als ~62,7 GB
    assert opts["iq2_xs"]["recommended"] and opts["q2_0"]["fits"] and opts["iq3_s"]["fits"]
    monkeypatch.setattr(strata.shutil, "which", lambda name: "/usr/bin/nvidia-smi")
    fake_smi = lambda argv, **kw: type("R", (), {"stdout": "580.95.05, 8.9\n"})()  # noqa: E731
    assert strata.nvidia_info(run=fake_smi) == {"driver": "580.95.05", "compute_cap": 8.9}


def test_vram_reserve_for_the_desktop():
    """AMD unter Linux: der Desktop braucht Platz auf derselben Karte – sonst schiebt amdgpu Experten in den vollen RAM."""
    assert strata.default_reserve({"vendor": "amd"}) == 3072 and strata.default_reserve({"vendor": "nvidia"}) == 700
    cmd = "/x/strata launch --model iq1_m --context 65536 --yes --no-tui --restart"
    assert strata.reserve_of(cmd) == 700 and strata.with_reserve(cmd, 700) == cmd
    with_3g = strata.with_reserve(cmd, 3072)
    assert with_3g.endswith(" --set vram-reserve-mib=3072") and strata.reserve_of(with_3g) == 3072
    again = strata.with_reserve(with_3g, 4096)
    assert again.count("vram-reserve-mib") == 1 and strata.reserve_of(again) == 4096
    assert strata.with_reserve(with_3g, 700) == cmd
    # 3 GB Reserve auf der RX 7900 XT: weniger Platz für Experten → 64K statt 128K empfohlen
    assert strata.recommend_context(19.98, 3072) == 65536 and strata.recommend_context(16, 3072) == 65536
    assert next(o for o in strata.context_options("iq1_m", 20, 31.3, 3072) if o["recommended"])["ctx"] == 65536


def test_context_follows_strata_rules():
    assert strata.recommend_context(12) == 32768 and strata.recommend_context(16) == 65536
    assert strata.recommend_context(19.98) == 131072  # RX 7900 XT meldet knapp unter 20 GB
    assert round(strata.kv_gb(131072), 1) == 1.8
    # RX 7900 XT + 32 GB mit dem Coder: RAM ist voll, der KV-Cache bleibt im VRAM
    home = {o["ctx"]: o for o in strata.context_options("iq1_m", 20, 31.3)}
    assert home[131072]["recommended"] and not home[131072]["kv_in_ram"] and "VRAM" in home[131072]["note"]
    # RTX 4080 + 64 GB mit IQ2_XS: 64K empfohlen, ab 64K liegt der KV-Cache im RAM – auch bei 256K
    work = {o["ctx"]: o for o in strata.context_options("iq2_xs", 16, 62.7)}
    assert work[65536]["recommended"] and work[131072]["kv_in_ram"] and work[262144]["kv_in_ram"]
    assert not work[32768]["kv_in_ram"] and all(o["fits"] for o in work.values())
    # IQ3_S: über 128K nach Stratas Schätzung mehr RAM, als 64 GB hergeben
    big = {o["ctx"]: o for o in strata.context_options("iq3_s", 24, 64)}
    assert big[131072]["fits"] and not big[262144]["fits"] and "RAM" in big[262144]["note"]
    assert {o["ctx"]: o for o in strata.context_options("iq3_s", 24, 128)}[262144]["fits"]


def test_install_checks_checksum_and_signature(home, monkeypatch):
    archive = terminal_archive()
    monkeypatch.setattr(strata, "TRANSPORT", feed(archive, sha="0" * 64))
    with pytest.raises(ValueError, match="Prüfsumme"):
        run(strata.install(run=Ran()))
    assert not strata.installed()

    monkeypatch.setattr(strata, "TRANSPORT", feed(archive))
    bad = Ran(code=1)
    with pytest.raises(ValueError, match="Signatur"):
        run(strata.install(run=bad))
    assert not strata.installed() and bad.calls[0][1:4] == ["update", "--check", "--no-engine"]

    seen = []

    async def progress(done, total):
        seen.append((done, total))
    assert run(strata.install(progress, run=Ran())) == "0.1.48"
    assert strata.installed() and strata.version() == "0.1.48" and seen[-1] == (len(archive), len(archive))
    assert run(strata.install(run=Ran())) == "0.1.48"  # schon aktuell: nichts zu tun


def test_profile_runs_strata_detached(home, cfg):
    state = home / "state.json"
    name = strata.add_model(cfg, state, "iq1_m", {})
    raw = json.loads(state.read_text())
    p = raw["added_profiles"][name]
    cmd = p["server"]["command"]
    assert "launch --model iq1_m --family coder --context 65536" in cmd and "--yes --no-tui --restart" in cmd
    assert "--api-key-env ORBWISE_STRATA_KEY" in cmd and p["api_key"] not in cmd  # Schlüssel nicht in der Befehlszeile
    assert p["server"]["env"]["ORBWISE_STRATA_KEY"] == p["api_key"]
    assert p["server"]["detached"] and p["server"]["ready_key"] == "loaded" and p["server"]["stop_command"].endswith(
        "launch --stop")
    assert raw["local_models"][name]["engine"] == "strata"
    with pytest.raises(ValueError):
        strata.add_model(cfg, state, "gibts-nicht", {})

    server = ServerConfig(**p["server"])
    assert is_strata(server) and _server_ctx(server) == 65536
    router = LLMRouter(LLMConfig(profiles={"s": ProfileConfig(**{**p, "server": server})}, active="s"),
                       state_path=home / "other.json")
    info = router.describe()[0]
    assert info["strata"] and not info["kv_settable"] and not info["ubatch_settable"] and info["spec_options"] == []
    router._apply_context("s", 131072)
    assert router.profiles["s"].server.command.count("--context") == 1 and "--context 131072" in \
        router.profiles["s"].server.command
    for call in (router.set_kv("s", "q8_0"), router.set_ubatch("s", 1024), router.set_spec("s", "ngram")):
        with pytest.raises(LLMError, match="Strata"):
            run(call)
    # VRAM-Reserve: gemerkt und nach einem Neustart wieder im Befehl
    assert info["strata_reserve"] == 700
    router.unloaded = True  # nicht geladen: gilt beim nächsten Start, ohne Strata hier zu starten
    run(router.set_strata_reserve("s", 3072))
    assert "--set vram-reserve-mib=3072" in router.profiles["s"].server.command
    assert router.describe()[0]["strata_reserve"] == 3072
    run(router.set_strata_reserve("s", 1024))  # schlanker Desktop: 1 GB
    assert "--set vram-reserve-mib=1024" in router.profiles["s"].server.command
    run(router.set_strata_reserve("s", 3072))
    with pytest.raises(LLMError, match="Reserve"):
        run(router.set_strata_reserve("s", 1000))
    again = LLMRouter(LLMConfig(profiles={"s": ProfileConfig(**{**p, "server": server})}, active="s"),
                      state_path=home / "other.json")
    assert again.describe()[0]["strata_reserve"] == 3072
    assert strata.reserve_of(strata.make_profile(cfg, "iq1_m", 9999, reserve=3072)["server"]["command"]) == 3072


class Health(BaseHTTPRequestHandler):
    loaded_after = 0.0
    tool_calls = True

    def log_message(self, *a):
        pass

    def do_GET(self):
        body = json.dumps({"status": "ok", "loaded": time.time() >= Health.loaded_after}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        self.rfile.read(int(self.headers["Content-Length"]))
        msg = {"role": "assistant", "content": "Es ist spät."}
        if Health.tool_calls:
            msg = {"role": "assistant", "content": "", "tool_calls": [
                {"id": "1", "type": "function", "function": {"name": "get_time", "arguments": "{}"}}]}
        out = json.dumps({"choices": [{"message": msg, "finish_reason": "stop"}]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(out)


@pytest.fixture
def health(monkeypatch):
    monkeypatch.setattr(ManagedServer, "STOP_WAIT_S", 0.3)  # der Fake hört nicht auf zu lauschen
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Health)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    Health.loaded_after, Health.tool_calls = 0.0, True
    yield srv.server_port
    srv.shutdown()


def test_detached_server_waits_for_loaded_and_stops_with_its_command(health, tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    marker = tmp_path / "stopped"
    Health.loaded_after = time.time() + 1.5  # erst „lädt noch“
    cfg = ServerConfig(command="true", detached=True, ready_key="loaded", startup_timeout=20,
                       stop_command=f"touch {marker}")
    server = ManagedServer(cfg, f"http://127.0.0.1:{health}/v1")
    assert not run(server.healthy())
    started = time.time()
    assert run(server.ensure_running()) is True and time.time() - started >= 1  # Starter endete mit 0 – kein Fehler
    run(server.stop())
    assert marker.exists()
    failing = ManagedServer(ServerConfig(command="exit 6", detached=True, ready_key="loaded", startup_timeout=20),
                            "http://127.0.0.1:9/v1")
    with pytest.raises(LLMError, match="Exit-Code 6"):
        run(failing.ensure_running())


def test_api_offers_the_coder_and_checks_tools(cfg, home, health, monkeypatch):
    from orbwise import metrics
    from orbwise import models as mdl
    monkeypatch.setenv("ORBWISE_SKIP_WARMUP", "1")
    monkeypatch.delenv("ORBWISE_FAKE_LLM", raising=False)
    monkeypatch.setenv("XDG_STATE_HOME", str(home))
    monkeypatch.setattr(mdl, "detect_gpu", lambda: {"vendor": "amd", "name": "RX 7900 XT", "vram_gb": 20.0})
    monkeypatch.setattr(metrics, "collect", lambda: {"gpu": None, "ram": {"used": 1, "total": 32 * 1024 ** 3 - 10 ** 9}})

    async def fake_install(progress=None, feed=strata.FEED, run=None):
        return "0.1.48"
    monkeypatch.setattr(strata, "install", fake_install)
    Health.tool_calls = False
    cfg.llm.base_url = "http://127.0.0.1:9"
    cfg.llm.profiles = {"qwen": ProfileConfig(model="qwen3:8b")}
    cfg.llm.active = "qwen"
    from orbwise.server import create_app
    app = create_app(cfg)
    with TestClient(app, base_url="http://localhost:8765") as client:
        st = client.get("/api/strata").json()
        assert st["supported"] and next(o for o in st["options"] if o["recommended"])["model"] == "iq1_m"
        assert client.post("/api/strata/install", json={"model": "nope"}).status_code == 400
        assert client.post("/api/strata/install", json={"model": "iq1_m"}).json()["tag"] == "Strata"
        for _ in range(100):
            profiles = {p["name"]: p for p in client.get("/api/models").json()["profiles"]}
            name = next((n for n, p in profiles.items() if p.get("strata")), None)
            if name:
                break
            time.sleep(0.05)
        p = profiles[name]
        assert p["engine"] == "Strata" and p["quant"] == "IQ1_M" and not p["kv_settable"] and p["tools_ok"] is None
        # AMD: 3 GB VRAM für den Desktop (sonst friert der PC ein) → auf der 20-GB-Karte 64K statt 128K empfohlen;
        # bei 32 GB RAM bleibt der KV-Cache im VRAM
        cmd = app.state.hub.agent.llm.profiles[name].server.command
        assert "--context 65536" in cmd and "--set vram-reserve-mib=3072" in cmd
        assert p["strata_reserve"] == 3072 and p["strata_reserve_default"] == 3072
        rec = next(o for o in p["ctx_options"] if o["recommended"])
        assert rec["ctx"] == 65536 and not rec["kv_in_ram"] and max(o["ctx"] for o in p["ctx_options"]) == 262144
        assert client.post(f"/api/models/{name}/strata-reserve", json={"mib": 1000}).status_code == 400
        assert client.post("/api/models/qwen/strata-reserve", json={"mib": 3072}).status_code == 400
        assert client.post(f"/api/models/{name}/strata-reserve", json={"mib": 4096}).json()["mib"] == 4096
        assert "--set vram-reserve-mib=4096" in app.state.hub.agent.llm.profiles[name].server.command
        assert client.post(f"/api/models/{name}/context", json={"ctx": 393216, "mode": "tools"}).status_code == 400
        assert client.post(f"/api/models/{name}/kv", json={"kv": "q8_0"}).status_code == 400
        assert next(o for o in client.get("/api/strata").json()["options"] if o["model"] == "iq1_m")["added"]
        # Laden: der „Server“ ist der Fake (Port umgebogen), der Start ein Befehl, der sofort mit 0 endet
        router = app.state.hub.agent.llm
        prof = router.profiles[name]
        router.profiles[name] = prof.model_copy(update={
            "base_url": f"http://127.0.0.1:{health}/v1",
            "server": prof.server.model_copy(update={"command": "true", "stop_command": "true"})})
        assert client.post(f"/api/models/{name}/activate").status_code == 200
        for _ in range(100):
            p = {x["name"]: x for x in client.get("/api/models").json()["profiles"]}[name]
            if p["tools_ok"] is not None:
                break
            time.sleep(0.05)
        assert p["tools_ok"] is False  # der Fake liefert keine Werkzeug-Aufrufe


def test_older_strata_profile_gets_the_amd_reserve_at_start(cfg, home, monkeypatch):
    """Vor dem Fix angelegt (ohne Reserve): beim Start bekommt das Profil auf AMD die 3 GB, ohne dass man etwas tut."""
    from orbwise import models as mdl
    monkeypatch.setenv("ORBWISE_SKIP_WARMUP", "1")
    monkeypatch.delenv("ORBWISE_FAKE_LLM", raising=False)
    monkeypatch.setattr(mdl, "detect_gpu", lambda: {"vendor": "amd", "name": "RX 7900 XT", "vram_gb": 20.0})
    state = cfg.memory.dir.parent / "state.json"
    name = strata.add_model(cfg, state, "iq1_m", {})
    raw = json.loads(state.read_text())
    raw.pop("strata_reserve")  # Stand vor dem Fix
    state.write_text(json.dumps(raw))
    cfg.llm.base_url = "http://127.0.0.1:9"
    cfg.llm.profiles = {"qwen": ProfileConfig(model="qwen3:8b")}
    cfg.llm.active = "qwen"
    from orbwise.server import create_app
    app = create_app(cfg)
    with TestClient(app, base_url="http://localhost:8765"):
        router = app.state.hub.agent.llm
        for _ in range(100):
            if "vram-reserve-mib=3072" in router.profiles[name].server.command:
                break
            time.sleep(0.05)
        assert "--set vram-reserve-mib=3072" in router.profiles[name].server.command
    assert json.loads(state.read_text())["strata_reserve"] == {name: 3072}
