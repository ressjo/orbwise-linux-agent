import asyncio
import json
import os
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest
from conftest import run
from fastapi.testclient import TestClient

from orbwise import askpass
from orbwise.tools import power as pw
from orbwise.tools import proc
from orbwise.tools.packages import helper_cmd, privileged
from orbwise.tools.registry import CONFIRM, SAFE, ToolContext
from orbwise.tools.safety import apply_privilege


def ctx(cfg):
    return ToolContext(cfg=cfg, memory=None)


# ---------------------------------------------------------------- Umschreiben der Befehle

@pytest.mark.parametrize("cmd,expected", [
    ("sudo shutdown now", "sudo -A shutdown now"),
    ("sudo -u root ls /root", "sudo -A -u root ls /root"),
    ("ls; sudo -n pacman -Syu", "ls; sudo -A pacman -Syu"),
    ("echo x | sudo tee /etc/f", "echo x | sudo -A tee /etc/f"),
    ("pkexec systemctl restart sshd", "sudo -A systemctl restart sshd"),
    ("ls -la", "ls -la"),
    # sudo im Befehlstext einer Shell: bekommt auch -A (sonst ohne Terminal kein Passwortdialog)
    ("bash -c 'sudo pacman -Syu'", "bash -c 'sudo -A pacman -Syu'"),
    ('sh -lc "sudo systemctl restart x && sudo ls"', 'sh -lc "sudo -A systemctl restart x && sudo -A ls"'),
    ("grep 'sudo ' /var/log/auth.log", "grep 'sudo ' /var/log/auth.log"),  # nur Text, kein Befehl
    ('echo "sudo is fine"', 'echo "sudo is fine"'),
])
def test_apply_privilege_dashboard(cmd, expected):
    assert apply_privilege(cmd, "dashboard") == expected


def test_default_mode_and_package_helpers(cfg):
    assert cfg.tools.privilege_cmd == "dashboard"
    assert privileged(ctx(cfg), ["pacman", "-Syu"]) == ["sudo", "-A", "pacman", "-Syu"]
    assert helper_cmd(ctx(cfg), "yay", "-Sua")[1:3] == ["--sudoflags", "-A"]
    cfg.tools.privilege_cmd = "pkexec"
    assert privileged(ctx(cfg), ["pacman", "-Syu"])[0] == "pkexec"


# ---------------------------------------------------------------- Umgebung & Token-Lebensdauer

def test_proc_run_grants_token_only_for_sudo_a(cfg, tmp_path, monkeypatch):
    helper = askpass.write_helper(tmp_path / "rt" / "orbwise" / "askpass")
    assert oct(helper.stat().st_mode)[-3:] == "700"
    broker = askpass.AskpassBroker(8765, helper=helper)
    monkeypatch.setattr(askpass, "BROKER", broker)
    rc, out = run(proc.run(ctx(cfg), ': sudo -A ; echo "$SUDO_ASKPASS|${ORBWISE_ASKPASS_TOKEN:+token}"',
                           timeout=10, stream=False))
    assert out.strip() == f"{helper}|token"
    assert broker.tokens == {}  # nach dem Befehl verfallen
    rc, out = run(proc.run(ctx(cfg), 'echo "${ORBWISE_ASKPASS_TOKEN:-keins}"', timeout=10, stream=False))
    assert out.strip() == "keins"


def test_root_denied_hint():
    out = proc.format_result(1, "sudo: no password was provided\nsudo: a password is required", 1000)
    assert "NICHT erteilt" in out and "Nicht mit anderen Befehlen" in out
    assert "NICHT erteilt" not in proc.format_result(0, "ok", 1000)
    assert "NICHT erteilt" not in proc.format_result(1, "No such file", 1000)


def test_sudo_without_prompt_is_not_a_refusal():
    """sudo in einem Skript ohne -A: kein Nein des Nutzers – das Modell soll es mit sudo vorne erneut versuchen."""
    for msg in ("sudo: a terminal is required to read the password; either use the -S option to read from standard "
                "input or configure an askpass helper\nsudo: a password is required",
                "sudo: no askpass program specified, try setting SUDO_ASKPASS"):
        out = proc.format_result(1, msg, 1000)
        assert "nicht nach dem Passwort fragen" in out and "NICHT erteilt" not in out


# ---------------------------------------------------------------- Helfer-Skript (so wie sudo es aufruft)

