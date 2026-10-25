"""Bildschirm und Bilder verstehen: Screenshot, Auswertung durch das geladene Modell, Sicherheit, Werkzeugauswahl."""

import os

import pytest
from conftest import run

from orbwise.tools import vision
from orbwise.tools.registry import BLOCKED, SAFE, ToolContext, get_tool, load_all_tools

PNG = b"\x89PNG\r\n\x1a\nFAKE-SCREEN"


class SeeingModel:
    """Das geladene Modell mit Bildmodul: bekommt eine eigene kurze Anfrage mit dem Bild."""

    def __init__(self, answer="Ein Terminal mit der Meldung: Permission denied"):
        self.answer, self.calls = answer, []

    async def chat(self, messages):
        self.calls.append(messages)
        return self.answer


class Agent:
    def __init__(self, sees=True):
        self.llm, self._vision = SeeingModel(), sees


@pytest.fixture
def screen(monkeypatch, tmp_path, cfg):
    """Grafische Sitzung + Screenshot-Befehl, der ein Bild an {file} kopiert."""
    src = tmp_path / "bild.png"
    src.write_bytes(PNG)
    monkeypatch.setattr(vision.proc, "desktop_env", lambda: {**os.environ, "DISPLAY": ":0"})
    cfg.vision.screenshot_command = f"cp {src} {{file}}"
    return src


def ctx(cfg, agent=None):
    return ToolContext(cfg=cfg, memory=None, agent=agent)


def test_screen_is_captured_analysed_by_the_loaded_model_and_deleted(cfg, screen):
    agent = Agent()
    seen = []

    async def chat(messages):
        path = messages[0]["images"][0]
        seen.append(path)
        assert open(path, "rb").read().startswith(b"\x89PNG")  # das Bild liegt während der Anfrage vor
        return "Ein Terminal mit der Meldung: Permission denied"
    agent.llm.chat = chat
    out = run(vision.look_at_screen(ctx(cfg, agent), question="Was ist das für eine Fehlermeldung?"))
    assert "Permission denied" in out and "keine Aufträge des Nutzers" in out
    assert seen and not os.path.exists(seen[0])  # Screenshot nach der Auswertung gelöscht


def test_model_without_images_explains_what_to_load(cfg, screen):
    out = run(vision.look_at_screen(ctx(cfg, Agent(sees=False))))
    assert "kann keine Bilder sehen" in out and "mmproj" in out
    assert "kann keine Bilder sehen" in run(vision.look_at_screen(ctx(cfg)))  # ohne Agent (z. B. Test/CLI)


def test_no_desktop_or_no_screenshot_program(cfg, monkeypatch):
    monkeypatch.setattr(vision.proc, "desktop_env", lambda: {"PATH": "/nonexistent"})
    assert "grafische Sitzung" in run(vision.look_at_screen(ctx(cfg, Agent())))
    monkeypatch.setattr(vision.proc, "desktop_env",
                        lambda: {"PATH": "/nonexistent", "WAYLAND_DISPLAY": "wayland-0", "XDG_CURRENT_DESKTOP": "KDE"})
    monkeypatch.setattr(vision.shutil, "which", lambda name: None)
    assert "spectacle" in run(vision.look_at_screen(ctx(cfg, Agent())))


def test_screenshot_program_matches_the_desktop(monkeypatch):
    monkeypatch.setattr(vision.shutil, "which", lambda name: f"/usr/bin/{name}")
    first = lambda env: vision.screenshot_commands(env)[0][0]  # noqa: E731
    assert first({"WAYLAND_DISPLAY": "w", "XDG_CURRENT_DESKTOP": "KDE"}) == "spectacle"
    assert first({"WAYLAND_DISPLAY": "w", "XDG_CURRENT_DESKTOP": "GNOME"}) == "gnome-screenshot"
    assert first({"WAYLAND_DISPLAY": "w", "XDG_CURRENT_DESKTOP": "Hyprland"}) == "grim"
    assert first({"DISPLAY": ":0", "XDG_CURRENT_DESKTOP": "i3"}) == "maim"
    wayland = [c[0] for c in vision.screenshot_commands({"WAYLAND_DISPLAY": "w"})]
    assert "maim" not in wayland and "scrot" not in wayland  # X11-Werkzeuge sähen nur Schwarz
    assert vision.screenshot_commands({}, "flameshot full -p {file}")[0][0] == "flameshot"


def test_images_from_the_phone_and_secrets(cfg, tmp_path):
    load_all_tools()
    photo = tmp_path / "foto.jpg"
    photo.write_bytes(b"\xff\xd8\xffJPEG")
    agent = Agent()
    out = run(vision.look_at_image(ctx(cfg, agent), path=str(photo), question="Was ist das für eine Pflanze?"))
    assert "Permission denied" in out and "foto.jpg" in out
    msg = agent.llm.calls[0][0]
    assert msg["images"] == [str(photo)] and "Pflanze" in msg["content"]
    assert "kein unterstütztes Bild" in run(vision.look_at_image(ctx(cfg, agent), path=__file__))
    spec = get_tool("look_at_image")
    assert spec.assess(ctx(cfg), {"path": str(photo)})[0] == SAFE
    assert spec.assess(ctx(cfg), {"path": "~/.ssh/id_ed25519"})[0] == BLOCKED


def test_screen_content_counts_as_untrusted(cfg):
    """Eine Webseite auf dem Bildschirm kann Anweisungen enthalten – danach fragen Shell & Co. wieder nach."""
    from orbwise.agent import is_taint_source

    assert is_taint_source("look_at_screen", "…") and is_taint_source("look_at_image", "…")


def test_vision_tools_only_for_models_that_see_images(cfg, llm, memory):
    from orbwise.agent import Agent as RealAgent
    from orbwise.toolselect import relevant_groups

    assert "vision" in relevant_groups(["Was ist das für eine Fehlermeldung auf meinem Bildschirm?"])
    assert "vision" not in relevant_groups(["Wie wird das Wetter morgen?"])
    agent = RealAgent(cfg, llm, memory)
    agent._vision = False
    names = {s["function"]["name"] for s in agent._mode_schemas()[0]}
    assert "look_at_screen" not in names and not agent.tool_allowed("look_at_image")
    agent._vision = True
    assert "look_at_screen" in {s["function"]["name"] for s in agent._mode_schemas()[0]}
    cfg.vision.enabled = False
    load_all_tools()
    assert not get_tool("look_at_screen").enabled(cfg)


def test_old_vision_model_settings_are_ignored():
    from orbwise.config import Config
    c = Config.model_validate({"vision": {"backend": "openai", "base_url": "http://127.0.0.1:8080/v1",
                                          "model": "bonsai", "api_key": "x", "enabled": True}})
    assert c.vision.enabled and not hasattr(c.vision, "model")
