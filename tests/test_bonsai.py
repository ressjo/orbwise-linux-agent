import subprocess
from pathlib import Path

from fastapi.testclient import TestClient

from orbwise import bonsai
from orbwise import models as mdl
from orbwise.config import LLMConfig
from orbwise.llm_router import LLMRouter

AMD_8GB = {"vendor": "amd", "name": "Navi 23 [Radeon RX 6650 XT / 6700S / 6800S]", "vram_gb": 8.0}
NVIDIA_16GB = {"vendor": "nvidia", "name": "NVIDIA GeForce RTX 4080 SUPER", "vram_gb": 16.0}


def test_profile_for_small_amd_card(tmp_path):
    p = bonsai.make_profile(AMD_8GB, tmp_path / "bonsai")
    env = p["server"]["env"]
    assert env == {"HSA_OVERRIDE_GFX_VERSION": "10.3.0", "BONSAI_CTX": "8192", "BONSAI_KV4": "1",
                   "BONSAI_MMPROJ_CPU": "1"}
    assert p["embed_on_cpu"] and p["unload_ollama"] and p["backend"] == "openai"
    assert p["server"]["command"].endswith(f"-np 1 --api-key {p['api_key']}") and len(p["api_key"]) >= 32 and not p["api_key"].startswith("-")


def test_profile_for_big_nvidia_card(tmp_path):
    p = bonsai.make_profile(NVIDIA_16GB, tmp_path / "bonsai", api_key="k")
    assert p["server"]["env"] == {"BONSAI_CTX": "65536"} and not p["embed_on_cpu"]
    assert bonsai.hsa_override("Navi 33 [Radeon RX 7600]") == "11.0.0" and bonsai.hsa_override("RTX 4080") == ""


def fake_run(target):
    calls = []

    def run(cmd, **kw):
        calls.append(cmd)
        if cmd[:2] == ["git", "clone"]:
            (target / ".git").mkdir(parents=True)
            (target / "scripts").mkdir()
            (target / "setup.sh").write_text("#!/bin/sh\n")
        if cmd == ["sh", "./setup.sh"]:
            assert kw["env"]["BONSAI_OPENWEBUI"] == "0" and kw["cwd"] == str(target)
            (target / bonsai.START_SCRIPT).write_text("#!/bin/sh\n")
            (target / "models" / "gguf").mkdir(parents=True, exist_ok=True)
            (target / "models" / "gguf" / "Bonsai-2-27B-PQ2_0.gguf").write_text("x")
        return subprocess.CompletedProcess(cmd, 0)

    return run, calls


def test_install_registers_and_router_uses_profile(tmp_path, monkeypatch):
    monkeypatch.setattr(bonsai.shutil, "which", lambda n: "/usr/bin/git")
    target, state = tmp_path / "bonsai", tmp_path / "state.json"
    run, calls = fake_run(target)
    assert not bonsai.is_set_up(target)
    assert bonsai.install(state, gpu=AMD_8GB, directory=target, run=run, out=lambda *_: None) == "bonsai"
    assert calls[0][:2] == ["git", "clone"] and calls[1] == ["sh", "./setup.sh"]
    assert bonsai.is_set_up(target)
    router = LLMRouter(LLMConfig(model="qwen3:8b"), state_path=state)
    assert router.active == "bonsai"
    p = router.profile
    assert p.backend == "openai" and p.server and p.server.env["BONSAI_KV4"] == "1" and p.api_key in p.server.command
    # zweiter Aufruf: vorhandenes Repo wird aktualisiert statt neu geklont
    run2, calls2 = fake_run(target)
    bonsai.setup(target, run=run2, out=lambda *_: None)
    assert calls2[0] == ["git", "-C", str(target), "pull", "--ff-only"]
    assert mdl.unregister_model(state, "bonsai") == "bonsai"


def test_setup_failure_and_foreign_folder(tmp_path, monkeypatch):
    monkeypatch.setattr(bonsai.shutil, "which", lambda n: "/usr/bin/git")
    msgs = []
    foreign = tmp_path / "bonsai"
    foreign.mkdir()
    (foreign / "notes.txt").write_text("x")
    assert not bonsai.setup(foreign, run=lambda *a, **k: None, out=msgs.append)
    assert "ORBWISE_BONSAI_DIR" in msgs[-1]
    target = tmp_path / "b2"
    failing = lambda cmd, **kw: subprocess.CompletedProcess(cmd, 1 if cmd[0] == "sh" else 0)  # noqa: E731
    assert not bonsai.setup(target, run=failing, out=msgs.append) and "fehlgeschlagen" in msgs[-1]


