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
        self.active = "llama"
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
    assert size_of("16:9") == (1376, 768) and size_of("1000x700") == (992, 704) and size_of("?") == (1024, 1024)
    assert size_of("1:1", 2.0) == (1440, 1440) and size_of("1:1", 0.5) == (736, 736)
    assert size_of("ref", 1.0, (3000, 2000)) == size_of("3:2", 1.0)  # wie Vorlage
    assert size_of("4000x100") == (2048, 256)  # Grenzen
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
    assert (body["width"], body["height"], body["batch_count"]) == (896, 1184, 2)
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


def test_several_cards_count_the_biggest_not_the_sum(engine):
    gib = 1024 ** 3
    engine.gpu_info = lambda: {"vram_total": 24 * gib, "gpus": [{"vram_total": 12 * gib}, {"vram_total": 12 * gib}]}
    assert engine.vram_gb() == 12 and engine._should_unload() and "--offload-to-cpu" in engine.command()


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
    with pytest.raises(ImageError, match="Bildmodelle"):
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
    assert setup.is_set_up(d) and set(setup.model_entry(d)["files"]) == {"diffusion", "llm", "vae", "vision"}
    assert setup.active_id(d) == "qwen-image"
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
        bad = client.post("/api/image/generate", json={"prompt": "x", "refs": [{"file": "fehlt.png"}]})
        assert bad.status_code == 400
        many = client.post("/api/image/generate", json={"prompt": "x", "refs": [{"data": "aGFsbG8="}] * 4})
        assert many.status_code == 400 and "3" in many.json()["detail"]


def test_tool_only_when_set_up(tmp_path, engine):
    from orbwise.tools import image_tools
    from orbwise.tools.registry import CONFIRM, ToolContext, get_tool, load_all_tools
    load_all_tools()
    spec = get_tool("generate_image")
    assert spec.risk == CONFIRM and not spec.is_enabled(Config.model_validate({"image": {"dir": str(tmp_path / "x")}}))
    assert spec.is_enabled(engine.cfg)
    ctx = ToolContext(cfg=engine.cfg, memory=None, services={"images": engine})
    out = run(image_tools.generate_image(ctx, "a lighthouse at dusk", size="16:9"))
    assert "Bild gespeichert" in out and "1376×768" in out


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


def test_image_size_from_headers():
    import struct

    from orbwise.imagegen import image_size
    png = b"\x89PNG\r\n\x1a\n" + b"\0\0\0\rIHDR" + struct.pack(">II", 1920, 1080) + b"\0" * 8
    assert image_size(png) == (1920, 1080)
    jpeg = (b"\xff\xd8" + b"\xff\xe0" + struct.pack(">H", 16) + b"\0" * 14
            + b"\xff\xc0" + struct.pack(">HBHH", 17, 8, 768, 1024) + b"\0" * 10)
    assert image_size(jpeg) == (1024, 768)
    assert image_size(b"GIF89a" + struct.pack("<HH", 300, 200)) == (300, 200)
    assert image_size(b"kein bild") is None


def test_several_references_in_order(engine):
    import struct
    portrait = b"\x89PNG\r\n\x1a\n" + b"\0\0\0\rIHDR" + struct.pack(">II", 600, 900) + b"\0" * 8
    saved = run(engine.generate("the woman from image 1 and the man from image 2 on a park bench",
                                ref_images=[portrait, b"\x89PNGzwei"], size="ref"))
    body = json.loads(Path(str(engine.directory / "fake_sd_server.py") + ".last.json").read_text())["body"]
    assert len(body["ref_images"]) == 2 and base64_of(portrait) == body["ref_images"][0]
    assert body["width"] < body["height"]  # Seitenverhältnis von Bild 1 (hochkant)
    assert saved[0]["refs"] == 2
    with pytest.raises(ImageError, match="Höchstens 3"):
        run(engine.generate("x", ref_images=[b"a", b"b", b"c", b"d"]))


def base64_of(data: bytes) -> str:
    import base64
    return base64.b64encode(data).decode()


