"""Mails senden (optional): nur nach Bestätigung, Empfänger/Betreff/Text im Fenster bearbeitbar."""

import asyncio
import email
import smtplib
from email import policy

import pytest
from conftest import run
from fake_imap import PASSWORD, USER, FakeImap
from fastapi.testclient import TestClient

from orbwise.agent import Agent
from orbwise.extras import mail
from orbwise.hub import Hub
from orbwise.tools.registry import CONFIRM, ToolContext, get_tool, load_all_tools, tool_schemas


class FakeSMTP:
    """Nimmt die Stelle von smtplib.SMTP ein und merkt sich, was gesendet würde."""
    sent: list = []
    fail: Exception | None = None

    def __init__(self, host, port, timeout=None):
        self.host, self.port, self.tls, self.user = host, port, False, None

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def starttls(self, context=None):
        self.tls = True

    def login(self, user, password):
        if password != PASSWORD:
            raise smtplib.SMTPAuthenticationError(535, b"nope")
        self.user = user

    def send_message(self, msg, to_addrs=None):
        if FakeSMTP.fail:
            raise FakeSMTP.fail
        FakeSMTP.sent.append({"msg": msg, "to": to_addrs, "tls": self.tls, "server": (self.host, self.port)})


@pytest.fixture
def smtp(cfg, monkeypatch):
    monkeypatch.delenv("ORBWISE_MAIL_PASSWORD", raising=False)
    monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)
    FakeSMTP.sent, FakeSMTP.fail = [], None
    with FakeImap() as server:
        m = cfg.mail
        m.host, m.port, m.security = "127.0.0.1", server.port, "none"
        m.username, m.password, m.send_enabled = USER, PASSWORD, True
        yield FakeSMTP


def test_send_is_optional_and_always_confirmed(cfg, monkeypatch):
    monkeypatch.setenv("ORBWISE_MAIL_PASSWORD", "x")
    load_all_tools()
    cfg.mail.username = USER
    assert "mail_send" not in {s["function"]["name"] for s in tool_schemas(cfg)}  # standardmäßig aus
    cfg.mail.send_enabled = True
    assert "mail_send" in {s["function"]["name"] for s in tool_schemas(cfg)}
    spec = get_tool("mail_send")
    assert spec.editable == ("to", "cc", "subject", "body")
    risk, reason = spec.assess(ToolContext(cfg=cfg, memory=None), {"to": "a@b.de"})
    assert risk == CONFIRM and "a@b.de" in reason
    assert cfg.mail.smtp_server == "127.0.0.1" and cfg.mail.smtp_port == 1025 and cfg.mail.sender == USER


def test_send_mail_and_reply(cfg, smtp):
    ctx = ToolContext(cfg=cfg, memory=None)
    out = run(mail.mail_send(ctx, to="Anna <anna@example.org>; bob@example.org", subject="Hallo",
                             body="Zeile 1\nZeile 2", cc="chef@example.org"))
    assert out.startswith("Mail gesendet an") and "Hallo" in out
    sent = smtp.sent[-1]
    msg = sent["msg"]
    assert sent["tls"] and sent["server"] == ("127.0.0.1", 1025)
    assert sent["to"] == ["anna@example.org", "bob@example.org", "chef@example.org"]
    assert msg["From"] == USER and msg["Subject"] == "Hallo" and "Anna <anna@example.org>" in msg["To"]
    assert msg["Message-ID"] and msg.get_content().startswith("Zeile 1")
    # Antwort: Bezug auf die Original-Mail
    run(mail.mail_send(ctx, to="joerg@example.org", subject="Re: Grüße aus Köln", body="Danke!", reply_uid=3))
    reply = smtp.sent[-1]["msg"]
    assert reply["In-Reply-To"] and reply["References"].endswith(reply["In-Reply-To"])


def test_send_errors_are_reported_not_raised(cfg, smtp):
    ctx = ToolContext(cfg=cfg, memory=None)
    assert "Ungültige Adresse" in run(mail.mail_send(ctx, to="kein-empfänger", subject="x", body="y"))
    assert run(mail.mail_send(ctx, to="", subject="x", body="y")) == "Kein Empfänger angegeben."
    smtp.fail = smtplib.SMTPRecipientsRefused({"x@y.de": (550, b"no")})
    assert "Empfänger abgelehnt" in run(mail.mail_send(ctx, to="x@y.de", subject="x", body="y"))
    cfg.mail.password = "falsch"
    smtp.fail = None
    assert "Anmeldung" in run(mail.mail_send(ctx, to="x@y.de", subject="x", body="y"))
    assert not smtp.sent


