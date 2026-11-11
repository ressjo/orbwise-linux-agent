"""Telegram-Bot: nur der eigene Chat, Antworten, Rückfragen per Knopf, Sprachnachrichten, Erinnerungen."""

import asyncio

import pytest
from conftest import run
from fake_telegram import TOKEN, FakeTelegram, wait_for
from fastapi.testclient import TestClient

from orbwise.extras.telegram import TelegramBot, split_text

ME = 4242


@pytest.fixture
def tg(cfg):
    cfg.telegram.token, cfg.telegram.chat_id = TOKEN, ME
    return FakeTelegram()


def make_bot(cfg, fake, run_fn, transcribe=None, on_stop=None):
    return TelegramBot(cfg, run_fn, lambda n, a: f"die Aktion {n}", lambda n: n == "mail_send",
                       transcribe=transcribe, transport=fake.transport, poll_timeout=0, on_stop=on_stop)


def test_split_text():
    parts = split_text("a" * 3000 + "\n\n" + "b" * 3000, limit=4000)
    assert parts == ["a" * 3000, "b" * 3000]  # an Absätzen geteilt
    parts = split_text("x" * 9000, limit=4000)
    assert [len(p) for p in parts] == [4000, 4000, 1000]


def test_only_own_chat_is_served(cfg, tg):
    asked = []

    async def run_fn(text, emit, confirm):
        asked.append(text)
        return f"Antwort auf {text}"

    async def scenario():
        bot = make_bot(cfg, tg, run_fn)
        worker = asyncio.create_task(bot._worker())
        tg.user_message(999, "Wie spät ist es?")          # fremder Chat
        tg.user_message(ME, "/start")
        tg.user_message(ME, "Wie wird das Wetter?")
        await bot.poll_once()
        await wait_for(lambda: any("Antwort auf" in t for t in tg.texts()))
        worker.cancel()

    run(scenario())
    assert asked == ["Wie wird das Wetter?"]  # nur der eigene Chat, /start ist keine Anfrage
    assert not [m for m in tg.sent if m["chat_id"] == 999]  # eingerichtet: Fremde bekommen gar keine Antwort
    mine = [m["text"] for m in tg.sent if m["chat_id"] == ME]
    assert "Schreib oder sprich" in mine[0] and mine[-1] == "Antwort auf Wie wird das Wetter?"


def test_chat_id_is_only_revealed_during_setup(cfg, tg):
    cfg.telegram.chat_id = 0

    async def scenario():
        bot = make_bot(cfg, tg, None)
        tg.user_message(999, "/start")
        await bot.poll_once()

    run(scenario())
    reply = next(m for m in tg.sent if m["chat_id"] == 999)
    assert "nicht freigeschaltet" in reply["text"] and "999" in reply["text"]


def test_token_never_leaks(cfg, tg, caplog):
    """Der Bot-Token steckt in jeder API-URL – er darf weder im Log noch in Antworten aufs Handy landen."""
    from orbwise.extras.telegram import redact

    assert redact(f"GET https://api.telegram.org/bot{TOKEN}/getUpdates", TOKEN).count("<token>") == 1
    assert "AAH" not in redact("https://api.telegram.org/bot1234567890:AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw/x", "")

    async def transcribe(data):
        return "egal"

    async def scenario():
        bot = make_bot(cfg, tg, None, transcribe=transcribe)
        tg.user_message(ME, voice="gibt-es-nicht")  # Telegram liefert 404 für die Datei
        await bot.poll_once()

    with caplog.at_level("DEBUG"):
        run(scenario())
        # auch Tracebacks und httpx-Logs (orbwise serve -v) werden bereinigt
        import logging
        try:
            raise RuntimeError(f"kaputt: https://api.telegram.org/bot{TOKEN}/getFile")
        except RuntimeError:
            logging.getLogger("orbwise.extras.telegram").exception("Fehler mit %s", TOKEN)
        logging.getLogger("httpx").info("HTTP Request: POST https://api.telegram.org/bot%s/getUpdates", TOKEN)
    assert "404" in caplog.text and "kaputt" in caplog.text and "Traceback" in caplog.text
    assert TOKEN not in caplog.text
    assert all(TOKEN not in t for t in tg.texts())


