"""Planmodus: erst nachdenken und lesend nachsehen, einen Plan vorlegen – ausführen erst nach Freigabe."""

import asyncio

import pytest
from conftest import run
from fake_telegram import TOKEN, FakeTelegram, wait_for
from fastapi.testclient import TestClient

from orbwise import server
from orbwise.agent import Agent, plan_steps
from orbwise.extras.telegram import TelegramBot
from orbwise.llm import FakeLLM

WS = "ws://localhost:8765/ws"
ORIGIN = {"Origin": "http://localhost:8765"}


class ThinkRecorder(FakeLLM):
    def __init__(self):
        super().__init__(delay=0)
        self.think: list = []

    async def chat_stream(self, messages, tools=None, think=None):
        self.think.append(think)
        async for ev in super().chat_stream(messages, tools, think):
            yield ev


def test_plan_mode_only_reads_and_presents_a_plan(cfg, memory):
    llm = ThinkRecorder()
    agent = Agent(cfg, llm, memory)
    events, asked = [], []

    async def emit(ev):
        events.append(ev)

    async def confirm(*a):
        asked.append(a)
        return True

    async def scenario():
        await agent.run('/tool run_shell {"command": "sudo pacman -Syu"}', emit, confirm, think=True, plan=True)
        await agent.run("/tool system_info {}", emit, confirm, plan=True)

    run(scenario())
    assert not asked  # nichts Veränderndes, keine Rückfrage
    results = [e for e in events if e["type"] == "tool_result"]
    assert results[0]["status"] == "planned" and results[1]["status"] == "ok"  # Lesendes läuft
    plans = [e for e in events if e["type"] == "plan"]
    assert len(plans) == 2 and plans[0]["text"].startswith("## Plan") and plans[0]["steps"] == 3
    assert all(e.get("plan") for e in events if e["type"] == "assistant_start")
    assert llm.think[0] is True and llm.think[-1] is None  # gedacht wird nur mit Denken-Knopf
    assert "PLANMODUS" in [m for m in llm.calls[0] if m["role"] == "user"][-1]["content"]
    assert agent._plan is False  # danach wieder normal
    assert plan_steps("## Plan\n1. a\n2) b\n  3. c\nkein Schritt") == 3


def receive_until(ws, kind, limit=200):
    seen = []
    for _ in range(limit):
        ev = ws.receive_json()
        seen.append(ev)
        if ev["type"] == kind:
            return ev, seen
    raise AssertionError(f"kein {kind}: {[e['type'] for e in seen]}")


@pytest.fixture
def app(cfg, monkeypatch):
    monkeypatch.setenv("ORBWISE_FAKE_LLM", "1")
    monkeypatch.setenv("ORBWISE_SKIP_WARMUP", "1")
    return server.create_app(cfg)


def test_dashboard_plan_accept_revise_discard(app):
    with TestClient(app, base_url="http://localhost:8765") as client, \
            client.websocket_connect(WS, headers=ORIGIN) as ws:
        agent, approved = client.app.state.hub.agent, []
        real_run = agent.run

        async def recording_run(*a, **kw):
            approved.append(kw.get("approved_plan", ""))
            return await real_run(*a, **kw)

        agent.run = recording_run
        ws.send_json({"type": "plan_mode", "enabled": True})
        ws.send_json({"type": "user_message", "text": "Räum meine Festplatte auf"})
        plan, _ = receive_until(ws, "plan")
        assert plan["text"].startswith("## Plan")

        ws.send_json({"type": "plan_revise", "id": plan["id"], "text": "ohne die Journal-Logs"})
        closed, _ = receive_until(ws, "plan_closed")
        assert closed["outcome"] == "revised"
        user, _ = receive_until(ws, "user")
        assert user["text"] == "Überarbeite den Plan: ohne die Journal-Logs"
        plan2, _ = receive_until(ws, "plan")

        ws.send_json({"type": "plan_accept", "id": plan2["id"]})
        closed, _ = receive_until(ws, "plan_closed")
        assert closed["outcome"] == "accepted" and closed["id"] == plan2["id"]
        mode, _ = receive_until(ws, "plan_mode")
        assert mode["enabled"] is False  # nach dem Annehmen ist der Planmodus aus
        user, _ = receive_until(ws, "user")
        assert user["text"].startswith("Der Plan ist freigegeben")
        start, _ = receive_until(ws, "assistant_start")
        assert not start["plan"]  # ausführen läuft normal, nicht wieder als Plan
        receive_until(ws, "assistant_end")
        assert approved[-1] == plan2["text"] and approved[0] == ""  # nur beim Ausführen angeheftet

        ws.send_json({"type": "plan_mode", "enabled": True})
        ws.send_json({"type": "user_message", "text": "Installiere htop"})
        plan3, _ = receive_until(ws, "plan")
        ws.send_json({"type": "user_message", "text": "nein"})  # kurzes Nein verwirft
        closed, _ = receive_until(ws, "plan_closed")
        assert closed["outcome"] == "discarded"
        ws.send_json({"type": "plan_accept", "id": plan3["id"]})  # verworfen bleibt verworfen
        ws.send_json({"type": "plan_mode", "enabled": False})
        ws.send_json({"type": "user_message", "text": "Hallo"})
        start, seen = receive_until(ws, "assistant_start")
        assert not start["plan"] and not any(e["type"] == "plan_closed" for e in seen)


