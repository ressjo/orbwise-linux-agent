"""Projekt nachträglich mit einem (leeren) GitHub-Repo verbinden und hochladen: origin setzen, notfalls git init und
erster Commit ohne Zugangsdaten, dann pushen – auch per Werkzeug und aus der Sandbox."""

import shutil
import subprocess

import pytest
from conftest import run

from orbwise import github
from orbwise.tools import github_tools
from orbwise.tools.registry import ToolContext, get_tool

pytestmark = pytest.mark.skipif(not shutil.which("git"), reason="git fehlt")
IDENT = {"login": "jo", "name": "Jo", "email": "7+jo@users.noreply.github.com"}


def sh(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True).stdout


@pytest.fixture
def gh(tmp_path, monkeypatch, cfg):
    """github.com/jo/neu.git zeigt auf ein lokales, leeres Bare-Repo; keine globale git-Identität."""
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True)
    empty = tmp_path / "gitconfig"
    empty.write_text("")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(empty))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", f"url.{remote}.insteadOf")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "https://github.com/jo/neu.git")
    cfg.github.token = "test-token-1234"
    return remote


@pytest.fixture
def plain(tmp_path):
    p = tmp_path / "proj"
    p.mkdir()
    (p / "app.py").write_text("print(1)\n")
    (p / ".env").write_text("API_KEY=geheim\n")
    (p / ".gitignore").write_text("build/\n")
    (p / "build").mkdir()
    (p / "build" / "out.bin").write_text("x")
    return p


def test_plain_folder_gets_init_first_commit_and_push(cfg, gh, plain):
    out = github.connect_and_push(cfg, str(plain), "jo/neu", identity=IDENT)
    assert sh(plain, "config", "remote.origin.url").strip() == "https://github.com/jo/neu.git"
    files = sh(plain, "ls-files").split()
    assert "app.py" in files and ".gitignore" in files and ".env" not in files and "build/out.bin" not in files
    assert out["skipped"] == [".env"] and out["files"] == 2 and out["branch"] == "main" and out["repo"] == "jo/neu"
    assert sh(plain, "log", "-1", "--format=%an <%ae> %s").strip() == "Jo <7+jo@users.noreply.github.com> Erster Commit"
    assert "main" in sh(gh, "branch")
    assert "test-token-1234" not in (plain / ".git" / "config").read_text()  # Token nie in der Konfiguration


def test_existing_repo_is_pushed_as_is(cfg, gh, plain):
    sh(plain, "init", "-q", "-b", "dev")
    sh(plain, "add", "app.py")
    sh(plain, "-c", "user.email=a@b", "-c", "user.name=A", "commit", "-qm", "eigener")
    out = github.connect_and_push(cfg, str(plain), "jo/neu")
    assert out["branch"] == "dev" and out["files"] == 0
    assert sh(gh, "log", "--format=%s", "dev").strip() == "eigener"  # nichts dazu committet


def test_another_origin_is_never_bent(cfg, gh, plain):
    sh(plain, "init", "-q")
    sh(plain, "remote", "add", "origin", "https://github.com/jo/alt.git")
    with pytest.raises(github.GitHubError, match="schon das Remote"):
        github.connect(str(plain), "jo/neu")
    assert sh(plain, "config", "remote.origin.url").strip() == "https://github.com/jo/alt.git"


def test_a_repo_that_is_not_empty_gets_a_clear_message(cfg, gh, plain, tmp_path):
    seed = tmp_path / "seed"
    seed.mkdir()
    sh(seed, "init", "-q", "-b", "main")
    sh(seed, "-c", "user.email=a@b", "-c", "user.name=A", "commit", "-q", "--allow-empty", "-m", "README")
    sh(seed, "push", "-q", str(gh), "main")
    with pytest.raises(github.GitHubError, match="nicht leer"):
        github.connect_and_push(cfg, str(plain), "jo/neu", identity=IDENT)


def test_empty_folder_says_nothing_to_upload(cfg, gh, tmp_path):
    p = tmp_path / "leer"
    p.mkdir()
    with pytest.raises(github.GitHubError, match="nichts zum Hochladen"):
        github.connect_and_push(cfg, str(p), "jo/neu", identity=IDENT)


def test_tool_connects_when_there_is_no_origin(cfg, gh, plain, monkeypatch):
    async def user(cfg, transport=None):
        return IDENT
    monkeypatch.setattr(github, "user", user)
    ctx = ToolContext(cfg=cfg, memory=None, cwd=str(plain))
    risk = github_tools._push_risk(ctx, {"repo": "jo/neu"})
    assert "verbindet das Projekt mit github.com/jo/neu" in risk[1]
    text = run(get_tool("github_push").func(ctx, repo="jo/neu"))
    assert "github.com/jo/neu verbunden" in text and ".env" in text
    assert "main" in sh(gh, "branch")


def test_endpoint_connects_the_sandbox_copy(cfg, gh, plain, monkeypatch, tmp_path):
    from fastapi.testclient import TestClient

    from orbwise.server import create_app
    sh(plain, "init", "-q", "-b", "main")
    sh(plain, "add", "app.py")
    sh(plain, "-c", "user.email=a@b", "-c", "user.name=A", "commit", "-qm", "basis")
    ws = tmp_path / "ws"
    sh(tmp_path, "clone", "-q", str(plain), str(ws))
    sh(ws, "checkout", "-q", "-b", "orbwise/arbeit")
    sh(ws, "-c", "user.email=a@b", "-c", "user.name=A", "commit", "-q", "--allow-empty", "-m", "aus der Sandbox")

    async def user(cfg, transport=None):
        return IDENT
    monkeypatch.setattr(github, "user", user)
    commits = []

    async def commit_all(cfg, sb, message):
        commits.append(sb["path"])
        return False
    from orbwise import sandbox
    monkeypatch.setattr(sandbox, "commit_all", commit_all)
    monkeypatch.setenv("ORBWISE_FAKE_LLM", "1")
    monkeypatch.setenv("ORBWISE_SKIP_WARMUP", "1")
    with TestClient(create_app(cfg), base_url="http://localhost:8765") as client:
        memory = client.app.state.hub.agent.memory
        memory.switch_mode("coding")
        memory.set_project(str(plain))
        memory.conversation.meta["sandbox"] = {"enabled": True, "path": str(ws), "project": str(plain), "origin": "",
                                               "name": "x", "branch": "orbwise/arbeit"}
        assert client.get("/api/project").json()["has_commits"] is True
        r = client.post("/api/github/connect", json={"repo": "jo/neu"})
        assert r.status_code == 200, r.text
        assert r.json()["branch"] == "orbwise/arbeit"
        assert commits == [str(ws)]  # offene Änderungen der Kopie wurden vorher committet
        assert memory.conversation.meta["sandbox"]["origin"] == "https://github.com/jo/neu.git"
    assert sh(plain, "config", "remote.origin.url").strip() == "https://github.com/jo/neu.git"
    assert sh(gh, "log", "--format=%s", "orbwise/arbeit").splitlines()[0] == "aus der Sandbox"