def test_confirmation_buttons(cfg, tg):
    results = []

    async def run_fn(text, emit, confirm):
        results.append(await confirm("c1", "install_package", {"names": "htop"}, ""))
        results.append(await confirm("c2", "mail_send", {"to": "a@b.de"}, ""))  # nur im Dashboard
        return "fertig"

    async def scenario():
        bot = make_bot(cfg, tg, run_fn)
        worker = asyncio.create_task(bot._worker())
        tg.user_message(ME, "Installier htop")
        await bot.poll_once()
        await wait_for(lambda: tg.buttons())
        ok, no = tg.buttons()
        assert ok.startswith("ok:") and no.startswith("no:")
        tg.press(999, ok)          # fremder Knopfdruck zählt nicht
        await bot.poll_once()
        await asyncio.sleep(0.05)
        assert not results
        tg.press(ME, ok)
        await bot.poll_once()
        await wait_for(lambda: "fertig" in tg.texts())
        worker.cancel()

    run(scenario())
    assert results == [True, False]
    assert "Soll ich die Aktion install_package ausführen?" in next(m["text"] for m in tg.sent if "reply_markup" in m)
    assert tg.edited and "ausgeführt" in tg.edited[-1]["text"]
    assert any("Das geht nur am PC" in t for t in tg.texts())


def test_voice_message_is_transcribed(cfg, tg):
    asked = []

    async def run_fn(text, emit, confirm):
        asked.append(text)
        return "ok"

    async def transcribe(data):
        assert data == b"OggS-fake"
        return "Erinner mich in zehn Minuten an den Tee"

    async def scenario():
        bot = make_bot(cfg, tg, run_fn, transcribe)
        worker = asyncio.create_task(bot._worker())
        tg.user_message(ME, voice="voice-1")
        await bot.poll_once()
        await wait_for(lambda: "ok" in tg.texts())
        worker.cancel()

    run(scenario())
    assert asked == ["Erinner mich in zehn Minuten an den Tee"]
    assert "🎙 „Erinner mich in zehn Minuten an den Tee“" in tg.texts()


def test_notify_needs_setup(cfg, tg):
    cfg.telegram.chat_id = 0
    bot = make_bot(cfg, tg, None)
    assert run(bot.notify("x")) is False and not tg.sent


def test_server_telegram_chat_confirm_and_reminder(cfg, tg, tmp_path, monkeypatch):
    monkeypatch.setenv("ORBWISE_FAKE_LLM", "1")
    monkeypatch.setenv("ORBWISE_SKIP_WARMUP", "1")
    monkeypatch.setattr(TelegramBot, "TRANSPORT", tg.transport)
    from orbwise.server import create_app
    target = tmp_path / "notiz.txt"
    with TestClient(create_app(cfg), base_url="http://localhost:8765") as client:
        memory = client.app.state.memory
        active_before = memory.conversation.chat_id

        def until(cond, timeout=8.0):
            client.portal.call(wait_for, cond, timeout)

        # Frage vom Handy → Antwort im eigenen Chat „📱 Telegram“
        tg.user_message(ME, "Hallo vom Handy")
        until(lambda: any("Hallo vom Handy" in t for t in tg.texts()))
        titles = [c["title"] for c in memory.chats.list()]
        assert "📱 Telegram" in titles and memory.conversation.chat_id == active_before
        # Aktion mit Rückfrage → Knopf am Handy
        tg.user_message(ME, f'/tool write_file {{"path": "{target}", "content": "vom Handy"}}')
        until(lambda: tg.buttons())
        tg.press(ME, tg.buttons()[0])
        until(lambda: target.exists())
        assert target.read_text() == "vom Handy"
        # Erinnerung → kommt aufs Handy
        tg.user_message(ME, '/tool set_reminder {"text": "Tee holen", "in_minutes": 0.02}')
        until(lambda: any(t.startswith("⏰") and "Tee holen" in t for t in tg.texts()), timeout=10)