# ---------------------------------------------------------------- Mehrere Bildmodelle
CATALOG_TREES = {
    **TREES,
    setup.ZIMAGE_REPO: ["z_image_turbo-Q4_K.gguf", "z_image_turbo-Q8_0.gguf"],
    setup.ZIMAGE_TEXT_REPO: ["Qwen3-4B-Instruct-2507-Q4_K_M.gguf", "Qwen3-4B-Instruct-2507-Q8_0.gguf"],
    setup.FLUX_REPO: ["ae.safetensors", "flux1-schnell.safetensors"],
    setup.FLUX_GGUF_REPO: ["flux1-schnell-Q4_K_S.gguf", "flux1-schnell-Q8_0.gguf"],
    setup.FLUX_TEXT_REPO: ["clip_l.safetensors", "t5xxl_fp16.safetensors", "t5xxl_fp8_e4m3fn.safetensors"],
    setup.SDXL_REPO: ["sd_xl_base_1.0.safetensors", "sd_xl_base_1.0_0.9vae.safetensors"],
    setup.SDXL_VAE_REPO: ["sdxl_vae.safetensors"],
}


def catalog_client(requests=None):
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w") as z:
        z.writestr("sd-server", "#!/bin/sh\n")

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if requests is not None:
            requests.append(url)
        if "api.github.com" in url:
            return httpx.Response(200, json={"assets": [{"name": "sd-bin-Linux-x86_64-vulkan.zip",
                                                         "browser_download_url": "https://x/vulkan.zip"}]})
        if url == "https://x/vulkan.zip":
            return httpx.Response(200, content=archive.getvalue())
        if "/api/models/" in url:
            repo = url.split("/api/models/")[1].split("/tree")[0]
            return httpx.Response(200, json=[{"type": "file", "path": p} for p in CATALOG_TREES[repo]])
        if "/resolve/main/" in url:
            return httpx.Response(200, content=b"weights:" + url.encode())
        return httpx.Response(404)
    return httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)


def test_each_catalog_model_finds_its_files():
    for spec in setup.CATALOG:
        small = spec.choose(CATALOG_TREES, 8)
        assert set(spec.required) <= set(small), spec.id
    flux = setup.BY_ID["flux-schnell"]
    assert flux.choose(CATALOG_TREES, 8)["t5xxl"][1] == "t5xxl_fp8_e4m3fn.safetensors"
    assert flux.choose(CATALOG_TREES, 24)["t5xxl"][1] == "t5xxl_fp16.safetensors"
    assert flux.choose(CATALOG_TREES, 24)["diffusion"][1] == "flux1-schnell-Q8_0.gguf"
    sdxl = setup.BY_ID["sdxl"].choose(CATALOG_TREES, 8)
    assert sdxl["diffusion"][1] == "sd_xl_base_1.0.safetensors" and sdxl["vae"] == (setup.SDXL_VAE_REPO, "sdxl_vae.safetensors")


def test_several_models_share_files_and_delete_cleanly(tmp_path):
    cfg = Config.model_validate({"image": {"dir": str(tmp_path / "img")}})
    d = tmp_path / "img"
    client = catalog_client()
    for model_id in ("z-image-turbo", "flux-schnell"):
        assert setup.install({"vram_gb": 8}, cfg, out=lambda s: None, client=client, model_id=model_id)
    assert setup.active_id(d) == "flux-schnell" and setup.is_set_up(d)
    assert setup.model_paths(d, "z-image-turbo")["vae"] == setup.model_paths(d, "flux-schnell")["vae"]  # nur einmal
    items = {m["id"]: m for m in setup.catalog(cfg, 8)}
    assert items["flux-schnell"]["active"] and items["z-image-turbo"]["installed"] and not items["sdxl"]["installed"]
    assert items["flux-schnell"]["fit"] == "tight" and items["z-image-turbo"]["fit"] == "ok" and items["sdxl"]["fit"] == "ok"
    setup.set_active(d, "z-image-turbo")
    assert setup.active_id(d) == "z-image-turbo"
    with pytest.raises(ValueError):
        setup.set_active(d, "sdxl")
    freed = setup.remove_model(cfg, "z-image-turbo")  # aktives gelöscht → das andere wird aktiv
    assert freed > 0 and setup.active_id(d) == "flux-schnell" and setup.is_set_up(d)
    assert setup.model_paths(d)["vae"].exists()  # das gemeinsame VAE bleibt
    assert not (d / "models" / "z_image_turbo-Q4_K.gguf").exists()
    setup.remove_model(cfg, "flux-schnell")
    assert not setup.is_set_up(d) and not any((d / "models").iterdir())
    assert setup.remove_model(cfg, "flux-schnell") == 0


