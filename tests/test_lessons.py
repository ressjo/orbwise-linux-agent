"""Lernen aus Erfahrung: Lektionen aus Fehlern, Korrekturen und 👎 – im Leerlauf nachgedacht, nur passende gehen mit,
mit festem Budget (das Kontextfenster wächst nicht mit der Zahl der Lektionen)."""

import json

from conftest import run

from orbwise import prompts
from orbwise.agent import Agent
from orbwise.config import ProfileConfig
from orbwise.memory.lessons import LessonStore


async def _noop(*a):
    return True


class Script:
    """Modell, das Schritt für Schritt vorgegebene Antworten/Aufrufe liefert und den Prompt mitschreibt."""

    def __init__(self, steps=None, reply="LEKTION: Auf dem NAS docker immer mit sudo aufrufen.", window=16384):
        self.steps = list(steps or [])
        self.reply = reply
        self.context_size = window
        self.profile = ProfileConfig(backend="openai", base_url="http://x/v1", model="m", num_ctx=window)
        self.active = "m"
        self.sent: list[list[dict]] = []
        self.chats: list[list[dict]] = []

    async def chat_stream(self, messages, tools=None, **kw):
        self.sent.append(messages)
        step = self.steps.pop(0) if self.steps else {"content": "Erledigt."}
        msg = {"role": "assistant", "content": step.get("content", "")}
        if step.get("call"):
            name, args = step["call"]
            msg["tool_calls"] = [{"function": {"name": name, "arguments": args}}]
        yield {"type": "done", "message": msg, "stats": {}}

    async def chat(self, messages):
        self.chats.append(messages)
        return self.reply


WORDS = ("alpha", "bravo", "charlie", "delta", "echo", "foxtrot", "golf", "hotel", "india", "juliet", "kilo", "lima")


def distinct(i: int) -> str:
    """Lektionstexte mit eigenem Wortschatz (sonst führt die Ähnlichkeitsprüfung sie zu Recht zusammen)."""
    return " ".join(f"{WORDS[(i + j) % len(WORDS)]}{i}x{j}" for j in range(6))


def store_of(memory) -> LessonStore:
    return memory.lessons


# ---------------------------------------------------------------- Speicher
def test_add_merges_similar_and_caps(memory):
    s = store_of(memory)
    a, merged = run(s.add("Docker auf dem NAS immer mit sudo aufrufen.", ["ssh"]))
    assert not merged
    b, merged = run(s.add("Docker auf dem NAS immer mit sudo aufrufen!", ["portainer"]))
    assert merged and b.id == a.id and set(b.scope) == {"ssh", "portainer"} and len(s.lessons) == 1
    s.max_count = 10
    for i in range(15):
        run(s.add(distinct(i)))
    assert len(s.lessons) == 10
    reloaded = LessonStore(memory.cfg.dir)
    assert len(reloaded.lessons) == 10  # gespeichert


def test_relevant_only_above_threshold(memory):
    s = store_of(memory)
    run(s.add("Docker auf dem NAS immer mit sudo aufrufen.", ["ssh"]))
    run(s.add("Paperless: Briefe der Telekom gehören zum Korrespondenten Telekom Deutschland.", ["paperless"]))
    hits = run(s.relevant("zeig die docker container auf dem nas", {"ssh"}))
    assert [h.text for h in hits] == ["Docker auf dem NAS immer mit sudo aufrufen."]
    assert run(s.relevant("wie wird das Wetter morgen?", set())) == []  # nichts Passendes → nichts


def test_prune_and_candidates(memory):
    s = store_of(memory)
    x, _ = run(s.add("Eine Regel, die nie hilft."))
    x.uses, x.helped = 9, 1
    assert s.prune() == [x.id] and not s.lessons
    for i in range(60):
        s.add_candidate({"kind": "error", "user": f"frage {i}"})
    assert len(s.pending()) == 50
    taken = s.take_candidates(3)
    assert [t["user"] for t in taken] == ["frage 10", "frage 11", "frage 12"] and len(s.pending()) == 47


# ---------------------------------------------------------------- Prompt: festes Budget, Cache bleibt
def _note_of(llm) -> str:
    return [m for m in llm.sent[0] if m["role"] == "user"][-1]["content"]


def test_matching_lessons_go_into_the_note_with_fixed_budget(cfg, memory):
    s = store_of(memory)
    for i in range(120):  # viele Lektionen …
        run(s.add(f"docker container auf dem nas {WORDS[i % len(WORDS)]}{i}"))
    assert len(s.lessons) > 50  # (Hash-Kollisionen der Test-Embeddings führen ein paar zusammen)
    llm = Script()
    agent = Agent(cfg, llm, memory)
    run(agent.run("zeig die docker container auf dem nas", _noop, _noop))
    note = _note_of(llm)
    section = note.split(prompts.SECTIONS["de"]["lessons"])[1].split("[/Kontext]")[0]
    plan = agent.plan()
    assert 0 < section.count("\n- ") <= plan.lessons_count
    assert len(section) // 3 <= plan.lessons + 20  # … aber nur das feste Budget im Prompt
    system_before = llm.sent[0][0]["content"]
    assert "docker container auf dem nas" not in system_before  # nie im Systemprompt → Prompt-Anfang bleibt im Cache


