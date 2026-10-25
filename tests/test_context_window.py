"""Kontextfenster wie Claude Code: append-only innerhalb einer Epoche, seltene Komprimierung mit Cache-Treffer,
Vorwärmen, begrenzte Werkzeug-Ausgaben.

Kern-Eigenschaft: Zwischen zwei Komprimierungen verlängert jede Anfrage die vorige nur hinten (gleiche Werkzeuge,
gleiche Nachrichten davor) – der Modell-Server liest dann nur das Neue ein."""

import asyncio
import json
import os

import pytest
from conftest import run
from fastapi.testclient import TestClient

from orbwise import prompts, server
from orbwise.agent import SUMMARY_MIN, Agent
from orbwise.config import ProfileConfig
from orbwise.memory.context import est_tokens, msg_tokens
from orbwise.tools import proc
from orbwise.tools.registry import ToolContext, get_tool, load_all_tools


async def _noop(*a):
    return True


def tool_names(tools) -> list[str]:
    return [t["function"]["name"] for t in tools or []]


def extends(prev: tuple, cur: tuple) -> bool:
    """Verlängert die Anfrage cur die Anfrage prev nur hinten (gleiche Werkzeuge, gleicher Anfang)?"""
    (pt, pm), (ct, cm) = prev, cur
    return tool_names(pt) == tool_names(ct) and cm[:len(pm)] == pm


PLACEHOLDER = {"role": "user", "content": ""}  # Vorwärmen hängt sie an – an ihrer Stelle kommt die nächste Frage


def request_tokens(tools, messages) -> int:
    """Größe einer Anfrage, geschätzt wie im Agenten (Nachrichten + Werkzeugbeschreibungen)."""
    return sum(msg_tokens(m) for m in messages) + est_tokens(json.dumps(tools or [], ensure_ascii=False))


def is_compaction(messages) -> bool:
    return "Kontextfenster ist fast voll" in (messages[-1].get("content") or "")


def requests(llm, skip: set[int] = frozenset()) -> list[tuple]:
    return [(o["tools"], c[:-1] if o["max_tokens"] == 1 and c[-1] == PLACEHOLDER else c)
            for i, (o, c) in enumerate(zip(llm.opts, llm.calls)) if i not in skip]


@pytest.fixture
def stamps(monkeypatch):
    """Jede Anfrage bekommt eine andere Uhrzeit – eingefrorene Notizen dürfen sich trotzdem nie ändern."""
    n = iter(range(1000))
    monkeypatch.setattr(prompts, "note_stamp", lambda cfg, now: f"Montag, 14:{next(n):02d} Uhr")


def test_prompt_only_grows_at_the_end_across_turns(cfg, llm, memory, stamps):
    run(memory.index.add("journal", "2026-09-01", "chat:anderer", "Das NAS heißt Tresor und hat die IP 10.0.0.5"))
    agent = Agent(cfg, llm, memory)
    run(agent.run("Wie heißt mein NAS Tresor?", _noop, _noop))  # mit Erinnerung
    run(agent.run('/tool remember {"fact": "Der Nutzer heißt Alex."}', _noop, _noop))  # neuer Fakt
    run(agent.run("/tool system_info {}", _noop, _noop))  # Werkzeug-Ergebnis
    run(agent.run("Räum auf", _noop, _noop, plan=True))  # Planmodus-Runde
    side_from = len(llm.calls)
    run(agent.run_in_chat("", "📱 Telegram", "Hallo vom Handy", _noop, _noop))  # Telegram dazwischen
    side = set(range(side_from, len(llm.calls)))
    run(agent.run("Und wie geht's?", _noop, _noop))
    reqs = requests(llm, side)
    assert len(reqs) >= 7
    breaks = [i for i in range(1, len(reqs)) if not extends(reqs[i - 1], reqs[i])]
    assert breaks == []  # kein einziges Mal von vorne einlesen
    first_user = [m for m in reqs[-1][1] if m["role"] == "user"][0]["content"]
    assert "Tresor" in first_user and "14:00" in first_user  # Notiz der ersten Frage steht unverändert da
    assert "Der Nutzer heißt Alex." not in reqs[-1][1][0]["content"]  # System-Prompt blieb gleich


