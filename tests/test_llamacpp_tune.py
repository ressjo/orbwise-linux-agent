"""llama.cpp-Tuning: mit llama-bench Build (ROCm ↔ Vulkan) und Batch-Größe messen, die schnellste verlustfreie
Einstellung übernehmen bzw. den Wechsel des Builds anbieten."""

import json
import os
import time
from pathlib import Path

import pytest
from conftest import run
from fastapi.testclient import TestClient
from test_llamacpp import AMD, WORKS, Fake, _tgz, fake, release, setup  # noqa: F401 – Fixtures
from test_llm_memory import write_gguf

from orbwise import llamacpp, llamacpp_tune
from orbwise.config import LLMConfig, ProfileConfig
from orbwise.llm_router import LLMRouter

META = {"qwen3.block_count": 4, "qwen3.attention.head_count": 8, "qwen3.attention.head_count_kv": 2,
        "qwen3.attention.key_length": 64, "qwen3.attention.value_length": 64, "qwen3.context_length": 65536}


def bench_json(pp0, pp_deep, tg, noise=True):
    rows = [{"n_prompt": 2048, "n_gen": 0, "n_depth": 0, "avg_ts": pp0},
            {"n_prompt": 2048, "n_gen": 0, "n_depth": 16384, "avg_ts": pp_deep},
            {"n_prompt": 0, "n_gen": 32, "n_depth": 0, "avg_ts": tg},
            {"n_prompt": 0, "n_gen": 32, "n_depth": 16384, "avg_ts": tg - 3}]
    return ("ggml_cuda_init: found 1 ROCm devices\n" if noise else "") + json.dumps(rows)


def test_parse_and_pick_best():
    got = llamacpp_tune.parse_bench(bench_json(800, 600, 30))
    assert got == {"pp_short": 800, "pp_deep": 600, "pp": 700, "tg": 30}
    assert llamacpp_tune.parse_bench("kaputt") is None and llamacpp_tune.parse_bench("[]") is None
    runs = [{"flavor": "rocm", "ubatch": 512, "pp": 600, "tg": 30},
            {"flavor": "rocm", "ubatch": 1024, "pp": 700, "tg": 30},
            {"flavor": "rocm", "ubatch": 2048, "pp": 710, "tg": 30},  # +1,4 %: gleich schnell → kleinere Batch
            {"flavor": "vulkan", "ubatch": 2048, "error": "zu wenig Grafikspeicher"}]
    assert llamacpp_tune.best(runs)["ubatch"] == 1024
    runs.append({"flavor": "vulkan", "ubatch": 1024, "pp": 820, "tg": 31})
    assert llamacpp_tune.best(runs)["flavor"] == "vulkan"
    assert llamacpp_tune.best([{"flavor": "rocm", "ubatch": 512, "error": "x"}]) is None


def test_bench_args_follow_the_profile():
    cmd = "~/x/current/llama-server -m ~/m.gguf -ngl 99 -c 65536 -ctk q8_0 -ctv q8_0 -ub 1024 -b 2048 -fa on"
    new = llamacpp_tune.bench_args(Path("/b/llama-bench"), cmd, Path("/m.gguf"), 2048, 16384,
                                   "-fa, --flash-attn <on|off|auto>\n-d, --n-depth <n>")
    assert new[:3] == ["/b/llama-bench", "-m", "/m.gguf"] and "-ctk" in new and new[new.index("-ctv") + 1] == "q8_0"
    assert new[new.index("-fa") + 1] == "on" and new[new.index("-ub") + 1] == "2048" and new[new.index("-d") + 1] == "0,16384"
    old = llamacpp_tune.bench_args(Path("/b/llama-bench"), "llama-server -m m.gguf", Path("/m.gguf"), 512, 16384,
                                   "-fa, --flash-attn <0|1>\n-p, --n-prompt")
    assert old[old.index("-fa") + 1] == "1" and "-d" not in old and "-ctk" not in old  # f16, kein -d im alten Build
    assert "IQ4_XS" in llamacpp_tune.quant_hint(Path("Qwen-27B-UD-IQ4_XS.gguf"))
    assert llamacpp_tune.quant_hint(Path("Qwen-27B-Q4_K_M.gguf")) == ""


