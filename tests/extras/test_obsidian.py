import pytest
from conftest import run

from orbwise.agent import Agent
from orbwise.extras import obsidian as ob
from orbwise.tools.registry import CONFIRM, SAFE, ToolContext, get_tool, load_all_tools, tool_schemas

TOOLS = {"obsidian_search", "obsidian_read", "obsidian_ask", "obsidian_create_note", "obsidian_append",
         "obsidian_update_note", "obsidian_open"}


@pytest.fixture
def vault(cfg, tmp_path):
    v = tmp_path / "Arbeit"
    (v / "Projekte").mkdir(parents=True)
    (v / ".obsidian").mkdir()
    (v / "Projekte" / "Server-Wartung.md").write_text(
        "---\ntags: [server, linux]\n---\n# Wartung\nJeden Freitag Updates einspielen.\n- [ ] Backup prüfen\n",
        encoding="utf-8")
    (v / "Einkaufsliste.md").write_text("- Brot\n- Käse\n", encoding="utf-8")
    (v / "Meeting Kunde.md").write_text("Besprechung zum Server-Umzug. #meeting\nPorts 443 und 8443 freigeben.\n",
                                        encoding="utf-8")
    (v / ".obsidian" / "workspace.md").write_text("Server geheim", encoding="utf-8")
    cfg.obsidian.vault = str(v)
    return v


def ctx(cfg, memory=None):
    return ToolContext(cfg=cfg, memory=memory)


def names(schemas):
    return {s["function"]["name"] for s in schemas}


def test_tools_only_when_vault_exists(cfg, tmp_path):
    load_all_tools()
    assert not TOOLS & names(tool_schemas(cfg))
    cfg.obsidian.vault = str(tmp_path / "gibt-es-nicht")
    assert not TOOLS & names(tool_schemas(cfg))
    cfg.obsidian.vault = str(tmp_path)
    assert TOOLS <= names(tool_schemas(cfg))


def test_risk_levels(cfg, vault):
    load_all_tools()
    for n in TOOLS - {"obsidian_update_note"}:
        assert get_tool(n).assess(ctx(cfg), {})[0] == SAFE
    assert get_tool("obsidian_update_note").assess(ctx(cfg), {"note": "x"})[0] == CONFIRM


def test_search_title_first_ignores_hidden(cfg, vault):
    out = run(ob.obsidian_search(ctx(cfg), "server"))
    lines = [line for line in out.splitlines() if line.startswith("- ")]
    assert lines[0].startswith("- Projekte/Server-Wartung.md")
    assert "Meeting Kunde.md" in out and ".obsidian" not in out
    assert "Keine Obsidian-Notizen" in run(ob.obsidian_search(ctx(cfg), "xyzunbekannt"))


def test_search_by_tag_folder_and_recent(cfg, vault):
    out = run(ob.obsidian_search(ctx(cfg), tag="#meeting"))
    assert "Meeting Kunde.md" in out and "Server-Wartung" not in out
    out = run(ob.obsidian_search(ctx(cfg), tag="linux"))
    assert "Server-Wartung" in out
    out = run(ob.obsidian_search(ctx(cfg), "server", folder="Projekte"))
    assert "Server-Wartung" in out and "Meeting" not in out
    assert "3 Notiz(en)" in run(ob.obsidian_search(ctx(cfg)))
    assert "nicht erlaubt" in run(ob.obsidian_search(ctx(cfg), "x", folder="../.."))


def test_read_by_title_and_path(cfg, vault):
    out = run(ob.obsidian_read(ctx(cfg), "server-wartung"))
    assert "Projekte/Server-Wartung.md" in out and "Jeden Freitag" in out
    assert "Jeden Freitag" in run(ob.obsidian_read(ctx(cfg), "Projekte/Server-Wartung.md"))
    assert "Jeden Freitag" in run(ob.obsidian_read(ctx(cfg), "[[Server-Wartung]]"))
    assert "Keine Notiz" in run(ob.obsidian_read(ctx(cfg), "gibt es nicht"))
    cfg.obsidian.max_chars = 10
    assert "weiter mit offset=10" in run(ob.obsidian_read(ctx(cfg), "Einkaufsliste"))


def test_ambiguous_and_escape(cfg, vault):
    (vault / "Server-Notizen.md").write_text("x", encoding="utf-8")
    assert "Mehrere Notizen" in run(ob.obsidian_read(ctx(cfg), "server"))
    (vault.parent / "geheim.md").write_text("geheim", encoding="utf-8")
    assert "nicht erlaubt" in run(ob.obsidian_read(ctx(cfg), "../geheim.md"))
    assert "nicht erlaubt" in run(ob.obsidian_append(ctx(cfg), "../geheim", "x"))
    assert (vault.parent / "geheim.md").read_text(encoding="utf-8") == "geheim"