def test_compaction_reuses_the_cached_prompt_and_keeps_everything(cfg, llm, memory, stamps):
    agent = Agent(cfg, llm, memory)
    for i in range(6):
        run(agent.run(f"Frage {i}: " + "x" * 2500, _noop, _noop))
    k = next(i for i, c in enumerate(llm.calls) if is_compaction(c))
    req, prev = llm.calls[k], llm.calls[k - 1]
    # Anfrage = der Prompt, den der Server schon kennt (+ letzte Antwort) + eine Anweisung → kaum Neues einzulesen
    assert req[:len(prev)] == prev and "Kontextfenster ist fast voll" in req[-1]["content"]
    assert llm.opts[k]["tools"] == llm.opts[k - 1]["tools"] and llm.opts[k]["tool_choice"] == "none"
    assert llm.opts[k]["think"] is False
    conv = memory.conversation
    assert len(conv.epochs) == 2 and "Alle Nutzernachrichten" in conv.running_summary
    assert "Nutzernachrichten seit der letzten Zusammenfassung" in conv.running_summary  # vom Code ergänzt
    assert llm.opts[k + 1]["max_tokens"] == 1  # gleich danach vorgewärmt
    after = llm.calls[k + 1]
    # Systemprompt und Werkzeuge bleiben gleich (im Cache des Servers) – die Zusammenfassung steht vor der ersten
    # Nachricht der neuen Epoche (wie bei Claude Code)
    assert after[0] == prev[0] and llm.opts[k + 1]["tools"] == llm.opts[k]["tools"]
    assert conv.running_summary not in after[0]["content"] and conv.running_summary in after[1]["content"]
    assert after[1]["content"].startswith("[Kontext") and after[1]["content"].count("[/Kontext]") == 1
    old = "Frage 0: " + "x" * 300  # nur noch gekürzt in der Liste der Nutzernachrichten
    assert old not in json.dumps(after[1:], ensure_ascii=False) and any("Frage" in (m.get("content") or "")
                                                                          for m in after[1:])
    # nichts gelöscht: der ganze Chat steht noch in der Datei (und in der Oberfläche)
    assert sum(1 for m in conv.history if m["role"] == "user") == 6
    # nach der Komprimierung wächst der Prompt wieder nur hinten
    reqs = requests(llm)
    breaks = [i for i in range(1, len(reqs)) if not extends(reqs[i - 1], reqs[i])]
    assert breaks == [k + 1]


def test_retrieval_finds_compacted_parts_of_the_same_chat(cfg, llm, memory, stamps):
    agent = Agent(cfg, llm, memory)
    run(agent.run("Mein Router heißt Fritzi " + "x" * 2500, _noop, _noop))
    for i in range(5):
        run(agent.run(f"Frage {i}: " + "y" * 2500, _noop, _noop))
    conv = memory.conversation
    assert len(conv.epochs) >= 2
    hits = run(memory.retrieve("Wie heißt mein Router Fritzi?", exclude_after=conv.window_start()))
    assert any("Fritzi" in h.text for h in hits)  # aus dem komprimierten Teil wieder auffindbar


class Worker:
    """Liest Datei um Datei (lange Ausgaben), bis `steps` erledigt sind – versteht Komprimierung und Vorwärmen."""

    profile = ProfileConfig(backend="openai", base_url="http://x/v1", model="klein", num_ctx=16384)
    context_size = 16384

    def __init__(self, folder, steps=24):
        self.folder, self.steps, self.done = folder, steps, 0
        self.requests: list[tuple] = []

    async def chat_stream(self, messages, tools=None, think=None, max_tokens=None, tool_choice=None):
        self.requests.append((tools, messages, max_tokens))
        last = messages[-1].get("content") or ""
        if "Kontextfenster ist fast voll" in last:
            msg = {"role": "assistant", "content": f"9. Nächster Schritt: weiter mit log{self.done}.txt"}
        elif max_tokens == 1:
            msg = {"role": "assistant", "content": "."}
        elif self.done < self.steps:
            msg = {"role": "assistant", "content": "", "tool_calls": [{"function": {
                "name": "read_file", "arguments": {"path": str(self.folder / f"log{self.done}.txt")}}}]}
            self.done += 1
        else:
            msg = {"role": "assistant", "content": f"Fertig: {self.done} Dateien gelesen."}
        yield {"type": "done", "message": msg, "stats": {}}

    async def chat(self, messages):
        return "Zusammenfassung"