def test_cancel_keeps_the_part_file(tmp_path):
    import threading
    cfg = Config.model_validate({"image": {"dir": str(tmp_path / "img")}})
    stop = threading.Event()
    stop.set()
    with pytest.raises(setup.InstallCancelled):
        setup.install({"vram_gb": 8}, cfg, out=lambda s: None, client=catalog_client(), model_id="sdxl", stop=stop)
    assert not setup.model_installed(tmp_path / "img", "sdxl")
    assert not setup.install({"vram_gb": 8}, cfg, out=lambda s: None, client=catalog_client(), model_id="gibtsnicht")


def test_old_setup_is_read_as_qwen_image(tmp_path):
    d = make_install(tmp_path)  # altes manifest.json: {"files": …, "vram_gb": 8}
    assert setup.active_id(d) == "qwen-image" and setup.is_set_up(d)
    assert set(setup.model_paths(d)) == {"diffusion", "llm", "vae", "vision"}


def test_command_steps_and_sampler_follow_the_active_model(engine, tmp_path):
    d = engine.directory
    client = catalog_client()
    assert setup.install({"vram_gb": 24}, engine.cfg, out=lambda s: None, client=client, model_id="sdxl")
    run(engine.select("sdxl"))
    cmd = engine.command()
    assert "--model" in cmd and "sd_xl_base_1.0.safetensors" in cmd and "--llm" not in cmd and "--diffusion-model" not in cmd
    engine.cfg.image.steps, engine.cfg.image.cfg_scale = 20, 6.0  # Standardwerte → die des Modells
    assert engine.defaults() == (25, 7.0, "euler_a") and not engine.can_edit()
    with pytest.raises(ImageError, match="Qwen-Image"):
        run(engine.generate("x", ref_images=[b"x"]))
    assert setup.install({"vram_gb": 24}, engine.cfg, out=lambda s: None, client=client, model_id="flux-schnell")
    run(engine.select("flux-schnell"))
    cmd = engine.command()
    assert "--clip_l" in cmd and "--t5xxl" in cmd and engine.defaults()[:2] == (4, 1.0)
    run(engine.select("qwen-image"))
    assert "--llm_vision" in engine.command() and engine.can_edit() and engine.status()["model"] == "qwen-image"
    assert d == engine.directory


def test_image_model_api(cfg, monkeypatch, tmp_path):
    from fastapi.testclient import TestClient

    from orbwise.server import create_app
    monkeypatch.setenv("ORBWISE_FAKE_LLM", "1")
    monkeypatch.setenv("ORBWISE_SKIP_WARMUP", "1")
    cfg.image.dir = str(tmp_path / "img")
    cfg.image.output_dir = str(tmp_path / "out")
    from orbwise import models as mdl
    monkeypatch.setattr(mdl, "detect_gpu", lambda: {"vendor": "amd", "name": "x", "vram_gb": 16.0, "gpus": []})
    real_install = setup.install
    monkeypatch.setattr(setup, "install", lambda gpu, c, out, client, model_id, stop: real_install(
        gpu, c, out, catalog_client(), model_id, stop))
    with TestClient(create_app(cfg), base_url="http://localhost:8765") as client:
        data = client.get("/api/image/models").json()
        assert [m["id"] for m in data["models"]] == ["qwen-image", "z-image-turbo", "flux-schnell", "sdxl"]
        assert not any(m["installed"] for m in data["models"]) and data["installing"] == ""
        assert client.post("/api/image/models/gibtsnicht/install").status_code == 404
        assert client.post("/api/image/models/sdxl/activate").status_code == 404
        with client.websocket_connect("ws://localhost:8765/ws", headers={"Origin": "http://localhost:8765"}) as ws:
            assert client.post("/api/image/models/z-image-turbo/install").json() == {"started": True}
            events = []
            while not any(e.get("type") == "image_install" and e.get("done") for e in events):
                events.append(ws.receive_json())
        done = next(e for e in events if e.get("done"))
        assert done["ok"] and any(e.get("pct") == 100 for e in events if e.get("type") == "image_install")
        st = client.get("/api/image/status").json()
        assert st["available"] and st["model"] == "z-image-turbo" and st["steps"] == 8 and not st["can_edit"]
        listed = {m["id"]: m for m in client.get("/api/image/models").json()["models"]}
        assert listed["z-image-turbo"]["active"] and listed["z-image-turbo"]["disk_gb"] >= 0
        gone = client.delete("/api/image/models/z-image-turbo").json()
        assert gone["available"] is False
        assert client.delete("/api/image/models/install").json() == {"ok": False}
