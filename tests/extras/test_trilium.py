import pytest
from conftest import run
from fake_trilium import TOKEN, FakeTrilium

from orbwise.agent import Agent
from orbwise.extras import trilium as tr
from orbwise.tools.registry import CONFIRM, SAFE, ToolContext, get_tool, load_all_tools, tool_schemas

TRILIUM_TOOLS = {"trilium_search", "trilium_read", "trilium_create_note", "trilium_append", "trilium_update_note"}


@pytest.fixture
def fake(monkeypatch, cfg):
    f = FakeTrilium()
    monkeypatch.setattr(tr, "TRANSPORT", f.transport())
    cfg.trilium.url = "http://trilium.local"
    cfg.trilium.token = TOKEN
    return f


def ctx(cfg):
    return ToolContext(cfg=cfg, memory=None)


def names(schemas):
    return {s["function"]["name"] for s in schemas}


def test_tools_only_when_configured(cfg, monkeypatch):
    monkeypatch.delenv("ORBWISE_TRILIUM_TOKEN", raising=False)
    load_all_tools()
    assert not TRILIUM_TOOLS & names(tool_schemas(cfg))
    cfg.trilium.url, cfg.trilium.token = "http://x", "t"
    assert TRILIUM_TOOLS <= names(tool_schemas(cfg))
    monkeypatch.setenv("ORBWISE_TRILIUM_TOKEN", "env")
    cfg.trilium.token = ""
    assert cfg.trilium.enabled


def test_risk_levels(cfg):
    load_all_tools()
    for n in TRILIUM_TOOLS - {"trilium_update_note"}:
        assert get_tool(n).assess(ctx(cfg), {})[0] == SAFE
    assert get_tool("trilium_update_note").assess(ctx(cfg), {"note": "x"})[0] == CONFIRM


def test_search_with_preview(cfg, fake):
    out = run(tr.trilium_search(ctx(cfg), "nas"))
    assert "NAS Setup (ID nasSetup01" in out and "/mnt/nas" in out
    req = fake.requests[0]
    assert req.headers["Authorization"] == TOKEN
    assert req.url.params["search"] == "nas" and req.url.params["orderBy"] == "dateModified"


def test_read_by_title_and_id(cfg, fake):
    out = run(tr.trilium_read(ctx(cfg), "NAS Setup"))
    assert "**/mnt/nas**" in out and "- SMB-Freigabe" in out
    assert "nasSetup01" in run(tr.trilium_read(ctx(cfg), "nasSetup01"))
    assert "Keine Notiz" in run(tr.trilium_read(ctx(cfg), "gibt es nicht"))


def test_create_goes_to_inbox(cfg, fake):
    out = run(tr.trilium_create_note(ctx(cfg), "Server-Wartung", "## Schritte\n- [ ] Update\n- [x] Backup\n\n```\nsudo apt upgrade\n```"))
    assert "angelegt" in out and "Inbox" in out
    note = fake.notes["new000000001"]
    assert note["parentNoteIds"] == ["inbox123"]
    assert "<h2>Schritte</h2>" in note["content"]
    assert 'class="todo-list"' in note["content"] and 'checked="checked"' in note["content"]
    assert "sudo apt upgrade" in note["content"]


def test_create_under_parent(cfg, fake):
    run(tr.trilium_create_note(ctx(cfg), "Detail", "Text", parent="NAS Setup"))
    assert fake.notes["new000000001"]["parentNoteIds"] == ["nasSetup01"]


def test_append_keeps_content(cfg, fake):
    assert "angehängt" in run(tr.trilium_append(ctx(cfg), "Einkaufsliste", "- Milch"))
    content = fake.notes["shop0000001"]["content"]
    assert content.startswith("<ul><li>Brot</li></ul>") and "<li>Milch</li>" in content


def test_update_replaces(cfg, fake):
    run(tr.trilium_update_note(ctx(cfg), "shop0000001", "- Käse", title="Einkauf"))
    assert fake.notes["shop0000001"]["content"] == "<ul><li>Käse</li></ul>"
    assert fake.notes["shop0000001"]["title"] == "Einkauf"


def test_bad_token_message(cfg, fake):
    cfg.trilium.token = "falsch"
    assert "Token ist ungültig" in run(tr.trilium_search(ctx(cfg), "nas"))


def test_status(cfg, fake):
    assert run(tr.trilium_status(cfg)) == {"enabled": True, "online": True, "version": "0.99.0"}


def test_agent_update_requires_confirmation(cfg, llm, memory, fake):
    agent = Agent(cfg, llm, memory)
    assert "Trilium" in agent.system_prompt()
    confirms = []

    async def emit(e):
        pass

    async def confirm(cid, name, args, reason):
        confirms.append(name)
        return False

    run(agent.run('/tool trilium_update_note {"note": "Einkaufsliste", "content": "leer"}', emit, confirm))
    assert confirms == ["trilium_update_note"]
    assert fake.notes["shop0000001"]["content"] == "<ul><li>Brot</li></ul>"


def test_html_text_roundtrip():
    md = "## Titel\nEin **fetter** Satz mit `code` & <tag>.\n\n1. eins\n2. zwei\n\n- [ ] offen"
    html = tr.text_to_html(md)
    assert "&lt;tag&gt;" in html and "<ol><li>eins</li>" in html
    text = tr.html_to_text(html)
    assert "## Titel" in text
    assert "**fetter**" in text and "`code`" in text and "<tag>" in text
    assert "1. eins" in text and "2. zwei" in text and "[ ] offen" in text
    assert tr.text_to_html("<p>schon html</p>") == "<p>schon html</p>"
