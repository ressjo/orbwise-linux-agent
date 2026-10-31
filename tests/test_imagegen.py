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


def test_only_one_model_in_memory_even_on_a_big_card(engine):
    engine._vram["gb"] = 24
    run(engine.generate("cat"))  # Standard „always“: auch mit 24 GB geht das Sprachmodell vorher raus
    assert engine.llm.llm_server.stopped == 1 and engine.llm.gpu_borrowed == engine.release


def test_big_card_keeps_llm_when_asked(engine):
    engine._vram["gb"] = 24
    engine.cfg.image.unload_llm = "auto"
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
    setup.FLUX_DEV_GGUF_REPO: ["flux1-dev-Q4_K_S.gguf", "flux1-dev-Q8_0.gguf"],
    "leejet/flux-3-image-klein-GGUF": ["flux-3-image-klein-Q4_K.gguf", "flux-3-image-klein-Q8_0.gguf"],
    "leejet/flux-3-image-klein-base-GGUF": ["flux-3-image-klein-base-Q4_K.gguf", "flux-3-image-klein-base-Q8_0.gguf"],
    setup.QWEN3VL_4B_REPO: ["Qwen3VL-4B-Instruct-Q4_K_M.gguf", "Qwen3VL-4B-Instruct-Q8_0.gguf",
                            "mmproj-Qwen3VL-4B-Instruct-F16.gguf"],
    setup.FLUX3_TE_UNCENSORED: ["Qwen3VL-4B-Instruct-Uncensored-Q4_K_M.gguf", "Qwen3VL-4B-Instruct-Uncensored-Q8_0.gguf"],
    setup.FLUX3_REPO: ["ae.safetensors", "model.safetensors"],
    "leejet/FLUX.2-klein-4B-GGUF": ["flux-2-klein-4b-Q4_K.gguf"],
    "unsloth/Qwen3-4B-GGUF": ["Qwen3-4B-Q4_K_M.gguf", "Qwen3-4B-Q8_0.gguf"],
    "unsloth/Qwen3-8B-GGUF": ["Qwen3-8B-Q4_K_M.gguf", "Qwen3-8B-Q8_0.gguf"],
    "Comfy-Org/flux2-dev": ["split_files/vae/flux2-vae.safetensors", "split_files/diffusion_models/flux2_dev_fp8.safetensors"],
    "someone/FLUX.2-klein-4B-GGUF": ["flux-2-klein-4b-Q4_K_M.gguf", "flux-2-klein-4b-Q8_0.gguf"],
    "someone/FLUX.2-klein-9B-GGUF": ["flux-2-klein-9b-Q4_K_M.gguf", "flux-2-klein-9b-Q8_0.gguf"],
}
SEARCH = {"FLUX.2-klein-4B": ["someone/FLUX.2-klein-4B-GGUF"], "FLUX.2-klein-9B": ["someone/FLUX.2-klein-9B-GGUF"]}


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
        if "/api/models?" in url:  # Suche (neue Modelle)
            return httpx.Response(200, json=[{"id": r} for r in SEARCH.get(request.url.params.get("search"), [])])
        if "/api/models/" in url:
            repo = url.split("/api/models/")[1].split("/tree")[0]
            if repo not in CATALOG_TREES:
                return httpx.Response(404, json={"error": "Repository not found"})
            return httpx.Response(200, json=[{"type": "file", "path": p} for p in CATALOG_TREES[repo]])
        if "/resolve/main/" in url:
            return httpx.Response(200, content=b"weights:" + url.encode())
        return httpx.Response(404)
    return httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)


