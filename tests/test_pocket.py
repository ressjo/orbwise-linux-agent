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

from orbwise import prompts, retire, state
from orbwise.voice import pocket
from orbwise.voice.tts import TTS, Speaker

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
    de = pocket.voice_list("de", "alba", {"alba": "en"})
    assert de[0]["name"] == "juergen" and de[0]["recommended"] and "ohne Akzent" in de[0]["description"]
    assert de[0]["language"] == "de" and len(de) == len(pocket.VOICES)
    alba = next(v for v in de if v["name"] == "alba")
    assert alba["current"] and alba["language"] == "en" and "ohne Akzent" in alba["description"]  # Englisch: ihre Sprache
    assert "Englisch" in next(v for v in de if v["name"] == "anna")["description"]
    en = pocket.voice_list("en", "", {})
    assert en[0]["name"] == "alba" and en[0]["language"] == "en"
    assert pocket.model_language("de") == "german" and pocket.model_language("xx") == "english"
    assert {"code": "en", "label": "Englisch"} in pocket.language_list(False)


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
        assert frames(tts.synth("Hallo", "juergen")) == 1050
        assert tts.state == "ready"
        args = json.loads(fake_binary.read_text())
        assert args[:1] == ["serve"] and args[args.index("--language") + 1] == "german"
        assert args[args.index("--default-voice") + 1] == "juergen" and "--quantize" in args
        assert frames(tts.synth("Hallo", "alba")) == 50  # gleicher Server, andere Stimme
        with pytest.raises(ValueError):
            tts.synth("x", "gibtsnicht")
        proc = tts.proc
        tts.set_language("de")  # gleiche Sprache: Server läuft weiter
        assert proc.poll() is None
        tts.set_language("en")  # andere Sprache → anderes Modell: Server beenden, nächster Satz startet neu
        assert proc.poll() is not None and tts.state == "stopped"
        tts.synth("Hi", "alba")
        args = json.loads(fake_binary.read_text())
        assert args[args.index("--language") + 1] == "english"
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
        tts.synth("Hallo", "juergen")
    assert tts.state == "error" and "Code 3" in tts.error


def test_tts_remembers_voice_and_language(cfg, tmp_path, monkeypatch):
    path = tmp_path / "state.json"
    tts = TTS(cfg.voice, tmp_path, path)
    assert tts.voice == "juergen" and tts.reply_language == "de"
    assert not tts.available() and "nicht installiert" in tts.error
    warmed = []
    monkeypatch.setattr(pocket.PocketTTS, "warm", lambda self, voice: warmed.append((voice, self.language)))
    assert not tts.select("gibtsnicht") and not tts.select("alba", "kl")
    assert tts.select("alba", "en") and tts.reply_language == "en" and tts.pocket.language == "en"
    assert warmed == []  # nicht installiert → nichts zu starten
    assert state.load(path)["tts"] == {"voice": "alba", "languages": {"alba": "en"}}
    assert tts.select("juergen") and tts.reply_language == "de"  # jede Stimme behält ihre Sprache
    assert tts.select("alba") and tts.reply_language == "en"
    assert tts.select("alba", "de") and state.load(path)["tts"]["languages"] == {}  # Standard wird nicht gespeichert

    tts.select("alba", "en")
    again = TTS(cfg.voice, tmp_path, path)
    assert again.voice == "alba" and again.reply_language == "en" and again.pocket.language == "en"

    exe = pocket.binary(tmp_path)
    exe.parent.mkdir(parents=True)
    exe.write_text("#!/bin/sh\n")
    monkeypatch.setattr(pocket, "cpu_supported", lambda cpuinfo=None: True)
    assert again.available() and again.error is None
    again.select("vera")
    assert warmed == [("vera", "de")]


def test_speaker_sends_language(cfg, tmp_path, monkeypatch):
    from conftest import run
    tts = TTS(cfg.voice, tmp_path, tmp_path / "state.json")
    tts.languages["juergen"] = "en"
    monkeypatch.setattr(tts, "available", lambda: True)
    monkeypatch.setattr(tts, "synth", lambda text: b"WAV")
    sent = []

    async def go():
        async def send(ev):
            sent.append(ev)
        sp = Speaker(tts, send, lambda: True)
        sp.start()
        sp.say("Good evening, all systems ready.")
        for _ in range(100):
            if sent:
                break
            await __import__("asyncio").sleep(0.01)
        await sp.close()
    run(go())
    assert sent and sent[0]["lang"] == "en" and sent[0]["audio"]


def test_spoken_phrases_follow_voice_language(cfg):
    try:
        prompts.set_spoken_language("en")
        assert prompts.spoken(cfg, "yes_no") == prompts.SPOKEN["en"]["yes_no"]
        prompts.set_spoken_language("fr")  # keine französischen Sätze → Sprache der Oberfläche
        assert prompts.spoken(cfg, "yes_no") == prompts.SPOKEN["de"]["yes_no"]
    finally:
        prompts.set_spoken_language(None)
    assert "Englisch" in prompts.reply_language_note(cfg, "en")


def test_remove_piper_voices(tmp_path):
    voices = tmp_path / "voices"
    voices.mkdir()
    (voices / "de_DE-thorsten-high.onnx").write_bytes(b"x" * 100)
    (voices / "de_DE-thorsten-high.onnx.json").write_bytes(b"{}")
    (voices / "voices_catalog.json").write_bytes(b"{}")
    assert retire.remove_piper_voices(voices) == 104 and not voices.exists()
    assert retire.remove_piper_voices(voices) == 0
    voices.mkdir()
    (voices / "notiz.txt").write_text("bleibt")
    (voices / "x.onnx").write_bytes(b"x")
    assert retire.remove_piper_voices(voices) == 1 and (voices / "notiz.txt").exists()


