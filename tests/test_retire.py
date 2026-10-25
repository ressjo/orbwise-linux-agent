"""Entfernte Engines aufräumen: Strata-Profile verschwinden aus state.json, Modell und Engine werden gelöscht."""

import json
import os
import time

from fastapi.testclient import TestClient

from orbwise import retire
from orbwise.config import ProfileConfig


def strata_state(tmp_path, active="qwen3-8-flash-next-coder-iq1-m"):
    models = tmp_path / "models" / "strata" / "iq1_m"
    models.mkdir(parents=True)
    (models / "experts.bin").write_bytes(b"x" * 1000)
    name = "qwen3-8-flash-next-coder-iq1-m"
    state = tmp_path / "state.json"
    state.write_text(json.dumps({
        "active_profile": active, "unloaded": True,
        "added_profiles": {name: {"backend": "openai", "model": "iq1_m", "server": {
            "command": "/x/strata/terminal/current/bin/strata launch --model iq1_m --context 65536 --no-tui",
            "detached": True, "stop_command": "/x/strata launch --stop"}},
            "eigenes": {"backend": "openai", "model": "q", "server": {"command": "llama-server -m q.gguf"}}},
        "local_models": {name: {"engine": "strata", "dir": str(models), "files": []},
                         "eigenes": {"files": ["q.gguf"], "downloaded": True}},
        "strata_reserve": {name: 3072}, "context": {name: 65536, "eigenes": 32768},
        "tuning": {name: {}}, "prefill_tps": {name: 900.0}}))
    return state, name, models


def test_clean_state_removes_strata_profiles(tmp_path):
    state, name, models = strata_state(tmp_path)
    found = retire.clean_state(state)
    assert found == {"profiles": [name], "strata": [name], "strata_dirs": [str(models)]}
    data = json.loads(state.read_text())
    assert list(data["added_profiles"]) == ["eigenes"] and list(data["local_models"]) == ["eigenes"]
    assert data["context"] == {"eigenes": 32768} and data["tuning"] == {} and "strata_reserve" not in data
    assert "active_profile" not in data and "unloaded" not in data  # anderes Profil wird normal geladen
    assert retire.clean_state(state) is None  # zweiter Lauf: nichts zu tun
    assert retire.clean_state(tmp_path / "fehlt.json") is None


def test_inactive_strata_profile_keeps_the_active_one(tmp_path):
    state, _, _ = strata_state(tmp_path, active="eigenes")
    retire.clean_state(state)
    data = json.loads(state.read_text())
    assert data["active_profile"] == "eigenes" and data["unloaded"] is True


def test_remove_files_stops_strata_and_keeps_foreign_links(tmp_path, monkeypatch):
    data_dir = tmp_path / "share" / "strata"
    exe = data_dir / "terminal" / "current" / "bin" / "strata"
    exe.parent.mkdir(parents=True)
    exe.write_text("#!/bin/sh\n")
    (data_dir / "engines").mkdir()
    (data_dir / "engines" / "hip.so").write_bytes(b"e" * 500)
    monkeypatch.setenv("STRATA_DATA_DIR", str(data_dir))
    _, _, models = strata_state(tmp_path)
    home = tmp_path / "home"
    (home / ".local" / "bin").mkdir(parents=True)
    os.symlink(exe, home / ".local" / "bin" / "strata")
    calls = []

    def run(argv, **kw):
        calls.append(argv)
        return None
    freed = retire.remove_strata_files([str(models)], run=run, home=home)
    assert calls == [[str(exe), "launch", "--stop"]]
    assert freed >= 1500 and not models.exists() and not models.parent.exists() and not data_dir.exists()
    assert not (home / ".local" / "bin" / "strata").is_symlink()
    other = home / ".local" / "bin" / "strata"
    os.symlink("/opt/strata/bin/strata", other)  # selbst installiert: bleibt
    retire.remove_strata_files([], run=run, home=home)
    assert other.is_symlink()


def test_server_start_removes_strata(cfg, tmp_path, monkeypatch):
    monkeypatch.setenv("ORBWISE_SKIP_WARMUP", "1")
    monkeypatch.delenv("ORBWISE_FAKE_LLM", raising=False)
    monkeypatch.setenv("STRATA_DATA_DIR", str(tmp_path / "keine-engine"))
    state, name, models = strata_state(cfg.memory.dir.parent)
    cfg.llm.base_url = "http://127.0.0.1:9"
    cfg.llm.profiles = {"qwen": ProfileConfig(model="qwen3:8b")}
    cfg.llm.active = "qwen"
    from orbwise.server import create_app
    with TestClient(create_app(cfg), base_url="http://localhost:8765") as client:
        names = [p["name"] for p in client.get("/api/models").json()["profiles"]]
        assert name not in names and "eigenes" in names
        for _ in range(100):
            if not models.exists():
                break
            time.sleep(0.05)
        assert not models.exists()
    assert name not in json.loads(state.read_text())["added_profiles"]


def test_bonsai_profiles_are_dropped_but_files_stay(tmp_path):
    bonsai_dir = tmp_path / "bonsai"
    (bonsai_dir / "models").mkdir(parents=True)
    state = tmp_path / "state.json"
    state.write_text(json.dumps({"active_profile": "bonsai", "added_profiles": {
        "bonsai": {"backend": "openai", "server": {"command": f"{bonsai_dir}/scripts/start_llama_server.sh -np 1"}},
        "bonsai-eigen": {"backend": "openai", "server": {"command": "llama-server -m b.gguf"}}},
        "kv_cache": {"bonsai": "q4_0"}}))
    found = retire.clean_state(state)
    assert found == {"profiles": ["bonsai"], "strata": [], "strata_dirs": []}
    data = json.loads(state.read_text())
    assert list(data["added_profiles"]) == ["bonsai-eigen"] and data["kv_cache"] == {} and "active_profile" not in data
    assert (bonsai_dir / "models").is_dir()  # ~/bonsai löscht Orbwise nicht


def test_learned_lessons_are_removed(tmp_path):
    from orbwise import retire
    (tmp_path / "lessons.json").write_text("[]")
    (tmp_path / "lessons_pending.jsonl").write_text("{}\n")
    (tmp_path / "facts.md").write_text("- bleibt")
    assert retire.remove_lessons(tmp_path) > 0
    assert not (tmp_path / "lessons.json").exists() and (tmp_path / "facts.md").exists()
    assert retire.remove_lessons(tmp_path) == 0


def test_old_config_with_learning_keys_still_loads():
    from orbwise.config import Config
    c = Config.model_validate({"memory": {"learning": True, "lessons_max": 300}})
    assert not hasattr(c.memory, "learning")
