"""Skills: einrichten über Oberfläche/CLI – Werte in state.json, Geheimnisse im Schlüsselbund, nie in config.yaml."""

import httpx
from conftest import run

from orbwise import keystore, skills
from orbwise.config import load_config
from orbwise.tools.registry import load_all_tools, tool_schemas


def names(cfg):
    load_all_tools()
    return {s["function"]["name"] for s in tool_schemas(cfg)}


def test_saving_a_skill_turns_its_tools_on_and_keeps_the_secret_out_of_state(cfg):
    assert "paperless_search" not in names(cfg)
    skills.save(cfg, "paperless", values={"url": "http://pl.local:8000", "token": "geheim123"})
    assert cfg.paperless.url == "http://pl.local:8000" and cfg.paperless.api_token == "geheim123"
    assert "paperless_search" in names(cfg)
    raw = skills.state_path(cfg).read_text()
    assert "pl.local" in raw and "geheim123" not in raw  # Geheimnis nur im Schlüsselbund
    assert keystore.get("paperless.token") == "geheim123"
    # leeres Geheimnis beim nächsten Speichern = unverändert
    skills.save(cfg, "paperless", values={"url": "http://pl2.local", "token": ""})
    assert cfg.paperless.url == "http://pl2.local" and cfg.paperless.api_token == "geheim123"


def test_status_never_returns_secrets(cfg):
    skills.save(cfg, "mail", values={"username": "ich@proton.me", "password": "bridge-pw"})
    st = skills.status(cfg)
    mail = next(s for s in st["skills"] if s["id"] == "mail")
    assert mail["active"] and mail["configured"] and not mail["missing"]
    pw = next(f for f in mail["fields"] if f["key"] == "password")
    assert pw["set"] and pw["source"] == "file" and "value" not in pw
    assert "bridge-pw" not in str(st)
    assert st["backend"] == "file"
    paperless = next(s for s in st["skills"] if s["id"] == "paperless")
    assert not paperless["configured"] and paperless["missing"] == ["url", "token"]


def test_switches_drive_disabled_and_extra(cfg):
    skills.save(cfg, "routines", enabled=False)
    assert "routine_tools" in cfg.tools.disabled and "routine_create" not in names(cfg)
    skills.save(cfg, "sysadmin", enabled=True)
    assert "sysadmin" in cfg.tools.extra and "top_processes" in names(cfg)
    skills.save(cfg, "routines", enabled=True)
    skills.save(cfg, "sysadmin", enabled=False)
    assert "routine_tools" not in cfg.tools.disabled and "sysadmin" not in cfg.tools.extra
    # Daten bleiben beim Abschalten erhalten
    skills.save(cfg, "weather", values={"location": "Freiburg"})
    skills.save(cfg, "weather", enabled=False)
    assert cfg.weather.location == "Freiburg" and "weather" not in names(cfg)


def test_remove_forgets_values_and_secrets(cfg):
    skills.save(cfg, "homeassistant", values={"url": "http://ha:8123", "token": "t"})
    skills.remove(cfg, "homeassistant")
    assert not cfg.homeassistant.enabled and keystore.get("homeassistant.token") == ""


def test_invalid_values_are_rejected_before_saving(cfg):
    import pytest
    with pytest.raises(ValueError):
        skills.save(cfg, "mail", values={"username": "x", "password": "y", "security": "quantum"})
    assert not skills.state_path(cfg).exists() and keystore.get("mail.password") == ""


def test_connection_test_uses_unsaved_values(cfg):
    seen = {}

    async def fake(c):
        seen["url"], seen["token"] = c.paperless.url, c.paperless.api_token
        return {"enabled": True, "online": True, "count": 42, "version": "2.14"}
    object.__setattr__(skills.BY_ID["paperless"], "test", fake)
    try:
        res = run(skills.test(cfg, "paperless", {"url": "http://pl", "token": "neu"}))
    finally:
        object.__setattr__(skills.BY_ID["paperless"], "test", skills._t_paperless)
    assert res["ok"] and "42" in res["message"] and seen == {"url": "http://pl", "token": "neu"}
    assert cfg.paperless.url == ""  # nichts gespeichert
    missing = run(skills.test(cfg, "paperless", {"url": "http://pl"}))
    assert missing["ok"] is False and "API-Token" in missing["message"]


def test_telegram_test_finds_the_chat_id(cfg, monkeypatch):
    from orbwise import telegram

    def handler(req):
        if req.url.path.endswith("/getMe"):
            return httpx.Response(200, json={"ok": True, "result": {"username": "mein_bot"}})
        return httpx.Response(200, json={"ok": True, "result": [{"message": {"chat": {"id": 4711}}}]})
    real = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    assert telegram.API
    res = run(skills.test(cfg, "telegram", {"token": "123:abc", "chat_id": ""}))
    assert res["details"]["chat_id"] == 4711 and "@mein_bot" in res["message"]


