"""Zugangs-Token für Oberfläche und API (web_auth.py): ohne Token nur Anmeldeseite bzw. 401, Einmal-Link setzt das
Cookie, eigene Schlüssel (/v1, Askpass) und Dateien der Oberfläche bleiben frei."""

import os
import stat

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from orbwise import web_auth

BASE = "http://localhost:8765"


@pytest.fixture
def app(cfg, monkeypatch):
    from orbwise import server
    monkeypatch.setenv("ORBWISE_FAKE_LLM", "1")
    monkeypatch.setenv("ORBWISE_SKIP_WARMUP", "1")
    return server.create_app(cfg)


def test_token_file_is_private_and_stays(tmp_path):
    path = tmp_path / "cfg" / "web-token"
    token = web_auth.load_or_create(path)
    assert len(token) >= 32 and stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    os.chmod(path, 0o644)
    assert web_auth.load_or_create(path) == token and stat.S_IMODE(path.stat().st_mode) == 0o600


def test_without_token_only_the_sign_in_page(app):
    with TestClient(app, base_url=BASE, anonymous=True) as c:
        page = c.get("/")
        assert page.status_code == 401 and "orbwise open" in page.text and "<form" in page.text
        assert c.get("/api/status").status_code == 401
        assert c.post("/api/mode", json={"mode": "coding"}).status_code == 401
        assert c.get("/api/chats", headers={"X-Orbwise-Token": "falsch"}).status_code == 401
        with pytest.raises(WebSocketDisconnect) as e, \
                c.websocket_connect("ws://localhost:8765/ws", headers={"Origin": BASE}) as ws:
            ws.receive_json()
        assert e.value.code == 4401
        # frei: Lebenszeichen, Dateien der Oberfläche, eigene Schlüssel
        assert c.get("/api/alive").json() == {"ok": True}
        assert c.get("/static/app.js").status_code == 200
        assert c.post("/api/askpass", json={"prompt": "x"}).status_code == 403  # Einmal-Token fehlt – nicht 401
        assert c.get("/v1/models").status_code == 503  # eigener API-Schlüssel; der API-Server ist noch aus


def test_one_time_link_signs_the_browser_in(app):
    token = web_auth.read_token()
    with TestClient(app, base_url=BASE, anonymous=True) as c:
        assert c.post("/api/open-code").status_code == 401  # Code nur mit Token
        url = c.post("/api/open-code", headers={"X-Orbwise-Token": token}).json()["url"]
        code_path = url.removeprefix("http://localhost:8765")
        r = c.get(code_path, follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/"
        cookie = r.headers["set-cookie"].lower()
        assert "httponly" in cookie and "samesite=strict" in cookie and token.lower() not in r.headers["location"]
        assert c.get("/").status_code == 200 and c.get("/api/status").status_code == 200
        with c.websocket_connect("ws://localhost:8765/ws", headers={"Origin": BASE}) as ws:
            assert ws.receive_json()["type"] == "hello"
    with TestClient(app, base_url=BASE, anonymous=True) as other:
        again = other.get(code_path, follow_redirects=False)  # Code gilt nur einmal
        assert again.status_code == 401 and "abgelaufen" in again.text


def test_pasting_the_token_on_the_sign_in_page(app):
    with TestClient(app, base_url=BASE, anonymous=True) as c:
        bad = c.post("/api/login", data={"token": "falsch"}, headers={"Origin": BASE}, follow_redirects=False)
        assert bad.status_code == 401 and "Falsches Token" in bad.text
        ok = c.post("/api/login", data={"token": web_auth.read_token()}, headers={"Origin": BASE},
                    follow_redirects=False)
        assert ok.status_code == 303 and c.get("/api/status").status_code == 200


def test_cli_headers_carry_the_token(app):
    assert web_auth.headers(8765) == {"Host": "localhost:8765", "X-Orbwise-Token": web_auth.read_token()}


def test_token_file_counts_as_secret(tmp_path):
    from orbwise.tools.secretpaths import is_secret_path
    assert is_secret_path(tmp_path / ".config" / "orbwise" / "web-token")
