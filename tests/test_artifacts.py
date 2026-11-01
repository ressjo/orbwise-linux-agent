"""Artefakte: Diagramme & Co. landen in einem Temp-Ordner, den die Einstellungen anzeigen und leeren; Bilder zeigt
der Agent direkt im Chat (show_image, frisch erzeugte auch per open_file) statt sie extern zu öffnen."""

import os
import time

from conftest import run
from fastapi.testclient import TestClient

from orbwise import artifacts, attachments, prompts
from orbwise.tools import files
from orbwise.tools.registry import ToolContext

PNG = b"\x89PNG\r\n\x1a\n" + b"\0" * 64


class FakeAgent:
    _shots: list = []


def make_ctx(cfg):
    events = []

    async def emit(ev):
        events.append(ev)
    return ToolContext(cfg=cfg, memory=None, emit=emit, call_id="c1", agent=FakeAgent()), events


def test_usage_and_clear(cfg):
    assert artifacts.usage(cfg) == {"path": str(artifacts.folder(cfg)), "bytes": 0, "files": 0}
    root = artifacts.ensure(cfg)
    (root / "plot.png").write_bytes(PNG)
    (root / "daten").mkdir()
    (root / "daten" / "export.csv").write_text("a,b\n1,2\n")
    assert artifacts.usage(cfg)["files"] == 2 and artifacts.usage(cfg)["bytes"] == len(PNG) + 8
    assert artifacts.clear(cfg)["files"] == 2
    assert root.is_dir() and artifacts.usage(cfg)["files"] == 0


def test_show_image_puts_it_under_the_answer(cfg, tmp_path):
    ctx, events = make_ctx(cfg)
    root = artifacts.ensure(cfg)
    (root / "umsatz.png").write_bytes(PNG)
    out = run(files.show_image(ctx, "umsatz.png"))  # bloßer Name: zuerst im Artefakt-Ordner
    assert out.startswith("Wird dem Nutzer im Chat gezeigt") and str(root / "umsatz.png") in out
    name = ctx.agent._shots[-1]
    assert events == [{"type": "tool_images", "id": "c1", "images": [name]}]
    assert attachments.path_of(cfg, name).read_bytes() == PNG  # Kopie bei den Anhängen – bleibt beim Leeren
    artifacts.clear(cfg)
    assert attachments.path_of(cfg, name).exists()
    (tmp_path / "notiz.txt").write_text("kein Bild")
    assert "kein Bild" in run(files.show_image(ctx, str(tmp_path / "notiz.txt")))
    assert "Nicht gefunden" in run(files.show_image(ctx, str(tmp_path / "fehlt.png")))


def test_open_file_shows_fresh_images_in_the_chat(cfg, tmp_path, monkeypatch):
    from orbwise.tools import proc
    calls = []

    async def fake_launch(argv, wait=2.0):
        calls.append(argv)
        return True, ""
    monkeypatch.setattr(proc, "launch", fake_launch)
    monkeypatch.setattr(files.shutil, "which", lambda n: f"/usr/bin/{n}")
    ctx, events = make_ctx(cfg)
    chart = tmp_path / "diagramm.png"
    chart.write_bytes(PNG)
    assert run(files.open_file(ctx, str(chart))).startswith("Im Chat gezeigt") and not calls
    assert events[-1]["type"] == "tool_images"
    old = tmp_path / "urlaub.png"  # altes Foto: wie gewünscht im Bildbetrachter
    old.write_bytes(PNG)
    past = time.time() - 3600
    os.utime(old, (past, past))
    assert run(files.open_file(ctx, str(old))) == f"Geöffnet: {old}" and calls[-1] == ["xdg-open", str(old)]
    assert run(files.open_file(ctx, str(chart), app="")).startswith("Im Chat")
    no_ui = ToolContext(cfg=cfg, memory=None)  # ohne Oberfläche (Telegram, CLI): wie bisher öffnen
    assert run(files.open_file(no_ui, str(chart))) == f"Geöffnet: {chart}"


def test_prompt_names_the_folder(cfg):
    hint = prompts.hints(cfg, {"show_image"})
    assert str(artifacts.folder(cfg)) in hint and "show_image" in hint
    assert str(artifacts.folder(cfg)) not in prompts.hints(cfg, {"read_file"})


def test_artifacts_endpoints(cfg, monkeypatch):
    monkeypatch.setenv("ORBWISE_FAKE_LLM", "1")
    monkeypatch.setenv("ORBWISE_SKIP_WARMUP", "1")
    from orbwise.server import create_app
    (artifacts.ensure(cfg) / "plot.png").write_bytes(PNG)
    with TestClient(create_app(cfg), base_url="http://localhost:8765") as client:
        assert client.get("/api/artifacts").json()["files"] == 1
        r = client.delete("/api/artifacts").json()
        assert r["freed"] == len(PNG) and r["removed"] == 1 and r["files"] == 0