def test_startup_check_and_errors_are_reported(cfg, tg, monkeypatch):
    from orbwise.extras import telegram

    async def scenario():
        bot = make_bot(cfg, tg, None)
        await bot.check()
        return bot

    bot = run(scenario())
    assert bot.status["bot"] == "@orbwise_test_bot" and tg.webhook_deleted
    assert "Token ungültig" in telegram.explain("Telegram getMe: Unauthorized")
    assert "anderes Programm" in telegram.explain("Conflict: terminated by other getUpdates request")
    assert "nicht erreichbar" in telegram.explain("ConnectError: [Errno -3] Temporary failure in name resolution")

    # falscher Token: der Bot läuft weiter, meldet den Fehler im Status statt still zu sterben
    cfg.telegram.token = "999:FALSCH"

    async def broken():
        bad = TelegramBot(cfg, None, lambda n, a: n, lambda n: False, transport=tg.transport, poll_timeout=0)
        task = asyncio.create_task(bad.serve())
        await wait_for(lambda: bad.status["error"])
        task.cancel()
        return bad.status

    status = run(broken())
    assert not status["running"] and "Token ungültig" in status["error"]


# ---------------------------------------------------------------- Dateien

def test_files_from_the_phone_are_saved(cfg, tg, tmp_path):
    cfg.telegram.inbox_dir = tmp_path / "inbox"
    asked = []

    async def run_fn(text, emit, confirm):
        asked.append(text)
        return "erledigt"

    async def scenario():
        bot = make_bot(cfg, tg, run_fn)
        worker = asyncio.create_task(bot._worker())
        # mit Beschriftung → direkt als Anfrage mit Hinweis auf die Datei
        tg.user_file(ME, "doc-1", b"%PDF-1.7 rechnung", name="Rechnung März.pdf", caption="ab in Paperless")
        # ohne Beschriftung → speichern, nachfragen; die nächste Nachricht bezieht sich darauf
        tg.user_file(ME, "doc-2", b"%PDF vertrag", name="../../etc/passwd")
        tg.user_file(ME, "photo-1", b"JPEGDATA", photo=True)
        await bot.poll_once()
        tg.user_message(ME, "fass das Foto zusammen")
        await bot.poll_once()
        tg.user_file(ME, "big", b"x", name="riesig.iso", size=30_000_000)
        await bot.poll_once()
        await wait_for(lambda: len(asked) == 2)
        worker.cancel()

    run(scenario())
    inbox = tmp_path / "inbox"
    saved = sorted(p.name for p in inbox.iterdir())
    assert any(n.startswith("foto-") for n in saved) and "Rechnung März.pdf" in saved
    assert "passwd" in saved and len(saved) == 3  # „../../etc/passwd“ landet entschärft im Eingangsordner
    assert (inbox / "Rechnung März.pdf").read_bytes() == b"%PDF-1.7 rechnung"
    assert asked[0].startswith("[Datei vom Handy empfangen und gespeichert unter") and asked[0].endswith("ab in Paperless")
    assert "foto-" in asked[1] and asked[1].endswith("fass das Foto zusammen")
    assert any("📥 Gespeichert:" in t and "Schreib mir, was damit passieren soll" in t for t in tg.texts())
    assert any("zu groß" in t for t in tg.texts()) and not (inbox / "riesig.iso").exists()


def test_send_file_tool(cfg, tg, tmp_path, monkeypatch):
    from orbwise.extras import telegram_tools
    from orbwise.tools.registry import BLOCKED, SAFE, ToolContext, get_tool, load_all_tools, tool_schemas

    load_all_tools()
    assert "telegram_send_file" in {s["function"]["name"] for s in tool_schemas(cfg)}
    doc = tmp_path / "Plan.pdf"
    doc.write_bytes(b"%PDF-1.7 plan")

    async def scenario():
        bot = make_bot(cfg, tg, None)
        ctx = ToolContext(cfg=cfg, memory=None, services={"telegram": bot})
        ok = await telegram_tools.telegram_send_file(ctx, path=str(doc), caption="Der Plan")
        missing = await telegram_tools.telegram_send_file(ctx, path=str(tmp_path / "fehlt.pdf"))
        return ok, missing

    ok, missing = run(scenario())
    assert ok.startswith("Aufs Handy geschickt: Plan.pdf") and "nicht gefunden" in missing
    assert tg.documents[0]["filename"] == "Plan.pdf" and b"%PDF-1.7 plan" in tg.documents[0]["raw"]
    assert b"Der Plan" in tg.documents[0]["raw"]
    spec = get_tool("telegram_send_file")
    ctx = ToolContext(cfg=cfg, memory=None)
    assert spec.assess(ctx, {"path": str(doc)})[0] == SAFE
    for secret in ("~/.ssh/id_ed25519", "/home/a/.gnupg/x", "~/.config/orbwise/config.yaml", "/proj/.env",
                   "~/.config/otherapp/config.yaml", "~/Backup/secrets.txt", "/etc/wireguard/wg0.conf"):
        assert spec.assess(ctx, {"path": secret})[0] == BLOCKED