def test_each_catalog_model_finds_its_files():
    for spec in setup.CATALOG:
        trees = {**CATALOG_TREES, "__search__": [r for q in spec.search for r in SEARCH.get(q, [])]}
        small = spec.choose(trees, 8)
        assert set(spec.required) <= set(small), spec.id
    klein = setup.BY_ID["flux2-klein-9b"].choose({**CATALOG_TREES, "__search__": SEARCH["FLUX.2-klein-9B"]}, 20)
    assert klein["diffusion"] == ("someone/FLUX.2-klein-9B-GGUF", "flux-2-klein-9b-Q8_0.gguf")  # 20 GB → Q8
    assert klein["llm"][1] == "Qwen3-8B-Q8_0.gguf" and klein["vae"][1].endswith("flux2-vae.safetensors")
    dev = setup.BY_ID["flux-dev"].choose(CATALOG_TREES, 20)
    assert dev["diffusion"] == (setup.FLUX_DEV_GGUF_REPO, "flux1-dev-Q8_0.gguf") and dev["vae"][1] == "ae.safetensors"
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
    monkeypatch.setattr(setup, "install", lambda gpu, c, out, client, model_id, stop, source=None: real_install(
        gpu, c, out, catalog_client(), model_id, stop, source))
    with TestClient(create_app(cfg), base_url="http://localhost:8765") as client:
        data = client.get("/api/image/models").json()
        assert [m["id"] for m in data["models"]] == ["qwen-image", "z-image-turbo", "flux-schnell", "flux3-klein",
                                                      "flux3-klein-uncensored", "flux3-klein-base", "flux2-klein-4b",
                                                      "flux2-klein-9b", "flux-dev", "sdxl"]
        assert [m["id"] for m in data["models"] if m["new"]] == ["flux3-klein"]
        assert not any(m["installed"] for m in data["models"]) and data["installing"] == ""
        assert client.post("/api/image/models/gibtsnicht/install").status_code == 404
        assert client.post("/api/image/models/sdxl/activate").status_code == 404
        with client.websocket_connect("ws://localhost:8765/ws", headers={"Origin": "http://localhost:8765"}) as ws:
            assert client.post("/api/image/models/z-image-turbo/install").json() == {"started": True,
                                                                                    "model": "z-image-turbo"}
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


def test_new_models_find_their_repository_by_search(tmp_path):
    cfg = Config.model_validate({"image": {"dir": str(tmp_path / "img")}})
    requests = []
    assert setup.install({"vram_gb": 12}, cfg, out=lambda s: None, client=catalog_client(requests),
                         model_id="flux2-klein-4b")
    d = tmp_path / "img"
    assert setup.active_id(d) == "flux2-klein-4b" and setup.is_set_up(d)
    assert setup.model_paths(d)["diffusion"].name == "flux-2-klein-4b-Q4_K.gguf"  # zuerst das Repo aus der sd.cpp-Doku
    assert any("search=FLUX.2-klein-4B" in u for u in requests)
    # Ausweich-Repositories, die es nicht gibt (404), brechen nichts ab
    assert any("Comfy-Org/flux2-klein/tree" in u for u in requests)
    assert setup.install({"vram_gb": 12}, cfg, out=lambda s: None, client=catalog_client(), model_id="flux2-klein-9b")
    assert setup.model_paths(d, "flux2-klein-9b")["vae"] == setup.model_paths(d, "flux2-klein-4b")["vae"]  # geteilt
    # nichts gefunden → klare Meldung statt Absturz
    SEARCH["FLUX.2-klein-4B"], saved = [], SEARCH["FLUX.2-klein-4B"]
    leejet = CATALOG_TREES.pop("leejet/FLUX.2-klein-4B-GGUF")  # auch das feste Repo fehlt
    try:
        out = []
        assert not setup.install({"vram_gb": 12}, Config.model_validate({"image": {"dir": str(tmp_path / "b")}}),
                                 out=out.append, client=catalog_client(), model_id="flux2-klein-4b")
        assert "nicht gefunden" in out[-1] and "diffusion" in out[-1]
    finally:
        SEARCH["FLUX.2-klein-4B"] = saved
        CATALOG_TREES["leejet/FLUX.2-klein-4B-GGUF"] = leejet