def test_create_in_inbox_with_frontmatter_never_overwrites(cfg, vault):
    out = run(ob.obsidian_create_note(ctx(cfg), "Idee: Backup/Plan?", "## Schritte\n- [ ] testen", tags="ideen, #backup"))
    path = vault / "Inbox" / "Idee Backup Plan.md"
    assert "angelegt" in out and path.exists()
    text = path.read_text(encoding="utf-8")
    meta, _, body = ob.split_frontmatter(text)
    assert meta["tags"] == ["ideen", "backup"] and "created" in meta and body.strip().startswith("## Schritte")
    run(ob.obsidian_create_note(ctx(cfg), "Idee: Backup/Plan?", "zweite"))
    assert (vault / "Inbox" / "Idee Backup Plan (2).md").read_text(encoding="utf-8").rstrip().endswith("zweite")
    run(ob.obsidian_create_note(ctx(cfg), "Detail", "Text", folder="Projekte/Neu"))
    assert (vault / "Projekte" / "Neu" / "Detail.md").exists()
    assert "nicht erlaubt" in run(ob.obsidian_create_note(ctx(cfg), "x", "y", folder="../../tmp"))
    # neue Notiz ist sofort auffindbar
    assert "Idee Backup Plan" in run(ob.obsidian_search(ctx(cfg), "backup"))


def test_append_list_and_text(cfg, vault):
    assert "angehängt" in run(ob.obsidian_append(ctx(cfg), "Einkaufsliste", "- Milch"))
    assert (vault / "Einkaufsliste.md").read_text(encoding="utf-8") == "- Brot\n- Käse\n- Milch\n"
    run(ob.obsidian_append(ctx(cfg), "Meeting Kunde", "Nachtrag: Termin steht."))
    assert (vault / "Meeting Kunde.md").read_text(encoding="utf-8").endswith("freigeben.\n\nNachtrag: Termin steht.\n")


def test_update_keeps_frontmatter(cfg, vault):
    run(ob.obsidian_update_note(ctx(cfg), "Server-Wartung", "Neuer Inhalt"))
    text = (vault / "Projekte" / "Server-Wartung.md").read_text(encoding="utf-8")
    assert text.startswith("---\ntags: [server, linux]\n---") and text.rstrip().endswith("Neuer Inhalt")
    assert "Jeden Freitag" not in text


def test_ask_short_and_long(cfg, vault, memory):
    out = run(ob.obsidian_ask(ctx(cfg), "Meeting Kunde", "Welche Ports?"))
    assert "vollständiger Text" in out and "8443" in out
    filler = "Allgemeines Blabla über das Wetter und andere Dinge. " * 60
    (vault / "Handbuch.md").write_text(filler + "\n\nDie Datenbank läuft auf Port 5432 mit Nutzer admin.\n\n" + filler,
                                       encoding="utf-8")
    cfg.obsidian.max_chars = 1500
    out = run(ob.obsidian_ask(ctx(cfg, memory), "Handbuch", "Auf welchem Port läuft die Datenbank?"))
    assert "5432" in out and "relevantesten" in out and len(out) < 2500
    out = run(ob.obsidian_ask(ctx(cfg), "Handbuch", "Datenbank Port"))  # ohne Embeddings
    assert "5432" in out


def test_open_uses_obsidian_uri_when_registered(cfg, vault, monkeypatch):
    launched = []

    async def fake_launch(argv, wait=2.0):
        launched.append(argv)
        return True, ""

    async def fake_run(ctx_, argv, timeout, stream=True, cwd=None):
        return 0, "obsidian.desktop\n"

    monkeypatch.setattr(ob.proc, "launch", fake_launch)
    monkeypatch.setattr(ob.proc, "run", fake_run)
    monkeypatch.setattr(ob.shutil, "which", lambda c: "/usr/bin/" + c)
    out = run(ob.obsidian_open(ctx(cfg), "Server-Wartung"))
    assert "in Obsidian" in out
    assert launched == [["xdg-open", "obsidian://open?vault=Arbeit&file=Projekte/Server-Wartung.md"]]


def test_status(cfg, vault, tmp_path):
    st = run(ob.obsidian_status(cfg))
    assert st["online"] and st["count"] == 3
    cfg.obsidian.vault = str(tmp_path / "weg")
    st = run(ob.obsidian_status(cfg))
    assert st["enabled"] and not st["online"] and "nicht gefunden" in st["error"]
    cfg.obsidian.vault = ""
    assert run(ob.obsidian_status(cfg)) == {"enabled": False, "online": False}


def test_system_prompt_mentions_obsidian_only_when_configured(cfg, llm, memory, vault):
    agent = Agent(cfg, llm, memory)
    assert "obsidian_create_note" in agent.system_prompt()
    cfg.obsidian.vault = ""
    assert "Obsidian" not in agent.system_prompt()