def own_build(cfg, flavor="rocm", tag="b1"):
    base = llamacpp.root(cfg)
    folder = base / f"{tag}-{flavor}"
    folder.mkdir(parents=True, exist_ok=True)
    for exe in ("llama-server", "llama-bench"):
        (folder / exe).write_text("#!/bin/sh\n")
    if not (base / "current").exists():
        os.symlink(folder.name, base / "current")
        (base / "manifest.json").write_text(json.dumps({"tag": tag, "flavor": flavor, "dir": folder.name}))
    return folder


def tunable_router(cfg, tmp_path):
    own_build(cfg)
    gguf = tmp_path / "Qwen-27B-UD-IQ4_XS.gguf"
    write_gguf(gguf, META)
    raw = llamacpp.make_profile(cfg, "qwen-27b", gguf, None, AMD, 8199)
    llm_cfg = LLMConfig(profiles={"qwen-27b": ProfileConfig.model_validate(raw), "klein": ProfileConfig(model="qwen3:8b")},
                        active="qwen-27b")
    return LLMRouter(llm_cfg, state_path=tmp_path / "state.json")


def test_tune_measures_both_builds_and_applies_the_batch(cfg, tmp_path):
    router = tunable_router(cfg, tmp_path)
    assert llamacpp_tune.uses_own_build(cfg, router.profiles["qwen-27b"].server.command)
    assert not llamacpp_tune.uses_own_build(cfg, "/opt/bonsai/start_llama_server.sh")
    calls, said = [], []

    async def unload():
        calls.append("unload")
        router.unloaded = True

    async def ensure_loaded():
        calls.append("load")
        router.unloaded = False
    router.unload, router.ensure_loaded = unload, ensure_loaded

    speeds = {("rocm", "512"): (600, 500, 30), ("rocm", "1024"): (700, 600, 30), ("rocm", "2048"): (710, 605, 30),
              ("vulkan", "512"): (650, 560, 31), ("vulkan", "1024"): (850, 700, 31)}

    async def bench(argv, env, timeout):
        flavor = "vulkan" if "vulkan" in argv[0] else "rocm"
        assert env["LD_LIBRARY_PATH"].startswith(str(Path(argv[0]).parent)) and "-ctk" not in argv
        ub = argv[argv.index("-ub") + 1]
        if (flavor, ub) not in speeds:
            return 1, "", "ggml_vulkan: Device memory allocation of size 123 failed: out of memory"
        return 0, bench_json(*speeds[(flavor, ub)]), ""

    async def fetch():
        return own_build(cfg, "vulkan")

    async def progress(text):
        said.append(text)
    result = run(llamacpp_tune.tune(cfg, router, "qwen-27b", AMD, progress, run=bench, fetch=fetch))
    assert calls == ["unload", "load"]
    assert result["best"]["flavor"] == "vulkan" and result["best"]["ubatch"] == 1024
    assert result["applied_ubatch"] == 1024 and "-ub 1024" in router.profiles["qwen-27b"].server.command
    assert result["before"]["ubatch"] == 512 and result["other"] == {"flavor": "vulkan", "gain_pct": 19}
    failed = next(r for r in result["runs"] if r["flavor"] == "vulkan" and r["ubatch"] == 2048)
    assert failed["error"] == "zu wenig Grafikspeicher"
    assert "IQ4_XS" in result["quant_hint"] and any("Vulkan · Batch 1024" in s for s in said)
    assert llamacpp_tune.saved(tmp_path / "state.json", "qwen-27b")["best"]["ubatch"] == 1024
    assert json.loads((tmp_path / "state.json").read_text())["ubatch"]["qwen-27b"] == 1024
    with pytest.raises(ValueError, match="llama.cpp"):
        run(llamacpp_tune.tune(cfg, router, "klein", AMD))


def working_rocm(tag):
    Fake.assets[f"llama-{tag}-bin-ubuntu-rocm-10.0-x64.tar.gz"] = _tgz({"build/bin/llama-server": WORKS,
                                                                         "build/bin/llama-bench": "#!/bin/sh\n"})


