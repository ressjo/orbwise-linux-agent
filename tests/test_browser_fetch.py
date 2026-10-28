"""Webseiten, die der einfache Abruf nicht lesen kann (JavaScript, Bot-Sperre), lädt Orbwise mit headless Chromium."""

import glob
import http.server
import threading

import pytest
from conftest import run

from orbwise.tools import browser
from orbwise.tools.registry import ToolContext
from orbwise.tools.web import fetch_page

JS_PAGE = b"""<html><head><title>App</title></head><body><div id="app"></div><script>
document.getElementById('app').innerHTML = '<article><h1>Tagesangebot</h1><p>' +
  'Heute gibt es frischen Kaffee aus Guji, Huila und Cerrado zum halben Preis. '.repeat(6) + '</p></article>';
</script></body></html>"""


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        if self.path == "/blocked":
            self.send_response(403)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(JS_PAGE)

    def log_message(self, *a):
        pass


@pytest.fixture
def site():
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


def test_js_page_and_bot_block_fall_back_to_the_browser(cfg, memory, site, monkeypatch):
    rendered = "<html><body><article><h1>Gerendert</h1><p>" + "Inhalt nach JavaScript. " * 20 + "</p></article></body></html>"
    calls = []

    async def fake_render(url, shot=None):
        calls.append(url)
        return rendered
    monkeypatch.setattr(browser, "render", fake_render)
    ctx = ToolContext(cfg=cfg, memory=memory)
    out = run(fetch_page(ctx, site + "/", "text"))
    assert "Per Browser geladen" in out and "Inhalt nach JavaScript" in out
    out = run(fetch_page(ctx, site + "/blocked", "text"))
    assert "Per Browser geladen" in out and calls == [site + "/", site + "/blocked"]

    monkeypatch.setattr(browser, "render", lambda url, shot=None: _none())
    assert "HTTP 403" in run(fetch_page(ctx, site + "/blocked", "text"))  # kein Browser: wie bisher


async def _none():
    return None


SHELL = sorted(glob.glob("/opt/pw-browsers/chromium_headless_shell-*/chrome-headless-shell-linux64/chrome-headless-shell"))


@pytest.mark.skipif(not (browser.binary() or SHELL), reason="kein Chromium")
def test_real_browser_reads_a_javascript_page(cfg, memory, site, monkeypatch):
    if not browser.binary():
        monkeypatch.setattr(browser, "binary", lambda: SHELL[-1])
    out = run(fetch_page(ToolContext(cfg=cfg, memory=memory), site + "/", "text"))
    assert "Tagesangebot" in out or "frischen Kaffee" in out
