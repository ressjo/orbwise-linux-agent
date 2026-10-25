"""Kontext-Stufen: unter 16k sparsam (wenige Werkzeuge vorab, der Rest nach Bedarf, kurze Ergebnisse und
Zusammenfassungen), ab 16k großzügig. In beiden Stufen werden alte Werkzeug-Ergebnisse ausgeblendet, bevor
zusammengefasst wird – und eine Komprimierung lässt Systemprompt und Werkzeuge im Cache (wie bei Claude Code)."""

import json
import re

import pytest
from conftest import run
from fake_paperless import TOKEN, FakePaperless
from test_context_window import Worker, extends, is_compaction, request_tokens
from test_prompt_size import all_integrations, first_request

from orbwise.agent import Agent
from orbwise.config import ProfileConfig
from orbwise.context_plan import plan_for
from orbwise.memory.context import Conversation, est_tokens
from orbwise.tools import paperless as pl
from orbwise.tools.registry import ToolContext, get_tool, load_all_tools
from orbwise.toolselect import SMALL_CORE


async def _noop(*a):
    return True


def tool_names(tools) -> set[str]:
    return {t["function"]["name"] for t in tools or []}


class Recorder:
    """Merkt sich die Werkzeuge jeder Anfrage; antwortet nur kurz."""

    def __init__(self, window: int):
        self.context_size = window
        self.profile = ProfileConfig(backend="openai", base_url="http://x/v1", model="klein", num_ctx=window)
        self.sent: list[set[str]] = []
        self.loader: list[str] = []

    async def chat_stream(self, messages, tools=None, **kw):
        self.sent.append(tool_names(tools))
        self.loader.append(next((t["function"]["description"] for t in tools or []
                                 if t["function"]["name"] == "load_tools"), ""))
        yield {"type": "done", "message": {"role": "assistant", "content": "ok"}, "stats": {}}


# ---------------------------------------------------------------- Stufen
@pytest.mark.parametrize("window,small", [(4096, True), (8192, True), (12288, True), (16384, False), (32768, False)])
def test_plan_switches_at_16k(window, small):
    assert plan_for(window).small is small


def test_plan_values_for_both_stages():
    s, big = plan_for(8192), plan_for(16384)
    assert (s.answer_reserve, big.answer_reserve) == (1024, 1500)
    assert s.output_chars() == 2457 and big.output_chars() > 6000  # groß: begrenzt dann tools.max_output_chars
    assert (s.memories, s.facts) == (491, 409) and (big.memories, big.facts) == (1310, 983)
    assert (s.summary_max, s.summary_floor) == (655, 409) and (big.summary_max, big.summary_floor) == (1638, 983)
    assert s.think_budget() == 1638 and big.think_budget() == 0  # nur das kleine Fenster begrenzt das Denken
    assert s.reserve(True) == 1024 + 1638 and big.reserve(True) == 3000 and big.reserve(False) == 1500


# ---------------------------------------------------------------- Werkzeuge nach Bedarf
def test_small_window_starts_lean(cfg, llm, memory, tmp_path):
    all_integrations(cfg, tmp_path)
    cfg.llm.num_ctx = 8192
    system, names, tool_tokens = first_request(cfg, llm, memory, "Hallo")
    assert set(names) == SMALL_CORE
    assert est_tokens(system) + tool_tokens <= 2500  # ab 16k ~4,8k
    for absent in ("power", "weather", "set_reminder", "daily_briefing", "Paperless", "mail_list"):
        assert absent not in system  # Hinweise nur zu Werkzeugen, die das Modell auch hat
    assert "date_info" in system and "remember" in system and "load_tools" in system
    loader = next(t for t in llm.opts[0]["tools"] if t["function"]["name"] == "load_tools")["function"]["description"]
    assert "weather (Wetter)" in loader and "paperless (" in loader and "power (" in loader


@pytest.mark.parametrize("question,present,absent", [
    ("Wie wird das Wetter morgen?", "weather", "power"),
    ("Fahr den Rechner herunter", "power", "weather"),
    ("Erinnere mich in 10 Minuten an den Tee", "set_reminder", "power"),
    ("Öffne Firefox", "open_app", "paperless_search"),
    ("Such meine Rechnung von der Telekom", "paperless_search", "paperless_apply_metadata"),
    ("Sortier den Posteingang in Paperless", "paperless_review_next", "mail_manage"),
    ("Trag morgen um 9 einen Termin beim Zahnarzt ein", "calendar_add", "paperless_search"),
    ("Welche Termine habe ich nächste Woche?", "calendar_events", "calendar_add"),
])
def test_small_window_loads_tools_by_keyword(cfg, llm, memory, tmp_path, question, present, absent):
    all_integrations(cfg, tmp_path)
    cfg.llm.num_ctx = 8192
    _, names, _ = first_request(cfg, llm, memory, question)
    assert present in names and absent not in names