# ---------------------------------------------------------------- Alle Text-zu-Bild-Modelle (Suche)
HF_IMAGE_MODELS = [
    {"id": "city96/FLUX.1-dev-gguf", "downloads": 900, "tags": ["gguf", "base_model:black-forest-labs/FLUX.1-dev"]},
    {"id": "someone/juggernaut-xl-v9", "downloads": 800, "likes": 40, "lastModified": "2026-01-02T00:00:00Z",
     "tags": ["base_model:stabilityai/stable-diffusion-xl-base-1.0", "license:openrail++"]},
    {"id": "someone/cool-style-lora", "downloads": 700, "tags": ["lora", "base_model:black-forest-labs/FLUX.1-dev"]},
    {"id": "Lykon/dreamshaper-8", "downloads": 600, "tags": ["stable-diffusion"]},
    {"id": "someone/mystery-model", "downloads": 500, "tags": []},
    {"id": "someone/Wan2.2-T2V", "downloads": 400, "tags": []},
]
JUGGERNAUT_TREE = [
    {"type": "file", "path": "juggernautXL_v9.safetensors", "lfs": {"size": int(6.9e9)}},
    {"type": "file", "path": "juggernautXL_v9-fp16-lora.safetensors", "lfs": {"size": int(0.2e9)}},
    {"type": "file", "path": "vae/diffusion_pytorch_model.safetensors", "lfs": {"size": int(0.3e9)}},
    {"type": "file", "path": "unet/diffusion_pytorch_model.safetensors", "lfs": {"size": int(5e9)}},
    {"type": "file", "path": "README.md", "size": 100},
]
FLUX_DEV_TREE = [{"type": "file", "path": f"flux1-dev-{q}.gguf", "lfs": {"size": int(gb * 1e9)}}
                 for q, gb in (("Q4_K_S", 6.8), ("Q8_0", 12.7), ("F16", 23.8))]


@pytest.fixture
def fake_hf(monkeypatch):
    from orbwise import models as mdl
    calls = []

    def get(path, params=None, base=None):
        calls.append((path, params))
        if path == "/models":
            search = dict(params).get("search", "").lower()
            return [m for m in HF_IMAGE_MODELS if search in m["id"].lower()]
        if path.endswith("/tree/main"):
            repo = path[len("/models/"):-len("/tree/main")]
            return {"someone/juggernaut-xl-v9": JUGGERNAUT_TREE, "city96/FLUX.1-dev-gguf": FLUX_DEV_TREE}.get(repo, [])
        repo = path[len("/models/"):]
        return next((m for m in HF_IMAGE_MODELS if m["id"] == repo), {})
    monkeypatch.setattr(mdl, "_hf_get", get)
    return calls


def test_families_are_detected_from_names_and_tags():
    d = setup.detect_family
    assert d("city96/FLUX.1-dev-gguf")[0] == "flux-dev" and d("x/flux1-schnell-Q4")[0] == "flux-schnell"
    assert d("x/some-flux-finetune")[0] == "" and "unklar" in d("x/some-flux-finetune")[1]  # Version nicht erkennbar
    assert d("x/some-flux-finetune", ["base_model:black-forest-labs/FLUX.1-dev"])[0] == "flux-dev"
    # gemeldet: FLUX 3 Image Klein ist eine eigene Generation (Qwen3-VL-4B), das „TE“-Repo nur ihr Text-Encoder
    assert d("AtomicChat/FLUX-3-Image-Klein-GGUF")[0] == "flux3-klein" and d("leejet/flux-3-image-klein-GGUF")[0] == "flux3-klein"
    assert d("leejet/flux-3-image-klein-base-GGUF")[0] == "flux3-klein-base"
    te = d("AtomicChat/FLUX-3-Image-Klein-TE-Uncensored-GGUF")
    assert te[0] == "" and "Text-Encoder" in te[1]
    assert "Text-Encoder" in d("AtomicChat/FLUX-3-Image-Klein-pup-Uncensored-GGUF")[1]
    assert d("someone/FLUX.2-klein-4B-GGUF")[0] == "flux2-klein-4b" and d("x/klein-model")[0] == "flux2-klein"
    assert d("x/FLUX.3-dev")[0] == "" and "FLUX 3" in d("x/FLUX.3-dev")[1]
    assert d("x/FLUX.2-dev")[0] == "" and "neuere" in d("x/FLUX.2-dev")[1]
    assert setup.klein_size("flux3-image-klein-Q8_0.gguf", int(4.4e9)) == "flux2-klein-4b"  # ~4 Mrd. Parameter
    assert setup.klein_size("flux3-image-klein-Q8_0.gguf", int(9.8e9)) == "flux2-klein-9b"
    assert setup.klein_size("klein-Q4_K_M.gguf", int(5.6e9)) == "flux2-klein-9b"  # 9B in Q4 ist ähnlich groß
    assert setup.klein_size("flux-2-klein-4b-Q8_0.gguf", int(9e9)) == "flux2-klein-4b"  # Name schlägt Größe
    assert d("city96/FLUX.1-schnell-gguf")[0] == "flux-schnell"
    assert d("x/y", ["base_model:stabilityai/stable-diffusion-xl-base-1.0"])[0] == "sdxl"
    assert d("x/ponyDiffusionV6XL")[0] == "sdxl" and d("Lykon/dreamshaper-8")[0] == "sd15"
    assert d("x/FLUX.2-klein-9B-GGUF")[0] == "flux2-klein-9b" and d("leejet/Z-Image-Turbo-GGUF")[0] == "z-image-turbo"
    assert d("x/FLUX.2-dev")[0] == "" and "nicht" in d("x/FLUX.2-dev")[1]
    assert d("x/style", ["lora"])[1].startswith("LoRA") and d("x/FLUX.1-Kontext-dev")[0] == ""
    assert d("x/mystery")[0] == "" and "unbekannt" in d("x/mystery")[1]