@pytest.fixture
def client(cfg, tmp_path, monkeypatch):
    monkeypatch.setenv("ORBWISE_FAKE_LLM", "1")
    monkeypatch.setenv("ORBWISE_SKIP_WARMUP", "1")
    monkeypatch.setenv("HF_HOME", str(tmp_path / "hf"))
    cfg.voice.enabled = True
    from orbwise.server import create_app
    app = create_app(cfg)
    yield TestClient(app, base_url="http://localhost:8765")
    prompts.set_spoken_language(None)


def test_pocket_endpoints(client, tmp_path, monkeypatch):
    data = client.get("/api/voices").json()
    assert data["pocket"]["installed"] is False and data["current"] == "juergen" and data["language"] == "de"
    assert data["voices"][0]["name"] == "juergen" and {"code": "en", "label": "Englisch"} in data["languages"]
    assert client.get("/api/voices/juergen/preview").status_code == 404

    def fake_install(data_dir, out):
        exe = pocket.binary(data_dir)
        exe.parent.mkdir(parents=True, exist_ok=True)
        exe.write_text("#!/bin/sh\n")
        return True
    monkeypatch.setattr(pocket, "install", fake_install)
    monkeypatch.setattr(pocket, "cpu_supported", lambda cpuinfo=None: True)
    monkeypatch.setattr(pocket.PocketTTS, "warm", lambda self, voice: None)
    assert client.post("/api/pocket/install", headers={"Origin": "http://evil.example"}).status_code == 403
    assert client.post("/api/pocket/install").json()["pocket"]["installed"]

    monkeypatch.setattr(pocket.PocketTTS, "synth", lambda self, text, voice: pocket.rewrap_wav(placeholder_wav(b"\0\0")))
    r = client.get("/api/voices/juergen/preview")
    assert r.status_code == 200 and r.headers["content-type"] == "audio/wav"

    assert client.post("/api/voices/select", json={"voice": "nope"}).status_code == 400
    r = client.post("/api/voices/select", json={"voice": "alba", "language": "en"}).json()
    assert r == {"ok": True, "current": "alba", "language": "en"}
    data = client.get("/api/voices").json()
    assert data["current"] == "alba" and data["language"] == "en"
    assert client.get("/api/voices/juergen/preview").status_code == 409  # Juergen spricht Deutsch, geladen ist Englisch
    assert client.get("/api/voices/alba/preview").status_code == 200
    assert prompts.spoken(None, "yes_no") == prompts.SPOKEN["en"]["yes_no"]

    r = client.delete("/api/pocket")
    assert r.json()["ok"] and not pocket.is_installed(tmp_path)

    monkeypatch.setattr(pocket, "install", lambda data_dir, out: out("✘ Download fehlgeschlagen: kaputt") or False)
    r = client.post("/api/pocket/install")
    assert r.status_code == 502 and r.json()["detail"] == "Download fehlgeschlagen: kaputt"


def test_voice_language_changes_answers(cfg, tmp_path, monkeypatch):
    """Englische Stimme → der Systemprompt verlangt englische Antworten; zurück auf Deutsch → Zeile weg."""
    monkeypatch.setenv("ORBWISE_FAKE_LLM", "1")
    monkeypatch.setenv("ORBWISE_SKIP_WARMUP", "1")
    cfg.voice.enabled = True
    seen = {}
    from orbwise import server as server_mod
    real_agent = server_mod.Agent

    def spy(*a, **k):
        seen["agent"] = real_agent(*a, **k)
        return seen["agent"]
    monkeypatch.setattr(server_mod, "Agent", spy)
    client = TestClient(server_mod.create_app(cfg), base_url="http://localhost:8765")
    try:
        agent = seen["agent"]
        assert agent.reply_language is None and "Englisch" not in agent.system_prompt()
        client.post("/api/voices/select", json={"voice": "juergen", "language": "en"})
        assert agent.reply_language == "en" and "immer auf Englisch" in agent.system_prompt()
        client.post("/api/voices/select", json={"voice": "juergen", "language": "de"})
        assert agent.reply_language is None and "immer auf Englisch" not in agent.system_prompt()
    finally:
        prompts.set_spoken_language(None)


def test_former_piper_users_get_pocket(cfg, tmp_path, monkeypatch):
    """Wer Piper-Stimmen hatte: beim Start löschen und Pocket TTS gleich einrichten."""
    import time
    monkeypatch.setenv("ORBWISE_FAKE_LLM", "1")
    monkeypatch.setenv("ORBWISE_SKIP_WARMUP", "1")
    cfg.voice.enabled = True
    voices = tmp_path / "voices"
    voices.mkdir()
    (voices / "de_DE-thorsten-high.onnx").write_bytes(b"x" * 10)
    installed = []
    monkeypatch.setattr(pocket, "cpu_supported", lambda cpuinfo=None: True)
    monkeypatch.setattr(pocket, "install", lambda data_dir, out: installed.append(data_dir) or False)
    from orbwise.server import create_app
    with TestClient(create_app(cfg), base_url="http://localhost:8765"):
        for _ in range(100):
            if installed:
                break
            time.sleep(0.02)
    assert installed == [tmp_path] and not voices.exists()