def test_long_task_compresses_rarely_and_continues(cfg, memory, tmp_path, stamps):
    for n in range(30):
        (tmp_path / f"log{n}.txt").write_text("\n".join(f"Log {n} Zeile {i}: " + "daten " * 12 for i in range(60)),
                                              encoding="utf-8")
    cfg.tools.max_steps = 40
    llm = Worker(tmp_path)
    agent = Agent(cfg, llm, memory)
    events = []

    async def emit(ev):
        events.append(ev)

    answer = run(agent.run("Lies alle Logs", emit, _noop))
    assert answer == "Fertig: 24 Dateien gelesen." and not any(e["type"] == "error" for e in events)
    compactions = [r for r in llm.requests if is_compaction(r[1])]
    clears = [e for e in events if e.get("phase") == "done" and e.get("cleared")]
    # 24 × ~1,6k Token Ausgabe passen nie in 16k: wird das Fenster voll, werden zuerst alte Ergebnisse ausgeblendet
    # (kein Modellaufruf) – zusammengefasst wird erst, wenn das nicht mehr reicht
    assert clears and 1 <= len(compactions) <= 2
    sizes = [request_tokens(tools, msgs) for tools, msgs, _ in llm.requests]
    assert max(sizes) <= 16384  # nie übergelaufen
    for tools, msgs, max_tokens in compactions:  # die Zusammenfassung passt mit hinein (knapp: kürzer, ≥ 500)
        assert max_tokens >= SUMMARY_MIN and request_tokens(tools, msgs) + max_tokens <= 16384
    # Dazwischen nur hinten verlängert (Brüche nur direkt nach dem Ausblenden bzw. einer Komprimierung)
    reqs = [(t, m) for t, m, _ in llm.requests]
    breaks = [i for i in range(1, len(reqs)) if not extends(reqs[i - 1], reqs[i])]
    assert len(breaks) == len(compactions) + len(clears)
    # nach einer Komprimierung mitten in der Aufgabe: Frage und letzter Schritt gehen wörtlich mit, und die Frage
    # sagt dem Modell, dass die Aufgabe weiterläuft
    k = next(i for i, r in enumerate(llm.requests) if is_compaction(r[1]))
    after = llm.requests[k + 1][1]
    assert [m["role"] for m in after[1:]] == ["user", "assistant", "tool"]
    assert "Die Aufgabe läuft noch" in after[1]["content"] and after[1]["content"].endswith("Lies alle Logs")
    assert "Nächster Schritt" in after[1]["content"]  # Zusammenfassung vor der ersten Nachricht der Epoche
    # nach der fertigen Aufgabe (Komprimierung in Ruhe) steht der Hinweis nicht mehr da
    if memory.conversation.epochs[-1].get("reason") == "idle":
        question = next(m for m in memory.conversation.epoch_messages() if m["role"] == "user")
        assert "Die Aufgabe läuft noch" not in question.get("note", "")


def test_one_big_task_does_not_compact_at_half_the_window(cfg, memory, tmp_path, stamps):
    """Gemeldet: bei „8k/16k“ wurde schon zusammengefasst – eine große Aufgabe als bisher einzige Runde galt als
    „typische nächste Runde“. Jetzt erst kurz vor der Grenze."""
    for n in range(4):
        (tmp_path / f"log{n}.txt").write_text("\n".join(f"Log {n} Zeile {i}: " + "daten " * 12 for i in range(60)),
                                              encoding="utf-8")
    llm = Worker(tmp_path, steps=3)
    agent = Agent(cfg, llm, memory)
    run(agent.run("Lies die Logs", _noop, _noop))
    agent.build_messages(display=False)
    assert 7000 <= agent._full_prompt <= 0.75 * 16384  # gut die Hälfte belegt – die alte Logik komprimierte hier
    assert not any(is_compaction(r[1]) for r in llm.requests) and len(memory.conversation.epochs) == 1
    assert agent.compact_limit() == 14884  # 91 % – erst dort (oder kurz davor nach einer Antwort)


