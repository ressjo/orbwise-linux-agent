"""Autostart beim Anmelden: standardmäßig aus, per Einstellungen/CLI ein- und ausschaltbar, fremde Einträge bleiben."""

from fastapi.testclient import TestClient

from orbwise import autostart, cli


def test_on_off_and_foreign_entries(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    assert not autostart.enabled()
    assert autostart.set_enabled(True) and autostart.path().is_file()
    text = autostart.path().read_text()
    assert "Exec=sh -c" in text and " serve" in text and "orbwise.log" in text and "NoDisplay=true" in text
    assert not autostart.set_enabled(False) and not autostart.path().exists()
    # abgeschaltet über die Desktop-Einstellungen (Hidden=true) zählt als aus
    autostart.path().write_text(autostart.content() + "Hidden=true\n")
    assert not autostart.enabled()
    # ein fremder Eintrag unter demselben Namen wird nicht gelöscht
    autostart.path().write_text("[Desktop Entry]\nName=Etwas anderes\nExec=foo\n")
    autostart.set_enabled(False)
    assert autostart.path().exists()


def test_cli(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    cli.main(["autostart", "on"])
    assert autostart.enabled() and "an" in capsys.readouterr().out.lower()
    cli.main(["autostart", "off"])
    assert not autostart.enabled()


def test_api(tmp_path, cfg, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.setenv("ORBWISE_FAKE_LLM", "1")
    monkeypatch.setenv("ORBWISE_SKIP_WARMUP", "1")
    from orbwise.server import create_app
    with TestClient(create_app(cfg), base_url="http://localhost:8765") as client:
        assert client.get("/api/autostart").json()["enabled"] is False
        assert client.post("/api/autostart", json={"enabled": True}).json()["enabled"] is True
        assert autostart.enabled()
        assert client.post("/api/autostart", json={"enabled": "ja"}).json()["enabled"] is False  # nur echtes true
        assert not autostart.path().exists()
