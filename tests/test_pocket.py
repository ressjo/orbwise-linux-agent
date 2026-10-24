import hashlib
import io
import json
import os
import sys
import tarfile
import wave

import httpx
import pytest
from fastapi.testclient import TestClient

from orbwise.voice import pocket
from orbwise.voice.tts import PiperTTS, Speaker

TRIPLE = "x86_64-unknown-linux-gnu"


def placeholder_wav(pcm: bytes, rate: int = 24000) -> bytes:
    """So streamt pocket-tts serve: Kopf mit Platzhalter-Länge (2·10⁹ Bytes), danach die Samples."""
    size = 2_000_000_000
    head = (b"RIFF" + (36 + size).to_bytes(4, "little") + b"WAVEfmt " + (16).to_bytes(4, "little")
            + (1).to_bytes(2, "little") + (1).to_bytes(2, "little") + rate.to_bytes(4, "little")
            + (rate * 2).to_bytes(4, "little") + (2).to_bytes(2, "little") + (16).to_bytes(2, "little")
            + b"data" + size.to_bytes(4, "little"))
    return head + pcm


def test_rewrap_wav_fixes_length():
    wav = pocket.rewrap_wav(placeholder_wav(b"\x01\x00" * 480 + b"\x07"))
    with wave.open(io.BytesIO(wav)) as w:
        assert (w.getframerate(), w.getnchannels(), w.getsampwidth(), w.getnframes()) == (24000, 1, 2, 480)
    with pytest.raises(ValueError):
        pocket.rewrap_wav(b"<html>")


def test_voice_list_puts_native_voice_first():
    de = pocket.voice_list("de", "pocket:alba", installed=True)
    assert de[0]["name"] == "pocket:juergen" and de[0]["recommended"] and "ohne Akzent" in de[0]["description"]
    assert {v["engine"] for v in de} == {"pocket"} and len(de) == len(pocket.VOICES)
    assert next(v for v in de if v["name"] == "pocket:alba")["current"]
    en = pocket.voice_list("en", "", installed=False)
    assert en[0]["name"] == "pocket:alba" and not en[0]["installed"]
    assert pocket.model_language("de") == "german" and pocket.model_language("xx") == "english"


def test_asset_and_checksum_helpers():
    assets = [{"name": "pocket-tts-3.3.0-aarch64-unknown-linux-gnu.tar.gz"},
              {"name": f"pocket-tts-3.3.0-{TRIPLE}.tar.gz"}, {"name": "SHA256SUMS"}]
    archive, sums = pocket.pick_asset(assets, TRIPLE)
    assert archive["name"].endswith(TRIPLE + ".tar.gz") and sums["name"] == "SHA256SUMS"
    assert pocket.expected_sha256(f"abc  other.tar.gz\nDEF *pocket-tts-3.3.0-{TRIPLE}.tar.gz\n",
                                  f"pocket-tts-3.3.0-{TRIPLE}.tar.gz") == "def"


def test_cpu_check_needs_avx2(monkeypatch):
    monkeypatch.setattr(pocket, "target_triple", lambda: TRIPLE)
    assert pocket.cpu_supported("flags : fpu sse4_2 avx avx2 fma")
    assert not pocket.cpu_supported("flags : fpu sse4_2 avx")


def release_archive(script: str = "#!/bin/sh\necho hi\n") -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, data in ((f"pocket-tts-3.3.0-{TRIPLE}/pocket-tts", script.encode()),
                           (f"pocket-tts-3.3.0-{TRIPLE}/LICENSE", b"MIT"), ("../evil", b"x")):
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def fake_github(archive: bytes, checksum: str | None = None) -> httpx.Client:
    name = f"pocket-tts-3.3.0-{TRIPLE}.tar.gz"

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path.endswith("/releases/latest"):
            return httpx.Response(200, json={"tag_name": "v4.0.1", "assets": [
                {"name": name, "browser_download_url": "https://dl.example/a.tgz"},
                {"name": "SHA256SUMS", "browser_download_url": "https://dl.example/sums"}]})
        if req.url.path == "/a.tgz":
            return httpx.Response(200, content=archive)
        if req.url.path == "/sums":
            return httpx.Response(200, text=f"{checksum or hashlib.sha256(archive).hexdigest()}  {name}\n")
        return httpx.Response(404)
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_install_unpacks_binary_and_checks_sum(tmp_path, monkeypatch):
    monkeypatch.setattr(pocket, "target_triple", lambda: TRIPLE)
    monkeypatch.setattr(pocket, "cpu_supported", lambda cpuinfo=None: True)
    lines = []
    archive = release_archive()
    assert not pocket.install(tmp_path, lines.append, fake_github(archive, checksum="0" * 64))
    assert "Prüfsumme" in lines[-1] and not pocket.is_installed(tmp_path)
    assert pocket.install(tmp_path, lines.append, fake_github(archive))
    exe = pocket.binary(tmp_path)
    assert exe.exists() and os.access(exe, os.X_OK)
    assert (tmp_path / "pocket-tts" / "LICENSE").read_text() == "MIT"
    assert not (tmp_path / "evil").exists() and not (tmp_path.parent / "evil").exists()
    assert pocket.PocketTTS(tmp_path).describe()["version"] == "v4.0.1"


