"""Piper als Standard-Sprachausgabe: eigene Python-Umgebung (GPL – nie im Orbwise-Prozess), Stimmen von Hugging Face,
Server nur auf der CPU, Auswahl und Verwaltung über die Oberfläche."""

import io
import json
import subprocess
import sys
import time
import wave

import httpx
import pytest
from fastapi.testclient import TestClient

from orbwise import state
from orbwise.voice import piper
from orbwise.voice.tts import TTS

# Ersatz für `python -m piper.http_server`: dieselbe Schnittstelle, meldet Umgebung und Anfrage zurück
FAKE_SERVER = r'''
import io, json, os, sys, wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
args = sys.argv
port = int(args[args.index("--port") + 1])
class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def do_GET(self):
        body = json.dumps({"voices": []}).encode()
        self.send_response(200); self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)
    def do_POST(self):
        req = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        buf = io.BytesIO()
        with wave.open(buf, "wb") as w:
            w.setnchannels(1); w.setsampwidth(2); w.setframerate(22050); w.writeframes(b"\0\0" * 10)
        data = buf.getvalue()
        self.send_response(200)
        self.send_header("Content-Type", "audio/wav")
        self.send_header("X-Voice", req.get("voice", ""))
        self.send_header("X-Text", req["text"].encode("unicode_escape").decode())
        self.send_header("X-Cuda", os.environ.get("CUDA_VISIBLE_DEVICES", "unset"))
        self.send_header("X-Args", " ".join(args[1:]))
        self.send_header("Content-Length", str(len(data))); self.end_headers(); self.wfile.write(data)
ThreadingHTTPServer(("127.0.0.1", port), H).serve_forever()
'''


def fake_install(data_dir, voices=("de_DE-thorsten-high",)):
    py = piper.python(data_dir)
    py.parent.mkdir(parents=True, exist_ok=True)
    py.write_text("")
    (piper.home(data_dir) / "installed").write_text("x")
    for v in voices:
        p = piper.voice_path(data_dir, v)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"onnx")
        p.with_suffix(".onnx.json").write_text("{}")


@pytest.fixture
def server_script(tmp_path, monkeypatch):
    script = tmp_path / "fake_piper_server.py"
    script.write_text(FAKE_SERVER)
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    real = piper.PiperTTS.command

    def command(self, voice_id):
        return [sys.executable, str(script), *real(self, voice_id)[3:]]  # statt venv/python -m piper.http_server
    monkeypatch.setattr(piper.PiperTTS, "command", command)
    return script


def test_install_uses_its_own_environment(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(piper.shutil, "which", lambda name: "/usr/bin/uv" if name == "uv" else None)

    def runner(cmd, **kw):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, "", "")
    out = []
    assert piper.install(tmp_path, out.append, runner)
    venv = str(piper.home(tmp_path) / "venv")
    assert calls[0][:2] == ["/usr/bin/uv", "venv"] and venv in calls[0]
    assert calls[1][:3] == ["/usr/bin/uv", "pip", "install"] and str(piper.python(tmp_path)) in calls[1]
    assert piper.PACKAGE in calls[1] and "http" in piper.PACKAGE  # Server-Extra (Flask) mit
    (piper.python(tmp_path)).parent.mkdir(parents=True, exist_ok=True)
    piper.python(tmp_path).write_text("")
    assert piper.is_installed(tmp_path)
    failing = lambda cmd, **kw: subprocess.CompletedProcess(cmd, 1, "", "no network")  # noqa: E731
    out = []
    assert not piper.install(tmp_path / "b", out.append, failing) and "no network" in out[-1]


def test_voice_download_paths_and_removal(tmp_path, monkeypatch):
    seen = []

    def handler(request):
        seen.append(str(request.url))
        return httpx.Response(200, content=b"data")
    client = httpx.Client(transport=httpx.MockTransport(handler))
    assert piper.download_voice(tmp_path, "de_DE-thorsten-high", lambda s: None, client)
    assert seen == ["https://huggingface.co/rhasspy/piper-voices/resolve/v1.0.0/de/de_DE/thorsten/high/"
                    "de_DE-thorsten-high.onnx.json",
                    "https://huggingface.co/rhasspy/piper-voices/resolve/v1.0.0/de/de_DE/thorsten/high/"
                    "de_DE-thorsten-high.onnx"]
    assert piper.has_voice(tmp_path, "de_DE-thorsten-high") and piper.downloaded(tmp_path) == ["de_DE-thorsten-high"]
    eva = piper.BY_ID["de_DE-eva_k-x_low"].url("https://hf-mirror.com", ".onnx")
    assert eva == "https://hf-mirror.com/rhasspy/piper-voices/resolve/v1.0.0/de/de_DE/eva_k/x_low/de_DE-eva_k-x_low.onnx"
    assert not piper.download_voice(tmp_path, "gibts_nicht", lambda s: None, client)
    assert piper.remove_voice(tmp_path, "de_DE-thorsten-high") == 8 and not piper.downloaded(tmp_path)


