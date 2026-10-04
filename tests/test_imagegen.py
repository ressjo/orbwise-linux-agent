"""Bild-Modus: Qwen-Image-2.1 über stable-diffusion.cpp – Startbefehl nach VRAM, Erzeugen mit Fortschritt und Ablage,
Übergabe des Grafikspeichers an/vom Sprachmodell, Galerie, Einrichtung (Dateiauswahl, Downloads) und API."""

import asyncio
import io
import json
import socket
import sys
import zipfile
from pathlib import Path

import httpx
import pytest
from conftest import run

from orbwise import imagegen_setup as setup
from orbwise.config import Config
from orbwise.imagegen import ImageEngine, ImageError, parse_progress, size_of

FAKE = Path(__file__).parent / "fake_sd_server.py"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def make_install(tmp_path: Path, vision: bool = True) -> Path:
    d = tmp_path / "img"
    (d / "bin").mkdir(parents=True)
    (d / "models").mkdir()
    fake = d / "fake_sd_server.py"
    fake.write_text(FAKE.read_text())
    binary = d / "bin" / "sd-server"
    binary.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{fake}" "$@"\n')
    binary.chmod(0o755)
    files = {"diffusion": "qwen_image_2.1-Q4_K.gguf", "llm": "Qwen3VL-8B-Instruct-Q4_K_M.gguf",
             "vae": "qwen_image_2.1_vae_bf16.safetensors"}
    if vision:
        files["vision"] = "mmproj-Qwen3VL-8B-Instruct-F16.gguf"
    for name in files.values():
        (d / "models" / name).write_bytes(b"x")
    (d / "manifest.json").write_text(json.dumps({"files": files, "vram_gb": 8}))
    return d


class FakeServer:
    def __init__(self):
        self.up, self.stopped = True, 0

    def running(self):
        return self.up

    async def healthy(self):
        return self.up

    async def stop(self):
        self.up = False
        self.stopped += 1

    async def stop_foreign(self):
        return False


class FakeOllama:
    async def unload_all(self):
        return []


class FakeRouter:
    def __init__(self):
        self.active = "bonsai"
        self.llm_server = FakeServer()
        self.ollama = FakeOllama()
        self.gpu_borrowed = None
        self.prepared = 0

    def server_for(self, name):
        return self.llm_server

    async def _prepare(self, name, progress):
        self.prepared += 1
        self.llm_server.up = True