def test_send_paperless_document_and_upload_from_inbox(cfg, tg, tmp_path, monkeypatch):
    from fake_paperless import TOKEN as PL_TOKEN
    from fake_paperless import FakePaperless

    from orbwise.extras import paperless as pl
    from orbwise.extras import telegram_tools
    from orbwise.tools.registry import CONFIRM, SAFE, ToolContext, get_tool, load_all_tools

    fake = FakePaperless()
    monkeypatch.setattr(pl, "TRANSPORT", fake.transport())
    monkeypatch.setattr(pl, "_KNOWN", {})
    cfg.paperless.url, cfg.paperless.token = "http://paperless.local:8000", PL_TOKEN
    cfg.telegram.inbox_dir = tmp_path / "inbox"
    (tmp_path / "inbox").mkdir()
    scan = tmp_path / "inbox" / "Brief.pdf"
    scan.write_bytes(b"%PDF brief")
    load_all_tools()
    doc_id = fake.docs[0]["id"]

    async def scenario():
        bot = make_bot(cfg, tg, None)
        ctx = ToolContext(cfg=cfg, memory=None, services={"telegram": bot})
        sent = await telegram_tools.telegram_send_file(ctx, paperless_id=doc_id)
        uploaded = await pl.paperless_upload(ctx, path=str(scan), title="Brief vom Amt")
        return sent, uploaded

    sent, uploaded = run(scenario())
    assert sent.startswith("Aufs Handy geschickt:") and tg.documents[0]["filename"].endswith(".pdf")
    assert b"%PDF-1.7 archiv" in tg.documents[0]["raw"]
    assert uploaded.startswith("An Paperless übergeben: Brief.pdf") and fake.uploads[-1]["title"] == "Brief vom Amt"
    spec, ctx = get_tool("paperless_upload"), ToolContext(cfg=cfg, memory=None)
    assert spec.assess(ctx, {"path": str(scan)})[0] == SAFE          # vom Handy geschickt
    assert spec.assess(ctx, {"path": "/etc/hosts"})[0] == CONFIRM    # beliebige Datei: nachfragen


# ---------------------------------------------------------------- Stoppen

def test_stop_from_the_phone(cfg, tg):
    started, pc_stops = asyncio.Event(), []
    confirmations = []

    async def slow(text, emit, confirm):
        if text == "Rückfrage":
            confirmations.append(await confirm("c1", "install_package", {"names": "htop"}, ""))
            return "nach Rückfrage"
        started.set()
        await asyncio.sleep(30)  # hängt – bis /stop kommt
        return "zu spät"

    async def on_stop():
        pc_stops.append(1)
        return 0  # am PC lief nichts

    async def scenario():
        bot = make_bot(cfg, tg, slow, on_stop=on_stop)
        worker = asyncio.create_task(bot._worker())
        # 1. nichts läuft
        tg.user_message(ME, "stopp")
        await bot.poll_once()
        # 2. laufende Anfrage abbrechen – die wartende gleich mit
        tg.user_message(ME, "Durchsuche alles")
        tg.user_message(ME, "Und noch was")
        await bot.poll_once()
        await started.wait()
        tg.user_message(ME, "/stop")
        await bot.poll_once()
        await asyncio.sleep(0.1)
        # 3. während einer offenen Knopf-Rückfrage: die ganze Anfrage wird abgebrochen
        tg.user_message(ME, "Rückfrage")
        await bot.poll_once()
        await wait_for(lambda: tg.buttons())
        tg.user_message(ME, "Stop!")
        await bot.poll_once()
        await asyncio.sleep(0.1)
        assert not bot.pending and (bot.current is None or bot.current.done())
        worker.cancel()

    run(scenario())
    texts = tg.texts()
    assert texts[0] == "Es läuft gerade nichts."
    assert texts.count("⏹ Gestoppt.") == 2 and "zu spät" not in texts
    assert confirmations == [] and "nach Rückfrage" not in texts  # nichts ausgeführt
    assert len(pc_stops) == 3  # jedes Mal auch am PC gestoppt