def test_helper_script_prints_password_and_ignores_proxy(tmp_path):
    seen = {}

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            seen["token"] = self.headers.get("X-Orbwise-Askpass")
            seen["body"] = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            body = json.dumps({"password": "geh eim!"}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.handle_request, daemon=True).start()
    helper = askpass.write_helper(tmp_path / "askpass")
    env = {**os.environ, "ORBWISE_ASKPASS_URL": f"http://127.0.0.1:{srv.server_port}/api/askpass",
           "ORBWISE_ASKPASS_TOKEN": "tok123", "http_proxy": "http://proxy.invalid:1", "HTTP_PROXY": "http://proxy.invalid:1"}
    res = subprocess.run([str(helper), "[sudo] Passwort für alex: "], env=env, capture_output=True, text=True,
                         timeout=20)
    assert res.returncode == 0 and res.stdout == "geh eim!\n"
    assert seen == {"token": "tok123", "body": {"prompt": "[sudo] Passwort für alex: ", "sudo_command": ""}}
    # ohne Token: sofort Fehler (sudo bricht dann ab)
    env.pop("ORBWISE_ASKPASS_TOKEN")
    assert subprocess.run([str(helper)], env=env, capture_output=True, timeout=20).returncode == 1


# ---------------------------------------------------------------- Server: Passwortfeld in der Oberfläche

@pytest.fixture
def app_client(cfg, monkeypatch, tmp_path):
    monkeypatch.setenv("ORBWISE_FAKE_LLM", "1")
    monkeypatch.setenv("ORBWISE_SKIP_WARMUP", "1")
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "run"))
    from orbwise.server import create_app
    return TestClient(create_app(cfg), base_url="http://localhost:8765")


def _next(ws, kind):
    for _ in range(50):
        ev = ws.receive_json()
        if ev["type"] == kind:
            return ev
    raise AssertionError(kind)


def test_password_roundtrip_via_ui(app_client, caplog, tmp_path):
    with app_client as client:
        broker = askpass.BROKER
        assert broker.ready() and broker.helper == tmp_path / "run" / "orbwise" / "askpass"
        # falsches Token → abgelehnt, ohne die Oberfläche zu fragen
        r = client.post("/api/askpass", json={"prompt": "x"}, headers={"X-Orbwise-Askpass": "falsch"})
        assert r.status_code == 403
        with client.websocket_connect("ws://localhost:8765/ws", headers={"Origin": "http://localhost:8765"}) as ws:
            ws.receive_json()  # hello
            with broker.grant("sudo -A systemctl poweroff") as env:
                token = env["ORBWISE_ASKPASS_TOKEN"]
                result = {}

                def helper_call():
                    result["r"] = client.post("/api/askpass", json={"prompt": "[sudo] Passwort"},
                                              headers={"X-Orbwise-Askpass": token})

                t = threading.Thread(target=helper_call)
                t.start()
                ev = _next(ws, "password_request")
                assert ev["command"] == "sudo -A systemctl poweroff" and not ev["retry"]
                ws.send_json({"type": "password", "id": ev["id"], "password": "Sehr-Geheim-42"})
                t.join(10)
                assert result["r"].status_code == 200 and result["r"].json() == {"password": "Sehr-Geheim-42"}
                assert result["r"].headers["cache-control"] == "no-store"
                assert _next(ws, "password_done")["id"] == ev["id"]

                # zweiter Versuch (falsches Passwort) → Abbruch in der Oberfläche
                t = threading.Thread(target=helper_call)
                t.start()
                ev = _next(ws, "password_request")
                assert ev["retry"]
                ws.send_json({"type": "password_cancel", "id": ev["id"]})
                t.join(10)
                assert result["r"].status_code == 403
            # nach Befehlsende ist das Token ungültig
            r = client.post("/api/askpass", json={}, headers={"X-Orbwise-Askpass": token})
            assert r.status_code == 403
    assert "Sehr-Geheim-42" not in caplog.text
    hist = json.dumps(client.app.state.memory.conversation.history)
    assert "Sehr-Geheim-42" not in hist


def test_no_ui_open_means_denied(app_client):
    with app_client as client:
        with askpass.BROKER.grant("sudo -A ls") as env:
            r = client.post("/api/askpass", json={}, headers={"X-Orbwise-Askpass": env["ORBWISE_ASKPASS_TOKEN"]})
            assert r.status_code == 403