def test_search_lists_all_text_to_image_models(fake_hf):
    found = {r["repo"]: r for r in setup.hf_image_search("", "downloads")}
    assert list(found) == ["city96/FLUX.1-dev-gguf", "someone/juggernaut-xl-v9", "Lykon/dreamshaper-8"]  # nur lauffähige
    params = dict(fake_hf[0][1])
    assert params["pipeline_tag"] == "text-to-image" and params["sort"] == "trendingScore"
    hits = {r["repo"]: r for r in setup.hf_image_search("someone", "likes")}
    assert dict(fake_hf[-1][1])["sort"] == "likes"
    assert hits["someone/juggernaut-xl-v9"]["family_label"] == "Stable Diffusion XL 1.0"
    assert hits["someone/juggernaut-xl-v9"]["license"] == "openrail++" and hits["someone/juggernaut-xl-v9"]["supported"]
    assert not hits["someone/cool-style-lora"]["supported"] and "LoRA" in hits["someone/cool-style-lora"]["reason"]
    assert not hits["someone/mystery-model"]["supported"] and not hits["someone/Wan2.2-T2V"]["supported"]


def test_files_of_a_repo_with_fit(fake_hf):
    assert setup.hf_image_files("someone/juggernaut-xl-v9", 8)["variants"][0]["fit"] == "tight"  # 6,9 GB + Puffer
    jugg = setup.hf_image_files("someone/juggernaut-xl-v9", 10)
    assert jugg["family"] == "sdxl" and [v["file"] for v in jugg["variants"]] == ["juggernautXL_v9.safetensors"]
    assert jugg["variants"][0]["fit"] == "ok" and jugg["variants"][0]["recommended"]  # ohne LoRA, VAE, Diffusers-Teile
    flux = setup.hf_image_files("city96/FLUX.1-dev-gguf", 16)
    assert [v["quant"] for v in flux["variants"]] == ["Q4_K_S", "Q8_0", "F16"]
    assert [v["fit"] for v in flux["variants"]] == ["ok", "ok", "big"]
    assert [v["quant"] for v in flux["variants"] if v.get("recommended")] == ["Q8_0"]
    with pytest.raises(ValueError):
        setup.hf_image_files("../etc", 8)