def test_fetch_and_use_the_other_build(setup, fake):  # noqa: F811
    release()
    working_rocm("b9999")
    lines = []
    assert llamacpp.install(setup, AMD, lines.append)["flavor"] == "rocm"
    assert llamacpp.other_flavor(AMD, "rocm") == "vulkan" and llamacpp.other_flavor({"vendor": "none"}, "cpu") is None
    folder = llamacpp.fetch_flavor(setup, AMD, "vulkan", lines.append)
    base = llamacpp.root(setup)
    assert folder == base / "b9999-vulkan" and (folder / "llama-server").exists()
    assert (base / "current").resolve().name == "b9999-rocm"  # Messen stellt nicht um
    assert llamacpp.fetch_flavor(setup, AMD, "vulkan", lines.append) == folder  # schon da: kein zweiter Download
    m = llamacpp.use_flavor(setup, "vulkan")
    assert m["flavor"] == "vulkan" and m["preferred_flavor"] == "vulkan" and m["previous"] == "b9999-rocm"
    assert (base / "current").resolve().name == "b9999-vulkan" and m["asset"].endswith("vulkan-x64.tar.gz")
    release("b10000")
    working_rocm("b10000")
    m2 = llamacpp.install(setup, AMD, lines.append, update=True)
    assert m2["flavor"] == "vulkan" and m2["preferred_flavor"] == "vulkan"  # Update bleibt bei der Wahl
    with pytest.raises(ValueError):
        llamacpp.use_flavor(setup, "cuda")


def test_api_tune_and_switch_build(cfg, tmp_path, monkeypatch):
    monkeypatch.setenv("ORBWISE_SKIP_WARMUP", "1")
    monkeypatch.delenv("ORBWISE_FAKE_LLM", raising=False)
    own_build(cfg)
    gguf = tmp_path / "Qwen-27B-Q4_K_M.gguf"
    write_gguf(gguf, META)
    state = cfg.memory.dir.parent / "state.json"
    name = llamacpp.add_model(cfg, state, gguf, None, AMD, {}, downloaded=False)
    cfg.llm.base_url = "http://127.0.0.1:9"
    cfg.llm.profiles = {"qwen": ProfileConfig(model="qwen3:8b")}
    cfg.llm.active = "qwen"

    async def fake_tune(cfg_, router, profile, gpu, progress=None):
        await progress("Messe ROCm · Batch 512 (1/3) …")
        result = {"at": time.time(), "flavor": "rocm", "ubatch": 1024, "applied_ubatch": 1024, "runs": [],
                  "best": None, "other": {"flavor": "vulkan", "gain_pct": 12}, "notes": [], "quant_hint": ""}
        llamacpp._write(router.state_path, {**llamacpp._read(router.state_path), "tuning": {profile: result}})
        return result
    monkeypatch.setattr(llamacpp_tune, "tune", fake_tune)
    from orbwise.server import create_app
    with TestClient(create_app(cfg), base_url="http://localhost:8765") as client:
        profiles = {p["name"]: p for p in client.get("/api/models").json()["profiles"]}
        assert profiles[name]["tunable"] and profiles[name]["tuning"] is None and not profiles["qwen"].get("tunable")
        assert client.post("/api/models/qwen/tune").status_code == 400
        assert client.post(f"/api/models/{name}/tune").json()["tag"] == "Tuning"
        for _ in range(100):
            got = client.get(f"/api/models/{name}/tune").json()["result"]
            if got:
                break
            time.sleep(0.05)
        assert got["other"]["gain_pct"] == 12
        assert {p["name"]: p for p in client.get("/api/models").json()["profiles"]}[name]["tuning"]["ubatch"] == 1024
        assert client.post("/api/llamacpp/flavor", json={"flavor": "metal"}).status_code == 400
        assert client.post("/api/llamacpp/flavor", json={"flavor": "vulkan"}).status_code == 400  # nicht geladen
        own_build(cfg, "vulkan")
        r = client.post("/api/llamacpp/flavor", json={"flavor": "vulkan"}).json()
        assert r["flavor"] == "vulkan" and not r["restart"]  # aktiv ist das Ollama-Modell
        assert client.get("/api/llamacpp").json()["flavor"] == "vulkan"


def test_run_process_and_timeout(tmp_path):
    ok = tmp_path / "bench.sh"
    ok.write_text("#!/bin/sh\necho '[{\"n_prompt\": 2048, \"n_gen\": 0, \"avg_ts\": 512.5}]'\n")
    ok.chmod(0o755)
    code, out, _ = run(llamacpp_tune.run_process([str(ok)], dict(os.environ), 10))
    assert code == 0 and llamacpp_tune.parse_bench(out)["pp"] == 512.5
    slow = tmp_path / "slow.sh"
    slow.write_text("#!/bin/sh\nsleep 5\n")
    slow.chmod(0o755)
    code, _, err = run(llamacpp_tune.run_process([str(slow)], dict(os.environ), 0.3))
    assert code == -1 and err
