"""Modell-Vorauswahl (GGUF-Repos auf Hugging Face), interaktive Auswahl, abgelöste Ollama-Einträge, Löschen."""

import json

from fastapi.testclient import TestClient

from orbwise import models as mdl
from orbwise.config import LLMConfig
from orbwise.llm_router import LLMRouter

Q8 = "hf.co/unsloth/Qwen3-8B-GGUF:Q4_K_M"
Q14 = "hf.co/unsloth/Qwen3-14B-GGUF:Q4_K_M"


def test_presets_are_hugging_face_gguf():
    for p in mdl.PRESETS:
        assert mdl.normalize_tag(p.tag) == p.tag and p.tag.endswith(":Q4_K_M"), p.tag
    assert len({p.tag for p in mdl.PRESETS}) == len(mdl.PRESETS)


def test_recommend_and_fit(tmp_path):
    assert mdl.recommend(16) == Q14 and mdl.recommend(8) == Q8
    assert "30B-A3B" in mdl.recommend(24) and "Qwen3-4B" in mdl.recommend(0)
    p14 = mdl.BY_TAG[Q14]
    assert mdl.fit(p14, 16) == "ok" and mdl.fit(p14, 9) == "tight" and mdl.fit(p14, 6) == "big"
    assert mdl.slug("Qwen3-8B Q4_K_M") == "qwen3-8b-q4-k-m"
    folder = tmp_path / "unsloth_Qwen3-8B-GGUF"
    folder.mkdir()
    (folder / "Qwen3-8B-Q4_K_M.gguf").write_bytes(b"x")
    (folder / "mmproj-Qwen3-8B-Q4_K_M.gguf").write_bytes(b"x")
    items = mdl.preset_list(16, tmp_path)
    assert [i["tag"] for i in items if i["installed"]] == [Q8]
    assert [i["tag"] for i in items if i["recommended"]] == [Q14]
    assert not any(i["installed"] for i in mdl.preset_list(16, None))


def test_only_hugging_face_names_are_accepted():
    assert mdl.normalize_tag("unsloth/Qwen3-8B-GGUF") == "hf.co/unsloth/Qwen3-8B-GGUF"
    assert mdl.normalize_tag("unsloth/Qwen3-8B-GGUF:Q5_K_M") == "hf.co/unsloth/Qwen3-8B-GGUF:Q5_K_M"
    assert mdl.normalize_tag("hf.co/unsloth/Qwen3-8B-GGUF:Q4_K_M") == Q8
    for old in ("qwen3:8b", "llama3.1", "; rm -rf /", "", "a/b/c"):  # Ollama-Namen gibt es nicht mehr
        assert mdl.normalize_tag(old) is None, old


def test_interactive_choice():
    answers = iter(["", "3", "unsinn ! name", "qwen3:7b", "bartowski/some-model-GGUF"])

    def ask(prompt):
        return next(answers)

    out = []
    assert mdl.choose_interactive(8, ask=ask, out=out.append) == Q8
    assert mdl.choose_interactive(8, ask=ask, out=out.append) == mdl.PRESETS[2].tag
    assert mdl.choose_interactive(8, ask=ask, out=out.append) == "hf.co/bartowski/some-model-GGUF"
    assert not any("Ollama" in line for line in out)


def test_old_ollama_entries_are_retired_not_loaded(tmp_path):
    """state.json von früher: per Ollama geladene Modelle – sie werden gemeldet statt angeboten, und lassen sich
    aus der Liste nehmen."""
    state = tmp_path / "state.json"
    state.write_text(json.dumps({"added_models": ["qwen3:14b", "llama3.1:8b"], "active_profile": "qwen3-14b"}))
    router = LLMRouter(LLMConfig(), state_path=state)
    assert list(router.profiles) == ["standard"] and router.active == "standard"
    assert router.retired == ["qwen3-14b", "llama3.1-8b"]
    assert mdl.unregister_model(state, "qwen3-14b") == "qwen3:14b"
    assert "active_profile" not in json.loads(state.read_text())
    assert LLMRouter(LLMConfig(), state_path=state).retired == ["llama3.1-8b"]
    assert mdl.unregister_model(state, "gibtsnicht") is None


def test_web_api_presets_pull_and_delete(cfg, monkeypatch):
    monkeypatch.setenv("ORBWISE_SKIP_WARMUP", "1")
    monkeypatch.delenv("ORBWISE_FAKE_LLM", raising=False)
    state = cfg.memory.dir.parent / "state.json"
    state.parent.mkdir(parents=True, exist_ok=True)
    for name, port in (("eins", 8091), ("zwei", 8092)):
        mdl.register_profile(state, name, {"backend": "openai", "base_url": f"http://127.0.0.1:{port}/v1", "model": name})
    state.write_text(json.dumps({**json.loads(state.read_text()), "added_models": ["qwen3:14b"]}))
    from orbwise.server import create_app
    with TestClient(create_app(cfg), base_url="http://localhost:8765") as client:
        presets = client.get("/api/models/presets").json()
        assert presets["presets"] and all(p["tag"].startswith("hf.co/") and not p["installed"] for p in presets["presets"])
        for bad in ("; rm -rf /", "qwen3:14b", ""):
            assert client.post("/api/models/pull", json={"tag": bad}).status_code == 400
        data = client.get("/api/models").json()
        models = {p["name"]: p for p in data["profiles"]}
        assert set(models) == {"eins", "zwei"} and all(p["deletable"] for p in models.values())
        assert all(p["engine"] == "OpenAI-API" for p in models.values())
        assert data["retired"] == ["qwen3-14b"] and data["embed"]["available"] is False and data["embed"]["problem"]
        assert client.delete("/api/models/gibtsnicht").status_code == 404
        assert client.post("/api/models/eins/activate").status_code == 200
        # aktives Modell: wird entladen, ein anderes gewählt (ohne Laden), dann gelöscht
        r = client.delete("/api/models/eins").json()
        assert r["ok"] and r["active"] == "zwei"
        listed = client.get("/api/models").json()
        assert listed["unloaded"] and [p["name"] for p in listed["profiles"]] == ["zwei"]
        assert client.delete("/api/models/zwei").status_code == 409  # das einzige Modell
    assert "eins" not in mdl.added_profiles(state)