def test_stop_from_the_phone_cancels_pc_tasks(cfg, tg, monkeypatch):
    monkeypatch.setenv("ORBWISE_FAKE_LLM", "1")
    monkeypatch.setenv("ORBWISE_SKIP_WARMUP", "1")
    monkeypatch.setattr(TelegramBot, "TRANSPORT", tg.transport)
    from orbwise.server import create_app
    with TestClient(create_app(cfg), base_url="http://localhost:8765") as client:
        hub = client.app.state.hub

        async def start_pc_task():
            task = asyncio.create_task(asyncio.sleep(60))  # z. B. eine laufende Routine
            hub.tasks.add(task)
            return task

        async def is_cancelled(task):
            await asyncio.sleep(0.05)
            return task.cancelled()

        task = client.portal.call(start_pc_task)
        tg.user_message(ME, "/stop")
        client.portal.call(wait_for, lambda: "⏹ Gestoppt." in tg.texts(), 8.0)
        assert client.portal.call(is_cancelled, task)


def test_dashboard_is_not_opened_to_the_network_by_accident(cfg, monkeypatch):
    from types import SimpleNamespace

    from orbwise import cli, server

    assert server.remote_bind_warning(cfg) == ""  # 127.0.0.1
    for host in ("localhost", "::1", "127.0.0.2"):
        cfg.host = host
        assert server.remote_bind_warning(cfg) == ""
    cfg.host = "0.0.0.0"
    assert "ohne Anmeldung" in server.remote_bind_warning(cfg)
    started = []
    monkeypatch.setattr(cli, "load_config", lambda: cfg)
    monkeypatch.setattr("uvicorn.run", lambda app, **kw: started.append(kw["host"]))
    with pytest.raises(SystemExit) as stop:
        cli.cmd_serve(SimpleNamespace(open=False, verbose=False))
    assert "allow_remote" in str(stop.value) and not started
    cfg.allow_remote = True
    cli.cmd_serve(SimpleNamespace(open=False, verbose=False))
    assert started == ["0.0.0.0"]


def test_stop_on_the_pc_cancels_a_request_from_the_phone(cfg, tg, monkeypatch):
    monkeypatch.setenv("ORBWISE_FAKE_LLM", "1")
    monkeypatch.setenv("ORBWISE_SKIP_WARMUP", "1")
    monkeypatch.setattr(TelegramBot, "TRANSPORT", tg.transport)
    from orbwise.server import create_app
    with TestClient(create_app(cfg), base_url="http://localhost:8765") as client:
        hub, bot = client.app.state.hub, client.app.state.telegram

        async def phone_request():
            bot.current = asyncio.create_task(asyncio.sleep(60))  # läuft gerade vom Handy
            return bot.current

        async def stop_on_pc(task):
            stopped = await hub.stop()
            await asyncio.sleep(0.05)
            return stopped, task.cancelled()

        task = client.portal.call(phone_request)
        stopped, cancelled = client.portal.call(stop_on_pc, task)
        assert stopped >= 1 and cancelled


def test_config_files_behind_a_symlink_are_not_sent(cfg, tmp_path):
    from orbwise.tools.registry import BLOCKED, SAFE, ToolContext, get_tool, load_all_tools

    load_all_tools()
    other = tmp_path / "someapp"
    other.mkdir()
    (other / "config.yaml").write_text("api_key: geheim")
    (tmp_path / "notizen.txt").symlink_to(other / "config.yaml")
    (tmp_path / "echt.txt").write_text("hallo")
    spec, ctx = get_tool("telegram_send_file"), ToolContext(cfg=cfg, memory=None)
    assert spec.assess(ctx, {"path": str(tmp_path / "notizen.txt")})[0] == BLOCKED
    assert spec.assess(ctx, {"path": str(tmp_path / "echt.txt")})[0] == SAFE