def test_plaintext_in_config_yaml_moves_to_the_keystore(tmp_path, monkeypatch):
    path = tmp_path / "config.yaml"
    path.write_text("# meine Config\nmemory:\n  dir: " + str(tmp_path / "mem") + "\n"
                    "paperless:\n  url: http://pl   # NAS\n  token: \"sehrgeheim\"   # aus dem Profil\n"
                    "tools:\n  brave_api_key: BSAabc\n", encoding="utf-8")
    cfg = load_config(path)
    text = path.read_text()
    assert "sehrgeheim" not in text and "BSAabc" not in text and "# meine Config" in text and "# NAS" in text
    assert 'token: ""   # in secrets.json' in text
    assert (tmp_path / "config.yaml.bak").read_text().count("sehrgeheim") == 1
    assert cfg.paperless.api_token == "sehrgeheim" and cfg.tools.brave_key == "BSAabc"
    assert keystore.get("paperless.token") == "sehrgeheim" and not skills.plaintext_left(cfg)
    again = load_config(path)  # zweiter Start: nichts mehr umzuziehen, Werte kommen aus dem Speicher
    assert again.paperless.api_token == "sehrgeheim"


def test_failed_keystore_leaves_config_untouched(tmp_path, monkeypatch):
    path = tmp_path / "config.yaml"
    original = "memory:\n  dir: " + str(tmp_path / "mem") + "\npaperless:\n  url: http://pl\n  token: abc\n"
    path.write_text(original, encoding="utf-8")
    monkeypatch.setattr(keystore, "set", lambda k, v: "file")
    monkeypatch.setattr(keystore, "get", lambda k: "")
    cfg = load_config(path)
    assert path.read_text() == original and not (tmp_path / "config.yaml.bak").exists()
    assert cfg.paperless.api_token == "abc"


def test_api_lists_saves_tests_and_removes(cfg, monkeypatch):
    from fastapi.testclient import TestClient

    from orbwise.server import create_app
    monkeypatch.setenv("ORBWISE_FAKE_LLM", "1")
    monkeypatch.setenv("ORBWISE_SKIP_WARMUP", "1")
    app = create_app(cfg)
    agent = app.state.hub.agent
    with TestClient(app, base_url="http://localhost:8765") as client:
        st = client.get("/api/skills").json()
        assert st["backend"] == "file" and {s["id"] for s in st["skills"]} >= {"mail", "paperless", "routines"}
        assert "paperless_search" not in {s["function"]["name"] for s in agent.all_schemas}
        r = client.post("/api/skills/paperless", json={"values": {"url": "http://pl", "token": "geheim"}})
        assert r.json() == {"ok": True}
        assert "paperless_search" in {s["function"]["name"] for s in agent.all_schemas}  # sofort, ohne Neustart
        listed = client.get("/api/skills").text
        assert "geheim" not in listed and '"set":true' in listed.replace(" ", "")
        assert client.post("/api/skills/mail", json={"values": {"security": "quantum"}}).status_code == 400
        assert client.post("/api/skills/nope", json={}).status_code == 404
        res = client.post("/api/skills/paperless/test", json={"values": {"url": ""}}).json()
        assert res["ok"] is False and "Adresse" in res["message"]
        assert client.delete("/api/skills/paperless").json() == {"ok": True}
        assert "paperless_search" not in {s["function"]["name"] for s in agent.all_schemas}


def test_console_setup_asks_tests_and_saves(cfg, monkeypatch):
    from orbwise import skills_cli
    calls = []

    async def fake(c):
        calls.append(c.paperless.api_token)
        return {"enabled": True, "online": len(calls) > 1, "error": "401 Unauthorized"}
    object.__setattr__(skills.BY_ID["paperless"], "test", fake)
    answers = iter(["3",                     # Auswahl: Paperless
                    "http://pl.local:8000",  # Adresse (Token kommt verdeckt über getpass)
                    "n",                     # Test schlägt fehl → nochmal
                    ""])                     # Adresse behalten
    secrets = iter(["falsch", "richtig"])
    out = []
    try:
        done = skills_cli.setup(cfg, None, ask=lambda p: next(answers), secret=lambda p: next(secrets),
                                out=out.append)
    finally:
        object.__setattr__(skills.BY_ID["paperless"], "test", skills._t_paperless)
    assert done == ["paperless"] and calls == ["falsch", "richtig"]
    assert cfg.paperless.url == "http://pl.local:8000" and cfg.paperless.api_token == "richtig"
    text = "\n".join(out)
    assert "Paperless-ngx" in text and "✘" in text and "✔" in text and "richtig" not in text


def test_console_overview_lists_everything(cfg):
    from orbwise import skills_cli
    out = []
    skills_cli.overview(cfg, out=out.append)
    text = "\n".join(out)
    assert "Verbindungen" in text and "paperless" in text and "routines" in text and "secrets.json" in text
