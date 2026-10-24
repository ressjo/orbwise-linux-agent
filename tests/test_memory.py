from datetime import datetime

from conftest import run

from orbwise.memory.context import Conversation, est_tokens


def test_journal_file_per_day(memory):
    memory.journal.append("Du", "Hallo", datetime(2026, 9, 20, 10, 0, 0))
    memory.journal.append("Jarvis", "Guten Morgen", datetime(2026, 9, 20, 10, 0, 5))
    memory.journal.append("Du", "Update bitte", datetime(2026, 9, 21, 9, 0, 0))
    assert memory.journal.days() == ["2026-09-21", "2026-09-20"]
    assert (memory.cfg.dir / "journal" / "2026-09-20.md").exists()
    entries = memory.journal.entries("2026-09-20")
    assert [(e.speaker, e.text) for e in entries] == [("Du", "Hallo"), ("Jarvis", "Guten Morgen")]
    assert "Sonntag, 20.09.2026" in memory.journal.read("2026-09-20")
    assert memory.journal.read("../../etc/passwd") is None


def test_facts_add_remove(memory):
    assert run(memory.remember("Das NAS ist unter /mnt/nas gemountet."))
    assert not run(memory.remember("das nas ist unter /mnt/nas gemountet."))
    run(memory.remember("Der Nutzer mag dunkle Themes."))
    assert len(memory.facts.list()) == 2
    assert run(memory.forget("dunkle")) == ["Der Nutzer mag dunkle Themes."]
    assert [f for f, _ in memory.facts.list()] == ["Das NAS ist unter /mnt/nas gemountet."]


def test_retrieval_finds_old_conversation(memory):
    run(memory.log_exchange("Installiere bitte den Steam Client", "Steam wurde installiert.", []))
    run(memory.log_exchange("Wie wird das Wetter morgen?", "Sonnig.", []))
    hits = run(memory.retrieve("Welche Spiele-Plattform hatten wir installiert, Steam?"))
    assert hits and "Steam" in hits[0].text
    assert (memory.cfg.dir / "journal").glob("*.md")


def test_rebuild_index(memory):
    run(memory.log_exchange("Suche meine Steuererklärung", "Gefunden in ~/Dokumente.", ["find_files → 1 Treffer"]))
    run(memory.remember("Der Nutzer heißt Alex."))
    n = run(memory.rebuild_index())
    assert n >= 2
    assert run(memory.retrieve("Steuererklärung"))


def test_summarize_day(memory):
    memory.journal.append("Du", "Mach ein Systemupdate", datetime(2026, 9, 20, 10, 0, 0))
    summary = run(memory.summarize_day("2026-09-20"))
    assert summary.startswith("Zusammenfassung")
    assert memory.summaries.read("2026-09-20")
    assert "2026-09-20" not in memory.days_needing_summary(include_today=True)


def test_epochs_survive_a_restart(tmp_path):
    conv = Conversation(tmp_path / "session.json")
    for i in range(3):
        conv.add({"role": "user", "content": f"Frage {i}"})
        conv.add({"role": "assistant", "content": f"Antwort {i}"})
    conv.start_epoch([4, 5], "- Zusammenfassung von Frage 0 und 1", carried=2)
    conv.save()
    again = Conversation(tmp_path / "session.json")
    assert again.running_summary == "- Zusammenfassung von Frage 0 und 1" and len(again.epochs) == 2
    assert [m["content"] for m in again.epoch_messages()] == ["Frage 2", "Antwort 2"]
    assert len(again.history) == 6  # nichts gelöscht – die Oberfläche zeigt weiter den ganzen Chat


