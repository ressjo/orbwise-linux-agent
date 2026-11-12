"""„Denke nach“ darf nicht hängen bleiben: nach dem Laden des Modells und nach jeder Antwort kommt „idle“."""

import asyncio

from conftest import run
from fastapi.testclient import TestClient

from orbwise import llm_router, server
from orbwise.agent import Agent
from orbwise.webapp import static

WS = "ws://localhost:8765/ws"
ORIGIN = {"Origin": "http://localhost:8765"}


def test_agent_reports_idle_right_after_the_answer(cfg, memory, llm):
    agent = Agent(cfg, llm, memory)
    events = []

    async def emit(ev):
        events.append(ev)

    async def confirm(*a):
        return False

    compacting = []

    async def make_room(*a, **kw):  # Platz schaffen läuft nach der Antwort – die Anzeige soll schon „bereit“ sein
        compacting.append([e.get("state") for e in events if e["type"] == "state"][-1])
        return True

    agent._over_limit = lambda idle=False: idle  # nach der Antwort fast voll
    agent._make_room = make_room
    run(agent.run("Hallo", emit, confirm))
    types = [(e["type"], e.get("state")) for e in events]
    end = types.index(("assistant_end", None))
    assert ("state", "idle") in types[end:]
    assert compacting == ["idle"]


def test_ui_leaves_thinking_after_model_start(cfg, monkeypatch):
    """Wer sich verbindet, während das Modell beim Start lädt, sieht „denke nach“ – danach muss „idle“ kommen."""
    monkeypatch.delenv("ORBWISE_FAKE_LLM", raising=False)
    monkeypatch.setenv("ORBWISE_SKIP_WARMUP", "1")

    async def slow_start(self, progress=None):
        await asyncio.sleep(0.6)

    monkeypatch.setattr(llm_router.LLMRouter, "start", slow_start)
    with TestClient(server.create_app(cfg), base_url="http://localhost:8765") as client:
        with client.websocket_connect(WS, headers=ORIGIN) as ws:
            hello = ws.receive_json()
            assert hello["type"] == "hello" and hello["busy"] is True
            seen = []
            while ("state", "idle") not in seen:
                ev = ws.receive_json()
                seen.append((ev["type"], ev.get("state")))
            assert ("model_active", None) in seen


def test_model_start_failure_also_ends_thinking(cfg, monkeypatch):
    monkeypatch.delenv("ORBWISE_FAKE_LLM", raising=False)
    monkeypatch.setenv("ORBWISE_SKIP_WARMUP", "1")

    async def broken_start(self, progress=None):
        await asyncio.sleep(0.4)
        raise llm_router.LLMError("llama-server ist beim Start beendet worden")

    monkeypatch.setattr(llm_router.LLMRouter, "start", broken_start)
    with TestClient(server.create_app(cfg), base_url="http://localhost:8765") as client:
        with client.websocket_connect(WS, headers=ORIGIN) as ws:
            assert ws.receive_json()["busy"] is True
            seen = []
            while ("state", "idle") not in seen:
                ev = ws.receive_json()
                seen.append((ev["type"], ev.get("state")))
            assert ("model_error", None) in seen


def test_cancelled_transcription_does_not_block_the_microphone():
    """Wird die Transkription abgebrochen, darf „Transkribiere …“ nicht stehen bleiben."""
    import threading

    import numpy as np

    from orbwise.config import VoiceConfig
    from orbwise.voice.listen import AudioSession

    release = threading.Event()

    class SlowSTT:
        def transcribe(self, audio):
            release.wait(5)
            return "zu spät"

    sent, heard = [], []

    async def send(ev):
        sent.append(ev)

    async def on_text(text):
        heard.append(text)

    async def scenario():
        s = AudioSession(VoiceConfig(), SlowSTT(), None, send, on_text)
        s.recorded = [np.ones(16000, dtype=np.int16)]
        await s._finish()
        assert s.mode == "transcribing"
        await asyncio.sleep(0.05)
        s.task.cancel()
        release.set()
        await asyncio.gather(s.task, return_exceptions=True)
        return s

    s = run(scenario())
    assert s.mode == "idle" and sent[-1] == {"type": "voice", "state": "idle"} and not heard


def test_ui_files_are_revalidated_after_updates(cfg):
    with TestClient(server.create_app(cfg), base_url="http://localhost:8765") as client:
        resp = client.get("/static/app.js")
        assert resp.status_code == 200 and resp.headers["cache-control"] == "no-cache"
        again = client.get("/static/app.js", headers={"If-None-Match": resp.headers["etag"]})
        assert again.status_code == 304