@pytest.fixture
def engine(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    d = make_install(tmp_path)
    cfg = Config.model_validate({"image": {"dir": str(d), "output_dir": str(tmp_path / "out"), "port": free_port(),
                                           "steps": 6, "startup_timeout": 30}})
    router = FakeRouter()
    vram = {"gb": 8}
    eng = ImageEngine(cfg, router, gpu_info=lambda: {"vram_total": vram["gb"] * 1024 ** 3})
    eng._vram = vram
    yield eng
    if eng.server is not None:
        run(eng.server.stop())


def test_sizes_and_progress_parsing():
    assert size_of("16:9") == (1344, 768) and size_of("1000x700") == (992, 672) and size_of("?") == (1024, 1024)
    p = parse_progress("loading …\n  |====>  | 4/20 - 2.50s/it\n  |=====> | 5/20 - 2.00s/it")
    assert p == {"step": 5, "steps": 20, "eta_s": 30.0}
    assert parse_progress("| 3/10 - 2.00it/s")["eta_s"] == 3.5
    assert parse_progress("nichts") is None


def test_command_follows_vram(engine):
    cmd = engine.command()
    assert "--diffusion-model" in cmd and "--llm_vision" in cmd and "--fa" in cmd
    assert "--offload-to-cpu" in cmd and "--clip-on-cpu" in cmd and "--vae-tiling" in cmd
    engine._vram["gb"] = 24
    big = engine.command()
    assert "--offload-to-cpu" not in big and "--clip-on-cpu" not in big
    engine.cfg.image.extra_args = "--diffusion-fa"
    assert engine.command().endswith("--diffusion-fa") and "--offload" not in engine.command()


def test_generate_saves_images_and_reports_progress(engine):
    events = []

    async def progress(state):
        events.append(state)
    saved = run(engine.generate("a red fox in the snow", size="3:4", seed=42, count=2, progress=progress))
    assert [s["seed"] for s in saved] == [42, 43]
    assert all(Path(s["path"]).read_bytes().startswith(b"\x89PNG") for s in saved)
    sent = json.loads(Path(str(engine.directory / "fake_sd_server.py") + ".last.json").read_text())
    body = sent["body"]
    assert (body["width"], body["height"], body["batch_count"]) == (864, 1152, 2)
    assert body["sample_params"]["sample_steps"] == 6 and body["sample_params"]["guidance"]["txt_cfg"] == 6.0
    assert body["vae_tiling_params"]["enabled"] is True  # 8-GB-Karte
    assert any(e.get("phase") == "loading" for e in events)
    assert any(e.get("steps") == 6 for e in events)  # Fortschritt aus der Ausgabe des Servers
    listed = engine.list()
    assert [e["file"] for e in listed[:2]] == [Path(saved[1]["path"]).name, Path(saved[0]["path"]).name]
    assert listed[0]["prompt"] == "a red fox in the snow"
    assert engine.path_of("../../etc/passwd") is None and engine.path_of(listed[0]["file"])
    assert engine.delete(listed[0]["file"]) and len(engine.list()) == 1


def test_gpu_handoff_on_small_card(engine):
    router = engine.llm
    run(engine.generate("cat", count=1))
    assert router.llm_server.stopped == 1 and engine.stopped_llm
    assert router.gpu_borrowed == engine.release
    assert engine.loaded()
    run(router.gpu_borrowed())  # Sprachmodell wird gebraucht → Bildserver weg, Sprachmodell zurück
    assert not engine.loaded() and router.prepared == 1 and router.gpu_borrowed is None


def test_big_card_keeps_llm(engine):
    engine._vram["gb"] = 24
    run(engine.generate("cat"))
    assert engine.llm.llm_server.stopped == 0 and engine.llm.gpu_borrowed is None


def test_edit_and_errors(engine, tmp_path):
    run(engine.generate("make the sky red", ref_images=[b"\x89PNGxx"]))
    sent = json.loads(Path(str(engine.directory / "fake_sd_server.py") + ".last.json").read_text())
    assert sent["body"]["ref_images"] and engine.list()[0]["edit"] is True
    with pytest.raises(ImageError, match="fehlgeschlagen"):
        run(engine.generate("this will fail"))
    with pytest.raises(ImageError):
        run(engine.generate("   "))
    no_vision = ImageEngine(Config.model_validate({"image": {"dir": str(make_install(tmp_path / "b", vision=False))}}))
    with pytest.raises(ImageError, match="mmproj"):
        run(no_vision.generate("x", ref_images=[b"x"]))


def test_cancel(engine):
    engine.cfg.image.steps = 60

    async def go():
        task = asyncio.create_task(engine.generate("slow"))
        for _ in range(200):
            await asyncio.sleep(0.05)
            if engine.job:
                break
        assert await engine.cancel()
        with pytest.raises(ImageError, match="abgebrochen"):
            await task
    run(go())


def test_not_set_up(tmp_path):
    eng = ImageEngine(Config.model_validate({"image": {"dir": str(tmp_path / "nix")}}))
    assert not eng.available() and eng.status()["available"] is False
    with pytest.raises(ImageError, match="qwen-image"):
        run(eng.generate("x"))


def test_router_gives_gpu_back_before_chat(cfg):
    from orbwise.llm_router import LLMRouter
    router = LLMRouter(cfg.llm)
    calls = []

    async def borrowed(restart=True):
        calls.append(restart)
        router.gpu_borrowed = None
    router.gpu_borrowed = borrowed

    async def fake_stream(messages, tools=None, **kw):
        yield {"type": "done", "message": {"role": "assistant", "content": "ok"}, "stats": {}}
    router.client.chat_stream = fake_stream

    async def go():
        return [ev async for ev in router.chat_stream([{"role": "user", "content": "hi"}])]
    assert run(go())[0]["type"] == "done" and calls == [True]


# ---------------------------------------------------------------- Einrichtung
TREES = {
    setup.DIFFUSION_REPO: ["README.md", "qwen_image_2.1-Q3_K.gguf", "qwen_image_2.1-Q4_K.gguf", "qwen_image_2.1-Q8_0.gguf"],
    setup.TEXT_REPO: ["Qwen3VL-8B-Instruct-Q4_K_M.gguf", "Qwen3VL-8B-Instruct-Q8_0.gguf",
                      "mmproj-Qwen3VL-8B-Instruct-F16.gguf", "mmproj-Qwen3VL-8B-Instruct-Q8_0.gguf"],
    setup.VAE_REPO: ["split_files/vae/qwen_image_vae.safetensors", "split_files/vae/qwen_image_2.1_vae_bf16.safetensors",
                     "split_files/text_encoders/x.safetensors"],
}


def test_pick_files_by_vram():
    small = setup.pick_files(TREES, 8)
    assert small["diffusion"][1] == "qwen_image_2.1-Q4_K.gguf"
    assert small["llm"][1] == "Qwen3VL-8B-Instruct-Q4_K_M.gguf"
    assert small["vision"][1] == "mmproj-Qwen3VL-8B-Instruct-F16.gguf"
    assert small["vae"][1] == "split_files/vae/qwen_image_2.1_vae_bf16.safetensors"
    big = setup.pick_files(TREES, 24)
    assert big["diffusion"][1].endswith("Q8_0.gguf") and big["llm"][1].endswith("Q8_0.gguf")


def test_install_downloads_and_writes_manifest(tmp_path):
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w") as z:
        z.writestr("sd-server", "#!/bin/sh\n")
        z.writestr("libstable-diffusion.so", "lib")
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        requests.append(url)
        if "api.github.com" in url:
            return httpx.Response(200, json={"assets": [
                {"name": "sd-master-abc-bin-Linux-Ubuntu-24.04-x86_64.zip", "browser_download_url": "https://x/cpu.zip"},
                {"name": "sd-master-abc-bin-Linux-Ubuntu-24.04-x86_64-vulkan.zip",
                 "browser_download_url": "https://x/vulkan.zip"}]})
        if url == "https://x/vulkan.zip":
            return httpx.Response(200, content=archive.getvalue())
        if "/api/models/" in url:
            repo = url.split("/api/models/")[1].split("/tree")[0]
            return httpx.Response(200, json=[{"type": "file", "path": p} for p in TREES[repo]])
        if "/resolve/main/" in url:
            return httpx.Response(200, content=b"weights")
        return httpx.Response(404)
    cfg = Config.model_validate({"image": {"dir": str(tmp_path / "img")}})
    out = []
    client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)
    assert setup.install({"vram_gb": 8}, cfg, out=out.append, client=client)
    d = tmp_path / "img"
    assert (d / "bin" / "sd-server").stat().st_mode & 0o100
    assert "https://x/cpu.zip" not in requests  # der Vulkan-Build, nicht der CPU-Build
    assert setup.is_set_up(d) and set(setup.manifest(d)["files"]) == {"diffusion", "llm", "vae", "vision"}
    assert setup.remove(cfg) > 0 and not setup.is_set_up(d)