def test_remove_deletes_program_and_hf_cache(tmp_path, monkeypatch):
    monkeypatch.setenv("HF_HOME", str(tmp_path / "hf"))
    monkeypatch.delenv("HF_HUB_CACHE", raising=False)
    exe = pocket.binary(tmp_path)
    exe.parent.mkdir(parents=True)
    exe.write_bytes(b"x" * 1000)
    model = tmp_path / "hf" / "hub" / "models--kyutai--pocket-tts-without-voice-cloning" / "blobs" / "m"
    model.parent.mkdir(parents=True)
    model.write_bytes(b"y" * 5000)
    other = tmp_path / "hf" / "hub" / "models--someone--else" / "f"
    other.parent.mkdir(parents=True)
    other.write_bytes(b"z")
    assert pocket.remove(tmp_path) == 6000
    assert not exe.exists() and not model.exists() and other.exists()


FAKE_SERVER = r'''#!{python}
import sys, json
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs
args = sys.argv[1:]
port = int(args[args.index("--port") + 1])
open({args_file!r}, "w").write(json.dumps(args))
HEAD = (b"RIFF" + (2000000036).to_bytes(4, "little") + b"WAVEfmt " + (16).to_bytes(4, "little")
        + (1).to_bytes(2, "little") + (1).to_bytes(2, "little") + (24000).to_bytes(4, "little")
        + (48000).to_bytes(4, "little") + (2).to_bytes(2, "little") + (16).to_bytes(2, "little")
        + b"data" + (2000000000).to_bytes(4, "little"))
class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def do_GET(self):
        self.send_response(200); self.end_headers(); self.wfile.write(b'{{"status":"healthy"}}')
    def do_POST(self):
        form = parse_qs(self.rfile.read(int(self.headers["Content-Length"])).decode())
        self.send_response(200); self.send_header("Content-Type", "audio/wav"); self.end_headers()
        n = len(form["text"][0]) * 10 + (1000 if form["voice_url"][0] == "juergen" else 0)
        self.wfile.write(HEAD + b"\x00\x01" * n)
HTTPServer(("127.0.0.1", port), H).serve_forever()
'''


@pytest.fixture
def fake_binary(tmp_path):
    exe = pocket.binary(tmp_path)
    exe.parent.mkdir(parents=True)
    args_file = tmp_path / "args.json"
    exe.write_text(FAKE_SERVER.format(python=sys.executable, args_file=str(args_file)))
    exe.chmod(0o755)
    return args_file


def frames(wav: bytes) -> int:
    with wave.open(io.BytesIO(wav)) as w:
        return w.getnframes()


def test_server_starts_speaks_and_stops(tmp_path, fake_binary):
    tts = pocket.PocketTTS(tmp_path, "de")
    try:
        assert frames(tts.synth("Hallo", "pocket:juergen")) == 1050
        assert tts.state == "ready"
        args = json.loads(fake_binary.read_text())
        assert args[:1] == ["serve"] and args[args.index("--language") + 1] == "german"
        assert args[args.index("--default-voice") + 1] == "juergen" and "--quantize" in args
        assert frames(tts.synth("Hallo", "pocket:alba")) == 50  # gleicher Server, andere Stimme
        with pytest.raises(ValueError):
            tts.synth("x", "pocket:gibtsnicht")
        proc = tts.proc
    finally:
        tts.stop()
    assert proc.poll() is not None and tts.state == "stopped"


def test_server_crash_is_reported(tmp_path):
    exe = pocket.binary(tmp_path)
    exe.parent.mkdir(parents=True)
    exe.write_text("#!/bin/sh\necho 'cannot download model' >&2\nexit 3\n")
    exe.chmod(0o755)
    tts = pocket.PocketTTS(tmp_path, "de")
    with pytest.raises(RuntimeError, match="cannot download model"):
        tts.synth("Hallo", "pocket:juergen")
    assert tts.state == "error" and "Code 3" in tts.error