def test_memories_first_turn_full_then_only_new_and_none_in_coding(cfg, llm, memory, stamps):
    run(memory.index.add("journal", "2026-09-01", "chat:anderer", "Das NAS heißt Tresor und steht im Keller"))
    agent = Agent(cfg, llm, memory)
    run(agent.run("Wo steht das NAS Tresor?", _noop, _noop))
    run(agent.run("Und wie heißt das NAS Tresor?", _noop, _noop))
    notes = [m.get("note", "") for m in memory.conversation.history if m["role"] == "user"]
    assert "im Keller" in notes[0] and "im Keller" not in notes[1]  # schon gezeigt → nicht noch einmal
    memory.switch_mode("coding")
    run(agent.run("Wo steht das NAS Tresor?", _noop, _noop))
    coding_note = [m for m in memory.conversation.history if m["role"] == "user"][-1]["note"]
    assert "Erinnerungen" not in coding_note  # Coding: nur auf Anfrage (recall)


@pytest.mark.parametrize("window,think,limit", [(8192, False, 7168), (16384, False, 14884),
                                                 (16384, True, 13384), (32768, False, 30402)])
def test_compaction_point_leaves_room_for_answer_and_summary(cfg, memory, window, think, limit):
    llm = type("W", (), {"context_size": window})()
    agent = Agent(cfg, llm, memory)
    agent._think = think
    assert agent.compact_limit() == limit


def test_manual_compact_with_focus(cfg, llm, memory):
    agent = Agent(cfg, llm, memory)
    for i in range(10):
        memory.conversation.add({"role": "user", "content": f"Frage {i} " + "x" * 2000})
        memory.conversation.add({"role": "assistant", "content": f"Antwort {i}"})
    events = []

    async def emit(ev):
        events.append(ev)

    assert run(agent.compact_now(emit, focus="die IP-Adressen"))
    instruction = next(c[-1]["content"] for c in llm.calls if "Kontextfenster ist fast voll" in c[-1]["content"])
    assert "Besonders wichtig laut Nutzer: die IP-Adressen" in instruction
    assert not run(agent.compact_now(emit))  # gleich danach: nichts mehr zu komprimieren


def test_sticky_tool_groups_only_grow_within_an_epoch(cfg, memory):
    """Ab 16k: einmal geladene Gruppen bleiben bis zur Komprimierung (unter 16k: siehe test_context_regimes.py)."""
    cfg.tools.extra = ["sysadmin"]
    class Small:
        context_size = 16384
        sent: list[set] = []

        async def chat_stream(self, messages, tools=None):
            self.sent.append(set(tool_names(tools)))
            yield {"type": "done", "message": {"role": "assistant", "content": "ok"}, "stats": {}}

    cfg.homeassistant.url, cfg.homeassistant.token = "http://ha", "t"
    llm = Small()
    agent = Agent(cfg, llm, memory)
    events = []

    async def emit(ev):
        events.append(ev)

    run(agent.run("Mach das Licht im Wohnzimmer an", emit, _noop))
    run(agent.run("Wie wird das Wetter?", emit, _noop))
    run(agent.run("Warum ist mein Rechner so langsam?", emit, _noop))
    assert "ha_control" in llm.sent[0] and "top_processes" not in llm.sent[1]
    assert llm.sent[0] <= llm.sent[1] <= llm.sent[2] and "top_processes" in llm.sent[2]
    added = [e for e in events if e.get("phase") == "tools_added"]
    assert [e["groups"] for e in added] == [["sysadmin"]]  # einmal neu einlesen – und das wird angezeigt


def test_compaction_retries_without_tools_when_the_model_calls_one(cfg, llm, memory):
    """llama-server liefert bei tool_choice "none" einen Aufruf als Text – das ist keine Zusammenfassung."""
    agent = Agent(cfg, llm, memory)
    for i in range(4):
        run(agent.run(f"Frage {i}: " + "x" * 2500, _noop, _noop))
    fake_stream, seen = llm.chat_stream, []

    async def stream(messages, tools=None, think=None, max_tokens=None, tool_choice=None):
        if is_compaction(messages):
            seen.append(tool_choice)
            if tools:
                call = '<tool_call>\n{"name": "recall", "arguments": {"query": "Frage"}}\n</tool_call>'
                yield {"type": "token", "text": call}
                yield {"type": "done", "message": {"role": "assistant", "content": call}}
                return
        async for ev in fake_stream(messages, tools, think=think, max_tokens=max_tokens, tool_choice=tool_choice):
            yield ev

    llm.chat_stream = stream
    assert run(agent.compact_now(_noop))
    assert seen == ["none", None]  # erst mit Werkzeugen (Cache), dann ohne
    summary = memory.conversation.running_summary
    assert "Nächster Schritt" in summary and "<tool_call>" not in summary


