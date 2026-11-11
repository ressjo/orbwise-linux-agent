"""Was die Doku über Sicherheit verspricht, entspricht dem Code: die Freigabe-Matrix ist berechnet, jede Zeile des
Bedrohungsmodells nennt einen Test, den es gibt, und `orbwise doctor --security` sagt, was auf dem Rechner gilt."""

import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from orbwise import security_check, security_matrix

ROOT = Path(__file__).parent.parent


def test_matrix_in_readme_and_docs_matches_the_code():
    table = security_matrix.render("en")
    for rel in security_matrix.FILES:
        text = (ROOT / rel).read_text(encoding="utf-8")
        assert security_matrix.embed(text, table) == text, \
            f"{rel}: Freigabe-Matrix veraltet – `python -m orbwise.security_matrix` ausführen"


def test_matrix_says_what_the_levels_promise():
    rows = dict(zip([en for _de, en, _n, _a in security_matrix.ROWS], security_matrix.matrix(), strict=True))
    assert rows["Reading: `ls`, `df`, `git status`"] == ["ask", "run", "run", "run"]
    assert rows["Scripts, builds, tests: `python3 x.py`, `npm test`"] == ["ask", "ask", "cage", "run"]
    assert rows["Root: `sudo …`, `su`, `pkexec`, `yay`"] == ["ask", "ask", "ask", "dialog"]
    assert rows["`git push`"] == ["ask"] * 4
    assert rows["`rm -rf /`, overwriting a disk, `chmod -R … /`"] == ["never"] * 4
    assert rows["The same file with the read tool"] == ["never"] * 4
    assert rows["Reading credentials: `cat ~/.ssh/id_rsa`"] == ["ask"] * 4  # auch in „Auto“
    assert "| **asks** | runs | runs | runs |" in security_matrix.render("en")
    assert "läuft im Käfig" in security_matrix.render("de")


def test_every_threat_names_a_test_that_exists():
    doc = (ROOT / "docs" / "security.md").read_text(encoding="utf-8")
    section = doc[doc.index("## Threat model"):doc.index("### What runs in which level")]
    named = re.findall(r"`(test_\w+)`", section)
    attacks = (ROOT / "tests" / "test_attacks.py").read_text(encoding="utf-8")
    defined = set(re.findall(r"^def (test_\w+)", attacks, re.M))
    assert len(named) >= 10 and set(named) <= defined, sorted(set(named) - defined)
    assert "### What it does not protect against" in doc
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "It does not protect against:" in readme and "tests/test_attacks.py" in readme
    assert "written with an AI coding assistant" in readme  # dass das Projekt mit KI entstanden ist, steht da


# ---------------------------------------------------------------- orbwise doctor --security
@pytest.fixture
def machine(cfg, monkeypatch, tmp_path):
    """Ein Rechner, auf dem alles greift – einzelne Tests schwächen je einen Punkt."""
    from orbwise import agent_safety, askpass, jail, web_auth
    token = tmp_path / "web-token"
    token.write_text("x")
    token.chmod(0o600)
    monkeypatch.setattr(agent_safety, "running_as_root", lambda: False)
    monkeypatch.setattr(jail, "available", lambda force=False: (True, ""))
    monkeypatch.setattr(askpass, "sudo_needs_no_password", lambda: False)
    monkeypatch.setattr(web_auth, "token_path", lambda: token)
    monkeypatch.setattr(security_check, "_in_group", lambda name: False)
    cfg.memory.dir.parent.mkdir(parents=True, exist_ok=True)
    return SimpleNamespace(cfg=cfg, token=token, monkeypatch=monkeypatch)


def _levels(cfg):
    return [x["level"] for x in security_check.report(cfg)]


def _warnings(cfg):
    return " | ".join(x["text"] for x in security_check.report(cfg) if x["level"] == "warn")


def test_a_well_set_up_machine_has_no_warnings(machine):
    lines = security_check.report(machine.cfg)
    assert [x["level"] for x in lines] == ["ok"] * 6
    text = security_check.render(lines)
    assert text.count("✔") == 6 and "Käfig (bubblewrap) funktioniert" in text and "„Nur lesen“" in text


def test_each_weakness_is_reported_with_its_consequence(machine):
    from orbwise import agent_safety, askpass, jail
    from orbwise.config import McpServerConfig
    cfg, mp = machine.cfg, machine.monkeypatch
    cfg.host = "0.0.0.0"
    assert "aus dem Netz erreichbar" in _warnings(cfg)
    cfg.host = "127.0.0.1"
    machine.token.chmod(0o644)
    assert "für andere lesbar" in _warnings(cfg)
    machine.token.chmod(0o600)
    (cfg.memory.dir.parent / "auto_mode").write_text("auto")
    assert "ohne Rückfrage" in _warnings(cfg)
    (cfg.memory.dir.parent / "auto_mode").write_text("files")
    mp.setattr(jail, "available", lambda force=False: (False, "bubblewrap fehlt"))
    assert "Kein Käfig (bubblewrap fehlt)" in _warnings(cfg)
    mp.setattr(jail, "available", lambda force=False: (True, ""))
    mp.setattr(askpass, "sudo_needs_no_password", lambda: True)
    assert "NOPASSWD" in _warnings(cfg) and "ohne Rückfrage als root" in _warnings(cfg)
    mp.setattr(askpass, "sudo_needs_no_password", lambda: False)
    mp.setattr(security_check, "_in_group", lambda name: name == "docker")
    assert "Gruppe „docker“" in _warnings(cfg)
    mp.setattr(security_check, "_in_group", lambda name: False)
    cfg.mcp.servers = {"a": McpServerConfig(command="x", allow=["*"]), "b": McpServerConfig(command="y")}
    assert 'allow: "*" (a)' in _warnings(cfg)
    cfg.mcp.servers = {"b": McpServerConfig(command="y")}
    assert _warnings(cfg) == "" and "1 MCP-Server" in security_check.render(security_check.report(cfg))
    mp.setattr(agent_safety, "running_as_root", lambda: True)
    assert "läuft als root" in _warnings(cfg)
    assert not any("sudo verlangt" in x["text"] for x in security_check.report(cfg))  # als root nichts zu sudo


def test_cli_prints_the_report(machine, capsys, monkeypatch):
    from orbwise import cli
    monkeypatch.setattr(cli, "load_config", lambda: machine.cfg)
    cli.main(["doctor", "--security"])
    out = capsys.readouterr().out
    assert out.startswith("Sicherheit auf diesem Rechner:") and "Alle Schutzmechanismen greifen" in out
    (machine.cfg.memory.dir.parent / "auto_mode").write_text("auto")
    cli.main(["doctor", "--security"])
    assert "1 Hinweis(e)" in capsys.readouterr().out
