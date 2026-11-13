"""Privater Chat: lebt nur im Arbeitsspeicher – keine Datei, kein Tagebuch, kein Suchindex, keine neuen Fakten;
mit dem Verlassen ist er weg (samt Anhängen und zwischengespeicherten Ausgaben)."""

import json
from pathlib import Path

from conftest import run
from fastapi.testclient import TestClient

from orbwise import attachments
from orbwise.agent import Agent
from orbwise.memory.files import day_str

PNG = b"\x89PNG\r\n\x1a\n" + b"privat" * 10
SECRET = "Zugspitzgeheimnis"


def files_with(root: Path, word: str) -> list[str]:
    hits = []
    for p in root.rglob("*"):
        if p.is_file():
            try:
                if word.encode() in p.read_bytes():
                    hits.append(str(p.relative_to(root)))
            except OSError:
                pass
    return hits


async def emit(e):
    pass


async def confirm(*a):
    return True


def test_private_chat_leaves_no_trace(cfg, llm, memory):
    agent = Agent(cfg, llm, memory)
    run(agent.run("Normale Frage", emit, confirm))
    normal = memory.conversation.chat_id
    before = sorted(p.name for p in memory.chats.dir.glob("*.json"))
    journal_before = memory.journal.read(day_str())
    count_before = memory.index.count()

    conv = memory.new_private_chat()
    assert memory.private and conv.chat_id != normal and conv.state_path is None
    run(agent.run(f"Mein Passwort ist {SECRET}", emit, confirm))
    assert any(SECRET in (m.get("content") or "") for m in conv.history)  # im Chat selbst steht es
    conv.save()  # speichern ist wirkungslos
    assert sorted(p.name for p in memory.chats.dir.glob("*.json")) == before
    assert memory.journal.read(day_str()) == journal_before and memory.index.count() == count_before
    assert files_with(cfg.memory.dir.parent, SECRET) == []  # nirgends auf der Platte
    assert not any(c["id"] == conv.chat_id for c in memory.chats.list())
    assert run(memory.retrieve(SECRET)) == [] or all(SECRET not in h.text for h in run(memory.retrieve(SECRET)))

    memory.switch_chat(normal)  # verlassen → weg
    assert not memory.private and conv.history == [] and memory.conversation.chat_id == normal
    assert not memory.chats.exists(conv.chat_id)


def test_nothing_is_remembered_but_memory_can_be_read(cfg, llm, memory):
    from orbwise.tools import memory_tools
    from orbwise.tools.registry import ToolContext
    run(memory.remember("Der Nutzer wohnt in Freiburg."))
    memory.new_private_chat()
    ctx = ToolContext(cfg=cfg, memory=memory, llm=llm) if "llm" in ToolContext.__annotations__ \
        else ToolContext(cfg=cfg, memory=memory)
    answer = run(memory_tools.remember(ctx, "Der Nutzer hat ein Geheimnis."))
    assert "privater Chat" in answer and "Geheimnis" not in memory.facts_text()
    assert "Freiburg" in memory.facts_text()  # lesen geht weiter
    assert "Freiburg" in run(memory_tools.recall(ctx, query="Freiburg"))


def test_every_way_out_ends_the_private_chat(cfg, llm, memory):
    for leave in (lambda: memory.new_chat(), lambda: memory.switch_mode("coding"),
                  lambda: memory.switch_mode("tools"), lambda: memory.new_private_chat()):
        conv = memory.new_private_chat()
        conv.add({"role": "user", "content": SECRET})
        leave()
        assert memory.conversation is not conv and conv.history == []
        memory.switch_mode("tools")
    assert files_with(cfg.memory.dir.parent, SECRET) == []
    # ein leerer privater Chat wird bei „Neuer Chat“ nicht als normaler weiterverwendet
    private = memory.new_private_chat()
    assert memory.new_chat() is not private and not memory.private


def test_attachments_and_cached_outputs_go_with_it(cfg, llm, memory, tmp_path):
    shared = attachments.save(cfg, PNG + b"geteilt")
    memory.conversation.add({"role": "user", "content": "Bild", "attachments": [shared]})
    memory.conversation.save()
    conv = memory.new_private_chat()
    own = attachments.save(cfg, PNG)
    cache = tmp_path / "cache" / "orbwise" / "outputs"
    cache.mkdir(parents=True)
    out = cache / "shell-abc.txt"
    out.write_text("lange Ausgabe")
    other = tmp_path / "nicht-anfassen.txt"
    other.write_text("x")
    conv.add({"role": "user", "content": "Was ist das?", "attachments": [own, shared]})
    conv.add({"role": "tool", "tool_name": "run_shell", "content":
              f"…\n[Vollständige Ausgabe (9000 Zeichen, 3 Zeilen): {out} – bei Bedarf Teile mit read_file lesen]\n"
              f"[Vollständige Ausgabe (1 Zeichen, 1 Zeilen): {other} – bei Bedarf …]"})
    memory.new_chat()
    assert attachments.path_of(cfg, own) is None and not out.exists()
    assert attachments.path_of(cfg, shared) is not None  # hängt auch an einem normalen Chat → bleibt
    assert other.exists()  # nur Dateien aus dem eigenen Zwischenspeicher


def test_routine_in_between_does_not_touch_or_end_it(cfg, llm, memory):
    conv = memory.new_private_chat()
    conv.add({"role": "user", "content": SECRET})
    with memory.in_chat("", "Routine") as other:
        assert not memory.private and memory.active is conv
        other.add({"role": "user", "content": "Routine läuft"})
    assert memory.conversation is conv and memory.private and conv.history
    assert files_with(cfg.memory.dir.parent, SECRET) == []
    memory.close()
    assert conv.history == []


def test_web_api(cfg, monkeypatch):
    monkeypatch.setenv("ORBWISE_FAKE_LLM", "1")
    monkeypatch.setenv("ORBWISE_SKIP_WARMUP", "1")
    from orbwise.server import create_app
    app = create_app(cfg)
    with TestClient(app, base_url="http://localhost:8765") as client:
        memory = app.state.hub.agent.memory
        r = client.post("/api/chats/private").json()
        assert r["private"] and memory.private
        memory.conversation.add({"role": "user", "content": SECRET})
        memory.conversation.add({"role": "assistant", "content": "ok"})
        history = client.get("/api/history").json()
        assert [m["content"] for m in history["messages"]] == [SECRET, "ok"]  # in der Oberfläche sichtbar
        chats = client.get("/api/chats").json()
        assert not any(c["id"] == r["id"] or c["active"] for c in chats)
        for method, url in (("post", f"/api/chats/{r['id']}/star"), ("get", f"/api/chats/{r['id']}/export"),
                            ("delete", f"/api/chats/{r['id']}")):
            assert getattr(client, method)(url).status_code in (404, 422)
        assert client.post("/api/chats").status_code == 200  # „Neuer Chat“ verlässt ihn
        assert not memory.private
        assert client.get("/api/history").json()["messages"] == []
    assert files_with(cfg.memory.dir.parent, SECRET) == []
    data = json.loads(next(cfg.memory.dir.glob("chats/*.json")).read_text())
    assert not data["meta"].get("private")