def test_page_loads_ui_files_with_content_version(cfg):
    """Neues app.js mit altem orb.js aus dem Cache ließ Werkzeug-Anzeige und „bereit“ ausfallen – jede Datei trägt
    deshalb ihren Inhalts-Hash, der Browser lädt nach einem Update alles frisch."""
    import hashlib
    import re

    with TestClient(server.create_app(cfg), base_url="http://localhost:8765") as client:
        html = client.get("/").text
        refs = dict(re.findall(r'/static/([\w.-]+)\?v=([0-9a-f]{10})"', html))
        assert {"orb.js", "app.js", "voicefx.js", "style.css"} <= set(refs)
        for name, digest in refs.items():
            if name != "app.js":
                assert hashlib.sha1((server.WEB_DIR / name).read_bytes()).hexdigest()[:10] == digest
        assert refs["app.js"] == static.content_digest(server.WEB_DIR / "app.js")
        assert client.get(f"/static/orb.js?v={refs['orb.js']}").status_code == 200


def test_startup_progress_is_reported_until_ready(cfg, monkeypatch):
    """Die Startanzeige sieht live, was noch lädt – erst wenn alles fertig ist, meldet der Server „ready“."""
    monkeypatch.setenv("ORBWISE_FAKE_LLM", "1")
    with TestClient(server.create_app(cfg), base_url="http://localhost:8765") as client:
        with client.websocket_connect(WS, headers=ORIGIN) as ws:
            assert ws.receive_json()["type"] == "hello"
            snap = ws.receive_json()
            assert snap["type"] == "startup"
            keys = [s["key"] for s in snap["steps"]]
            assert keys == ["memory", "model", "stt", "tts", "wake", "telegram"]
            for _ in range(50):
                if snap.get("ready"):
                    break
                ev = ws.receive_json()
                if ev["type"] == "startup":
                    snap = ev
            assert snap["ready"]
            states = {s["key"]: s["state"] for s in snap["steps"]}
            assert states["model"] == "ok" and states["memory"] == "ok"
            assert states["stt"] == "off" and states["telegram"] == "off"  # Stimme aus, Telegram nicht eingerichtet
        assert client.get("/api/startup").json()["ready"]


def test_app_js_version_follows_its_modules(tmp_path, monkeypatch):
    """Ändert sich nur ein Modul unter js/, muss sich trotzdem der Verweis auf app.js ändern."""
    web = tmp_path / "web"
    (web / "js").mkdir(parents=True)
    (web / "app.js").write_text('import { init } from "./js/a.js";\n')
    (web / "js" / "a.js").write_text("export function init() {}\n")
    monkeypatch.setattr(static, "WEB_DIR", web)
    monkeypatch.setattr(static, "MODULES_DIR", web / "js")
    before = static.content_digest(web / "app.js")
    (web / "js" / "a.js").write_text("export function init() { console.log(1); }\n")
    assert static.content_digest(web / "app.js") != before


def test_every_module_is_loaded_and_revalidated(cfg):
    """app.js erreicht jedes Modul unter js/ (direkt oder über andere Module), und die Module werden wie app.js bei
    jedem Laden neu geprüft – sonst liefe nach einem Update ein altes Modul aus dem Cache mit dem neuen app.js."""
    import re

    present = {p.name for p in (server.WEB_DIR / "js").glob("*.js")}
    imports = {name: set(re.findall(r'from "\./([\w-]+\.js)"', (server.WEB_DIR / "js" / name).read_text()))
               for name in present}
    for module, targets in imports.items():  # Importe zwischen den Modulen zeigen auf vorhandene Dateien
        assert targets <= present, (module, targets - present)
    entry = (server.WEB_DIR / "app.js").read_text(encoding="utf-8")
    reached, todo = set(), set(re.findall(r'from "\./js/([\w-]+\.js)"', entry))
    while todo:
        name = todo.pop()
        reached.add(name)
        todo |= imports.get(name, set()) - reached
    assert reached == present and "core.js" in present
    with TestClient(server.create_app(cfg), base_url="http://localhost:8765") as client:
        resp = client.get("/static/js/core.js")
        assert resp.status_code == 200 and resp.headers["cache-control"] == "no-cache"
        assert "javascript" in resp.headers["content-type"]