def test_install_a_searched_model_with_the_family_parts(tmp_path, engine):
    cfg = engine.cfg
    d = engine.directory
    CATALOG_TREES["someone/juggernaut-xl-v9"] = ["juggernautXL_v9.safetensors"]
    try:
        model_id = setup.custom_id("someone/juggernaut-xl-v9", "juggernautXL_v9.safetensors")
        source = {"repo": "someone/juggernaut-xl-v9", "file": "juggernautXL_v9.safetensors", "family": "sdxl",
                  "label": "juggernaut-xl-v9"}
        assert setup.install({"vram_gb": 8}, cfg, out=lambda s: None, client=catalog_client(), model_id=model_id,
                             source=source)
    finally:
        CATALOG_TREES.pop("someone/juggernaut-xl-v9")
    assert setup.active_id(d) == model_id and setup.model_installed(d, model_id)
    paths = setup.model_paths(d, model_id)
    assert paths["diffusion"].name == "juggernautXL_v9.safetensors" and paths["vae"].name == "sdxl_vae.safetensors"
    run(engine.select(model_id))
    engine.cfg.image.steps = 20  # Standardwert → die Schritte der Familie
    assert "--model" in engine.command() and engine.defaults() == (25, 7.0, "euler_a")
    assert engine.status()["model_label"] == "juggernaut-xl-v9"
    listed = {m["id"]: m for m in setup.catalog(cfg, 8)}
    assert listed[model_id]["custom"] and listed[model_id]["active"] and listed[model_id]["repo"] == "someone/juggernaut-xl-v9"
    assert setup.remove_model(cfg, model_id) > 0 and model_id not in {m["id"] for m in setup.catalog(cfg, 8)}


def test_api_searches_and_installs_only_listed_files(cfg, monkeypatch, tmp_path, fake_hf):
    from fastapi.testclient import TestClient

    from orbwise.server import create_app
    monkeypatch.setenv("ORBWISE_FAKE_LLM", "1")
    monkeypatch.setenv("ORBWISE_SKIP_WARMUP", "1")
    cfg.image.dir = str(tmp_path / "img")
    cfg.image.output_dir = str(tmp_path / "out")
    started = []
    monkeypatch.setattr(setup, "install", lambda *a: started.append(a) or False)
    with TestClient(create_app(cfg), base_url="http://localhost:8765") as client:
        assert [r["repo"] for r in client.get("/api/image/hf/search").json()["results"]][:1] == ["city96/FLUX.1-dev-gguf"]
        files = client.get("/api/image/hf/files", params={"repo": "city96/FLUX.1-dev-gguf"}).json()
        assert files["family"] == "flux-dev" and len(files["variants"]) == 3
        bad = client.post("/api/image/hf/install", json={"repo": "city96/FLUX.1-dev-gguf", "file": "../../etc/passwd"})
        assert bad.status_code == 400
        lora = client.post("/api/image/hf/install", json={"repo": "someone/cool-style-lora", "file": "x.safetensors"})
        assert lora.status_code == 400 and "LoRA" in lora.json()["detail"]
        ok = client.post("/api/image/hf/install", json={"repo": "city96/FLUX.1-dev-gguf", "file": "flux1-dev-Q8_0.gguf"})
        assert ok.json()["model"] == setup.custom_id("city96/FLUX.1-dev-gguf", "flux1-dev-Q8_0.gguf")
        for _ in range(100):
            if started:
                break
            import time
            time.sleep(0.02)
        assert started[0][-1] == {"repo": "city96/FLUX.1-dev-gguf", "file": "flux1-dev-Q8_0.gguf", "family": "flux-dev",
                                  "label": "FLUX.1-dev-gguf Q8_0"}


# ---------------------------------------------------------------- Bild-Modus: das Sprachmodell bleibt draußen
def test_entering_image_mode_unloads_the_llm_and_keeps_it_out(engine):
    router = engine.llm
    assert run(engine.enter())  # Klick auf „Bild“: sofort entladen, noch kein Bildserver
    assert router.llm_server.stopped == 1 and router.gpu_borrowed == engine.release and not engine.loaded()
    run(engine.generate("cat"))  # erst das Bild lädt das (aktive) Bildmodell
    assert engine.loaded() and "--diffusion-model" in engine.command() and router.prepared == 0
    run(engine._idle_release())  # Leerlauf bei offener Ansicht: nur der Bildserver geht
    assert not engine.loaded() and router.gpu_borrowed == engine.release and router.prepared == 0
    run(engine.leave())  # Ansicht verlassen: Sprachmodell zurück
    assert router.prepared == 1 and router.gpu_borrowed is None and not engine.view_open