# ---------------------------------------------------------------- Power-Tool

class Calls(list):
    replies: list


@pytest.fixture
def calls(monkeypatch):
    seen = Calls()
    replies = []

    async def fake_run(ctx, cmd, timeout, stream=True, cwd=None):
        seen.append(cmd)
        return replies.pop(0) if replies else (0, "")

    monkeypatch.setattr(pw.proc, "run", fake_run)
    monkeypatch.setattr(pw, "START_DELAY", 0)
    seen.replies = replies
    return seen


def test_power_risks(cfg):
    assert pw._risk(ctx(cfg), {"action": "poweroff"})[0] == CONFIRM
    assert pw._risk(ctx(cfg), {"action": "Neustart"})[0] == CONFIRM
    assert pw._risk(ctx(cfg), {"action": "lock"})[0] == SAFE
    assert "in 10 Minuten" in pw._risk(ctx(cfg), {"action": "shutdown", "delay_minutes": 10})[1]


def test_power_poweroff_runs_without_root_after_delay(cfg, calls):
    import asyncio

    async def go():
        out = await pw.power(ctx(cfg), "shutdown")
        await asyncio.sleep(0.05)
        return out

    out = run(go())
    assert "heruntergefahren" in out
    assert calls == [["systemctl", "poweroff"]]


def test_power_fallback_to_password_and_delay(cfg, calls):
    calls.replies.extend([(1, "Access denied"), (0, "")])
    out = run(pw.power(ctx(cfg), "poweroff", delay_minutes=10))
    assert calls == [["shutdown", "-h", "+10"], ["sudo", "-A", "shutdown", "-h", "+10"]]
    assert "in 10 Minuten heruntergefahren" in out
    calls.clear()
    assert run(pw.power(ctx(cfg), "lock")) == "Erledigt: den Bildschirm sperren."
    assert calls == [["loginctl", "lock-session"]]
    assert "Unbekannte Aktion" in run(pw.power(ctx(cfg), "explodieren"))


# ---------------------------------------------------------------- Passwort eine Zeit lang merken

def _broker(tmp_path, seconds=900):
    helper = tmp_path / "askpass"
    helper.write_text("#!/bin/sh\n")
    asked: list[dict] = []

    async def notify(ev):
        if ev["type"] == "password_request":
            asked.append(ev)

    broker = askpass.AskpassBroker(8765, notify=notify, helper=helper, remember_seconds=seconds)
    return broker, asked


async def _sudo(broker, typed: list, accepted: str, remember=True, remote=False):
    """Simuliert sudo -A: fragt so lange, bis das Passwort stimmt (max. 3×). Liefert die Zahl der Anfragen."""
    token_var = askpass.REMOTE.set(remote)
    try:
        with broker.grant("sudo -A pacman -Syu") as env:
            token = env["ORBWISE_ASKPASS_TOKEN"]
            for _ in range(askpass.MAX_USES):
                task = asyncio.create_task(broker.request(token))
                await asyncio.sleep(0)
                if broker.pending:
                    broker.answer(next(iter(broker.pending)), typed.pop(0) if typed else None, remember)
                pw = await task
                if pw is None or pw == accepted:
                    return pw
            return None
    finally:
        askpass.REMOTE.reset(token_var)


def test_password_is_remembered_after_sudo_accepted_it(tmp_path):
    async def scenario():
        broker, asked = _broker(tmp_path)
        assert await _sudo(broker, ["falsch", "richtig"], "richtig") == "richtig"
        assert len(asked) == 2 and asked[0]["remember"] == 15 and broker.cached_until()
        # zweiter Root-Befehl: kein Dialog mehr
        assert await _sudo(broker, [], "richtig") == "richtig" and len(asked) == 2
        # Telegram/Routinen: gemerktes Passwort gilt nicht – dort wird gefragt
        await _sudo(broker, ["richtig"], "richtig", remote=True)
        assert len(asked) == 3 and asked[2]["remember"] == 0
        broker.forget()
        assert broker.cached_until() is None
        await _sudo(broker, ["richtig"], "richtig")
        assert len(asked) == 4

    run_async(scenario())


