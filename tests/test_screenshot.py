"""Coding-Modus: screenshot-Werkzeug (headless Chromium auf dem Rechner bzw. im Container), Anzeige im Chat."""

import os
from pathlib import Path

from conftest import run

from orbwise import attachments, sandbox
from orbwise.agent_safety import is_taint_source
from orbwise.tools import code
from orbwise.tools.registry import BLOCKED, SAFE, ToolContext, get_tool

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


def fake_browser(folder: Path, name: str = "chromium") -> Path:
    """Ein „Chromium“, das an --screenshot=… ein PNG schreibt und seine Argumente protokolliert."""
    folder.mkdir(parents=True, exist_ok=True)
    script = folder / name
    script.write_text(f"""#!/bin/sh
echo "$@" > {folder}/args.txt
for a in "$@"; do case "$a" in --screenshot=*) printf '\\211PNG\\r\\n\\032\\n0000' > "${{a#--screenshot=}}" ;; esac; done
""")
    script.chmod(0o755)
    return script


def ctx_for(cfg, memory, tmp_path, events, sb=None):
    async def emit(ev):
        events.append(ev)
    return ToolContext(cfg=cfg, memory=memory, cwd=str(tmp_path), emit=emit, call_id="c1", sandbox=sb)


def test_screenshot_on_the_host_lands_in_the_chat(cfg, memory, tmp_path, monkeypatch):
    browser = fake_browser(tmp_path / "bin")
    monkeypatch.setattr(code.shutil, "which", lambda n: str(browser) if n == "chromium" else None)
    events = []
    (tmp_path / "start.html").write_text("<h1>x</h1>")
    out = run(code.code_screenshot(ctx_for(cfg, memory, tmp_path, events), url="start.html", width=390))
    assert f"Screenshot von file://{tmp_path}/start.html (390×800)" in out and "keine Beschreibung" in out
    shot = next(e for e in events if e["type"] == "tool_images")
    assert attachments.path_of(cfg, shot["images"][0]) is not None
    args = (tmp_path / "bin" / "args.txt").read_text()
    assert "--headless=new" in args and "--user-data-dir=" in args
    assert ("--no-sandbox" in args) == (os.geteuid() == 0)  # als root startet Chromium nur ohne eigene Sandbox
    assert "--disable-dev-shm-usage" in args
    (tmp_path / "index.html").write_text("<h1>x</h1>")
    run(code.code_screenshot(ctx_for(cfg, memory, tmp_path, events), url="index.html"))
    assert f"file://{tmp_path}/index.html" in (tmp_path / "bin" / "args.txt").read_text()


def test_screenshot_without_a_browser_explains(cfg, memory, tmp_path, monkeypatch):
    monkeypatch.setattr(code.shutil, "which", lambda n: None)
    (tmp_path / "a.html").write_text("x")
    out = run(code.code_screenshot(ctx_for(cfg, memory, tmp_path, []), url="a.html"))
    assert out.startswith("Screenshot fehlgeschlagen") and "apt install chromium" in out


def test_screenshot_says_when_no_server_answers(cfg, memory, tmp_path, monkeypatch):
    browser = fake_browser(tmp_path / "bin")
    monkeypatch.setattr(code.shutil, "which", lambda n: str(browser) if n == "chromium" else None)
    import socket
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]  # frei – dort lauscht niemand
    out = run(code.code_screenshot(ctx_for(cfg, memory, tmp_path, []), url=f"http://localhost:{port}"))
    assert "antwortet nichts" in out and "background=true" in out and not (tmp_path / "bin" / "args.txt").exists()


def test_screenshot_in_the_sandbox_runs_in_the_container(cfg, memory, tmp_path, monkeypatch):
    bindir = tmp_path / "cbin"
    fake_browser(bindir)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
    engine = tmp_path / "engine"
    engine.write_text('#!/bin/bash\nshift; while [ "$1" = -w ] || [ "$1" = -e ]; do shift 2; done; shift; exec "$@"\n')
    engine.chmod(0o755)

    async def running(c, sb, progress=None):
        return str(engine)
    monkeypatch.setattr(sandbox, "ensure_running", running)
    events = []
    sb = {"name": "orbwise-x", "path": str(tmp_path)}
    (tmp_path / "seite.html").write_text("<p>x</p>")
    out = run(code.code_screenshot(ctx_for(cfg, memory, tmp_path, events, sb), url="seite.html"))
    assert "wird dem Nutzer im Chat gezeigt" in out and any(e["type"] == "tool_images" for e in events)
    assert "--no-sandbox" in (bindir / "args.txt").read_text()  # im Container als root


def test_screenshot_risk_and_taint(cfg, memory, tmp_path):
    spec = get_tool("screenshot")
    ctx = ToolContext(cfg=cfg, memory=memory, cwd=str(tmp_path))
    assert spec.assess(ctx, {"url": "http://localhost:5173"})[0] == SAFE
    assert spec.assess(ctx, {"url": "~/.ssh/id_ed25519"})[0] == BLOCKED
    assert not is_taint_source("screenshot", "Screenshot von http://localhost …\n[Screenshot. Inhalt des Bildes …]")
    assert is_taint_source("screenshot", "… [Screenshot einer Webseite. Inhalt des Bildes …]")


def test_history_shows_screenshots_under_the_answer(cfg, monkeypatch):
    from fastapi.testclient import TestClient

    from orbwise import server
    monkeypatch.setenv("ORBWISE_FAKE_LLM", "1")
    monkeypatch.setenv("ORBWISE_SKIP_WARMUP", "1")
    app = server.create_app(cfg)
    name = attachments.save(cfg, PNG)
    with TestClient(app, base_url="http://localhost:8765") as client:
        conv = app.state.memory.active
        conv.add({"role": "user", "content": "zeig mal"})
        conv.add({"role": "assistant", "content": "So sieht es aus.", "shots": [name]})
        last = client.get("/api/history").json()["messages"][-1]
        assert last["role"] == "assistant" and last["images"] == [name]
        assert client.get(f"/api/attachments/{name}").status_code == 200