def test_preset_and_web_pull_refuses_bonsai(cfg, monkeypatch):
    assert mdl.BY_TAG["bonsai"].kind == "bonsai"
    monkeypatch.setenv("ORBWISE_SKIP_WARMUP", "1")
    monkeypatch.delenv("ORBWISE_FAKE_LLM", raising=False)
    cfg.llm.base_url = "http://127.0.0.1:9"
    from orbwise.server import create_app
    with TestClient(create_app(cfg), base_url="http://localhost:8765") as client:
        r = client.post("/api/models/pull", json={"tag": "bonsai"})
        assert r.status_code == 400 and "orbwise model add bonsai" in r.json()["detail"]


LDD_MISSING = """\tlinux-vdso.so.1 (0x00007ffc)
\tlibcudart.so.12 => not found
\tlibcublas.so.12 => not found
\tlibc.so.6 => /usr/lib/libc.so.6 (0x00007f)
"""


def cuda_checkout(tmp_path):
    d = tmp_path / "bonsai"
    (d / "bin" / "cuda").mkdir(parents=True)
    (d / bonsai.CUDA_SERVER).write_text("")
    return d


def fake_cuda_run(d, libs_after_install=True, calls=None):
    calls = [] if calls is None else calls

    def run(cmd, **kw):
        calls.append(cmd)
        if cmd[0] == "nvidia-smi":
            return subprocess.CompletedProcess(cmd, 0, stdout="| NVIDIA-SMI 570.1  Driver Version: 570.1  CUDA Version: 12.8 |")
        if cmd[0] == "ldd":
            path = kw["env"].get("LD_LIBRARY_PATH", "")
            ok = "cuda_runtime/lib" in path and "cublas/lib" in path
            return subprocess.CompletedProcess(cmd, 0, stdout="\tlibc.so.6 => /usr/lib/libc.so.6\n" if ok else LDD_MISSING)
        if "--target" in cmd and libs_after_install:
            target = Path(cmd[cmd.index("--target") + 1])
            for sub, lib in (("cuda_runtime", "libcudart.so.12"), ("cublas", "libcublas.so.12")):
                (target / "nvidia" / sub / "lib").mkdir(parents=True, exist_ok=True)
                (target / "nvidia" / sub / "lib" / lib).write_text("")
        return subprocess.CompletedProcess(cmd, 0)

    return run, calls


def test_cuda_runtime_is_downloaded_when_missing(tmp_path):
    d = cuda_checkout(tmp_path)
    run, calls = fake_cuda_run(d)
    msgs = []
    path = bonsai.ensure_cuda_libs(d, run=run, out=msgs.append)
    pip = next(c for c in calls if "--target" in c)
    assert pip[-2:] == ["nvidia-cuda-runtime-cu12<12.9", "nvidia-cublas-cu12<12.9"] and str(d / "cuda-libs") in pip
    assert "nvidia/cuda_runtime/lib" in path and "nvidia/cublas/lib" in path and "✔" in msgs[-1]
    # zweiter Lauf: Bibliotheken schon da → kein erneuter Download
    run2, calls2 = fake_cuda_run(d)
    assert bonsai.ensure_cuda_libs(d, run=run2, out=msgs.append) == path
    assert not any("--target" in c for c in calls2)
    assert "LD_LIBRARY_PATH" in bonsai.make_profile(NVIDIA_16GB, d, lib_path=path)["server"]["env"]


def test_cuda_runtime_not_needed_or_failing(tmp_path):
    assert bonsai.ensure_cuda_libs(tmp_path, run=None, out=print) == ""  # kein CUDA-Build (z. B. AMD)
    d = cuda_checkout(tmp_path)
    ok = lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, stdout="\tlibc.so.6 => /usr/lib/libc.so.6\n")  # noqa: E731
    assert bonsai.ensure_cuda_libs(d, run=ok, out=print) == ""  # System-CUDA vorhanden
    run, _ = fake_cuda_run(d, libs_after_install=False)
    msgs = []
    assert bonsai.ensure_cuda_libs(d, run=run, out=msgs.append) is None and "libcudart.so.12" in msgs[-1]
    assert bonsai.cuda_packages(["libcudart.so.13"]) == ["nvidia-cuda-runtime==13.*", "nvidia-cublas==13.*"]
    assert bonsai.cuda_packages(["libfoo.so.1"]) == []
    assert bonsai.cuda_packages(["libcudart.so.13"], (13, 0)) == ["nvidia-cuda-runtime>=13,<13.1", "nvidia-cublas>=13,<13.1"]


