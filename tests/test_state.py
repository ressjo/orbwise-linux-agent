"""state.json über orbwise.state: atomar schreiben, nichts verlieren, wenn mehrere gleichzeitig ändern."""

import json
import threading

from orbwise import state


def test_load_missing_or_broken(tmp_path):
    path = tmp_path / "state.json"
    assert state.load(path) == {} and state.load(None) == {}
    path.write_text("{kaputt")
    assert state.load(path) == {}
    path.write_text("[1, 2]")
    assert state.load(path) == {}  # kein Objekt → leer


def test_update_is_atomic_and_keeps_other_keys(tmp_path):
    path = tmp_path / "sub" / "state.json"
    state.save(path, {"active_profile": "a", "context": {"a": 8192}})
    got = state.update(path, lambda data: data.setdefault("context", {}).update(b=16384) or "ok")
    assert got == "ok" and json.loads(path.read_text()) == {"active_profile": "a", "context": {"a": 8192, "b": 16384}}
    assert [p.name for p in path.parent.iterdir()] == ["state.json"]  # keine Temp-Datei übrig

    def boom(data):
        data["active_profile"] = "kaputt"
        raise RuntimeError("Absturz mitten im Ändern")
    try:
        state.update(path, boom)
    except RuntimeError:
        pass
    assert json.loads(path.read_text())["active_profile"] == "a"  # nichts Halbes geschrieben


def test_parallel_updates_lose_nothing(tmp_path):
    path = tmp_path / "state.json"

    def worker(i):
        for j in range(25):
            state.update(path, lambda data, k=f"{i}-{j}": data.setdefault("keys", {}).update({k: True}))
    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(state.load(path)["keys"]) == 8 * 25


def test_update_without_path_only_runs_the_change():
    assert state.update(None, lambda data: data.setdefault("x", 1)) == 1