def test_tts_dispatches_and_falls_back_to_piper(cfg, tmp_path, monkeypatch):
    cfg.voice.tts_voice = tmp_path / "voices" / "de_DE-thorsten-high.onnx"
    tts = PiperTTS(cfg.voice, tmp_path)
    assert not tts.select("pocket:juergen")  # noch nicht installiert
    exe = pocket.binary(tmp_path)
    exe.parent.mkdir(parents=True)
    exe.write_text("#!/bin/sh\n")
    warmed = []
    monkeypatch.setattr(tts.pocket, "warm", warmed.append)
    assert tts.select("pocket:juergen") and tts.current == "pocket:juergen" and warmed == ["pocket:juergen"]
    assert not tts.select("pocket:gibtsnicht")
    assert tts.available() and tts.error is None

    monkeypatch.setattr(tts.pocket, "synth", lambda text, voice: b"POCKET")
    assert tts.speak("Hallo") == (b"POCKET", 1.0)

    def broken(text, voice):
        raise RuntimeError("weg")
    monkeypatch.setattr(tts.pocket, "synth", broken)
    with pytest.raises(RuntimeError):  # keine Piper-Stimme da → Fehler bleibt
        tts.speak("Hallo")
    voices = tmp_path / "voices"
    voices.mkdir()
    (voices / "de_DE-thorsten-high.onnx").write_bytes(b"x")
    monkeypatch.setattr(tts, "synth", lambda text, name=None: f"PIPER:{name}".encode())
    assert tts.speak("Hallo") == (b"PIPER:de_DE-thorsten-high", None)

    exe.unlink()  # Pocket entfernt → zurück zu Piper
    assert tts.available() is False or tts.current == "de_DE-thorsten-high"
    assert tts.current == "de_DE-thorsten-high"


def test_speaker_sends_rate_for_pocket(cfg, tmp_path, monkeypatch):
    from conftest import run
    cfg.voice.tts_voice = tmp_path / "x.onnx"
    tts = PiperTTS(cfg.voice, tmp_path)
    monkeypatch.setattr(tts, "available", lambda: True)
    monkeypatch.setattr(tts, "speak", lambda text: (b"WAV", 1.0))
    sent = []

    async def go():
        async def send(ev):
            sent.append(ev)
        sp = Speaker(tts, send, lambda: True)
        sp.start()
        sp.say("Guten Abend, alles bereit.")
        for _ in range(100):
            if sent:
                break
            await __import__("asyncio").sleep(0.01)
        await sp.close()
    run(go())
    assert sent and sent[0]["rate"] == 1.0 and sent[0]["audio"]


@pytest.fixture
def client(cfg, tmp_path, monkeypatch):
    monkeypatch.setenv("ORBWISE_FAKE_LLM", "1")
    monkeypatch.setenv("ORBWISE_SKIP_WARMUP", "1")
    monkeypatch.setenv("HF_HOME", str(tmp_path / "hf"))
    cfg.voice.enabled = True
    cfg.voice.tts_voice = tmp_path / "voices" / "de_DE-thorsten-high.onnx"

    async def no_catalog(cache_dir, transport=None):
        return {}
    from orbwise.voice import catalog
    monkeypatch.setattr(catalog, "fetch_catalog", no_catalog)
    from orbwise.server import create_app
    return TestClient(create_app(cfg), base_url="http://localhost:8765")


def test_pocket_endpoints(client, tmp_path, monkeypatch):
    data = client.get("/api/voices").json()
    assert data["pocket"]["installed"] is False
    juergen = next(v for v in data["voices"] if v["name"] == "pocket:juergen")
    assert juergen["engine"] == "pocket" and not juergen["installed"]
    assert client.get("/api/voices/pocket:juergen/preview").status_code == 404

    def fake_install(data_dir, out):
        exe = pocket.binary(data_dir)
        exe.parent.mkdir(parents=True, exist_ok=True)
        exe.write_text("#!/bin/sh\n")
        return True
    monkeypatch.setattr(pocket, "install", fake_install)
    monkeypatch.setattr(pocket.PocketTTS, "warm", lambda self, voice: None)
    assert client.post("/api/pocket/install", headers={"Origin": "http://evil.example"}).status_code == 403
    assert client.post("/api/pocket/install").json()["pocket"]["installed"]
    data = client.get("/api/voices").json()
    assert next(v for v in data["voices"] if v["name"] == "pocket:juergen")["installed"]

    monkeypatch.setattr(pocket.PocketTTS, "synth", lambda self, text, voice: pocket.rewrap_wav(placeholder_wav(b"\0\0")))
    r = client.get("/api/voices/pocket:juergen/preview")
    assert r.status_code == 200 and r.headers["X-Playback-Rate"] == "1.0"

    r = client.delete("/api/pocket")
    assert r.json()["ok"] and not pocket.is_installed(tmp_path)

    monkeypatch.setattr(pocket, "install", lambda data_dir, out: out("✘ Download fehlgeschlagen: kaputt") or False)
    r = client.post("/api/pocket/install")
    assert r.status_code == 502 and r.json()["detail"] == "Download fehlgeschlagen: kaputt"