def test_yes_loads_what_the_model_proposed(cfg, memory, tmp_path):
    all_integrations(cfg, tmp_path)
    llm = Recorder(8192)
    agent = Agent(cfg, llm, memory)
    memory.conversation.add({"role": "user", "content": "Was ist neu?"})
    memory.conversation.add({"role": "assistant", "content": "Drei Newsletter. Soll ich sie archivieren?"})
    run(agent.run("Ja, bitte", _noop, _noop))
    assert "mail_manage" in llm.sent[-1]  # „ja“ bezieht sich auf den Vorschlag


def test_small_window_repacks_only_when_something_new_is_needed(cfg, memory, tmp_path):
    all_integrations(cfg, tmp_path)
    llm = Recorder(8192)
    agent = Agent(cfg, llm, memory)
    events = []

    async def emit(ev):
        events.append(ev)

    run(agent.run("Mach das Licht im Wohnzimmer an", emit, _noop))
    run(agent.run("Und im Flur auch", emit, _noop))
    assert llm.sent[1] == llm.sent[0] and "ha_control" in llm.sent[0]  # nichts Neues nötig – gleicher Anfang
    run(agent.run("Wie wird das Wetter?", emit, _noop))
    assert {"weather", "ha_control"} <= llm.sent[2]  # Gebrauchtes bleibt, solange es in die Grenze passt
    run(agent.run("Warum ist mein Rechner so langsam?", emit, _noop))
    assert "top_processes" in llm.sent[3] and "ha_control" not in llm.sent[3]  # Grenze: Älteres fällt weg
    assert "kill_process" not in llm.sent[3]  # nur der lesende Teil – nichts klingt nach Ändern
    assert "homeassistant (" in llm.loader[3] and "sysadmin (mehr:" in llm.loader[3]  # nachladbar, was fehlt
    changed = [e for e in events if e.get("phase") == "done" and e.get("tools_dropped")]
    assert changed and "homeassistant" in changed[-1]["tools_dropped"]
    plan = agent.plan()
    assert sum(agent._tool_cost(n) for n in llm.sent[2]) <= plan.tool_share * agent.context_budget()


def test_loaded_group_stays_pinned(cfg, memory, tmp_path):
    all_integrations(cfg, tmp_path)
    llm = Recorder(8192)
    agent = Agent(cfg, llm, memory)
    run(agent.run('/tool load_tools {"groups": "mail"}', _noop, _noop))  # Recorder ruft nichts auf – direkt laden
    memory.conversation.epoch["pinned"] = ["mail", "mail:write"]
    run(agent.run("Und sonst?", _noop, _noop))
    assert {"mail_list", "mail_manage"} <= llm.sent[-1]


# ---------------------------------------------------------------- Ergebnisse begrenzen
@pytest.fixture
def paperless(monkeypatch, cfg):
    f = FakePaperless()
    monkeypatch.setattr(pl, "TRANSPORT", f.transport())
    monkeypatch.setattr(pl, "_KNOWN", {})
    cfg.paperless.url, cfg.paperless.token = "http://paperless.local:8000", TOKEN
    return f


@pytest.mark.parametrize("window", [8192, 32768])
def test_service_tools_respect_the_window(cfg, paperless, window):
    ctx = ToolContext(cfg=cfg, memory=None, output_chars=plan_for(window).output_chars())
    out = run(pl.paperless_read(ctx, 7))
    offset = 2457 if window == 8192 else cfg.paperless.max_chars  # 8k: kürzere Abschnitte, ab 32k wie eingestellt
    assert f"weiter mit offset={offset}" in out


def test_overlong_results_are_cut_and_saved(cfg, llm, memory, monkeypatch):
    load_all_tools()
    cfg.llm.num_ctx = 8192
    huge = "\n".join(f"Zeile {i}: " + "inhalt " * 10 for i in range(400))
    monkeypatch.setattr(get_tool("system_info"), "func", lambda ctx: _const(huge))
    run(Agent(cfg, llm, memory).run("/tool system_info {}", _noop, _noop))
    result = [m for m in memory.conversation.history if m["role"] == "tool"][-1]["content"]
    assert len(result) <= 2600 and "Vollständige Ausgabe" in result
    path = re.search(r"Ausgabe \([^)]*\): (\S+) – ", result).group(1)
    assert open(path, encoding="utf-8").read() == huge  # nichts verloren


async def _const(text):
    return text


# ---------------------------------------------------------------- Ausblenden statt zusammenfassen
class SmallWorker(Worker):
    profile = ProfileConfig(backend="openai", base_url="http://x/v1", model="klein", num_ctx=8192)
    context_size = 8192