def test_reinstall_keeps_api_key_and_adds_lib_path(tmp_path, monkeypatch):
    monkeypatch.setattr(bonsai.shutil, "which", lambda n: "/usr/bin/" + n)
    state = tmp_path / "state.json"
    d = tmp_path / "bonsai"
    run, _ = fake_run(d)
    bonsai.install(state, gpu=NVIDIA_16GB, directory=d, run=run, out=lambda *_: None)
    key = mdl.added_profiles(state)["bonsai"]["api_key"]
    (d / "bin" / "cuda").mkdir(parents=True)
    (d / bonsai.CUDA_SERVER).write_text("")
    cuda_run, _ = fake_cuda_run(d)

    def both(cmd, **kw):
        return cuda_run(cmd, **kw) if cmd[0] in ("ldd", "nvidia-smi") or "--target" in cmd else run(cmd, **kw)

    bonsai.install(state, gpu=NVIDIA_16GB, directory=d, run=both, out=lambda *_: None)
    p = mdl.added_profiles(state)["bonsai"]
    assert p["api_key"] == key and key in p["server"]["command"] and "cublas" in p["server"]["env"]["LD_LIBRARY_PATH"]


def test_router_error_names_missing_library(tmp_path, monkeypatch):
    from orbwise import llm_router
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    log = tmp_path / "orbwise-llm.log"
    log.write_text("\n===== Starte: old\nerror while loading shared libraries: libold.so.1: x\n"
                   "\n===== Starte: ~/bonsai/scripts/start_llama_server.sh\n"
                   "llama-server: error while loading shared libraries: libcudart.so.12: cannot open shared object file\n")
    hint = llm_router.library_hint("~/bonsai/scripts/start_llama_server.sh -np 1")
    assert "libcudart.so.12" in hint and "orbwise model add bonsai" in hint and "libold" not in hint
    log.write_text("\n===== Starte: x\nall good\n")
    assert llm_router.library_hint("x") == ""


def test_compact_variant_downloads_only_its_file_and_gets_16k_on_8gb(tmp_path, monkeypatch):
    """Ternary-Bonsai-2-27B-PTQ1_0 (5,9 statt 7,2 GB): auf 8-GB-Karten bleibt Platz für 16k Kontext."""
    monkeypatch.setattr(bonsai.shutil, "which", lambda n: "/usr/bin/" + n)
    target, state = tmp_path / "bonsai", tmp_path / "state.json"
    base_run, calls = fake_run(target)

    def run(cmd, **kw):
        if cmd == ["sh", "./setup.sh"]:
            assert kw["env"]["BONSAI_SKIP_GGUF"] == "1"  # nicht zusätzlich die 7,2-GB-Datei laden
            (target / bonsai.START_SCRIPT).write_text("#!/bin/sh\n")
            return subprocess.CompletedProcess(cmd, 0)
        if len(cmd) > 2 and cmd[1] == "-c" and bonsai.COMPACT_FILE in cmd:
            calls.append(cmd)
            assert bonsai.COMPACT_REPO in cmd
            (target / bonsai.COMPACT_DIR / bonsai.COMPACT_FILE).write_text("x")
            return subprocess.CompletedProcess(cmd, 0)
        return base_run(cmd, **kw)

    assert bonsai.install(state, gpu=AMD_8GB, directory=target, run=run, out=lambda *_: None,
                          variant=bonsai.COMPACT_NAME) == "bonsai-kompakt"
    assert bonsai.is_set_up(target, bonsai.COMPACT_NAME) and not bonsai.is_set_up(target)
    p = mdl.added_profiles(state)["bonsai-kompakt"]
    env = p["server"]["env"]
    assert env["BONSAI_GGUF"] == f"{bonsai.COMPACT_DIR}/{bonsai.COMPACT_FILE}" and env["BONSAI_CTX"] == "16384"
    assert env["BONSAI_KV4"] == "1" and p["label"] == "Bonsai 2 27B kompakt"
    # Varianten getrennt: Löschen der kompakten lässt die normale in Ruhe
    (target / "models" / "bonsai2-gguf" / "27B").mkdir(parents=True)
    (target / "models" / "bonsai2-gguf" / "27B" / "Ternary-Bonsai-2-27B-PQ2_0.gguf").write_text("y")
    assert [f.name for f in bonsai.model_files(target)] == ["Ternary-Bonsai-2-27B-PQ2_0.gguf"]
    assert bonsai.remove_model_files(target, bonsai.COMPACT_NAME) == 1
    assert bonsai.model_files(target) and not bonsai.model_files(target, bonsai.COMPACT_NAME)
    assert mdl.BY_TAG["bonsai-kompakt"].kind == "bonsai" and mdl.BY_TAG["bonsai-kompakt"].download_gb == 5.9
    assert bonsai.make_profile(AMD_8GB, target)["server"]["env"]["BONSAI_CTX"] == "8192"  # normale: weiter 8k