def test_changed_password_is_forgotten_and_asked_again(tmp_path):
    async def scenario():
        broker, asked = _broker(tmp_path)
        await _sudo(broker, ["alt"], "alt")
        assert broker.cached_until()
        assert await _sudo(broker, ["neu"], "neu") == "neu"  # gemerktes „alt“ abgelehnt → Dialog
        assert len(asked) == 2 and not asked[1]["retry"]  # kein „falsches Passwort“ – der Nutzer hat nichts falsch getippt
        assert await _sudo(broker, [], "neu") == "neu" and len(asked) == 2  # jetzt ist „neu“ gemerkt

    run_async(scenario())


def test_not_remembered_when_unticked_disabled_or_expired(tmp_path):
    async def scenario():
        broker, _ = _broker(tmp_path)
        await _sudo(broker, ["pw"], "pw", remember=False)
        assert broker.cached_until() is None
        off, asked = _broker(tmp_path, seconds=0)
        await _sudo(off, ["pw"], "pw")
        assert off.cached_until() is None and asked[0]["remember"] == 0
        short, _ = _broker(tmp_path, seconds=1)
        await _sudo(short, ["pw"], "pw")
        assert short.cached_until()
        short._cached = ("pw", 0)  # abgelaufen
        assert short.cached_until() is None
        three, _ = _broker(tmp_path)  # dreimal falsch: nichts merken
        assert await _sudo(three, ["a", "b", "c"], "richtig") is None and three.cached_until() is None

    run_async(scenario())


def run_async(coro):
    asyncio.run(coro)


# ---------------------------------------------------------------- sudo aus Skripten (Wrapper im PATH)

FAKE_SUDO = """#!/bin/sh
echo "FAKE-SUDO $* token=${ORBWISE_ASKPASS_TOKEN:+ja} inner=$ORBWISE_SUDO_CMD"
"""


@pytest.fixture
def fake_sudo(tmp_path, monkeypatch):
    """Ein „sudo“, das nur seine Argumente ausgibt – vorn im PATH, damit der Wrapper es als echtes sudo findet."""
    folder = tmp_path / "realbin"
    folder.mkdir()
    (folder / "sudo").write_text(FAKE_SUDO)
    (folder / "sudo").chmod(0o755)
    monkeypatch.setenv("PATH", f"{folder}{os.pathsep}{os.environ['PATH']}")
    return folder / "sudo"


def _wrapper(tmp_path, fake_sudo):
    target = askpass.write_wrapper(tmp_path / "bin")
    assert target and str(fake_sudo) in target.read_text()
    return target


@pytest.mark.parametrize("args, expected", [
    (["pacman", "-Syu"], "-A pacman -Syu"),
    (["-u", "root", "id"], "-A -u root id"),
    (["-E", "make", "install"], "-A -E make install"),
    (["-n", "true"], "-n true"),          # nicht-interaktiv gewollt: so lassen
    (["-S", "ls"], "-S ls"),              # Passwort von stdin gewollt: so lassen
    (["-nE", "ls"], "-nE ls"),
    (["-A", "ls"], "-A ls"),              # schon -A: nicht doppelt
    (["ls", "-n"], "-A ls -n"),           # -n gehört hier zu ls, nicht zu sudo
])
def test_wrapper_adds_askpass_only_with_token(tmp_path, fake_sudo, args, expected):
    wrapper = _wrapper(tmp_path, fake_sudo)
    env = {**os.environ, "ORBWISE_ASKPASS_TOKEN": "t"}
    out = subprocess.run([str(wrapper), *args], env=env, capture_output=True, text=True, timeout=10).stdout
    assert out.startswith(f"FAKE-SUDO {expected} token=ja inner={' '.join(args)}")
    # ohne Token (z. B. du selbst im Terminal): unverändert
    env.pop("ORBWISE_ASKPASS_TOKEN")
    out = subprocess.run([str(wrapper), *args], env=env, capture_output=True, text=True, timeout=10).stdout
    assert out.startswith(f"FAKE-SUDO {' '.join(args)} token= inner=")