def test_small_window_hides_old_results_before_summarising(cfg, memory, tmp_path):
    for n in range(20):
        (tmp_path / f"log{n}.txt").write_text("\n".join(f"Log {n} Zeile {i}: " + "daten " * 12 for i in range(60)),
                                              encoding="utf-8")
    cfg.tools.max_steps = 40
    llm = SmallWorker(tmp_path, steps=16)
    agent = Agent(cfg, llm, memory)
    events = []

    async def emit(ev):
        events.append(ev)

    assert run(agent.run("Lies alle Logs", emit, _noop)) == "Fertig: 16 Dateien gelesen."
    clears = [e for e in events if e.get("phase") == "done" and e.get("cleared")]
    compactions = [r for r in llm.requests if is_compaction(r[1])]
    assert clears and len(compactions) <= len(clears)  # erst ausblenden (ohne Modellaufruf), selten zusammenfassen
    assert max(request_tokens(t, m) for t, m, _ in llm.requests) <= 8192
    reqs = [(t, m) for t, m, _ in llm.requests]
    breaks = [i for i in range(1, len(reqs)) if not extends(reqs[i - 1], reqs[i])]
    assert len(breaks) == len(clears) + len(compactions)  # sonst nur hinten verlängert
    for tools, msgs, max_tokens in compactions:  # kleines Fenster: kurze Anweisung, kurze Zusammenfassung
        assert "knappe Zusammenfassung" in msgs[-1]["content"] and "9." not in msgs[-1]["content"]
        assert max_tokens <= 900
    hidden = [m for m in memory.conversation.history if m.get("cleared") and m["role"] == "tool"]
    assert hidden and all("ausgeblendet" in m["content"] and m["full_content"] != m["content"] for m in hidden)
    path = re.search(r"vollständig in (\S+) ", hidden[0]["content"]).group(1)
    assert open(path, encoding="utf-8").read() == hidden[0]["full_content"]
    last = [m for m in memory.conversation.history if m["role"] == "tool"][-1]
    assert not last.get("cleared")  # der letzte Schritt bleibt ungekürzt


def test_hiding_shortens_long_arguments_and_keeps_originals():
    conv = Conversation()
    conv.add({"role": "user", "content": "Schreib die Datei und lies zwei andere"})
    conv.add({"role": "assistant", "content": "", "tool_calls": [
        {"function": {"name": "write_file", "arguments": {"path": "/tmp/x.txt", "content": "a" * 3000}}}]})
    conv.add({"role": "tool", "content": "Geschrieben.", "tool_name": "write_file"})
    for path, letter in (("/tmp/y.txt", "b"), ("/tmp/z.txt", "c")):
        conv.add({"role": "assistant", "content": "", "tool_calls": [
            {"function": {"name": "read_file", "arguments": {"path": path}}}]})
        conv.add({"role": "tool", "content": letter * 2000, "tool_name": "read_file"})
    idx = conv.clearable(keep_tokens=100)
    assert conv.clear_results(idx, lambda m: "[weg]") == 1  # „Geschrieben.“ ist zu kurz, der letzte Schritt bleibt
    write = conv.history[1]
    assert write["cleared"] and len(write["tool_calls"][0]["function"]["arguments"]["content"]) < 300
    assert write["full_tool_calls"][0]["function"]["arguments"]["content"] == "a" * 3000
    assert write["tool_calls"][0]["function"]["arguments"]["path"] == "/tmp/x.txt"
    assert conv.history[4]["content"] == "[weg]" and conv.history[4]["full_content"] == "b" * 2000
    assert conv.history[6]["content"] == "c" * 2000 and conv.epoch["cleared"] == 1


# ---------------------------------------------------------------- Zusammenfassen
def test_coding_compaction_attaches_current_files(cfg, llm, memory, tmp_path):
    memory.switch_mode("coding")
    memory.set_project(str(tmp_path))
    (tmp_path / "app.py").write_text("def hallo():\n    return 'Welt'\n", encoding="utf-8")
    agent = Agent(cfg, llm, memory)
    run(agent.run('/tool read_file {"path": "app.py"}', _noop, _noop))
    (tmp_path / "app.py").write_text("def hallo():\n    return 'Orbwise'\n", encoding="utf-8")  # später geändert
    for i in range(8):
        memory.conversation.add({"role": "user", "content": f"Frage {i} " + "x" * 2000})
        memory.conversation.add({"role": "assistant", "content": f"Antwort {i}"})
    assert run(agent.compact_now(_noop))
    first = agent.build_messages()[1]["content"]
    assert "Zuletzt bearbeitete Dateien" in first and "return 'Orbwise'" in first  # frisch von der Platte
    assert "Zuletzt bearbeitete" not in memory.conversation.running_summary  # die Anzeige bleibt übersichtlich


