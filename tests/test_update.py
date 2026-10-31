import os
import subprocess
from pathlib import Path

import pytest

from orbwise import update as upd

ROOT = Path(__file__).resolve().parents[1]


def git(cwd, *args):
    subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True,
                   env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
                        "GIT_COMMITTER_EMAIL": "t@t"})


@pytest.fixture
def repos(tmp_path, monkeypatch):
    origin = tmp_path / "origin"
    origin.mkdir()
    git(origin, "init", "-q", "-b", "main")
    (origin / "a.txt").write_text("1")
    git(origin, "add", ".")
    git(origin, "commit", "-q", "-m", "Erster Stand")
    clone = tmp_path / "orbwise"
    subprocess.run(["git", "clone", "-q", str(origin), str(clone)], check=True)
    (clone / ".venv/bin").mkdir(parents=True)
    (clone / ".venv/bin/orbwise").write_text("")
    monkeypatch.setattr(upd, "server_running", lambda port: False)
    monkeypatch.setattr(upd.shutil, "which", lambda n: f"/usr/bin/{n}")
    return origin, clone


def fake_runner(calls):
    def runner(cmd, **kw):
        if cmd[0] == "git":
            return subprocess.run(cmd, capture_output=True, text=True)
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, "", "")
    return runner


def test_update_pulls_new_commit(repos):
    origin, clone = repos
    (origin / "a.txt").write_text("2")
    git(origin, "commit", "-qam", "Telemetrie hinzugefügt")
    out, calls = [], []
    assert upd.update(clone, runner=fake_runner(calls), out=out.append) == 0
    assert (clone / "a.txt").read_text() == "2"
    assert any("Telemetrie hinzugefügt" in line for line in out)
    assert calls and calls[0][1:3] == ["sync", "--extra"]


def test_already_current_skips_sync(repos):
    _, clone = repos
    out, calls = [], []
    assert upd.update(clone, runner=fake_runner(calls), out=out.append) == 0
    assert "neuesten Stand" in "".join(out) and not calls


def test_missing_venv_triggers_sync(repos):
    _, clone = repos
    (clone / ".venv/bin/orbwise").unlink()
    calls = []
    assert upd.update(clone, runner=fake_runner(calls), out=lambda s: None) == 0
    assert calls


def test_local_changes_block_update(repos):
    origin, clone = repos
    (clone / "a.txt").write_text("lokal")
    out = []
    assert upd.update(clone, runner=fake_runner([]), out=out.append) == 1
    assert (clone / "a.txt").read_text() == "lokal"
    assert "stash" in "".join(out)


def test_restart_when_server_runs(repos, monkeypatch):
    origin, clone = repos
    (origin / "a.txt").write_text("3")
    git(origin, "commit", "-qam", "neu")
    monkeypatch.setattr(upd, "server_running", lambda port: True)
    restarted = []
    upd.update(clone, runner=fake_runner([]), restart=lambda p, r: restarted.append(p), out=lambda s: None)
    assert restarted == [8765]


def test_zip_download_gets_hint(tmp_path):
    out = []
    assert upd.update(tmp_path, out=out.append) == 1
    assert "bootstrap.sh" in "".join(out)


def test_launcher_self_heals(tmp_path):
    home = tmp_path / "orbwise"
    (home / ".venv/bin").mkdir(parents=True)
    (home / "pyproject.toml").write_text("")
    fakebin = tmp_path / "bin"
    fakebin.mkdir()
    # uv-Attrappe legt die fehlende .venv an
    (fakebin / "uv").write_text(f'#!/bin/sh\nprintf "#!/bin/sh\\necho gestartet \\"\\$@\\"\\n" > "{home}/.venv/bin/orbwise"\n'
                                f'chmod +x "{home}/.venv/bin/orbwise"\n')
    (fakebin / "uv").chmod(0o755)
    launcher = tmp_path / "orbwise-launcher"
    launcher.write_text((ROOT / "scripts/orbwise-launcher").read_text().replace("@ORBWISE_HOME@", str(home)))
    launcher.chmod(0o755)
    env = {**os.environ, "PATH": f"{fakebin}:/usr/bin:/bin"}
    env.pop("ORBWISE_HOME", None)
    r = subprocess.run([str(launcher), "doctor"], capture_output=True, text=True, env=env)
    assert r.returncode == 0 and r.stdout.strip() == "gestartet doctor"
    assert "recreating" in r.stderr

    r = subprocess.run([str(launcher)], capture_output=True, text=True,
                       env={**env, "ORBWISE_HOME": str(tmp_path / "weg")})
    assert r.returncode == 1 and "was not found" in r.stderr


