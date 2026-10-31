import asyncio
from pathlib import Path

import pytest

from orbwise.config import Config
from orbwise.llm import FakeLLM
from orbwise.memory import Memory


@pytest.fixture(autouse=True)
def _own_cache_dir(tmp_path: Path, monkeypatch):
    """Große Werkzeug-Ausgaben (proc.clip_saved) landen im Test-Ordner, nicht in ~/.cache."""
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))


@pytest.fixture(autouse=True)
def _own_keystore(tmp_path: Path, monkeypatch):
    """Zugangsdaten nie in den echten Schlüsselbund des Entwicklers – Tests nutzen eine Datei im Test-Ordner."""
    from orbwise import keystore
    monkeypatch.setenv("ORBWISE_SECRETS", "file")
    monkeypatch.setenv("ORBWISE_SECRETS_FILE", str(tmp_path / "secrets.json"))
    keystore.reset()
    yield
    keystore.reset()


@pytest.fixture(autouse=True)
def _not_root(monkeypatch):
    """Tests laufen wie bei einem normalen Benutzer – auch wenn der Testrechner (Container) root ist."""
    from orbwise import agent_safety
    monkeypatch.setattr(agent_safety, "running_as_root", lambda: False)


@pytest.fixture(autouse=True)
def _no_github_login(monkeypatch):
    """Kein GitHub-Token vom Testrechner (GH_TOKEN, gh auth) – sonst sind die GitHub-Werkzeuge je nach Rechner an
    oder aus. Tests, die ein Token brauchen, setzen cfg.github.token."""
    from orbwise import github
    for name in ("ORBWISE_GITHUB_TOKEN", "GH_TOKEN", "GITHUB_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(github, "_gh_cache", (float("inf"), ""))  # „gh auth token“ gilt als schon gefragt: keins


@pytest.fixture(autouse=True)
def _web_token(tmp_path: Path, monkeypatch):
    """Zugangs-Token im Test-Ordner; TestClient schickt es von selbst mit (die Server-Tests prüfen Funktionen, nicht
    die Anmeldung – die prüft test_web_auth.py mit einem Client ohne Token)."""
    from fastapi.testclient import TestClient

    from orbwise import web_auth
    monkeypatch.setenv("ORBWISE_WEB_TOKEN_FILE", str(tmp_path / "web-token"))
    original = TestClient.__init__

    def with_token(self, *args, **kwargs):
        if kwargs.pop("anonymous", False) is False:
            kwargs["headers"] = {"X-Orbwise-Token": web_auth.read_token(), **(kwargs.get("headers") or {})}
        original(self, *args, **kwargs)
    monkeypatch.setattr(TestClient, "__init__", with_token)


@pytest.fixture
def cfg(tmp_path: Path) -> Config:
    c = Config()
    c.memory.dir = tmp_path / "memory"
    c.tools.search_paths = [tmp_path]
    c.voice.enabled = False
    return c


@pytest.fixture
def llm() -> FakeLLM:
    return FakeLLM(delay=0)


@pytest.fixture
def memory(cfg, llm):
    m = Memory(cfg.memory, llm)
    yield m
    m.close()


def run(coro):
    return asyncio.run(coro)