def test_prewarm_reads_the_chat_after_telegram(cfg, llm, memory):
    agent = Agent(cfg, llm, memory)
    run(agent.run("Hallo", _noop, _noop))
    assert not agent.cache_cold() and not run(agent.prewarm())  # schon im Cache – nichts zu tun
    run(agent.run_in_chat("", "📱 Telegram", "Hallo vom Handy", _noop, _noop))
    assert agent.cache_cold()  # der Server hat jetzt den Telegram-Chat im Cache
    assert run(agent.prewarm())
    pre = llm.calls[-1]
    assert llm.opts[-1]["max_tokens"] == 1 and not agent.cache_cold()
    # Verlauf samt letzter Antwort, dann eine leere Nutzernachricht – eine Antwort am Ende würde llama-server
    # als angefangene Antwort fortsetzen (anders formatiert als im nächsten Prompt)
    assert pre[-2]["role"] == "assistant" and pre[-1] == {"role": "user", "content": ""}
    run(agent.run("Und weiter?", _noop, _noop))
    assert llm.calls[-1][:len(pre) - 1] == pre[:-1]  # die nächste Frage nutzt genau das Vorgewärmte


def test_prewarm_uses_the_thinking_mode_of_the_next_question(cfg, llm, memory):
    """Die Chat-Vorlage rendert mit anderem Denkmodus schon den Anfang anders – früher wurde immer ohne Denken
    vorgewärmt, und die Frage mit Denken las trotzdem alles neu ein."""
    agent = Agent(cfg, llm, memory)
    run(agent.run("Hallo", _noop, _noop, think="high"))
    agent._cache_owner = None  # z. B. nach Telegram
    assert run(agent.prewarm(think="high"))
    pre = llm.opts[-1]
    run(agent.run("Und weiter?", _noop, _noop, think="high"))
    assert pre["max_tokens"] == 1 and (pre["think"], pre["effort"]) == (True, "high")
    assert (llm.opts[-1]["think"], llm.opts[-1]["effort"]) == (True, "high")
    assert agent._reread["reason"] == "appended"  # die Frage baut genau auf dem Vorgewärmten auf
    agent._cache_owner = None
    assert run(agent.prewarm())  # ohne Denken-Knopf: Einstellung des Profils – nichts mitschicken
    assert llm.opts[-1]["think"] is None and llm.opts[-1]["effort"] is None


def test_think_toggle_makes_the_cache_cold_and_prewarms(cfg, monkeypatch, tmp_path):
    monkeypatch.setenv("ORBWISE_FAKE_LLM", "1")
    monkeypatch.setenv("ORBWISE_SKIP_WARMUP", "1")
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "run"))
    app = server.create_app(cfg)
    hub = app.state.hub
    calls = []
    with TestClient(app, base_url="http://localhost:8765") as client:
        hub.prewarm_soon = lambda: calls.append(hub.think)
        with client.websocket_connect("ws://localhost:8765/ws", headers={"Origin": "http://localhost:8765"}) as ws:
            ws.receive_json()  # hello
            hub.agent._cache_owner = ("x",)
            ws.send_json({"type": "think", "enabled": True, "level": "high"})
            ws.send_json({"type": "think", "enabled": True, "level": "high"})  # unverändert – nichts zu tun
            synced = 0
            for _ in range(40):
                synced += ws.receive_json()["type"] == "settings_sync"
                if synced == 2:
                    break
    # (None: Vorwärmen beim Verbinden der Oberfläche) – nur der echte Wechsel wärmt neu vor
    assert hub.think == "high" and hub.agent._cache_owner is None and calls == [None, "high"]


def test_chat_switch_during_prewarm_warms_the_new_chat_afterwards(cfg, memory):
    """Läuft noch das Vorwärmen des vorigen Chats, wird der neue danach eingelesen (statt erst bei der Frage)."""
    from orbwise.llm import FakeLLM
    llm = FakeLLM(delay=0.01)
    memory.llm = llm
    agent = Agent(cfg, llm, memory)
    hub = server.Hub(cfg, agent, None, None, None)

    async def scenario():
        await agent.run("Hallo", _noop, _noop)
        agent._cache_owner = None  # z. B. nach einer Telegram-Anfrage
        hub.prewarm_soon()
        await asyncio.sleep(0)  # Vorwärmen des ersten Chats läuft
        assert agent._prewarming
        memory.new_chat()
        hub.prewarm_soon()  # Chatwechsel während des Vorwärmens
        await hub._prewarm
        return [o["max_tokens"] for o in llm.opts].count(1)

    assert run(scenario()) == 2 and not agent.cache_cold()