def test_agent_sends_what_the_user_edited(cfg, memory, llm, smtp):
    agent = Agent(cfg, llm, memory)
    events, asked = [], []

    async def emit(ev):
        events.append(ev)

    async def confirm(call_id, name, args, reason):
        asked.append(dict(args))
        # Nutzer ändert im Fenster Empfänger und Text – und versucht ein nicht freigegebenes Feld
        return True, {"to": "neu@example.org", "subject": args["subject"], "body": "Geänderter Text", "reply_uid": 99}

    run(agent.run('/tool mail_send {"to": "alt@example.org", "subject": "Termin", "body": "Entwurf"}', emit, confirm))
    assert asked[0]["to"] == "alt@example.org"
    msg = smtp.sent[-1]["msg"]
    assert msg["To"] == "neu@example.org" and msg.get_content().strip() == "Geänderter Text"
    assert "In-Reply-To" not in msg  # reply_uid ist nicht bearbeitbar
    result = next(e for e in events if e["type"] == "tool_result")["text"]
    assert "Vom Nutzer vor dem Ausführen geändert: to, body" in result


def test_agent_denied_mail_is_not_sent(cfg, memory, llm, smtp):
    agent = Agent(cfg, llm, memory)

    async def emit(ev):
        pass

    async def deny(*a):
        return False

    run(agent.run('/tool mail_send {"to": "a@example.org", "subject": "x", "body": "y"}', emit, deny))
    assert not smtp.sent


def test_hub_returns_edits_and_ignores_spoken_yes(cfg, smtp):
    load_all_tools()
    sent = []
    hub = Hub.__new__(Hub)
    hub.cfg, hub.pending, hub.editable_pending = cfg, {}, set()

    class Speaker:
        def say(self, *a):
            pass

    async def broadcast(ev):
        sent.append(ev)

    hub.speaker, hub.broadcast = Speaker(), broadcast

    async def scenario():
        args = {"to": "a@example.org", "subject": "x", "body": "y"}
        task = asyncio.create_task(hub.confirm("c1", "mail_send", args, ""))
        await asyncio.sleep(0.01)
        for cid in list(hub.pending):  # „Ja“ per Sprache zählt hier nicht
            if cid not in hub.editable_pending:
                hub.resolve(cid, True)
        assert not task.done()
        hub.resolve("c1", True, {"to": "b@example.org"})
        return await task

    assert asyncio.run(scenario()) == (True, {"to": "b@example.org"})
    request = next(e for e in sent if e["type"] == "confirm_request")
    assert request["editable"] == ["to", "cc", "subject", "body"]


def test_websocket_confirm_with_edits(cfg, smtp, monkeypatch):
    monkeypatch.setenv("ORBWISE_FAKE_LLM", "1")
    monkeypatch.setenv("ORBWISE_SKIP_WARMUP", "1")
    from orbwise.server import create_app
    with TestClient(create_app(cfg), base_url="http://localhost:8765") as client:
        with client.websocket_connect("ws://localhost:8765/ws", headers={"Origin": "http://localhost:8765"}) as ws:
            ws.receive_json()
            ws.send_json({"type": "user_message",
                          "text": '/tool mail_send {"to": "alt@example.org", "subject": "Hi", "body": "Entwurf"}'})
            while True:
                ev = ws.receive_json()
                if ev["type"] == "confirm_request":
                    break
            assert ev["editable"] == ["to", "cc", "subject", "body"] and ev["args"]["to"] == "alt@example.org"
            ws.send_json({"type": "confirm", "id": ev["id"], "approved": True,
                          "args": {"to": "neu@example.org", "subject": "Hi!", "body": "Fertig", "cc": ""}})
            while True:
                ev = ws.receive_json()
                if ev["type"] == "tool_result":
                    break
            assert ev["status"] == "ok" and "neu@example.org" in ev["text"]
    msg = smtp.sent[-1]["msg"]
    parsed = email.message_from_bytes(msg.as_bytes(), policy=policy.default)
    assert parsed["To"] == "neu@example.org" and parsed["Subject"] == "Hi!"
