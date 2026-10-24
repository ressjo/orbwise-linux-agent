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
def _not_root(monkeypatch):
    """Tests laufen wie bei einem normalen Benutzer – auch wenn der Testrechner (Container) root ist."""
    from orbwise import agent_safety
    monkeypatch.setattr(agent_safety, "running_as_root", lambda: False)


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
