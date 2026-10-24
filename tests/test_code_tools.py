"""Werkzeuge des Coding-Modus nach OpenCode: read, edit, write, grep, glob, shell, websearch."""

import shutil

import pytest
from conftest import run

from orbwise.tools import code
from orbwise.tools.registry import CONFIRM, ToolContext, get_tool, load_all_tools


class Conv:
    def __init__(self):
        self.meta = {}


class Mem:
    def __init__(self):
        self.conversation = Conv()


@pytest.fixture
def project(tmp_path):
    p = tmp_path / "proj"
    (p / "src").mkdir(parents=True)
    (p / "src" / "app.py").write_text("def a():\n    return 1\n\n\ndef b():\n    return 2\n", encoding="utf-8")
    (p / "README.md").write_text("# Projekt\n", encoding="utf-8")
    return p


@pytest.fixture
def ctx(cfg, project):
    load_all_tools()
    return ToolContext(cfg=cfg, memory=Mem(), cwd=str(project))


def call(ctx, name, **args):
    return run(get_tool(name).func(ctx, **args))


def test_read_numbers_lines_pages_and_lists_folders(ctx, project):
    out = call(ctx, "read", path="src/app.py")
    assert out.startswith("1: def a():\n2:     return 1") and "Ende der Datei – 6 Zeilen" in out
    part = call(ctx, "read", path="src/app.py", offset=5, limit=1)
    assert part.startswith("5: def b():") and "weiter mit offset=6" in part
    listing = call(ctx, "read", path=".")
    assert "src/" in listing.splitlines()[1] and "README.md" in listing
    assert "Meintest du" in call(ctx, "read", path="src/ap.py") and "app.py" in call(ctx, "read", path="src/ap.py")
    (project / ".env").write_text("TOKEN=x")
    assert get_tool("read").assess(ctx, {"path": ".env"})[0] == "blocked"


def test_read_brings_nested_agents_md_once(ctx, project):
    (project / "src" / "AGENTS.md").write_text("Tabs statt Leerzeichen.")
    first = call(ctx, "read", path="src/app.py")
    assert "src/AGENTS.md" in first and "Tabs statt Leerzeichen." in first
    assert "AGENTS.md" not in call(ctx, "read", path="src/app.py")


def test_edit_exact_normalised_and_reindented(ctx, project):
    f = project / "src" / "app.py"
    out = call(ctx, "edit", path="src/app.py", oldString="    return 2", newString="    return 3")
    assert "return 3" in f.read_text() and "+1 −1" in out and "-    return 2" in out and "+    return 3" in out
    # typografische Anführungszeichen im oldString
    f.write_text('msg = "hallo"\n', encoding="utf-8")
    call(ctx, "read", path="src/app.py")  # von außen geändert → erst neu lesen
    call(ctx, "edit", path="src/app.py", oldString="msg = “hallo”", newString='msg = "hi"')
    assert f.read_text() == 'msg = "hi"\n'
    # Einrückung vergessen: Stelle trotzdem eindeutig → newString bekommt die echte Einrückung
    f.write_text("class A:\n    def f(self):\n        return 1\n", encoding="utf-8")
    call(ctx, "read", path="src/app.py")
    call(ctx, "edit", path="src/app.py", oldString="def f(self):\n    return 1", newString="def f(self):\n    return 2")
    assert f.read_text() == "class A:\n    def f(self):\n        return 2\n"
    # aus read übernommene Zeilennummern
    call(ctx, "edit", path="src/app.py", oldString="3:         return 2", newString="3:         return 4")
    assert f.read_text().endswith("return 4\n")


