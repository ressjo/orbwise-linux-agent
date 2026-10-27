"""Käfig für Auto-Shell-Befehle (bubblewrap): was ohne Rückfrage läuft, kann nur im Projekt bzw. /tmp schreiben,
sieht keine Zugangsdaten und kann Orbwise selbst nicht bedienen."""

import os
import shutil
import socket
import subprocess
import sys
import time

import pytest
from conftest import run
from fastapi.testclient import TestClient

from orbwise import jail
from orbwise.agent import Agent
from orbwise.tools.registry import ToolContext, get_tool

HAVE_BWRAP = bool(shutil.which("bwrap")) and jail.available(force=True)[0]
needs_bwrap = pytest.mark.skipif(not HAVE_BWRAP, reason="bubblewrap nicht nutzbar")


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    h = tmp_path / "home"
    (h / "proj").mkdir(parents=True)
    (h / ".ssh").mkdir()
    (h / ".ssh" / "id_ed25519").write_text("KEY")
    monkeypatch.setenv("HOME", str(h))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    return h


def test_argv_locks_down_the_system(home):
    argv = jail.argv("make", str(home / "proj"), [str(home / "proj")], "chat1")
    joined = " ".join(argv)
    assert argv[0].endswith("bwrap") and "--ro-bind / /" in joined and "--clearenv" in argv
    assert f"--bind {home / 'proj'} {home / 'proj'}" in joined
    assert f"--tmpfs {home / '.ssh'} --remount-ro {home / '.ssh'}" in joined  # Schlüssel unsichtbar
    assert "--unshare-all" in argv and "--share-net" in argv and "--die-with-parent" in argv
    assert argv[-3:] == ["/bin/bash", "-c", "make"]


def test_environment_has_no_secrets(monkeypatch):
    monkeypatch.setenv("ORBWISE_TELEGRAM_TOKEN", "geheim")
    monkeypatch.setenv("GITHUB_TOKEN", "geheim")
    monkeypatch.setenv("MY_API_KEY", "geheim")
    env = jail.env()
    assert "geheim" not in env.values() and env["TMPDIR"] == "/tmp" and "PATH" in env


def test_denied_recognises_the_cage():
    assert jail.denied("touch: cannot touch '/home/x/a': Read-only file system")
    assert not jail.denied("make: *** [all] Error 2")


def _risk(agent, command, project=None):
    ctx = ToolContext(cfg=agent.cfg, memory=agent.memory, cwd=project)
    risk, reason = agent._final_risk("run_shell", get_tool("run_shell"), {"command": command}, ctx)
    return risk, reason, ctx.jail


def test_auto_runs_shell_only_in_the_cage(cfg, llm, memory, monkeypatch):
    agent = Agent(cfg, llm, memory)
    agent.auto_mode = "auto"
    monkeypatch.setattr(jail, "available", lambda force=False: (True, ""))
    risk, reason, cage = _risk(agent, "python3 -c 'import shutil'")
    assert risk == "safe" and cage and cage["writable"] == [] and "isoliert" in reason
    risk, _, cage = _risk(agent, "ls -la")  # lesend (Positivliste): wie bisher, ohne Käfig
    assert risk == "safe" and cage is None
    risk, _, cage = _risk(agent, "rm x")  # die Sperrliste fragt weiter – dann ohne Käfig, wie gesehen
    assert risk == "confirm" and cage is None

    monkeypatch.setattr(jail, "available", lambda force=False: (False, "bubblewrap fehlt – installieren"))
    risk, reason, cage = _risk(agent, "python3 -c 'import shutil'")
    assert risk == "confirm" and cage is None and "bubblewrap fehlt" in reason


def _collect(agent, text, answer):
    asked = []

    async def emit(ev):
        pass

    async def confirm(*a):
        asked.append(a)
        return answer
    run(agent.run(text, emit, confirm))
    return asked


@needs_bwrap
def test_cage_blocks_writes_outside_and_asks_to_run_without_it(cfg, llm, memory, home):
    agent = Agent(cfg, llm, memory)
    agent.auto_mode = "auto"
    evil = home / "evil.txt"
    cmd = f"python3 -c \"open('{evil}', 'w').write('x')\""
    asked = _collect(agent, '/tool run_shell {"command": "%s"}' % cmd.replace('"', '\\"'), False)
    assert len(asked) == 1 and "isoliert" in asked[0][3] and not evil.exists()  # erst im Käfig, dann gefragt
    asked = _collect(agent, '/tool run_shell {"command": "%s"}' % cmd.replace('"', '\\"'), True)
    assert len(asked) == 1 and evil.read_text() == "x"  # bestätigt: ohne Käfig


@needs_bwrap
def test_cage_hides_credentials_and_keeps_the_project_writable(home):
    proj = home / "proj"
    out = subprocess.run(jail.argv("echo ok > a.txt; ls -A ~/.ssh | wc -l; echo t=$ORBWISE_X_TOKEN; "
                                   "python3 -c \"import shutil; shutil.rmtree('%s')\"" % (home / ".ssh"),
                                   str(proj), [str(proj)], "t"),
                         capture_output=True, text=True, env={**os.environ, "ORBWISE_X_TOKEN": "geheim"})
    assert (proj / "a.txt").read_text() == "ok\n"
    assert out.stdout.splitlines()[:2] == ["0", "t="]
    assert "Read-only file system" in out.stderr and (home / ".ssh" / "id_ed25519").exists()


@needs_bwrap
def test_connections_from_the_cage_are_recognised():
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(4)
    port = server.getsockname()[1]
    code = f"import socket,time; s=socket.create_connection(('127.0.0.1',{port})); time.sleep(30)"
    proc = subprocess.Popen(jail.argv(f"{sys.executable} -c \"{code}\"", "/", [], "peer"), start_new_session=True)
    try:
        jail.register(proc.pid)
        caged, _ = server.accept()
        free = socket.create_connection(("127.0.0.1", port))
        mine, _ = server.accept()
        time.sleep(0.2)
        assert jail.peer_is_jailed(caged.getpeername()[1], port)
        assert not jail.peer_is_jailed(mine.getpeername()[1], port)
        free.close()
    finally:
        proc.kill()
        proc.wait()
        server.close()
    assert not jail.active()


def test_server_refuses_requests_from_the_cage(cfg, monkeypatch):
    monkeypatch.setenv("ORBWISE_FAKE_LLM", "1")
    monkeypatch.setenv("ORBWISE_SKIP_WARMUP", "1")
    from orbwise.server import create_app
    with TestClient(create_app(cfg), base_url="http://localhost:8765") as client:
        assert client.get("/api/memory/scope").status_code == 200
        monkeypatch.setattr(jail, "active", lambda: True)
        monkeypatch.setattr(jail, "peer_is_jailed", lambda port, server_port: True)
        assert client.get("/api/memory/scope").status_code == 403
        assert client.post("/api/mode", json={"mode": "coding"}).status_code == 403
