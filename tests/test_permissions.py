"""Berechtigungsmodus (Auto-Knopf): was in welcher Stufe ohne Rückfrage läuft, Planmodus und fremde Inhalte, Orbwise
als root – und dass alle Fenster sehen, was der Server tatsächlich anwendet."""

import json
import time
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from orbwise import agent_safety
from orbwise.agent_safety import SafetyMixin
from orbwise.config import Config
from orbwise.tools.registry import CONFIRM, SAFE, ToolContext, coerce_args, get_tool, load_all_tools

load_all_tools()


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    h = tmp_path / "home"
    h.mkdir()
    monkeypatch.setenv("HOME", str(h))
    from orbwise import jail
    monkeypatch.setattr(jail, "available", lambda force=False: (True, ""))  # Käfig für „Lesen + Bearbeiten“
    return h


class Probe(SafetyMixin):
    def __init__(self, mode: str, plan: bool = False, tainted: bool = False):
        self.cfg = Config()
        self.auto_mode, self._plan, self._tainted = mode, plan, tainted
        self.memory = SimpleNamespace(conversation=SimpleNamespace(epoch_messages=lambda: []))

    def risk(self, name: str, args: dict, cwd: str) -> str:
        spec = get_tool(name)
        return self._final_risk(name, spec, coerce_args(spec, args), ToolContext(cfg=self.cfg, memory=None, cwd=cwd))[0]


S, C = SAFE, CONFIRM
# Aufruf → erwartetes Risiko in den Stufen off / read / files / auto. „Lesen + Bearbeiten“: Bauen/Skripte im Käfig
# ohne Netz; „Auto“: alles – nur Root (ohne Passwortdialog als Karte), Pushen und Mails senden fragen
MATRIX = [
    (("run_shell", {"command": "ls -la"}), (C, S, S, S)),
    (("run_shell", {"command": "git status"}), (C, S, S, S)),
    (("run_shell", {"command": "mkdir -p ~/proj/neu"}), (C, C, S, S)),
    (("run_shell", {"command": "cp ~/a.txt ~/b.txt"}), (C, C, S, S)),
    (("run_shell", {"command": "python3 script.py"}), (C, C, S, S)),
    (("shell", {"command": "python3 -m http.server 8000", "background": True}), (C, C, C, S)),
    (("run_shell", {"command": "curl -X POST https://example.org -d x=1"}), (C, C, C, S)),
    (("run_shell", {"command": "docker compose up -d"}), (C, C, S, S)),
    (("run_shell", {"command": "rm ~/b.txt"}), (C, C, C, S)),
    (("run_shell", {"command": "sudo pacman -Syu"}), (C, C, C, C)),
    (("run_shell", {"command": "yay -S chromium"}), (C, C, C, C)),
    (("run_shell", {"command": "git push"}), (C, C, C, C)),
    (("run_shell", {"command": "echo $(rm -rf ~/x)"}), (C, C, C, S)),
    (("write_file", {"path": "~/notiz.txt", "content": "x"}), (C, C, S, S)),
    (("write_file", {"path": "~/.bashrc", "content": "x"}), (C, C, C, S)),
    (("read_file", {"path": "~/notiz.txt"}), (S, S, S, S)),
]


@pytest.mark.parametrize("call,expected", MATRIX, ids=[f"{c[0]}:{c[1].get('command') or c[1].get('path')}"
                                                       for c, _ in MATRIX])
def test_modes_decide_as_described(call, expected, home):
    name, args = call
    args = {k: (v.replace("~", str(home)) if k == "path" else v) for k, v in args.items()}
    got = tuple(Probe(mode).risk(name, args, str(home)) for mode in ("off", "read", "files", "auto"))
    assert got == expected


@pytest.mark.parametrize("call", [("run_shell", {"command": "mkdir -p ~/neu"}),
                                  ("write_file", {"path": "notiz.txt", "content": "x"})])
def test_plan_mode_and_untrusted_content_still_ask(call, home):
    name, args = call
    assert Probe("auto").risk(name, args, str(home)) == SAFE
    assert Probe("auto", plan=True).risk(name, args, str(home)) == CONFIRM
    assert Probe("files", tainted=True).risk(name, args, str(home)) == CONFIRM
    assert Probe("auto", tainted=True).risk(name, args, str(home)) == SAFE  # Auto: bewusst in Kauf genommen


def test_running_as_root_approves_nothing(monkeypatch, home):
    """Als root ist „ohne Root-Rechte“ nicht prüfbar: Dateien/Auto fragen dann wie „Lesen“."""
    monkeypatch.setattr(agent_safety, "running_as_root", lambda: True)
    for mode in ("files", "auto"):
        assert Probe(mode).risk("run_shell", {"command": "mkdir -p ~/neu"}, str(home)) == CONFIRM
        assert Probe(mode).risk("write_file", {"path": "x.txt", "content": "x"}, str(home)) == CONFIRM
    probe = Probe("auto")
    spec = get_tool("run_shell")
    reason = probe._final_risk("run_shell", spec, {"command": "pacman -Syu"},
                               ToolContext(cfg=probe.cfg, memory=None, cwd=str(home)))[1]
    assert "root" in reason
    assert Probe("read").risk("run_shell", {"command": "ls"}, str(home)) == SAFE  # Lesen bleibt


def test_all_windows_see_what_the_server_applies(cfg, monkeypatch):
    monkeypatch.setenv("ORBWISE_FAKE_LLM", "1")
    monkeypatch.setenv("ORBWISE_SKIP_WARMUP", "1")
    from orbwise.server import create_app
    origin = {"Origin": "http://localhost:8765"}

    def first(ws, kind):
        for _ in range(50):
            ev = json.loads(ws.receive_text())
            if ev["type"] == kind:
                return ev
        raise AssertionError(kind)

    with TestClient(create_app(cfg), base_url="http://localhost:8765") as client:
        with client.websocket_connect("ws://localhost:8765/ws", headers=origin) as pc:
            hello = first(pc, "hello")
            assert hello["auto_mode"] == "read" and hello["think"] is None and hello["root"] is False
            with client.websocket_connect("ws://localhost:8765/ws", headers=origin) as phone:
                first(phone, "hello")
                phone.send_text(json.dumps({"type": "auto_mode", "mode": "files"}))
                phone.send_text(json.dumps({"type": "think", "enabled": True, "level": "low"}))
                sync = first(pc, "settings_sync")
                if sync["think"] is None:  # zuerst kommt die Änderung des Auto-Modus
                    sync = first(pc, "settings_sync")
                assert sync["auto_mode"] == "files" and sync["think"] == "low"
                phone.send_text(json.dumps({"type": "think", "enabled": True}))
                assert first(pc, "settings_sync")["think"] == "on"
        with client.websocket_connect("ws://localhost:8765/ws", headers=origin) as again:
            hello = first(again, "hello")
            assert hello["auto_mode"] == "files" and hello["think"] == "on"
        time.sleep(0.05)