def test_read_file_pages_instead_of_cutting_the_middle(cfg, tmp_path):
    load_all_tools()
    ctx = ToolContext(cfg=cfg, memory=None)
    big = tmp_path / "gross.log"
    big.write_text("\n".join(f"Zeile {i}" for i in range(1, 5001)), encoding="utf-8")
    read = get_tool("read_file").func
    first = run(read(ctx, path=str(big)))
    head, *lines = first.splitlines()
    end = len(lines)
    assert head.startswith(f"[{big}: Zeilen 1–{end} von 5000") and f"offset={end + 1}" in head
    assert lines[0] == "Zeile 1" and lines[-1] == f"Zeile {end}"
    page = run(read(ctx, path=str(big), offset=2500, limit=3))
    assert page.splitlines()[1:] == ["Zeile 2500", "Zeile 2501", "Zeile 2502"]
    small = tmp_path / "klein.txt"
    small.write_text("nur kurz", encoding="utf-8")
    assert run(read(ctx, path=str(small))) == "nur kurz"


def test_long_outputs_are_saved_completely(tmp_path):
    text = "\n".join(f"Ausgabezeile {i}" for i in range(3000))
    out = proc.clip_saved(text, 2000, "shell")
    assert len(out) < 2400 and "Ausgabezeile 0" in out and "Ausgabezeile 2999" in out
    path = out.rsplit("Ausgabe (", 1)[1].split(": ", 1)[1].split(" – ")[0]
    assert open(path, encoding="utf-8").read() == text  # nichts verloren
    assert oct(os.stat(os.path.dirname(path)).st_mode & 0o777) == "0o700"
    assert proc.clip_saved("kurz", 2000) == "kurz"


@pytest.fixture
def client(cfg, monkeypatch):
    monkeypatch.setenv("ORBWISE_FAKE_LLM", "1")
    monkeypatch.setenv("ORBWISE_SKIP_WARMUP", "1")
    return TestClient(server.create_app(cfg), base_url="http://localhost:8765")


def test_compact_command_and_history_dividers(client):
    with client, client.websocket_connect("ws://localhost:8765/ws", headers={"Origin": "http://localhost:8765"}) as ws:
        conv = client.app.state.memory.conversation
        for i in range(10):
            conv.add({"role": "user", "content": f"Frage {i} " + "x" * 2000})
            conv.add({"role": "assistant", "content": f"Antwort {i}"})
        ws.send_json({"type": "user_message", "text": "/compact"})
        for _ in range(200):
            ev = ws.receive_json()
            if ev["type"] == "compacted":
                break
        assert ev["summary"] and ev["after"] < ev["before"]
        h = client.get("/api/history").json()
        assert len(h["messages"]) == 20 and h["epochs"] and h["epochs"][0]["summary"] == ev["summary"]
        assert h["messages"][h["epochs"][0]["at"]]["content"].startswith("Frage 9")
        assert not any(m["content"].startswith("/compact") for m in h["messages"])  # Befehl, keine Nachricht


def test_model_level_is_sent_without_own_limits(cfg, llm, memory, stamps):
    """Die Stufe gibt das Modell vor – Orbwise bricht im großen Fenster nicht selbst ab und schreibt auch keinen
    „denke kurz“-Hinweis dazu (das zwang früher einen zweiten Anlauf ohne Denken, samt neu eingelesenem Prompt)."""
    llm.thought_repeat = 200  # ~4.000 Token Denkkette
    agent = Agent(cfg, llm, memory)
    events = []

    async def emit(ev):
        events.append(ev)

    assert run(agent.run("Wie spät ist es?", emit, _noop, think="low"))
    assert len(llm.opts) == 1 and (llm.opts[0]["think"], llm.opts[0]["effort"]) == (True, "low")
    assert not any(e.get("phase") == "think_cut" for e in events)
    note = [m for m in memory.conversation.history if m["role"] == "user"][-1]["note"]
    assert "Denke nur kurz" not in note
    agent._set_think("low")
    assert agent.answer_reserve() == 3000 and agent.think_budget() == 0
    agent._set_think(False)
    assert agent.answer_reserve() == 1500