def test_trimmed_history_never_starts_with_tool(tmp_path):
    conv = Conversation()
    conv.add({"role": "user", "content": "a" * 3000})
    conv.add({"role": "assistant", "content": "", "tool_calls": [{"function": {"name": "x"}}]})
    conv.add({"role": "tool", "content": "b" * 300})
    conv.add({"role": "assistant", "content": "fertig"})
    msgs = conv.trimmed_history(150)
    assert msgs and msgs[0]["role"] != "tool"
    assert all("ts" not in m for m in msgs)
    assert est_tokens("abc") >= 1


def test_trimmed_history_always_keeps_current_question():
    from orbwise.memory.context import Conversation
    conv = Conversation()
    conv.add({"role": "user", "content": "alte Frage"})
    conv.add({"role": "assistant", "content": "alte Antwort"})
    conv.add({"role": "user", "content": "Suche im Web nach Llama"})
    for i in range(4):
        conv.add({"role": "assistant", "content": "", "tool_calls": [{"function": {"name": "fetch_url", "arguments": {}}}]})
        conv.add({"role": "tool", "content": f"Seite {i} " + "x" * 6000, "tool_name": "fetch_url"})
    out = conv.trimmed_history(3000)
    assert out[0] == {"role": "user", "content": "Suche im Web nach Llama"}
    assert [m["role"] for m in out].count("tool") == 4          # nichts verwaist, nur gekürzt
    assert sum(len(m.get("content") or "") for m in out) < 3000 * 3 + 2000
    assert "gekürzt" in out[1 + 1]["content"]                   # ältestes Ergebnis zuerst gekürzt
    assert conv.history[4]["content"].startswith("Seite 0 xxx") and len(conv.history[4]["content"]) > 6000  # Original bleibt


def test_budget_respects_model_context(cfg, memory):
    from orbwise.agent import Agent
    from orbwise.config import ProfileConfig

    class Small:
        profile = ProfileConfig(backend="openai", base_url="http://x/v1", model="llama", num_ctx=8192)

    assert Agent(cfg, Small(), memory).context_budget() == 8192 - 1024  # kleines Fenster: knappere Antwortreserve
    cfg.memory.context_budget_tokens = 5000
    assert Agent(cfg, Small(), memory).context_budget() == 5000


def test_remember_replaces_an_outdated_version_of_a_fact(cfg, llm, memory):
    run(memory.remember("Das NAS ist unter /mnt/nas gemountet"))
    run(memory.remember("Der Nutzer heißt Alex"))
    added, replaced = run(memory.remember_fact("Das NAS ist unter /media/nas gemountet"))
    assert added and replaced == "Das NAS ist unter /mnt/nas gemountet"
    facts = [f for f, _ in memory.facts.list()]
    assert facts == ["Der Nutzer heißt Alex", "Das NAS ist unter /media/nas gemountet"]  # kein Widerspruch
    added, replaced = run(memory.remember_fact("Der Nutzer mag Pizza"))
    assert added and replaced is None and len(memory.facts.list()) == 3  # anderer Fakt wird angehängt


def test_only_relevant_memories_reach_the_prompt(cfg, llm, memory):
    from orbwise.agent import Agent
    from orbwise.memory.index import Hit
    agent = Agent(cfg, llm, memory)
    near = Hit(1, "journal", "2026-09-01", "Das NAS heißt Tresor", 0, 0.03, sim=0.71)
    far = Hit(2, "journal", "2026-09-01", "Pizza bestellt", 0, 0.03, sim=0.31)
    fact = Hit(3, "fact", "2026-09-01", "Der Nutzer heißt Alex", 0, 0.03, sim=0.9)
    assert agent._relevant("Wie heißt mein NAS?", near)
    assert not agent._relevant("Wie heißt mein NAS?", far)
    assert not agent._relevant("Wie heiße ich?", fact)  # Fakten stehen schon im Systemprompt
    plain = Hit(4, "journal", "2026-09-01", "Das NAS heißt Tresor", 0, 0.03)  # ohne Embeddings
    assert agent._relevant("Wie heißt mein NAS?", plain) and not agent._relevant("Hallo", plain)