def test_stop_servers_terminates_others_but_not_itself(monkeypatch):
    import signal
    me = os.getpid()
    monkeypatch.setattr(upd.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 0, f"{me}\n4242\n4343\n", ""))
    killed = []
    monkeypatch.setattr(upd.os, "kill", lambda pid, sig: killed.append((pid, sig)))
    monkeypatch.setattr(upd, "server_running", lambda port: False)
    monkeypatch.setattr(upd, "_is_server", lambda pid: True)
    assert upd.stop_servers(8765) == 2
    assert killed == [(4242, signal.SIGTERM), (4343, signal.SIGTERM)]


def test_shutdown_endpoint_ends_the_own_process(cfg, monkeypatch):
    import signal
    import time

    from fastapi.testclient import TestClient

    from orbwise import server
    monkeypatch.setenv("ORBWISE_FAKE_LLM", "1")
    monkeypatch.setenv("ORBWISE_SKIP_WARMUP", "1")
    killed = []
    monkeypatch.setattr(server.os, "kill", lambda pid, sig: killed.append((pid, sig)))
    with TestClient(server.create_app(cfg), base_url="http://localhost:8765") as client:
        assert client.post("/api/shutdown", headers={"Origin": "http://evil.example"}).status_code == 403
        assert client.post("/api/shutdown").json() == {"ok": True}
        time.sleep(0.6)
    assert killed == [(os.getpid(), signal.SIGTERM)]


def test_only_real_servers_count(tmp_path, monkeypatch):
    """Eine Shell, in deren Befehlszeile „orbwise serve“ nur vorkommt, wird nicht beendet."""
    proc = subprocess.Popen(["bash", "-c", "sleep 30 # orbwise serve"])
    try:
        assert not upd._is_server(proc.pid)
    finally:
        proc.kill()


def test_check_reports_new_commits(repos):
    origin, clone = repos
    (clone / "pyproject.toml").write_text('[project]\nname = "orbwise"\nversion = "1.2.3"\n')
    info = upd.check(clone)
    assert info["git"] and info["version"] == "1.2.3" and info["behind"] == 0 and not info["can_update"]
    assert info["subject"] == "Erster Stand" and info["branch"] == "main" and len(info["date"]) == 10
    for text in ("2", "3"):
        (origin / "a.txt").write_text(text)
        git(origin, "commit", "-qam", f"Neu {text}")
    info = upd.check(clone)
    assert info["behind"] == 2 and info["changes"] == ["Neu 3", "Neu 2"] and info["can_update"] and not info["error"]
    (clone / "a.txt").write_text("lokal")
    blocked = upd.check(clone, fetch=False)
    assert blocked["behind"] == 2 and not blocked["can_update"] and "stash" in blocked["reason"]


def test_check_without_git_or_remote(repos, tmp_path):
    assert upd.check(tmp_path / "nix")["reason"].startswith("kein Git")
    origin, clone = repos
    git(clone, "remote", "set-url", "origin", str(tmp_path / "weg"))
    assert upd.check(clone)["error"]