def test_small_window_scales_memories_facts_and_thinking(cfg, llm, memory):
    cfg.llm.num_ctx = 8192
    for i in range(60):
        memory.facts.add(f"Fakt Nummer {i}: " + "wichtig " * 6)
    agent = Agent(cfg, llm, memory)
    assert est_tokens(agent.epoch_system().split("## Dauerhafte Fakten")[1]) <= 409 + 5
    budgets = []
    memory.format_hits = lambda hits, budget=None, taken=None: budgets.append(budget) or ""
    run(agent.run("Was weißt du über mich?", _noop, _noop))
    assert budgets and budgets[0] <= 491
    llm.thought_repeat = 200  # ~4.000 Token Denkkette
    run(agent.run("Denk gründlich nach", _noop, _noop, think="high"))
    assert llm.opts[-2]["think"] is True and llm.opts[-1]["think"] is False  # auch „gründlich“ wird begrenzt
    agent._set_think("high")
    assert agent.think_budget() == 1638 and agent.answer_reserve() == 1024 + 1638


def test_large_window_summary_keeps_system_prompt_and_tools(cfg, llm, memory):
    agent = Agent(cfg, llm, memory)
    run(agent.run("Mach das Licht an und sag mir das Wetter", _noop, _noop))
    for i in range(10):
        memory.conversation.add({"role": "user", "content": f"Frage {i} " + "x" * 2000})
        memory.conversation.add({"role": "assistant", "content": f"Antwort {i}"})
    before = agent.build_messages(display=False)[0]
    tools_before = json.dumps(agent.schemas)
    assert run(agent.compact_now(_noop))
    after = agent.build_messages(display=False)
    assert after[0] == before and json.dumps(agent.schemas) == tools_before
    assert memory.conversation.running_summary.split("\n")[0] in after[1]["content"]


def _tool_steps(conv, n: int, size: int) -> None:
    conv.add({"role": "user", "content": "Lies die Logs"})
    for i in range(n):
        conv.add({"role": "assistant", "content": "", "tool_calls": [
            {"function": {"name": "read_file", "arguments": {"path": f"/var/log/l{i}.log"}}}]})
        conv.add({"role": "tool", "content": f"Log {i}\n" + "zeile mit inhalt\n" * (size // 17), "tool_name": "read_file"})
    conv.add({"role": "assistant", "content": "Fertig."})


def test_hiding_after_the_answer_prewarms_the_new_start(cfg, llm, memory):
    agent = Agent(cfg, llm, memory)  # 16k
    _tool_steps(memory.conversation, 8, 6000)
    agent.choose_tools()
    agent._cache_owner = agent._cache_key()  # der Server kennt den bisherigen Anfang
    assert run(agent._make_room(_noop, "", idle=True))
    assert memory.conversation.epoch["cleared"] >= 5 and len(memory.conversation.epochs) == 1  # ohne Zusammenfassung
    assert llm.opts[-1]["max_tokens"] == 1 and not agent.cache_cold()  # neuer Anfang gleich vorgewärmt


def test_overflow_hides_old_results_when_that_covers_it(cfg, llm, memory):
    from orbwise.llm import ContextOverflow
    agent = Agent(cfg, llm, memory)
    _tool_steps(memory.conversation, 8, 3000)  # die neuesten ~4k Token bleiben, ältere lassen sich ausblenden
    agent.choose_tools()
    small = ContextOverflow("zu groß", n_ctx=16384, n_prompt=16384 - 1500 + 500)  # 500 Token Überhang
    assert run(agent._make_room(_noop, "", overflow=small))
    assert memory.conversation.epoch["cleared"] >= 1 and not llm.calls  # ohne Modellaufruf


def test_small_window_keeps_used_tools_for_follow_ups_and_across_compaction(cfg, llm, memory, tmp_path):
    """Gemeldet: bei unter 16k fehlten bei der zweiten Paperless-Frage die Paperless-Werkzeuge (Jarvis wich auf curl
    aus) – nach einer Komprimierung bzw. wenn eine neue Gruppe dazukam."""
    all_integrations(cfg, tmp_path)
    cfg.llm.num_ctx = 8192
    agent = Agent(cfg, llm, memory)

    def sent() -> set[str]:
        return tool_names(llm.opts[-1]["tools"])

    run(agent.run('/tool paperless_search {"query": "Rechnung"}', _noop, _noop))
    run(agent.run("Zeig mir die vom Mai und erinnere mich morgen an die Überweisung", _noop, _noop))
    assert {"paperless_search", "set_reminder"} <= sent()  # neue Gruppe – Paperless bleibt trotzdem
    for i in range(8):
        memory.conversation.add({"role": "user", "content": f"Frage {i} " + "x" * 1200})
        memory.conversation.add({"role": "assistant", "content": f"Antwort {i}"})
    assert run(agent.compact_now(_noop))
    run(agent.run("Und welche davon ist die neueste?", _noop, _noop))
    assert "paperless_search" in sent()  # auch nach dem Zusammenfassen
