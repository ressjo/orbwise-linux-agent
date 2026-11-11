"""Werkzeug-Aufrufe, die ein Modell als Text schreibt (Vorlage und Server passen nicht zusammen): erkennen,
ausführen, nicht als rohes JSON im Chat zeigen. Dazu die Denk-Bremse: Gedanken, die sich im Kreis drehen, werden abgebrochen."""

import pytest
from conftest import run

from orbwise import textcalls
from orbwise.agent import Agent, thought_loop

KNOWN = {"read", "edit", "shell"}
READ = {"function": {"name": "read", "arguments": {"path": "a.py"}}}


@pytest.mark.parametrize("text, rest, calls", [
    ('<tool_call>\n{"name": "read", "arguments": {"path": "a.py"}}\n</tool_call>', "", [READ]),
    ('Ich lese.\n<tool_call>{"name": "read", "arguments": {"path": "a.py"}}</tool_call>\n'
     '<tool_call>{"name": "shell", "arguments": "{\\"command\\": \\"ls\\"}"}</tool_call>', "Ich lese.",
     [READ, {"function": {"name": "shell", "arguments": {"command": "ls"}}}]),
    ('<tool_call>\n<function=edit>\n<parameter=path>\na.py\n</parameter>\n<parameter=oldString>\nx = 1\n  y\n'
     '</parameter>\n<parameter=replaceAll>\ntrue\n</parameter>\n</function>\n</tool_call>', "",
     [{"function": {"name": "edit", "arguments": {"path": "a.py", "oldString": "x = 1\n  y", "replaceAll": True}}}]),
    ('<function=read>{"path": "a.py"}</function>', "", [READ]),
    ('[TOOL_CALLS] [{"name": "read", "arguments": {"path": "a.py"}}]', "", [READ]),
    ('[TOOL_CALLS]read{"path": "a.py"}', "", [READ]),
    ('{"name": "read", "parameters": {"path": "a.py"}}', "", [READ]),
])
def test_common_formats_become_calls(text, rest, calls):
    assert textcalls.parse(text, KNOWN) == (rest, calls)


@pytest.mark.parametrize("text", [
    '{"name": "unbekannt", "arguments": {}}',                              # kein Werkzeug dieses Namens
    'So sieht ein Aufruf aus: {"name": "read", "arguments": {}}',          # JSON in einer Erklärung
    '<tool_call>{"name": "read", "arguments": {"path": "a.p',              # abgeschnitten
    'Normale Antwort mit <b>HTML</b> und {"a": 1}.',
    '<tool_call>{"name": "read", "arguments": [1, 2]}</tool_call>',        # Argumente sind kein Objekt
    "",
])
def test_everything_else_stays_text(text):
    assert textcalls.parse(text, KNOWN) == (text, [])


def test_gate_holds_back_what_could_be_a_call():
    def stream(text, size=7):
        gate = textcalls.Gate()
        return "".join(gate.feed(text[i:i + size]) for i in range(0, len(text), size)), gate.held()
    call = '<tool_call>{"name": "read", "arguments": {}}</tool_call>'
    assert stream(call) == ("", call)
    assert stream("Ich lese die Datei.\n" + call) == ("Ich lese die Datei.\n", call)
    assert stream('{"name": "read", "arguments": {}}')[0] == ""
    plain = "Der Vergleich a < b und ein [Link] bleiben, auch {\"a\": 1}."
    assert stream(plain, 3) == (plain, "")
    assert stream("Fertig <") == ("Fertig ", "<")  # ein möglicher Anfang am Ende wartet – der Aufrufer zeigt ihn dann


class TextCaller:
    """Schreibt den Aufruf als Text in die Antwort – wie ein Modell, dessen Vorlage der Server nicht versteht."""

    def __init__(self, fmt):
        self.fmt = fmt

    async def chat_stream(self, messages, tools=None, **_):
        if messages[-1]["role"] == "tool":
            text = "Inhalt gelesen."
        else:
            text = "Ich sehe nach.\n" + self.fmt
        for i in range(0, len(text), 5):
            yield {"type": "token", "text": text[i:i + 5]}
        yield {"type": "done", "message": {"role": "assistant", "content": text}, "stats": {}}