def test_systemd_unit_from_cgroup(monkeypatch, tmp_path):
    monkeypatch.delenv("INVOCATION_ID", raising=False)
    assert upd.systemd_unit() is None
    monkeypatch.setenv("INVOCATION_ID", "x")
    cg = tmp_path / "cgroup"
    cg.write_text("0::/user.slice/user-1000.slice/user@1000.service/app.slice/orbwise.service\n")
    monkeypatch.setattr(upd, "Path", lambda p: cg if p == "/proc/self/cgroup" else Path(p))
    assert upd.systemd_unit() == "orbwise.service"


def test_update_from_the_web_ui(cfg, monkeypatch):
    from fastapi.testclient import TestClient

    from orbwise.server import create_app
    monkeypatch.setenv("ORBWISE_FAKE_LLM", "1")
    monkeypatch.setenv("ORBWISE_SKIP_WARMUP", "1")
    checks, restarts = [], []
    monkeypatch.setattr(upd, "check", lambda *a, **k: checks.append(1) or {
        "version": "0.1.0", "git": True, "commit": "abc1234", "date": "2026-10-31", "subject": "x", "branch": "main",
        "behind": 1, "changes": ["Neu"], "can_update": True, "reason": "", "error": ""})

    def fake_update(root, port, runner, restart, out):
        out("Hole Updates …")
        restart(port, Path("/x"))
        return 0
    monkeypatch.setattr(upd, "update", fake_update)
    monkeypatch.setattr(upd, "restart_self", lambda port, root: restarts.append((port, root)))
    with TestClient(create_app(cfg), base_url="http://localhost:8765") as client:
        assert client.get("/api/update").json()["version"]  # ohne Prüfen: nur der installierte Stand
        assert client.get("/api/update", params={"check": 1}).json()["behind"] == 1
        client.get("/api/update", params={"check": 1})
        assert len(checks) == 1  # höchstens alle 3 Stunden
        client.get("/api/update", params={"check": 1, "force": 1})
        assert len(checks) == 2
        with client.websocket_connect("ws://localhost:8765/ws", headers={"Origin": "http://localhost:8765"}) as ws:
            assert client.post("/api/update").json() == {"started": True}
            events = []
            while not any(e.get("type") == "update_done" for e in events):
                events.append(ws.receive_json())
        progress = [e["text"] for e in events if e.get("type") == "update_progress"]
        done = next(e for e in events if e.get("type") == "update_done")
        assert progress == ["Hole Updates …"] and done["ok"] and done["restart"]
        for _ in range(50):
            if restarts:
                break
            import time
            time.sleep(0.05)
        assert restarts == [(8765, Path("/x"))]


def test_restart_self_starts_again_once_the_port_is_free(tmp_path, monkeypatch):
    import socket
    import sys
    import time
    monkeypatch.delenv("INVOCATION_ID", raising=False)
    home = tmp_path / "home"
    (home / ".local/bin").mkdir(parents=True)
    marker = tmp_path / "gestartet"
    launcher = home / ".local/bin/orbwise"
    launcher.write_text(f'#!/bin/sh\necho "$@" > "{marker}"\n')
    launcher.chmod(0o755)
    monkeypatch.setattr(upd.Path, "home", lambda: home)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    started, killed = [], []
    real_popen = subprocess.Popen
    monkeypatch.setattr(upd.subprocess, "Popen", lambda args, **kw: started.append(args))
    monkeypatch.setattr(upd.os, "kill", lambda pid, sig: killed.append(pid))
    with socket.socket() as s:  # Port, auf dem „der alte Server“ noch lauscht
        s.bind(("127.0.0.1", 0))
        s.listen()
        port = s.getsockname()[1]
        upd.restart_self(port, tmp_path)
        assert killed == [os.getpid()] and started[0][0] == sys.executable
        helper = real_popen(started[0])  # der echte Helfer wartet, bis der Port frei ist
        time.sleep(0.6)
        assert helper.poll() is None and not marker.exists()
    helper.wait(timeout=10)
    for _ in range(50):
        if marker.exists():
            break
        time.sleep(0.1)
    assert marker.read_text().strip() == "serve"
