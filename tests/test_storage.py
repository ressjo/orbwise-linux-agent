"""Speicherplatz (Einstellungen → Allgemein): Überblick über Modelle, Programme, Zwischenspeicher – und was sich leeren lässt."""

import json
import os
import time

from fastapi.testclient import TestClient

from orbwise import llamacpp, storage
from orbwise.config import ProfileConfig, ServerConfig

MB = 1024 * 1024


def _file(path, size):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\0" * size)
    return path


def _old(path, seconds=3600):
    t = time.time() - seconds
    os.utime(path, (t, t))
    return path


def _setup(tmp_path, cfg, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "share"))
    cfg.llm.models_dir = str(tmp_path / "models")
    cfg.image.dir = str(tmp_path / "image")


def test_clear_cache_keeps_busy_recent_and_own_text(tmp_path, cfg, monkeypatch):
    _setup(tmp_path, cfg, monkeypatch)
    base = storage.cache_dir()
    gone = [_old(_file(base / "outputs" / "a.txt", 3000)), _old(_file(base / "paperless" / "doc.pdf", 5000))]
    log = _old(_file(base / "jobs" / "job1.log", 100))                 # gehört zu einem Job dieser Sitzung
    fresh = _file(base / "jail-tmp" / "chat" / "work.txt", 200)        # gerade benutzt
    own = _old(_file(base / "quality" / "eigener-text.txt", 400))      # vom Nutzer abgelegt
    freed = storage.clear_cache({str(log)})
    assert freed == 8000 and not any(p.exists() for p in gone)
    assert log.exists() and fresh.exists() and own.exists()
    assert not (base / "outputs").exists() and not (base / "paperless").exists()  # leere Ordner sind weg


def test_old_llamacpp_builds_go_current_stays(tmp_path, cfg, monkeypatch):
    _setup(tmp_path, cfg, monkeypatch)
    base = llamacpp.root(cfg)
    _file(base / "b2-vulkan" / "llama-server", 2 * MB)
    _old(base / "b2-vulkan")
    _old(_file(base / "b1-vulkan" / "llama-server", 3 * MB).parent)
    _old(_file(base / "b2-rocm" / "llama-server", 4 * MB).parent)
    os.symlink("b2-vulkan", base / "current")
    (base / "manifest.json").write_text(json.dumps({"tag": "b2", "dir": "b2-vulkan", "previous": "b1-vulkan"}))
    assert storage.llamacpp_parts(cfg) == (2 * MB, 7 * MB)
    assert storage.remove_old_llamacpp(cfg) == 7 * MB
    assert sorted(p.name for p in base.iterdir()) == ["b2-vulkan", "current", "manifest.json"]
    assert llamacpp.manifest(cfg)["previous"] is None and llamacpp.installed(cfg)


def test_overview_counts_without_double_counting(tmp_path, cfg, monkeypatch):
    _setup(tmp_path, cfg, monkeypatch)
    data = storage.data_dir(cfg)
    _file(data / "memory" / "notes.md", 1000)
    _file(llamacpp.root(cfg) / "b2-vulkan" / "llama-server", 2 * MB)
    (llamacpp.root(cfg) / "manifest.json").write_text(json.dumps({"dir": "b2-vulkan"}))
    _file(tmp_path / "models" / "fremd.gguf", 3 * MB)   # selbst abgelegt
    _file(storage.cache_dir() / "outputs" / "x.txt", 500)
    o = storage.overview(cfg, models=[], images=[], image_dir=tmp_path / "image")
    groups = {g["key"]: g for g in o["groups"]}
    assert groups["models"]["items"][0]["bytes"] == 3 * MB and not groups["models"]["items"][0]["delete"]
    assert groups["programs"]["items"][0]["bytes"] == 2 * MB
    assert groups["cache"]["bytes"] == 500 and groups["cache"]["clear"] == "/api/storage/cache"
    own = groups["data"]["items"][0]["bytes"]
    assert 1000 <= own < 1000 + 4096  # llama.cpp liegt auch im Datenordner, zählt aber nur einmal
    assert o["total"] == sum(g["bytes"] for g in o["groups"])


def test_storage_api_lists_models_and_clears(tmp_path, cfg, monkeypatch):
    _setup(tmp_path, cfg, monkeypatch)
    monkeypatch.setenv("ORBWISE_SKIP_WARMUP", "1")
    monkeypatch.delenv("ORBWISE_FAKE_LLM", raising=False)
    gguf = _file(tmp_path / "models" / "qwen" / "Qwen-Q4_K_M.gguf", 5 * MB)
    mine = _file(tmp_path / "models" / "eigen.gguf", 2 * MB)
    cfg.llm.base_url = "http://127.0.0.1:9"
    cfg.llm.profiles = {
        "qwen": ProfileConfig(backend="openai", base_url="http://127.0.0.1:8099/v1", model="qwen",
                              server=ServerConfig(command=f"llama-server -m {gguf} -ngl 99")),
        "eigen": ProfileConfig(backend="openai", base_url="http://127.0.0.1:8098/v1", model="eigen",
                               server=ServerConfig(command=f"llama-server -m {mine} -ngl 99")),
        "olla": ProfileConfig(backend="ollama", model="qwen3")}
    cfg.llm.active = "olla"
    state = storage.data_dir(cfg) / "state.json"
    state.parent.mkdir(parents=True, exist_ok=True)
    state.write_text(json.dumps({
        "local_models": {"qwen": {"files": [str(gguf)], "downloaded": True},
                         "eigen": {"files": [str(mine)], "downloaded": False}},
        "added_profiles": {"qwen": {}, "eigen": {}}}))
    _old(_file(storage.cache_dir() / "outputs" / "x.txt", 700))
    from orbwise.server import create_app
    with TestClient(create_app(cfg), base_url="http://localhost:8765") as client:
        o = client.get("/api/storage").json()
        models = {i["label"]: i for i in {g["key"]: g for g in o["groups"]}["models"]["items"]}
        assert models["qwen"]["bytes"] == 5 * MB and models["qwen"]["delete"] == "/api/models/qwen"
        assert models["eigen"]["bytes"] == 2 * MB and not models["eigen"]["delete"]  # eigene Datei bleibt
        assert "Weitere Dateien im Modellordner" not in models  # alles im Ordner ist schon gezählt
        cache = {g["key"]: g for g in o["groups"]}["cache"]
        assert cache["bytes"] >= 700
        assert client.delete("/api/storage/cache").json()["freed"] >= 700
        assert client.delete("/api/storage/llamacpp-old").json() == {"freed": 0}
        assert client.delete("/api/storage/browser").json() == {"freed": 0}
        assert client.delete("/api/storage/snapshots").json() == {"freed": 0}