def test_no_matching_lesson_no_section(cfg, memory):
    run(store_of(memory).add("Docker auf dem NAS immer mit sudo aufrufen."))
    llm = Script()
    run(Agent(cfg, llm, memory).run("wie spät ist es?", _noop, _noop))
    assert prompts.SECTIONS["de"]["lessons"] not in _note_of(llm)


def test_learning_off(cfg, memory):
    cfg.memory.learning = False
    run(store_of(memory).add("Docker auf dem NAS immer mit sudo aufrufen."))
    llm = Script()
    run(Agent(cfg, llm, memory).run("docker container auf dem nas", _noop, _noop))
    assert prompts.SECTIONS["de"]["lessons"] not in _note_of(llm)


# ---------------------------------------------------------------- Kandidaten erkennen
def test_failed_then_recovered_becomes_candidate(cfg, memory):
    llm = Script(steps=[{"call": ("read_file", {"path": "/gibts/nicht.txt"})},
                        {"call": ("list_directory", {"path": str(cfg.memory.dir)})},
                        {"content": "Die Datei liegt woanders."}])
    agent = Agent(cfg, llm, memory)
    run(agent.run("lies die notizen datei", _noop, _noop))
    cands = store_of(memory).pending()
    assert len(cands) == 1 and cands[0]["kind"] == "error" and "read_file" in cands[0]["steps"][0]


def test_clean_turn_no_candidate_and_correction_is_one(cfg, memory):
    llm = Script(steps=[{"content": "Es sind 3 Container."}, {"content": "Ok, 4."}])
    agent = Agent(cfg, llm, memory)
    run(agent.run("wie viele container laufen", _noop, _noop))
    assert store_of(memory).pending() == []
    run(agent.run("nein, das ist falsch, es sind 4", _noop, _noop))
    cands = store_of(memory).pending()
    assert len(cands) == 1 and cands[0]["kind"] == "correction" and "es sind 4" in cands[0]["correction"]
    assert cands[0]["user"] == "wie viele container laufen"


def test_feedback(cfg, memory):
    events = []

    async def emit(ev):
        events.append(ev)
    llm = Script(steps=[{"content": "Antwort."}])
    agent = Agent(cfg, llm, memory)
    run(agent.run("frage", emit, _noop))
    end = next(e for e in events if e["type"] == "assistant_end")
    assert end["feedback"] is True
    assert agent.feedback(end["id"], False, "falsche Datei")
    assert store_of(memory).pending()[0]["correction"] == "falsche Datei"
    assert not agent.feedback("gibtsnicht", True)


# ---------------------------------------------------------------- Nachdenken im Leerlauf
def test_reflect_creates_lesson(cfg, memory):
    store_of(memory).add_candidate({"kind": "error", "user": "docker auf dem nas", "groups": ["ssh"],
                                    "steps": ["ssh_run → [admin@nas] Exit-Code 1", "ssh_run → [admin@nas] Exit-Code 0"]})
    llm = Script()
    agent = Agent(cfg, llm, memory)
    assert run(agent.reflect_lessons()) == 1
    lesson = store_of(memory).lessons[0]
    assert lesson.text == "Auf dem NAS docker immer mit sudo aufrufen." and lesson.scope == ["ssh"]
    assert "Exit-Code 1" in llm.chats[0][1]["content"] and not store_of(memory).pending()


def test_reflect_none(cfg, memory):
    store_of(memory).add_candidate({"kind": "error", "user": "x"})
    llm = Script(reply="KEINE")
    assert run(Agent(cfg, llm, memory).reflect_lessons()) == 0 and not store_of(memory).lessons


def test_lessons_api(cfg, monkeypatch):
    from fastapi.testclient import TestClient

    from orbwise.server import create_app
    monkeypatch.setenv("ORBWISE_FAKE_LLM", "1")
    monkeypatch.setenv("ORBWISE_SKIP_WARMUP", "1")
    with TestClient(create_app(cfg), base_url="http://localhost:8765") as client:
        r = client.post("/api/lessons", json={"text": "Backups liegen unter /mnt/nas/backup"})
        assert r.status_code == 200 and not r.json()["merged"]
        lid = r.json()["lesson"]["id"]
        data = client.get("/api/lessons").json()
        assert data["enabled"] and data["lessons"][0]["text"].startswith("Backups") and "vec" not in data["lessons"][0]
        assert client.patch(f"/api/lessons/{lid}", json={"text": "Backups: /mnt/nas/backup"}).json()["lesson"]["text"] \
            == "Backups: /mnt/nas/backup"
        assert client.delete(f"/api/lessons/{lid}").json() == {"ok": True}
        assert client.delete(f"/api/lessons/{lid}").status_code == 404
        assert client.post("/api/lessons", json={"text": "  "}).status_code == 400
    assert json.loads((cfg.memory.dir / "lessons.json").read_text()) == []