def test_server_runs_on_the_cpu_and_speaks(tmp_path, server_script):
    fake_install(tmp_path, ("de_DE-thorsten-high", "de_DE-kerstin-low"))
    engine = piper.PiperTTS(tmp_path)
    try:
        wav = engine.synth("Am 31.10. um 14:30 Uhr kostet es 1.234,50 €.", "de_DE-thorsten-high")
        with wave.open(io.BytesIO(wav)) as w:
            assert w.getframerate() == 22050
        assert engine.state == "ready" and engine.describe()["downloaded"] == ["de_DE-thorsten-high", "de_DE-kerstin-low"]
        # zweite Stimme über denselben Server (lädt sie bei Bedarf), Text unverändert – Piper liest Zahlen selbst
        with httpx.Client() as c:
            r = c.post(engine.base_url + "/synthesize", json={"text": "1.234,50 €", "voice": "de_DE-kerstin-low"})
        assert r.headers["X-Voice"] == "de_DE-kerstin-low" and r.headers["X-Text"] == "1.234,50 \\u20ac"
        assert r.headers["X-Cuda"] == ""  # nur CPU – kein Grafikspeicher
        assert "--data-dir" in r.headers["X-Args"] and "de_DE-thorsten-high.onnx" in r.headers["X-Args"]
        with pytest.raises(ValueError):
            engine.synth("x", "gibts_nicht")
    finally:
        engine.stop()
    assert engine.state == "stopped"


def test_missing_install_or_voice_is_explained(tmp_path):
    engine = piper.PiperTTS(tmp_path)
    with pytest.raises(RuntimeError, match="nicht installiert"):
        engine.synth("x", "de_DE-thorsten-high")
    fake_install(tmp_path, ())
    with pytest.raises(RuntimeError, match="nicht geladen"):
        engine.synth("x", "de_DE-thorsten-high")


def test_tts_prefers_piper_and_switches_engines(cfg, tmp_path, monkeypatch, server_script):
    path = tmp_path / "state.json"
    state.update(path, lambda d: d.__setitem__("tts", {"voice": "juergen", "languages": {}}))  # früher Pocket
    tts = TTS(cfg.voice, tmp_path, path)
    assert tts.voice == "piper:de_DE-thorsten-high" and tts.engine() == "piper"  # einmalig auf Piper umgestellt
    assert state.load(path)["tts"]["piper_default"] is True
    assert not tts.select("piper:de_DE-kerstin-low")  # erst laden
    fake_install(tmp_path, ("de_DE-thorsten-high", "en_US-lessac-high"))
    assert tts.available()
    try:
        assert tts.select("piper:en_US-lessac-high") and tts.reply_language == "en"  # die Stimme bestimmt die Sprache
        with wave.open(io.BytesIO(tts.synth("Hello."))) as w:
            assert w.getnframes() == 10
        assert tts.select("juergen", "de") and tts.engine() == "pocket" and tts.piper.state == "stopped"
        again = TTS(cfg.voice, tmp_path, path)
        assert again.voice == "juergen"  # die eigene Wahl bleibt – umgestellt wird nur einmal
        assert again.label_of("piper:de_DE-thorsten-high") == "Thorsten (Piper)" and again.label_of("juergen") == "Juergen (Pocket)"
    finally:
        tts.stop()


def test_piper_endpoints(cfg, tmp_path, monkeypatch, server_script):
    from orbwise.voice import pocket
    monkeypatch.setenv("ORBWISE_FAKE_LLM", "1")
    monkeypatch.setenv("ORBWISE_SKIP_WARMUP", "1")
    cfg.voice.enabled = True
    installs, downloads = [], []
    monkeypatch.setattr(piper, "install", lambda data_dir, out: installs.append(1) or fake_install(data_dir, ()) or True)

    def download(data_dir, voice_id, out, client=None, stop=None):
        out(f"  {voice_id}: 0.1 / 0.1 GB (100 %)")
        downloads.append(voice_id)
        fake_install(data_dir, (voice_id,))
        return True
    monkeypatch.setattr(piper, "download_voice", download)
    monkeypatch.setattr(pocket, "cpu_supported", lambda cpuinfo=None: False)
    from orbwise.server import create_app
    with TestClient(create_app(cfg), base_url="http://localhost:8765") as client:
        for _ in range(200):  # Start richtet Piper mit der Standardstimme selbst ein
            if downloads:
                break
            time.sleep(0.02)
        assert downloads == ["de_DE-thorsten-high"]
        data = client.get("/api/voices").json()
        assert data["engine"] == "piper" and data["piper"]["installed"]
        voices = {v["id"]: v for v in data["piper"]["voices"]}
        assert voices["de_DE-thorsten-high"]["current"] and voices["de_DE-thorsten-high"]["recommended"]
        assert list(voices)[0] == "de_DE-thorsten-high"  # eigene Sprache und Empfehlung zuerst
        assert client.post("/api/piper/voices/gibts_nicht").status_code == 404
        with client.websocket_connect("ws://localhost:8765/ws", headers={"Origin": "http://localhost:8765"}) as ws:
            assert client.post("/api/piper/voices/en_US-lessac-high").json() == {"started": True}
            events = []
            while not any(e.get("type") == "piper_progress" and e.get("done") for e in events):
                events.append(ws.receive_json())
        assert any(e.get("pct") == 100 for e in events if e.get("type") == "piper_progress")
        assert client.get("/api/voices").json()["current"] == "piper:en_US-lessac-high"
        r = client.get("/api/voices/piper:de_DE-thorsten-high/preview")
        assert r.status_code == 200 and r.headers["content-type"] == "audio/wav"
        assert client.delete("/api/piper/voices/en_US-lessac-high").status_code == 409  # aktiv
        assert client.delete("/api/piper/voices/de_DE-thorsten-high").json()["ok"]
        assert client.delete("/api/piper").status_code == 409  # Piper ist aktiv
        assert installs == [1]
        app_tts = client.app.state.hub.speaker.tts
        app_tts.stop()
    assert json.loads((tmp_path / "state.json").read_text())["tts"]["voice"] == "piper:en_US-lessac-high"