def test_idle_after_leaving_brings_the_llm_back(engine):
    run(engine.generate("cat"))
    engine.view_open = False
    run(engine._idle_release())
    assert engine.llm.prepared == 1 and not engine.loaded()


def test_own_image_backend_does_not_unload_the_llm(engine, tmp_path):
    setup.add_api_model(engine.cfg, "Forge", "a1111", "http://127.0.0.1:9")
    assert not run(engine.enter()) and engine.llm.llm_server.stopped == 0 and engine.llm.gpu_borrowed is None


def test_embeddings_go_to_the_cpu_while_images_have_the_gpu(cfg):
    from orbwise.llm_router import LLMRouter
    router = LLMRouter(cfg.llm)
    seen = []

    async def embed(texts):
        seen.append(router.ollama.embed_on_cpu)
        return [[0.0]] * len(texts)
    router.ollama.embed = embed

    async def borrowed(restart=True):
        pass
    run(router.embed(["a"]))
    router.gpu_borrowed = borrowed
    run(router.embed(["b"]))
    assert seen == [False, True] and router.ollama.embed_on_cpu is False  # danach wieder wie eingestellt


def test_no_background_maintenance_in_image_mode(cfg, monkeypatch, tmp_path):
    from fastapi.testclient import TestClient

    from orbwise.server import create_app
    monkeypatch.setenv("ORBWISE_FAKE_LLM", "1")
    monkeypatch.setenv("ORBWISE_SKIP_WARMUP", "1")
    cfg.image.dir = str(tmp_path / "img")
    with TestClient(create_app(cfg), base_url="http://localhost:8765") as client:
        resting = client.app.state.llm_resting
        assert resting() is False
        client.post("/api/image/enter")
        for _ in range(50):
            if client.app.state.images.view_open:
                break
            import time
            time.sleep(0.02)
        assert resting() is True  # keine Tageszusammenfassung, kein Index-Neuaufbau, solange die Bild-Ansicht offen ist
        client.post("/api/image/release")
        for _ in range(50):
            if not client.app.state.images.view_open:
                break
            time.sleep(0.02)
        assert resting() is False


# ---------------------------------------------------------------- Gemeldet: „flux3-image-klein“ als FLUX.1 gestartet
def test_klein_model_gets_the_klein_parts_not_flux1(fake_hf, tmp_path):
    HF_IMAGE_MODELS.append({"id": "someone/flux2-image-klein-GGUF", "downloads": 10, "tags": ["gguf"]})
    try:
        from orbwise import models as mdl
        real = mdl._hf_get

        def get(path, params=None, base=None):
            if path == "/models/someone/flux2-image-klein-GGUF/tree/main":
                return [{"type": "file", "path": "flux2-image-klein-Q8_0.gguf", "lfs": {"size": int(4.4e9)}}]
            return real(path, params, base)
        mdl._hf_get = get
        files = setup.hf_image_files("someone/flux2-image-klein-GGUF", 24)
        assert files["family"] == "flux2-klein" and files["variants"][0]["family"] == "flux2-klein-4b"
    finally:
        HF_IMAGE_MODELS.pop()
        mdl._hf_get = real


def test_misfiled_model_is_flagged(tmp_path):
    cfg = Config.model_validate({"image": {"dir": str(tmp_path / "img")}})
    d = tmp_path / "img"
    (d / "models").mkdir(parents=True)
    for name, size in (("flux3-image-klein-Q8_0.gguf", 10), ("clip_l.safetensors", 1), ("t5xxl_fp8_e4m3fn.safetensors", 1),
                       ("ae.safetensors", 1)):
        (d / "models" / name).write_bytes(b"x" * size)
    (d / "manifest.json").write_text(json.dumps({"active": "hf-klein", "models": {"hf-klein": {
        "files": {"diffusion": "flux3-image-klein-Q8_0.gguf", "clip_l": "clip_l.safetensors",
                  "t5xxl": "t5xxl_fp8_e4m3fn.safetensors", "vae": "ae.safetensors"},
        "family": "flux-dev", "repo": "AtomicChat/FLUX-3-Image-Klein-TE-Uncensored-GGUF", "label": "flux3-image-klein"}}}))
    item = next(m for m in setup.catalog(cfg, 24) if m["id"] == "hf-klein")
    assert "Text-Encoder" in item["misfiled"]  # das gemeldete Repo: nur der Text-Encoder von FLUX 3 Klein