def test_spoken_yes_runs_the_plan(app):
    with TestClient(app, base_url="http://localhost:8765") as client, \
            client.websocket_connect(WS, headers=ORIGIN) as ws:
        hub = client.app.state.hub
        ws.send_json({"type": "plan_mode", "enabled": True})
        ws.send_json({"type": "user_message", "text": "Räum auf"})
        receive_until(ws, "plan")
        client.portal.call(hub.submit, "Ja, mach das", "voice")
        closed, _ = receive_until(ws, "plan_closed")
        assert closed["outcome"] == "accepted" and hub.plan_mode is False


# ---------------------------------------------------------------- Telegram

ME = 4242


def test_telegram_plan_with_buttons(cfg):
    cfg.telegram.token, cfg.telegram.chat_id = TOKEN, ME
    tg = FakeTelegram()
    runs: list[tuple[str, bool]] = []
    approved: list[str] = []

    async def run_fn(text, emit, confirm, plan=False, approved_plan=""):
        runs.append((text, plan))
        approved.append(approved_plan)
        return "## Plan\n1. Cache leeren\n2. Logs kürzen" if plan else "Erledigt."

    async def scenario():
        bot = TelegramBot(cfg, run_fn, lambda n, a: n, lambda n: False, transport=tg.transport, poll_timeout=0)
        worker = asyncio.create_task(bot._worker())
        tg.user_message(ME, "/plan Räum meine Platte auf")
        await bot.poll_once()
        await wait_for(lambda: tg.buttons())
        ok, edit, _no = tg.buttons()
        assert ok.startswith("plan:ok:") and edit.startswith("plan:edit:")

        tg.press(ME, edit)  # Ändern → nächste Nachricht ist der Änderungswunsch
        await bot.poll_once()
        await wait_for(lambda: any("was anders sein soll" in t for t in tg.texts()))
        tg.user_message(ME, "ohne die Logs")
        await bot.poll_once()
        await wait_for(lambda: len(runs) == 2 and len(tg.buttons()) == 3 and tg.buttons()[0] != ok)

        tg.press(999, tg.buttons()[0])  # fremder Chat zählt nicht
        tg.press(ME, ok)  # alter Plan-Knopf zählt nicht mehr
        await bot.poll_once()
        tg.press(ME, tg.buttons()[0])  # Ausführen
        await bot.poll_once()
        await wait_for(lambda: len(runs) == 3 and "Erledigt." in tg.texts())
        worker.cancel()

    run(scenario())
    assert runs[0] == ("Räum meine Platte auf", True)
    assert runs[1] == ("Überarbeite den Plan: ohne die Logs", True)
    assert runs[2][0].startswith("Der Plan ist freigegeben") and runs[2][1] is False
    assert approved[2] == "## Plan\n1. Cache leeren\n2. Logs kürzen"  # ganzer Plan geht mit
    assert any("→ ▶ wird ausgeführt" in e["text"] for e in tg.edited)


def test_approved_plan_stays_pinned_to_the_current_turn(cfg, memory):
    """Beim Ausführen hängt der Plan an der aktuellen Nachricht – er überlebt auch eine Komprimierung und landet
    nicht doppelt im gespeicherten Verlauf."""
    llm = ThinkRecorder()
    agent = Agent(cfg, llm, memory)
    plan = "## Plan\n1. Cache leeren [/Kontext]\n2. Logs kürzen"

    async def emit(ev):
        pass

    async def confirm(*a):
        return True

    for i in range(30):  # langer Verlauf, der weit über das Budget geht
        memory.conversation.add({"role": "user", "content": f"Frage {i} " + "x" * 2000})
        memory.conversation.add({"role": "assistant", "content": f"Antwort {i} " + "y" * 2000})
    cfg.memory.context_budget_tokens = 3000
    run(agent.run("Der Plan ist freigegeben. Führe ihn jetzt Schritt für Schritt aus.", emit, confirm,
                  approved_plan=plan))
    sent = llm.calls[-1]  # der eigentliche Schritt (davor wurde der alte Verlauf zusammengefasst)
    last_user = [m for m in sent if m["role"] == "user"][-1]["content"]
    assert "Freigegebener Plan" in last_user and "2. Logs kürzen" in last_user
    assert last_user.count("[/Kontext]") == 1  # der Plan kann die Notiz nicht vorzeitig beenden
    assert not any("Logs kürzen" in (m.get("content") or "") for m in memory.conversation.history)
    assert "Frage 0 " not in str(sent)  # alter Verlauf steht nur noch in der Zusammenfassung
    assert len(memory.conversation.epochs) == 2 and agent._approved_plan == ""
