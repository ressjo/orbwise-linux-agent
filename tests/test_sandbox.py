"""Coding-Sandbox: Arbeitskopie (workspace.py), Container-Befehle (sandbox.py), Freigaben, GitHub (github.py)."""

import asyncio
import json
import os
import shutil
import subprocess
from pathlib import Path

import httpx
import pytest
from conftest import run

from orbwise import github, sandbox, workspace
from orbwise.agent import Agent

git_missing = pytest.mark.skipif(not shutil.which("git"), reason="git fehlt")


def sh(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True).stdout


@pytest.fixture
def data(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    return tmp_path


@pytest.fixture
def repo(data):
    p = data / "proj"
    p.mkdir()
    sh(p, "init", "-q")
    sh(p, "config", "user.email", "dev@example.org")
    sh(p, "config", "user.name", "Dev")
    (p / "app.py").write_text("x = 1\n")
    (p / "alt.py").write_text("alt\n")
    sh(p, "add", ".")
    sh(p, "commit", "-qm", "init")
    sh(p, "remote", "add", "origin", "https://github.com/owner/proj.git")
    (p / "app.py").write_text("x = 2\n")  # nicht committet
    (p / "neu.py").write_text("neu\n")     # neu, nicht ignoriert
    (p / "alt.py").unlink()               # im Projekt gelöscht
    return p


# ---------------------------------------------------------------- Arbeitskopie
@git_missing
def test_workspace_copies_the_current_state_on_its_own_branch(repo):
    st = workspace.create("chat123456", str(repo), "Login reparieren")
    ws = Path(st["path"])
    assert (ws / "app.py").read_text() == "x = 2\n" and (ws / "neu.py").exists() and not (ws / "alt.py").exists()
    assert st["branch"] == "orbwise/login-reparieren-chat12" and sh(ws, "rev-parse", "--abbrev-ref", "HEAD").strip() == st["branch"]
    assert sh(ws, "remote", "get-url", "origin").strip() == "https://github.com/owner/proj.git"
    assert sh(ws, "config", "user.name").strip() == "Dev"
    assert (ws / ".git").is_dir() and sh(ws, "rev-parse", "--git-common-dir").strip() == ".git"  # eigenes .git
    assert workspace.changes(st) == []


@git_missing
def test_apply_copies_changes_and_reports_conflicts(repo):
    st = workspace.create("chatabc", str(repo))
    ws = Path(st["path"])
    (ws / "app.py").write_text("x = 3\n")
    (ws / "extra.py").write_text("e\n")
    (ws / "neu.py").unlink()
    (repo / "app.py").write_text("x = 99\n")  # inzwischen im Projekt selbst geändert → Konflikt
    out = workspace.apply(st)
    assert out["conflicts"] == ["app.py"] and set(out["applied"]) == {"extra.py", "neu.py"}
    assert (repo / "extra.py").exists() and not (repo / "neu.py").exists() and (repo / "app.py").read_text() == "x = 99\n"
    out = workspace.apply(st, force=True)
    assert "app.py" in out["applied"] and (repo / "app.py").read_text() == "x = 3\n"
    assert workspace.changes(st) == []  # neue Basis
    workspace.discard(st)
    assert not ws.exists() and repo.exists()


@git_missing
def test_apply_never_follows_symlinks_out_of_the_copy(repo, data):
    secret = data / "host-secret.txt"
    secret.write_text("geheim")
    st = workspace.create("chatlink", str(repo))
    (Path(st["path"]) / "link.txt").symlink_to(secret)
    out = workspace.apply(st)
    assert out["applied"] == ["link.txt"]
    assert (repo / "link.txt").is_symlink() and os.readlink(repo / "link.txt") == str(secret)


def test_workspace_without_git_skips_heavy_folders(data):
    p = data / "plain"
    (p / "node_modules" / "x").mkdir(parents=True)
    (p / "node_modules" / "x" / "i.js").write_text("1")
    (p / "main.c").write_text("int main(){}")
    st = workspace.create("plainchat", str(p))
    assert (Path(st["path"]) / "main.c").exists() and not (Path(st["path"]) / "node_modules").exists()
    assert st["branch"] == ""


# ---------------------------------------------------------------- Container
def test_engine_prefers_podman_and_explains_when_missing(monkeypatch):
    sandbox._engine_cache.clear()
    monkeypatch.setattr(sandbox.shutil, "which", lambda n: f"/usr/bin/{n}" if n in ("podman", "docker") else None)
    monkeypatch.setattr(sandbox, "_info_ok", lambda b: (True, ""))

    class C:
        class sandbox:
            engine = "auto"
    assert sandbox.engine(C)[0] == "/usr/bin/podman"
    sandbox._engine_cache.clear()
    monkeypatch.setattr(sandbox.shutil, "which", lambda n: None)
    binary, error = sandbox.engine(C)
    assert binary is None and "podman" in error
    sandbox._engine_cache.clear()


def test_run_and_exec_arguments(cfg):
    argv = sandbox.run_argv("/usr/bin/podman", cfg, "orbwise-x", "/home/u/ws", False, "img:1")
    assert argv[argv.index("-v") + 1] == "/home/u/ws:/home/u/ws:Z" and argv[argv.index("-w") + 1] == "/home/u/ws"
    assert ["--network", "none"] == argv[argv.index("--network"):argv.index("--network") + 2]
    assert "--cap-drop" in argv and "no-new-privileges" in argv and "--user" not in argv
    docker = sandbox.run_argv("/usr/bin/docker", cfg, "orbwise-x", "/w", True, "img:1")
    assert "--network" not in docker and docker[docker.index("--user") + 1] == f"{os.getuid()}:{os.getgid()}"
    ex = sandbox.exec_argv("/usr/bin/podman", "orbwise-x", "/w/sub", "pytest -q", 120)
    assert ex[:6] == ["/usr/bin/podman", "exec", "-w", "/w/sub", "orbwise-x", "bash"]
    assert ex[-1].startswith("timeout -k 5 120 bash -lc ")
    assert sandbox.strip_sudo("sudo apt-get install -y jq && sudo -E make") == "apt-get install -y jq && make"


@pytest.fixture
def fake_engine(data, monkeypatch):
    """Ein „Container-Programm“, das exec-Befehle einfach lokal ausführt – so lässt sich der Weg durch die Sandbox
    ohne Podman/Docker prüfen."""
    script = data / "fake-engine"
    log = data / "engine.log"
    script.write_text(f"""#!/bin/bash
echo "$@" >> {log}
case "$1" in
  info) echo ok ;;
  inspect) echo running ;;
  exec) shift; while [ "$1" = -w ] || [ "$1" = -e ]; do [ "$1" = -w ] && cd "$2"; shift 2; done; shift; exec "$@" ;;
  *) exit 0 ;;
esac
""")
    script.chmod(0o755)
    sandbox._engine_cache.clear()
    monkeypatch.setattr(sandbox, "engine", lambda cfg: (str(script), ""))
    return log


@git_missing
def test_shell_runs_through_the_container_and_never_asks(cfg, llm, memory, repo, fake_engine):
    agent = Agent(cfg, llm, memory)
    memory.switch_mode("coding")
    memory.set_project(str(repo))
    sb = run(agent.sandbox_enable())
    assert agent.project_dir() == sb["path"]
    events = []

    async def emit(ev):
        events.append(ev)

    async def confirm(*a):
        return False
    agent.auto_mode = "read"
    run(agent.run('/tool shell {"command": "sudo touch gebaut.txt; pwd"}', emit, confirm))
    call = next(e for e in events if e["type"] == "tool_call")
    assert call["risk"] == "safe" and "Sandbox" in call["reason"]
    assert (Path(sb["path"]) / "gebaut.txt").exists() and not (repo / "gebaut.txt").exists()
    assert f"exec -w {sb['path']} {sb['name']} bash -lc" in fake_engine.read_text()
    # Auto „Aus“ fragt auch in der Sandbox; Dateien außerhalb der Kopie fragen
    agent.auto_mode = "off"
    events.clear()
    run(agent.run('/tool write {"path": "a.txt", "content": "x"}', emit, confirm))
    assert next(e for e in events if e["type"] == "tool_call")["risk"] == "confirm"
    agent.auto_mode = "auto"
    events.clear()
    run(agent.run('/tool write ' + json.dumps({"path": str(repo / "b.txt"), "content": "x"}), emit, confirm))
    assert next(e for e in events if e["type"] == "tool_call")["risk"] == "confirm"
    # Systemprompt nennt die Sandbox
    assert "Container" in agent.coding_env() and sb["branch"] in agent.coding_env()
    run(agent.sandbox_disable())
    assert agent.project_dir() == str(repo) and not Path(sb["path"]).exists()


@pytest.mark.skipif(not shutil.which("docker") and not shutil.which("podman"), reason="keine Container-Engine")
def test_real_container_round_trip(cfg, llm, memory, repo, data):
    sandbox._engine_cache.clear()
    binary, _ = sandbox.engine(cfg)
    if not binary or not sandbox.image_ready(binary, "debian:bookworm-slim"):
        pytest.skip("Engine läuft nicht oder Testimage fehlt")
    cfg.sandbox.image = "debian:bookworm-slim"
    agent = Agent(cfg, llm, memory)
    memory.switch_mode("coding")
    memory.set_project(str(repo))
    sb = run(agent.sandbox_enable())
    try:
        out = []

        async def emit(ev):
            if ev["type"] == "tool_result":
                out.append(ev["text"])

        async def confirm(*a):
            return True
        run(agent.run('/tool shell {"command": "cat /etc/debian_version >/dev/null && echo drin > c.txt"}', emit, confirm))
        assert (Path(sb["path"]) / "c.txt").read_text().strip() == "drin"
    finally:
        run(agent.sandbox_disable())


# ---------------------------------------------------------------- GitHub
def test_token_order(cfg, monkeypatch, data):
    for name in ("ORBWISE_GITHUB_TOKEN", "GH_TOKEN", "GITHUB_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    gh = data / "gh"
    gh.write_text("#!/bin/sh\necho gho_fromgh\n")
    gh.chmod(0o755)
    monkeypatch.setattr(github.shutil, "which", lambda n: str(gh) if n == "gh" else None)
    github._gh_cache = None
    assert github.token_info(cfg) == ("gho_fromgh", "gh")
    monkeypatch.setenv("GH_TOKEN", "envtok")
    assert github.token_info(cfg) == ("envtok", "env")
    cfg.github.token = "cfgtok"
    assert github.token_info(cfg) == ("cfgtok", "config")
    github._gh_cache = None


def test_api_user_and_repo_search(cfg):
    cfg.github.token = "t"

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Bearer t"
        if request.url.path == "/user":
            return httpx.Response(200, json={"login": "jo", "id": 7, "name": None})
        if request.url.path == "/user/repos":
            return httpx.Response(200, json=[{"full_name": "jo/orbwise", "private": True, "pushed_at": "2026"},
                                             {"full_name": "jo/other", "description": "x"}])
        return httpx.Response(404)
    transport = httpx.MockTransport(handler)
    assert run(github.user(cfg, transport)) == {"login": "jo", "name": "jo", "email": "7+jo@users.noreply.github.com"}
    found = run(github.repos(cfg, "orb", transport))
    assert [r["full_name"] for r in found] == ["jo/orbwise"] and found[0]["private"]


@git_missing
def test_clone_and_push_keep_the_token_out_of_the_repo(cfg, data, monkeypatch):
    remote = data / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True)
    seed = data / "seed"
    seed.mkdir()
    sh(seed, "init", "-q")
    sh(seed, "-c", "user.email=a@b", "-c", "user.name=A", "commit", "-q", "--allow-empty", "-m", "start")
    sh(seed, "push", "-q", str(remote), "HEAD:main")
    cfg.github.token = "geheimes-token"
    cfg.github.clone_dir = str(data / "Projekte")
    path = github.clone(cfg, "owner/remote", url=str(remote))
    assert Path(path) == data / "Projekte" / "remote"
    assert "geheimes-token" not in (Path(path) / ".git" / "config").read_text()
    sh(path, "checkout", "-q", "-b", "orbwise/test")
    sh(path, "-c", "user.email=a@b", "-c", "user.name=A", "commit", "-q", "--allow-empty", "-m", "arbeit")
    out = github.push(cfg, path)
    assert out["branch"] == "orbwise/test" and "orbwise/test" in sh(remote, "branch")
    assert github.clone(cfg, "owner/remote", url=str(remote)) == path  # zweites Mal: fetch statt Fehler
    with pytest.raises(github.GitHubError):
        github.clone(cfg, "kein repo")


def test_compare_link_for_github_remotes(cfg, monkeypatch):
    monkeypatch.setattr(github, "git", lambda args, cwd=None, tok="", timeout=600:
                        "https://github.com/jo/app.git\n" if args[:2] == ["remote", "get-url"] else "")
    out = github.push(cfg, "/x", "feature")
    assert out["compare"] == "https://github.com/jo/app/compare/feature?expand=1"


def test_push_always_asks_even_in_auto(cfg, llm, memory, repo, monkeypatch):
    cfg.github.token = "t"
    agent = Agent(cfg, llm, memory)
    memory.switch_mode("coding")
    memory.set_project(str(repo))
    agent.auto_mode = "auto"
    events = []

    async def emit(ev):
        events.append(ev)

    async def confirm(*a):
        return False
    run(agent.run('/tool github_push {}', emit, confirm))
    call = next(e for e in events if e["type"] == "tool_call")
    assert call["risk"] == "confirm" and "owner/proj" in call["reason"]
    assert "github_push" not in {s["function"]["name"] for s in agent.all_schemas}  # nicht im Tools-Modus


def test_sandbox_endpoints(cfg, repo, fake_engine, monkeypatch):
    from fastapi.testclient import TestClient

    from orbwise import server
    monkeypatch.setenv("ORBWISE_FAKE_LLM", "1")
    monkeypatch.setenv("ORBWISE_SKIP_WARMUP", "1")
    client = TestClient(server.create_app(cfg), base_url="http://localhost:8765")
    with client:
        assert client.get("/api/sandbox").status_code == 409  # Tools-Modus
        client.post("/api/mode", json={"mode": "coding"})
        chat = client.get("/api/history").json()["chat"]["id"]
        assert client.post("/api/sandbox", json={"enabled": True}).status_code == 400  # ohne Projekt
        client.post(f"/api/chats/{chat}/project", json={"path": str(repo)})
        info = client.get("/api/project").json()
        assert info["repo"] == "owner/proj" and info["branch"] in ("master", "main") and not info["sandbox"]
        st = client.post("/api/sandbox", json={"enabled": True}).json()
        assert st["enabled"] and st["branch"].startswith("orbwise/") and st["github"] == "owner/proj"
        info = client.get("/api/project").json()
        assert info["repo"] == "owner/proj" and info["branch"] == st["branch"] and info["sandbox"]
        (Path(st["path"]) / "fix.py").write_text("ok\n")
        files = client.get("/api/sandbox/changes").json()["files"]
        assert [f["path"] for f in files] == ["fix.py"]
        assert "+ok" in client.get("/api/sandbox/changes", params={"path": "fix.py"}).json()["diff"]
        assert client.post("/api/sandbox/apply", json={}).json()["applied"] == ["fix.py"]
        assert (repo / "fix.py").read_text() == "ok\n"
        assert client.post("/api/sandbox", json={"network": False}).json()["network"] is False
        client.post("/api/sandbox", json={"enabled": False})
        assert not Path(st["path"]).exists()
        gh = client.get("/api/github/status").json()
        assert "connected" in gh and "token" not in json.dumps(gh).lower().replace("token_", "")
    asyncio.set_event_loop(asyncio.new_event_loop())