def test_unrecognised_model_file_gets_a_helpful_message(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    d = make_install(tmp_path)
    (d / "bin" / "sd-server").write_text("#!/bin/sh\necho \"[ERROR] diffusion_engine.cpp:992 - get sd version from file "
                                         "failed: ''\"\necho '[ERROR] main.cpp:93 - new_sd_ctx_t failed'\nexit 1\n")
    cfg = Config.model_validate({"image": {"dir": str(d), "output_dir": str(tmp_path / "out"), "port": free_port(),
                                           "startup_timeout": 20}})
    engine = ImageEngine(cfg, gpu_info=lambda: {"vram_total": 24 * 1024 ** 3})
    with pytest.raises(ImageError) as e:
        run(engine.generate("cat"))
    assert "sd-server aktualisieren" in str(e.value) and "löschen und neu laden" in str(e.value)


def test_sd_server_update_replaces_the_build(tmp_path):
    builds = {"v1": b"#!/bin/sh\necho eins\n", "v2": b"#!/bin/sh\necho zwei\n"}
    latest = ["v1"]

    def handler(request):
        url = str(request.url)
        if "api.github.com" in url:
            return httpx.Response(200, json={"tag_name": latest[0], "assets": [
                {"name": f"sd-{latest[0]}-bin-Linux-x86_64-vulkan.zip", "browser_download_url": f"https://x/{latest[0]}.zip"}]})
        tag = url.rsplit("/", 1)[-1][:-4]
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr("sd-server", builds[tag])
        return httpx.Response(200, content=buf.getvalue())
    client = httpx.Client(transport=httpx.MockTransport(handler))
    d = tmp_path / "img"
    out = []
    assert setup.install_binary(d, client, out.append)
    assert setup.runtime_version(d) == "v1" and b"eins" in setup.server_binary(d).read_bytes()
    assert setup.install_binary(d, client, out.append, update=True) and "aktuell" in out[-1]
    latest[0] = "v2"
    assert setup.install_binary(d, client, out.append, update=True) and "aktualisiert" in out[-1]
    assert setup.runtime_version(d) == "v2" and b"zwei" in setup.server_binary(d).read_bytes()
    assert not (d / "bin.new").exists() and not (d / "bin.old").exists()


def test_typing_loads_a_resting_llm_but_not_in_image_mode(cfg, monkeypatch, tmp_path):
    import time

    from fastapi.testclient import TestClient

    from orbwise.server import create_app
    monkeypatch.setenv("ORBWISE_SKIP_WARMUP", "1")
    monkeypatch.delenv("ORBWISE_FAKE_LLM", raising=False)
    cfg.llm.base_url = "http://127.0.0.1:9"
    cfg.image.dir = str(tmp_path / "img")
    with TestClient(create_app(cfg), base_url="http://localhost:8765") as client:
        llm = client.app.state.hub.agent.llm
        images = client.app.state.images
        calls = []

        async def ensure_loaded():
            if llm.unloaded:  # wie das echte: nur ein entladenes Modell wird geladen
                calls.append("load")
                llm.unloaded = False
        monkeypatch.setattr(llm, "ensure_loaded", ensure_loaded)

        async def give_back(restart=True):
            calls.append("image out")
            llm.gpu_borrowed = None

        def prewarm(ws):
            ws.send_json({"type": "prewarm"})
            for _ in range(100):
                time.sleep(0.02)
                if calls:
                    break
        llm.unloaded = True  # von Hand entladen
        images.view_open = True  # Bild-Ansicht offen: Tippen dort lädt nichts
        with client.websocket_connect("ws://localhost:8765/ws", headers={"Origin": "http://localhost:8765"}) as ws:
            prewarm(ws)
            assert calls == []
            images.view_open = False
            prewarm(ws)  # Tools/Coding: Tippen lädt sofort
            assert calls == ["load"]
            calls.clear()
            llm.gpu_borrowed, llm.unloaded = give_back, False  # Bildmodell hält den Speicher, Ansicht zu
            prewarm(ws)
            assert calls == ["image out"]



# ---------------------------------------------------------------- FLUX.3 Image Klein (sd.cpp docs/flux3.md)
def test_flux3_klein_gets_qwen3vl_and_the_flux2_vae():
    normal = setup.BY_ID["flux3-klein"].choose(CATALOG_TREES, 8)
    assert normal["diffusion"] == ("leejet/flux-3-image-klein-GGUF", "flux-3-image-klein-Q4_K.gguf")
    assert normal["llm"] == (setup.QWEN3VL_4B_REPO, "Qwen3VL-4B-Instruct-Q4_K_M.gguf")  # nicht mmproj
    assert normal["vae"][1].endswith("flux2-vae.safetensors")  # FLUX.2-VAE, nicht das FLUX.1-ae
    uncensored = setup.BY_ID["flux3-klein-uncensored"].choose(CATALOG_TREES, 24)
    assert uncensored["llm"] == (setup.FLUX3_TE_UNCENSORED, "Qwen3VL-4B-Instruct-Uncensored-Q8_0.gguf")
    assert uncensored["diffusion"][1] == "flux-3-image-klein-Q8_0.gguf"
    base = setup.BY_ID["flux3-klein-base"].choose(CATALOG_TREES, 8)
    assert base["diffusion"] == ("leejet/flux-3-image-klein-base-GGUF", "flux-3-image-klein-base-Q4_K.gguf")
    assert (setup.BY_ID["flux3-klein"].steps, setup.BY_ID["flux3-klein"].cfg_scale) == (16, 1.0)
    assert (setup.BY_ID["flux3-klein-base"].steps, setup.BY_ID["flux3-klein-base"].cfg_scale) == (20, 3.5)
    # ohne Comfy-Repo: das ae.safetensors aus dem FLUX.3-Repo – unter eigenem Namen (FLUX.1 hat auch eins)
    trees = {k: v for k, v in CATALOG_TREES.items() if not k.startswith("Comfy-Org/")}
    vae = setup.BY_ID["flux3-klein"].choose(trees, 8)["vae"]
    assert vae == (setup.FLUX3_REPO, "ae.safetensors")
    assert setup.local_name(*vae) == "black-forest-labs__flux-3-image-klein__ae.safetensors"
    assert setup.local_name(setup.FLUX_REPO, "ae.safetensors") == "ae.safetensors"


def test_flux3_install_updates_sd_server_and_can_edit(engine, monkeypatch):
    calls = []
    real = setup.install_binary
    monkeypatch.setattr(setup, "install_binary", lambda d, c, o, update=False: calls.append(update) or real(d, c, o, update))
    assert setup.install({"vram_gb": 12}, engine.cfg, out=lambda s: None, client=catalog_client(), model_id="flux3-klein")
    assert calls == [True]  # FLUX.3 braucht einen aktuellen sd-server
    run(engine.select("flux3-klein"))
    cmd = engine.command()
    assert "flux-3-image-klein" in cmd and "Qwen3VL-4B" in cmd and "--clip_l" not in cmd and "--t5xxl" not in cmd
    engine.cfg.image.steps, engine.cfg.image.cfg_scale = 20, 6.0
    assert engine.defaults() == (16, 1.0, "euler") and engine.can_edit()  # Vorlagen ohne mmproj
    assert setup.install({"vram_gb": 12}, engine.cfg, out=lambda s: None, client=catalog_client(), model_id="sdxl")
    assert calls == [True, False]  # ältere Modelle: sd-server bleibt, wie er ist