def test_no_wrapper_without_sudo(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", str(tmp_path / "leer"))
    assert askpass.write_wrapper(tmp_path / "bin") is None and not (tmp_path / "bin" / "sudo").exists()


def test_user_shell_gives_scripts_the_password_dialog(cfg, tmp_path, monkeypatch, fake_sudo):
    """Ein Skript ruft sudo selbst auf (ohne sudo im Befehl): es bekommt -A, ein Token und nie das gemerkte
    Passwort; interne Werkzeugaufrufe (proc.run) bleiben, wie sie waren."""
    helper = askpass.write_helper(tmp_path / "rt" / "orbwise" / "askpass")
    broker = askpass.AskpassBroker(8765, helper=helper)
    monkeypatch.setattr(askpass, "BROKER", broker)
    script = tmp_path / "install.sh"
    script.write_text("#!/bin/sh\nsudo pacman -S htop\n")
    script.chmod(0o755)
    seen = []
    original = broker.grant

    def spy(command, use_cache=True, implicit=False):
        seen.append((command, use_cache, implicit))
        return original(command, use_cache, implicit)
    monkeypatch.setattr(broker, "grant", spy)
    rc, out = run(proc.run_user_shell(ctx(cfg), str(script), timeout=10))
    assert "FAKE-SUDO -A pacman -S htop token=ja inner=pacman -S htop" in out
    assert seen == [(str(script), False, True)] and broker.tokens == {}
    # sichtbares sudo -A im Befehl: wie bisher (gemerktes Passwort erlaubt)
    seen.clear()
    run(proc.run_user_shell(ctx(cfg), "sudo -A true", timeout=10))
    assert seen == [("sudo -A true", True, False)]
    # interner Aufruf ohne Wrapper: kein Token, sudo bleibt sudo
    rc, out = run(proc.run(ctx(cfg), str(script), timeout=10, stream=False))
    assert "FAKE-SUDO pacman -S htop token= inner=" in out


def test_implicit_sudo_needs_a_verified_caller_and_shows_the_inner_command(tmp_path):
    async def scenario():
        asked = []

        async def notify(ev):
            if ev["type"] == "password_request":
                asked.append(ev)
                broker.answer(ev["id"], "pw", False)

        broker = askpass.AskpassBroker(8765, notify=notify, helper=askpass.write_helper(tmp_path / "askpass"))
        with broker.grant("./install.sh", use_cache=False, implicit=True) as env:
            token = env["ORBWISE_ASKPASS_TOKEN"]
            # ein Skript fragt selbst mit dem Token an (kein sudo dahinter) → abgelehnt, ohne Dialog
            assert await broker.request(token, caller=lambda: False) is None
            # nicht feststellbar, wer fragt → bei sudo aus Skripten ebenfalls abgelehnt
            assert await broker.request(token, caller=lambda: None) is None
            assert asked == []
            # echtes sudo über den Helfer → Dialog mit dem, was wirklich Root bekommt
            assert await broker.request(token, "[sudo] pw", "pacman -S htop", caller=lambda: True) == "pw"
            assert asked[0]["command"] == "./install.sh\n→ sudo pacman -S htop"
        with broker.grant("sudo -A pacman -Syu") as env:  # sichtbares sudo: „unklar“ reicht wie bisher
            assert await broker.request(env["ORBWISE_ASKPASS_TOKEN"], "", "pacman -Syu", caller=lambda: None) == "pw"
            assert asked[1]["command"] == "sudo -A pacman -Syu"  # nichts doppelt angehängt

    run_async(scenario())


def test_caller_check_finds_the_helper_under_sudo(tmp_path):
    """Echte Prozesskette: der Helfer verbindet sich mit einem Test-Server; der Server prüft, wem die Verbindung
    gehört. Direkt gestartet (Elternprozess pytest) → nein; unter einem Elternprozess namens sudo nur, wenn dieser
    als root läuft (setuid wie das echte sudo)."""
    helper = askpass.write_helper(tmp_path / "askpass")
    verdicts = []

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            self.rfile.read(int(self.headers["Content-Length"]))
            verdicts.append(askpass.caller_is_sudo_helper(self.client_address[1], srv.server_port, helper))
            body = b'{"password": "x"}'
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    env = {**os.environ, "ORBWISE_ASKPASS_URL": f"http://127.0.0.1:{srv.server_port}/api/askpass",
           "ORBWISE_ASKPASS_TOKEN": "t", "NO_PROXY": "127.0.0.1"}
    try:
        subprocess.run([str(helper)], env=env, capture_output=True, timeout=20)
        parent = tmp_path / "sudo"  # Prozessname „sudo“ – wie sudo, das den Helfer startet
        parent.write_text(f"#!/bin/sh\n{helper} prompt\n")
        parent.chmod(0o755)
        subprocess.run([str(parent)], env=env, capture_output=True, timeout=20)
    finally:
        srv.shutdown()
    assert verdicts == [False, os.geteuid() == 0]