@pytest.mark.parametrize("fmt", [
    '<tool_call>\n{"name": "read_file", "arguments": {"path": "PFAD"}}\n</tool_call>',
    '[TOOL_CALLS] [{"name": "read_file", "arguments": {"path": "PFAD"}}]',
    '<function=read_file><parameter=path>PFAD</parameter></function>',
])
def test_agent_runs_a_call_written_as_text(cfg, memory, tmp_path, fmt):
    (tmp_path / "notiz.txt").write_text("Geheimzahl 4711")
    agent = Agent(cfg, TextCaller(fmt.replace("PFAD", str(tmp_path / "notiz.txt"))), memory)
    events = []

    async def emit(ev):
        events.append(ev)

    async def confirm(*a):
        return True
    answer = run(agent.run("Lies die Notiz", emit, confirm))
    assert any(e["type"] == "tool_call" and e["name"] == "read_file" for e in events)
    assert any("4711" in e.get("text", "") for e in events if e["type"] == "tool_result")
    shown = "".join(e["text"] for e in events if e["type"] == "token")
    assert "tool_call" not in shown and "TOOL_CALLS" not in shown and "<function" not in shown  # kein rohes JSON
    assert "Ich sehe nach." in shown and answer.endswith("Inhalt gelesen.")
    first = next(m for m in memory.conversation.history if m["role"] == "assistant")
    assert first["content"] == "Ich sehe nach." and first["tool_calls"][0]["function"]["name"] == "read_file"


def test_text_that_only_looks_like_a_call_is_still_shown(cfg, memory):
    agent = Agent(cfg, TextCaller('<tool_call>{"name": "gibt_es_nicht", "arguments": {}}</tool_call>'), memory)
    events = []

    async def emit(ev):
        events.append(ev)
    answer = run(agent.run("Hallo", emit, emit))
    shown = "".join(e["text"] for e in events if e["type"] == "token")
    assert "gibt_es_nicht" in shown and "gibt_es_nicht" in answer and not any(e["type"] == "tool_call" for e in events)


# ---------------------------------------------------------------- Denk-Bremse
def test_thought_loop_is_recognised():
    step = "Ich sollte die Datei lesen. Aber vielleicht zuerst suchen? Nein, lesen ist besser. Oder doch erst suchen, um sicher zu sein. "
    assert thought_loop(step * 2) == 0                       # zu kurz
    varied = "".join(f"Schritt {i}: ich prüfe Datei nummer {i * 7} und vergleiche sie mit Version {i * 3}. " for i in range(60))
    assert thought_loop(varied) == 0                         # lang, aber jedes Mal etwas Neues
    looped = "Zuerst der Plan: app.py ansehen. " + step * 20
    at = thought_loop(looped)
    assert 0 < at < len(looped) / 4                          # das Brauchbare: bis zur ersten Wiederholung
    assert " ".join(looped.split())[:at].startswith("Zuerst der Plan")


class Thinker:
    """Denkt vor jeder Antwort – im Kreis (loop) oder kurz. Zählt, wie oft mit und ohne Denken gefragt wurde."""

    def __init__(self, loop=False, plan=()):
        self.loop, self.plan, self.think_flags = loop, list(plan), []

    async def thinking_caps(self):
        return "toggle"

    async def chat_stream(self, messages, tools=None, think=None, **_):
        self.think_flags.append(bool(think))
        if think:
            words = ("Soll ich lesen oder suchen? Lesen wäre gut, aber suchen vielleicht besser, also nochmal überlegen. "
                     * (60 if self.loop else 1)).split(" ")
            for w in words:
                yield {"type": "reasoning", "text": w + " "}
        n = sum(1 for m in messages if m["role"] == "tool")
        call = self.plan[n] if n < len(self.plan) else None
        if call and tools is not None and messages[-1]["role"] != "user" or call and n == 0:
            yield {"type": "done", "message": {"role": "assistant", "content": "", "tool_calls": [{"function": call}]},
                   "stats": {}}
        else:
            yield {"type": "token", "text": "Fertig."}
            yield {"type": "done", "message": {"role": "assistant", "content": "Fertig."}, "stats": {}}


def _ask(cfg, memory, llm):
    agent = Agent(cfg, llm, memory)
    events = []

    async def emit(ev):
        events.append(ev)

    async def confirm(*a):
        return True
    answer = run(agent.run("Was steht drin?", emit, confirm, think=True))
    return answer, events


def test_looping_thoughts_are_cut_also_in_a_large_window(cfg, memory):
    cfg.llm.num_ctx = 65536
    llm = Thinker(loop=True)
    answer, events = _ask(cfg, memory, llm)
    cut = [e for e in events if e.get("phase") == "think_cut"]
    assert len(cut) == 1 and cut[0]["loop"] is True and cut[0]["tokens"] < 1500  # früh, nicht erst nach 6.000 Wörtern
    assert llm.think_flags == [True, False] and answer == "Fertig."  # derselbe Schritt noch einmal ohne Denken


def test_short_thoughts_are_never_cut(cfg, memory):
    llm = Thinker()
    _answer, events = _ask(cfg, memory, llm)
    assert llm.think_flags == [True] and not any(e.get("phase") == "think_cut" for e in events)