def test_edit_errors_and_stale_files(ctx, project):
    f = project / "src" / "app.py"
    f.write_text("x = 1\nx = 1\n", encoding="utf-8")
    assert "2-mal" in call(ctx, "edit", path="src/app.py", oldString="x = 1", newString="x = 2")
    assert "Zeilen 1, 2" in call(ctx, "edit", path="src/app.py", oldString="x = 1", newString="x = 2")
    call(ctx, "edit", path="src/app.py", oldString="x = 1", newString="x = 2", replaceAll=True)
    assert f.read_text() == "x = 2\nx = 2\n"
    assert "nicht vor" in call(ctx, "edit", path="src/app.py", oldString="y = 3", newString="z")
    call(ctx, "read", path="src/app.py")
    import os
    import time
    f.write_text("x = 5\n", encoding="utf-8")  # von außen geändert (Editor)
    os.utime(f, ns=(time.time_ns() + 10**9, time.time_ns() + 10**9))
    assert "neu lesen" in call(ctx, "edit", path="src/app.py", oldString="x = 5", newString="x = 6")
    call(ctx, "read", path="src/app.py")
    call(ctx, "edit", path="src/app.py", oldString="x = 5", newString="x = 6")
    assert f.read_text() == "x = 6\n"
    # neue Datei per leerem oldString, CRLF bleibt erhalten
    call(ctx, "edit", path="neu.txt", oldString="", newString="a\n")
    assert (project / "neu.txt").read_text() == "a\n"
    (project / "win.txt").write_bytes(b"a\r\nb\r\n")
    call(ctx, "edit", path="win.txt", oldString="b", newString="c")
    assert (project / "win.txt").read_bytes() == b"a\r\nc\r\n"


def test_write_and_preview_show_a_diff(ctx, project):
    spec = get_tool("write")
    risk, reason = spec.assess(ctx, {"path": "src/new.py", "content": "x"})
    assert risk == CONFIRM and "erstellt" in reason
    preview = spec.preview(ctx, {"path": "README.md", "content": "# Projekt\nNeu\n"})
    assert "+Neu" in preview
    out = call(ctx, "write", path="src/new.py", content="print(1)\n")
    assert (project / "src" / "new.py").read_text() == "print(1)\n" and "Angelegt" in out
    edit_preview = get_tool("edit").preview(ctx, {"path": "src/app.py", "oldString": "return 1",
                                                  "newString": "return 9"})
    assert "-    return 1" in edit_preview and "+    return 9" in edit_preview
    assert (project / "src" / "app.py").read_text().count("return 1") == 1  # Vorschau ändert nichts


@pytest.mark.parametrize("ripgrep", [True, False])
def test_grep_and_glob_with_and_without_ripgrep(ctx, project, monkeypatch, ripgrep):
    if ripgrep and not shutil.which("rg"):
        pytest.skip("ripgrep fehlt")
    if not ripgrep:
        monkeypatch.setattr(code.shutil, "which", lambda name: None)
    out = call(ctx, "grep", pattern=r"return \d")
    assert "src/app.py" in out and "2: return 1" in out and "6: return 2" in out
    assert "Keine Treffer" in call(ctx, "grep", pattern="RETURN")
    assert "2: return 1" in call(ctx, "grep", pattern="RETURN 1", caseSensitive=False)
    assert "Keine Treffer" in call(ctx, "grep", pattern="return", include="*.md")
    files = call(ctx, "glob", pattern="**/*.py")
    assert "src/app.py" in files and "README.md" not in files


def test_shell_runs_in_project_or_workdir(ctx, project):
    assert str(project) in call(ctx, "shell", command="pwd")
    assert str(project / "src") in call(ctx, "shell", command="pwd", workdir="src")
    assert "gibt es nicht" in call(ctx, "shell", command="pwd", workdir="fehlt")


def test_websearch_uses_brave_first(ctx, monkeypatch):
    from orbwise.tools import web
    seen = []

    async def brave(key, query, n, language="de", cfg=None):
        seen.append((key, query))
        return [{"title": "Doku", "href": "https://example.org", "body": "Text"}]
    monkeypatch.setattr(web, "_brave", brave)
    ctx.cfg.tools.brave_api_key = "k"
    out = call(ctx, "websearch", query="fastapi lifespan")
    assert seen == [("k", "fastapi lifespan")] and "https://example.org" in out
