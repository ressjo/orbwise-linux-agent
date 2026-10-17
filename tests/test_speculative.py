"""Spekulatives Decoding: MTP nur, wenn Modell (Schichten oder mtp-Beidatei) und llama.cpp-Build es können;
N-Gramm mit jedem Modell; Einstellung wird gemerkt und beim nächsten Start angewendet."""

import asyncio
import json

import pytest
from test_llm_memory import write_gguf

from orbwise import bonsai
from orbwise.config import LLMConfig, ProfileConfig, ServerConfig
from orbwise.llm import LLMError
from orbwise.llm_router import LLMRouter, spec_stats
from orbwise.models import _pick_mtp, is_sidecar

NEW_HELP = ("--spec-type [none|draft-simple|draft-eagle3|draft-mtp|draft-dflash|ngram-simple|ngram-mod]\n"
            "--spec-default   enable default speculative decoding config\n"
            "-fa, --flash-attn [on|off|auto]")
OLD_HELP = "-fa, --flash-attn [on|off|auto]\n--draft-max N"


def run(coro):
    return asyncio.run(coro)


def router(tmp_path, cmd_mtp, cmd_plain):
    cfg = LLMConfig(profiles={
        "ollama": ProfileConfig(model="qwen3:8b"),
        "mtp": ProfileConfig(backend="openai", base_url="http://127.0.0.1:8091/v1", model="a",
                             server=ServerConfig(command=cmd_mtp)),
        "plain": ProfileConfig(backend="openai", base_url="http://127.0.0.1:8092/v1", model="b",
                               server=ServerConfig(command=cmd_plain)),
    }, active="ollama")
    return LLMRouter(cfg, state_path=tmp_path / "state.json")


def options(r):
    return {p["name"]: {o["mode"]: o for o in p["spec_options"]} for p in r.describe()}


def test_mtp_needs_model_and_build(tmp_path, monkeypatch):
    with_mtp, plain = tmp_path / "GLM-Q4_K_M.gguf", tmp_path / "plain" / "Llama-Q4_K_M.gguf"
    plain.parent.mkdir()
    write_gguf(with_mtp, {"glm4moe.nextn_predict_layers": 1, "glm4moe.block_count": 47})
    write_gguf(plain, {"llama.block_count": 32})
    monkeypatch.setattr(bonsai, "_server_help", lambda *a, **k: NEW_HELP)
    r = router(tmp_path, f"llama-server -m {with_mtp} -c 8192 -np 1", f"llama-server -m {plain} -c 8192")
    opts = options(r)
    assert opts["mtp"]["mtp"]["ok"] and opts["mtp"]["mtp"]["layers"] == 1 and opts["mtp"]["ngram"]["ok"]
    assert not opts["plain"]["mtp"]["ok"] and "keine MTP" in opts["plain"]["mtp"]["why"] and opts["plain"]["ngram"]["ok"]
    assert not opts["ollama"]["mtp"]["ok"] and "Ollama" in opts["ollama"]["ngram"]["why"]

    run(r.set_spec("mtp", "mtp"))
    assert r.profiles["mtp"].server.command.endswith("-np 1 --spec-type draft-mtp")
    run(r.set_spec("plain", "ngram"))
    assert r.profiles["plain"].server.command.endswith("-c 8192 --spec-default")
    with pytest.raises(LLMError):
        run(r.set_spec("plain", "mtp"))
    assert {p["name"]: p["spec"] for p in r.describe()} == {"ollama": "off", "mtp": "mtp", "plain": "ngram"}
    assert json.loads((tmp_path / "state.json").read_text())["speculative"] == {"mtp": "mtp", "plain": "ngram"}

    again = router(tmp_path, f"llama-server -m {with_mtp} -c 8192 -np 1", f"llama-server -m {plain} -c 8192")
    assert {p["name"]: p["spec"] for p in again.describe()} == {"ollama": "off", "mtp": "mtp", "plain": "ngram"}
    run(again.set_spec("mtp", "off"))
    assert again.profiles["mtp"].server.command == f"llama-server -m {with_mtp} -c 8192 -np 1"

    # älterer Build: geht nicht, mit Hinweis aufs Aktualisieren
    monkeypatch.setattr(bonsai, "_server_help", lambda *a, **k: OLD_HELP)
    old = options(again)["mtp"]
    assert not old["mtp"]["ok"] and "aktualisieren" in old["mtp"]["why"] and not old["ngram"]["ok"]


def test_mtp_sidecar_file(tmp_path, monkeypatch):
    model = tmp_path / "Qwen3.6-27B-UD-Q4_K_XL.gguf"
    write_gguf(model, {"qwen35.block_count": 64})
    write_gguf(tmp_path / "mtp-Qwen3.6-27B-Q8_0.gguf", {"qwen35.nextn_predict_layers": 1})
    write_gguf(tmp_path / "mtp-Qwen3.6-27B-UD-Q4_K_XL.gguf", {"qwen35.nextn_predict_layers": 1})
    monkeypatch.setattr(bonsai, "_server_help", lambda *a, **k: NEW_HELP)
    r = router(tmp_path, f"llama-server -m {model} -c 4096", f"llama-server -m {model} -c 4096")
    mtp = options(r)["mtp"]["mtp"]
    assert mtp["ok"] and mtp["layers"] == 0 and mtp["sidecar"].endswith("mtp-Qwen3.6-27B-UD-Q4_K_XL.gguf")
    run(r.set_spec("mtp", "mtp"))
    assert r.profiles["mtp"].server.command.endswith(
        f"--spec-type draft-mtp --spec-draft-model {tmp_path}/mtp-Qwen3.6-27B-UD-Q4_K_XL.gguf")


def test_sidecars_are_not_models_and_get_downloaded():
    assert is_sidecar("mtp-Qwen3.6-27B-Q8_0.gguf") and is_sidecar("sub/mmproj-F16.gguf")
    assert not is_sidecar("Qwen3.6-27B-Q4_K_M.gguf")
    files = [{"path": "mtp-X-Q8_0.gguf", "size": 1}, {"path": "mtp-X-Q4_K_M.gguf", "size": 1}]
    assert _pick_mtp(files, "Q4_K_M")["path"] == "mtp-X-Q4_K_M.gguf"
    assert _pick_mtp(files, "IQ2_XXS")["path"] == "mtp-X-Q8_0.gguf"
    assert _pick_mtp([], "Q4_K_M") is None


def test_hit_rate_from_the_log():
    log = ("slot print_timing: id  0 | draft acceptance rate = 0.40000 (    4 accepted /    10 generated)\n"
           "slot print_timing: id  0 | draft acceptance = 0.64286 (   90 accepted /   140 generated), mean len =  2.50\n")
    assert spec_stats(log) == {"rate": 0.64286, "accepted": 90, "generated": 140}
    assert spec_stats("nichts") is None