def test_api_without_setup(cfg, monkeypatch, tmp_path):
    from fastapi.testclient import TestClient

    from orbwise.server import create_app
    monkeypatch.setenv("ORBWISE_FAKE_LLM", "1")
    monkeypatch.setenv("ORBWISE_SKIP_WARMUP", "1")
    cfg.image.dir = str(tmp_path / "none")
    cfg.image.output_dir = str(tmp_path / "out")
    with TestClient(create_app(cfg), base_url="http://localhost:8765") as client:
        assert client.get("/api/image/status").json()["available"] is False
        assert client.get("/api/image/list").json() == {"images": []}
        assert client.get("/api/image/file/..%2F..%2Fetc%2Fpasswd").status_code == 404
        assert client.post("/api/image/release").json() == {"ok": True}


def test_tool_only_when_set_up(tmp_path, engine):
    from orbwise.tools import image_tools
    from orbwise.tools.registry import CONFIRM, ToolContext, get_tool, load_all_tools
    load_all_tools()
    spec = get_tool("generate_image")
    assert spec.risk == CONFIRM and not spec.is_enabled(Config.model_validate({"image": {"dir": str(tmp_path / "x")}}))
    assert spec.is_enabled(engine.cfg)
    ctx = ToolContext(cfg=engine.cfg, memory=None, services={"images": engine})
    out = run(image_tools.generate_image(ctx, "a lighthouse at dusk", size="16:9"))
    assert "Bild gespeichert" in out and "1344×768" in out


def test_chat_waits_for_running_image(engine):
    engine.cfg.image.steps = 8

    async def go():
        task = asyncio.create_task(engine.generate("lighthouse"))
        for _ in range(200):
            await asyncio.sleep(0.05)
            if engine.job:
                break
        await engine.llm.gpu_borrowed()  # Chat-Frage mitten im Bild
        assert task.done() and len(task.result()) == 1  # Bild wurde fertig, danach erst freigegeben
        assert not engine.loaded() and engine.llm.prepared == 1
    run(go())
