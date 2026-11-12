import httpx
import pytest
from conftest import run
from fake_homeassistant import TOKEN, FakeHA

from orbwise.extras import homeassistant as ha
from orbwise.tools.registry import CONFIRM, SAFE, ToolContext, get_tool, load_all_tools, tool_schemas

TOOLS = {"ha_find", "ha_state", "ha_control"}


@pytest.fixture
def fake(monkeypatch, cfg):
    f = FakeHA()
    monkeypatch.setattr(ha, "TRANSPORT", f.transport())
    cfg.homeassistant.url = "http://homeassistant.local:8123/"
    cfg.homeassistant.token = TOKEN
    return f


def ctx(cfg):
    return ToolContext(cfg=cfg, memory=None)


def test_only_when_configured(cfg, monkeypatch):
    monkeypatch.delenv("ORBWISE_HA_TOKEN", raising=False)
    load_all_tools()
    assert not TOOLS & {s["function"]["name"] for s in tool_schemas(cfg)}
    cfg.homeassistant.url, cfg.homeassistant.token = "http://ha", "t"
    assert TOOLS <= {s["function"]["name"] for s in tool_schemas(cfg)}


def test_find_by_room_name_and_domain(cfg, fake):
    out = run(ha.ha_find(ctx(cfg), "wohnzimmer"))
    assert "light.wohnzimmer_decke – Wohnzimmer Decke [Wohnzimmer]: off" in out and "kueche" not in out
    out = run(ha.ha_find(ctx(cfg), "", "light"))
    assert "Küche [Küche]: on (80 %)" in out and "climate" not in out
    assert "22.4 °C" in run(ha.ha_find(ctx(cfg), "temperatur büro"))
    assert "ist 20.5 °C, soll 21 °C" in run(ha.ha_find(ctx(cfg), "bad"))
    assert "Keine passenden" in run(ha.ha_find(ctx(cfg), "keller"))


def test_control_actions(cfg, fake):
    out = run(ha.ha_control(ctx(cfg), "light.wohnzimmer_decke", "brightness", "40"))
    assert fake.calls[-1] == ("light.turn_on", {"entity_id": "light.wohnzimmer_decke", "brightness_pct": 40.0})
    assert "on (40 %)" in out
    run(ha.ha_control(ctx(cfg), "climate.bad", "temperature", "22,5 °C"))
    assert fake.calls[-1] == ("climate.set_temperature", {"entity_id": "climate.bad", "temperature": 22.5})
    run(ha.ha_control(ctx(cfg), "scene.filmabend", "on"))
    assert fake.calls[-1][0] == "scene.turn_on"
    run(ha.ha_control(ctx(cfg), "cover.garagentor", "open"))
    assert fake.calls[-1][0] == "cover.open_cover"
    assert "nur bei Lichtern" in run(ha.ha_control(ctx(cfg), "climate.bad", "brightness", "50"))
    assert "gibt es nicht" in run(ha.ha_control(ctx(cfg), "light.gibtsnicht", "on"))
    assert "Ungültige entity_id" in run(ha.ha_control(ctx(cfg), "../../api", "on"))


def test_risk_for_sensitive_devices(cfg):
    spec = get_tool("ha_control") or (load_all_tools() and get_tool("ha_control"))
    assert spec.assess(ctx(cfg), {"entity_id": "light.kueche", "action": "on"})[0] == SAFE
    assert spec.assess(ctx(cfg), {"entity_id": "lock.haustuer", "action": "unlock"})[0] == CONFIRM
    assert spec.assess(ctx(cfg), {"entity_id": "cover.garagentor", "action": "open"})[0] == CONFIRM
    assert spec.assess(ctx(cfg), {"entity_id": "cover.rollo_kueche", "action": "open"})[0] == SAFE


def test_errors_and_status(cfg, fake, monkeypatch):
    st = run(ha.ha_status(cfg))
    assert st["online"] and st["entities"] == 7 and st["version"] == "2026.9.1"
    cfg.homeassistant.token = "falsch"
    assert "Token ist ungültig" in run(ha.ha_find(ctx(cfg), "x"))

    def boom(request):
        raise httpx.ConnectError("[Errno 111] Connection refused")

    monkeypatch.setattr(ha, "TRANSPORT", httpx.MockTransport(boom))
    assert "lehnt die Verbindung ab" in run(ha.ha_state(ctx(cfg), "light.kueche"))
