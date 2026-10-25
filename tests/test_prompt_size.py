"""Anfangs-Kontext: Hinweise kommen nur mit ihren Werkzeugen, der Prompt-Anfang bleibt schlank."""

import json

from conftest import run

from orbwise.agent import Agent
from orbwise.memory.context import est_tokens


async def _noop(*a):
    return True


def all_integrations(cfg, tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    cfg.obsidian.vault = str(vault)
    cfg.trilium.url, cfg.trilium.token = "http://trilium.local", "x"
    cfg.paperless.url, cfg.paperless.token = "http://paperless.local", "x"
    cfg.homeassistant.url, cfg.homeassistant.token = "http://ha.local", "x"
    cfg.calendar.url, cfg.calendar.username, cfg.calendar.password = "http://cal.local", "u", "x"
    cfg.mail.username, cfg.mail.password = "u", "x"
    cfg.telegram.token, cfg.telegram.chat_id = "x", 1
    cfg.llm.num_ctx = 16384


def first_request(cfg, llm, memory, question):
    agent = Agent(cfg, llm, memory)
    run(agent.run(question, _noop, _noop))
    system = llm.calls[0][0]["content"]
    tools = llm.opts[0]["tools"] or []
    return system, [t["function"]["name"] for t in tools], est_tokens(json.dumps(tools, ensure_ascii=False))


def test_new_chat_starts_lean_with_all_integrations(cfg, llm, memory, tmp_path):
    all_integrations(cfg, tmp_path)
    system, names, tool_tokens = first_request(cfg, llm, memory, "Hallo")
    # vorher ~5.550 (Hinweise zu allen Diensten standen immer im Systemprompt) – nicht wieder wachsen lassen
    assert est_tokens(system) + tool_tokens <= 4900  # inkl. edit_file, todo_write, load_tools und date_info
    assert "paperless_search" not in names and "telegram_send_file" not in names and "search_nas" not in names
    assert "Paperless" not in system and "mail_list" not in system and "top_processes" not in system
    assert "Gemountetes NAS" not in system  # kein NAS eingerichtet
    assert "fremde Daten" in system  # Schutz vor Anweisungen aus Mails/Webseiten gilt immer


def test_hint_comes_with_its_tools(cfg, llm, memory, tmp_path):
    all_integrations(cfg, tmp_path)
    system, names, _ = first_request(cfg, llm, memory, "Such meine Rechnung von der Telekom")
    assert "paperless_search" in names and "paperless_ask" in system  # Werkzeuge und ihre Anleitung
    assert "mail_list" not in system and "calendar_events" not in system


def test_all_hints_when_every_tool_fits(cfg, llm, memory, tmp_path):
    all_integrations(cfg, tmp_path)
    cfg.llm.num_ctx = 131072  # großes Fenster: alle Werkzeuge, alle Hinweise
    system, names, _ = first_request(cfg, llm, memory, "Hallo")
    assert "paperless_search" in names and "mail_list" in names
    assert "paperless_ask" in system and "mail_list" in system and "top_processes" not in system
    assert "ps, systemctl, journalctl" in system and "cleanup_system" in system  # Systemabfragen über run_shell


def test_missing_tools_can_be_loaded_on_demand(cfg, llm, memory, tmp_path):
    all_integrations(cfg, tmp_path)
    agent = Agent(cfg, llm, memory)
    # Frage ohne Stichwort für Smart Home – erst load_tools lädt die Gruppe
    run(agent.run('/tool load_tools {"groups": "homeassistant, quatsch"}', _noop, _noop))
    first = llm.opts[0]["tools"]
    loader = next(t for t in first if t["function"]["name"] == "load_tools")["function"]["description"]
    assert "paperless (Dokumente" in loader and "mail (" in loader  # das Modell weiß, was es gibt
    assert "ha_control" not in [t["function"]["name"] for t in first]
    assert "load_tools" in llm.calls[0][0]["content"]  # Hinweis im Systemprompt
    result = [m for m in memory.conversation.history if m["role"] == "tool"][-1]["content"]
    assert "ha_control" in result and "Unbekannt: quatsch" in result
    after = [t["function"]["name"] for t in llm.opts[-1]["tools"]]
    assert "ha_control" in after and "ha_find" in llm.calls[-1][0]["content"]  # Werkzeuge + Hinweis


def test_no_loader_when_every_tool_fits(cfg, llm, memory, tmp_path):
    all_integrations(cfg, tmp_path)
    cfg.llm.num_ctx = 131072
    system, names, _ = first_request(cfg, llm, memory, "Hallo")
    assert "load_tools" not in names and "load_tools" not in system