def test_cache_flags_for_the_hybrid_model(tmp_path):
    """Bonsai 2 ist ein Hybrid-Modell: früheren Prompt nur über Checkpoints weiterverwenden – Orbwise hängt die
    passenden llama-server-Optionen an den Bonsai-Starter, aber nur, was der vorhandene Server kennt."""
    d = tmp_path / "bonsai"
    (d / "bin" / "vulkan").mkdir(parents=True)
    (d / "bin" / "vulkan" / "llama-server").write_text("")
    script = f"{d}/scripts/start_llama_server.sh -np 1 --api-key k"
    full_help = "-c, --ctx-size N\n--ctx-checkpoints N\n--cache-ram N\n--cache-idle-slots\n"
    calls = []

    def run(cmd, **kw):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout=full_help, stderr="")

    flags = bonsai.cache_flags(script, run=run, mem_mib=31 * 1024)
    assert flags == "--ctx-checkpoints 32 --cache-ram 4096 --cache-idle-slots"
    assert calls == [[str(d / "bin" / "vulkan" / "llama-server"), "--help"]]
    bonsai.cache_flags(script, run=run, mem_mib=8 * 1024)
    assert len(calls) == 1  # Hilfetext nur einmal je Ordner
    assert "--cache-ram 1228" in bonsai.cache_flags(script, run=run, mem_mib=8 * 1024)  # 15 % von 8 GB
    own = bonsai.cache_flags(script + " --ctx-checkpoints 8", run=run, mem_mib=31 * 1024)
    assert "--ctx-checkpoints" not in own and "--cache-ram 4096" in own  # vom Nutzer gesetzt – bleibt
    assert bonsai.cache_flags("python3 serve.py", run=run) == ""  # kein llama-server
    # eigenes Modell (z. B. Qwen von Hugging Face) mit direkt gestartetem llama-server: dieselben Optionen
    binary = d / "bin" / "vulkan" / "llama-server"
    direct = bonsai.cache_flags(f"{binary} -m ~/models/q.gguf -np 1", run=run, mem_mib=31 * 1024)
    assert direct == "--ctx-checkpoints 32 --cache-ram 4096 --cache-idle-slots"

    old = tmp_path / "alt"
    (old / "bin" / "cpu").mkdir(parents=True)
    (old / "bin" / "cpu" / "llama-server").write_text("")
    older = bonsai.cache_flags(f"{old}/scripts/start_llama_server.sh", mem_mib=16384,
                               run=lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, stdout="-c, --ctx-size N\n"))
    assert older == ""  # ältere Version kennt die Optionen nicht – nichts anhängen

    def broken(cmd, **kw):
        raise OSError("libvulkan fehlt")
    gone = tmp_path / "kaputt"
    (gone / "bin" / "x").mkdir(parents=True)
    (gone / "bin" / "x" / "llama-server").write_text("")
    assert bonsai.cache_flags(f"{gone}/scripts/start_llama_server.sh", run=broken) == ""


def test_managed_server_starts_with_cache_flags(tmp_path, monkeypatch):
    import asyncio
    import socket
    import sys

    from orbwise.config import ServerConfig
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

    monkeypatch.setattr(bonsai, "cache_flags", flags)
    fake = Path(__file__).parent / "fake_llama_server.py"
    server = ManagedServer(ServerConfig(command=f"{sys.executable} {fake} {port}", startup_timeout=20),
                           f"http://127.0.0.1:{port}/v1")
    try:
        assert asyncio.run(server.ensure_running())
        log = (tmp_path / "orbwise-llm.log").read_text()
        assert f"{fake} {port} --ctx-checkpoints 32" in log and seen
    finally:
        asyncio.run(server.stop())
