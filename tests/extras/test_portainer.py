"""Portainer: Container/Stacks auflisten, Updates ohne Download prüfen, aktualisieren (Stack neu ausrollen bzw.
Container neu erstellen), Aktionen, Logs und neue Stacks – gegen eine simulierte Portainer-API."""

import json
import struct

import httpx
import pytest
from conftest import run

from orbwise.config import Config
from orbwise.extras import portainer as pt
from orbwise.tools.registry import CONFIRM, SAFE, ToolContext, get_tool, load_all_tools

COMPOSE = "services:\n  jellyfin:\n    image: jellyfin/jellyfin:latest\n"


class FakePortainer:
    def __init__(self):
        self.calls: list[tuple[str, str, dict]] = []
        self.containers = [
            {"Id": "a1" * 32, "Names": ["/jellyfin"], "Image": "jellyfin/jellyfin:latest", "ImageID": "sha256:old",
             "State": "running", "Status": "Up 3 days", "Labels": {"com.docker.compose.project": "media"}},
            {"Id": "b2" * 32, "Names": ["/pihole"], "Image": "pihole/pihole:latest", "ImageID": "sha256:ph",
             "State": "running", "Status": "Up 1 day", "Labels": {}},
            {"Id": "c3" * 32, "Names": ["/backup"], "Image": "sha256:abc", "ImageID": "sha256:abc",
             "State": "exited", "Status": "Exited (0)", "Labels": {}},
        ]
        self.stacks = [{"Id": 7, "Name": "media", "EndpointId": 2, "Status": 1, "Env": [{"name": "TZ", "value": "x"}],
                        "GitConfig": None}]
        self.recreate_supported = True
        self.new_create_api = True

    def handler(self, request: httpx.Request) -> httpx.Response:
        path, method = request.url.path, request.method
        body = json.loads(request.content) if request.content else {}
        self.calls.append((method, path, body))
        assert request.headers["X-API-Key"] == "ptr_test"
        if path == "/api/status":
            return httpx.Response(200, json={"Version": "2.21.0"})
        if path == "/api/endpoints":
            return httpx.Response(200, json=[{"Id": 1, "Name": "alt", "Status": 2}, {"Id": 2, "Name": "nas", "Status": 1}])
        if path == "/api/endpoints/2/docker/containers/json":
            return httpx.Response(200, json=self.containers)
        if path.startswith("/api/endpoints/2/docker/images/"):
            image_id = path.split("/images/")[1].removesuffix("/json")
            digests = {"sha256:old": ["jellyfin/jellyfin@sha256:111"], "sha256:ph": ["pihole/pihole@sha256:222"]}
            return httpx.Response(200, json={"RepoDigests": digests.get(image_id, [])})
        if path.startswith("/api/endpoints/2/docker/distribution/"):
            name = path.split("/distribution/")[1].removesuffix("/json")
            remote = {"jellyfin/jellyfin:latest": "sha256:999", "pihole/pihole:latest": "sha256:222"}
            return httpx.Response(200, json={"Descriptor": {"digest": remote[name]}})
        if path.endswith("/logs"):
            frames = b"".join(struct.pack(">BxxxI", s, len(t)) + t for s, t in ((1, b"start ok\n"), (2, b"warn x\n")))
            return httpx.Response(200, content=frames)
        for action in ("start", "stop", "restart"):
            if path.endswith(f"/containers/{'b2' * 32}/{action}") and method == "POST":
                self.containers[1]["State"] = "exited" if action == "stop" else "running"
                return httpx.Response(204)
        if method == "DELETE" and "/containers/" in path:
            self.containers = [c for c in self.containers if c["Id"] not in path]
            return httpx.Response(204)
        if path == "/api/stacks" and method == "GET":
            return httpx.Response(200, json=self.stacks)
        if path == "/api/stacks/7/file":
            return httpx.Response(200, json={"StackFileContent": COMPOSE})
        if path == "/api/stacks/7" and method == "PUT":
            return httpx.Response(200, json=self.stacks[0])
        if path.endswith("/recreate"):
            if not self.recreate_supported:
                return httpx.Response(404, json={"message": "not found"})
            self.containers[1]["ImageID"] = "sha256:new"
            return httpx.Response(200, json={})
        if path == "/api/stacks/create/standalone/string":
            if not self.new_create_api:
                return httpx.Response(404, text="404 page not found")
            self.stacks.append({"Id": 8, "Name": body["name"], "EndpointId": 2, "Status": 1, "Env": body["env"]})
            return httpx.Response(200, json=self.stacks[-1])
        if path == "/api/stacks" and method == "POST":
            self.stacks.append({"Id": 9, "Name": body["Name"], "EndpointId": 2, "Status": 1, "Env": body["Env"]})
            return httpx.Response(200, json=self.stacks[-1])
        if path.startswith("/api/stacks/7/") and method == "POST":
            return httpx.Response(200, json={})
        return httpx.Response(404, json={"message": f"unexpected {method} {path}"})


@pytest.fixture
def fake(monkeypatch):
    f = FakePortainer()
    monkeypatch.setattr(pt, "TRANSPORT", httpx.MockTransport(f.handler))
    return f


@pytest.fixture
def ctx():
    cfg = Config.model_validate({"portainer": {"url": "https://nas.local:9443/", "token": "ptr_test"}})
    return ToolContext(cfg=cfg, memory=None)


def test_enabled_only_when_configured():
    load_all_tools()
    spec = get_tool("portainer_containers")
    assert not spec.is_enabled(Config())
    assert spec.is_enabled(Config.model_validate({"portainer": {"url": "http://x", "token": "t"}}))


def test_list_uses_first_environment_that_is_up(fake, ctx):
    out = run(pt.portainer_containers(ctx))
    assert "3 Container in nas" in out and "jellyfin: running" in out and "Stack media" in out
    assert "pihole" in run(pt.portainer_containers(ctx, filter="pi")) and "jellyfin" not in run(
        pt.portainer_containers(ctx, filter="pi"))


def test_check_updates_compares_digests_without_pulling(fake, ctx):
    out = run(pt.portainer_check_updates(ctx))
    assert "Update verfügbar" in out and "jellyfin" in out.split("Aktuell")[0]
    assert "Aktuell: pihole" in out
    assert not any("images/create" in p for _, p, _ in fake.calls)  # nichts heruntergeladen


def test_update_container_of_stack_redeploys_stack_with_pull(fake, ctx):
    out = run(pt.portainer_update(ctx, "jellyfin"))
    assert "Stack media" in out
    put = next(b for m, p, b in fake.calls if m == "PUT" and p == "/api/stacks/7")
    assert put["pullImage"] is True and put["stackFileContent"] == COMPOSE and put["env"][0]["name"] == "TZ"


def test_update_single_container_recreates_with_pull(fake, ctx):
    out = run(pt.portainer_update(ctx, "pihole"))
    assert "neu erstellt" in out
    assert any(p == f"/api/docker/2/containers/{'b2' * 32}/recreate" and b == {"PullImage": True}
               for _, p, b in fake.calls)
    fake.recreate_supported = False
    assert "Recreate" in run(pt.portainer_update(ctx, "pihole"))  # alte Portainer-Version: klarer Hinweis


def test_container_actions(fake, ctx):
    assert "stop ✔ – exited" in run(pt.portainer_container_action(ctx, "pihole", "stop"))
    assert "Unbekannte Aktion" in run(pt.portainer_container_action(ctx, "pihole", "explode"))
    assert "nicht eindeutig" not in run(pt.portainer_container_action(ctx, "pihole", "start"))
    assert "Kein Container" in run(pt.portainer_container_action(ctx, "nginx", "start"))
    assert "entfernt" in run(pt.portainer_container_action(ctx, "backup", "remove"))


def test_logs_are_demultiplexed(fake, ctx):
    out = run(pt.portainer_logs(ctx, "jellyfin", lines=10))
    assert "start ok\nwarn x" in out and "\x01" not in out


def test_deploy_new_stack_and_fallback_for_old_portainer(fake, ctx):
    out = run(pt.portainer_deploy_stack(ctx, "Jelly Fin", COMPOSE, env="TZ=Europe/Berlin\nPUID=1000"))
    assert "jelly-fin installiert" in out
    body = next(b for m, p, b in fake.calls if p == "/api/stacks/create/standalone/string")
    assert body["env"] == [{"name": "TZ", "value": "Europe/Berlin"}, {"name": "PUID", "value": "1000"}]
    fake.new_create_api = False
    assert "installiert" in run(pt.portainer_deploy_stack(ctx, "immich", COMPOSE))
    assert any(m == "POST" and p == "/api/stacks" for m, p, _ in fake.calls)
    assert "keine docker-compose" in run(pt.portainer_deploy_stack(ctx, "x", "hallo"))


def test_redeploy_existing_stack_with_new_compose(fake, ctx):
    assert "aktualisiert" in run(pt.portainer_deploy_stack(ctx, "media", COMPOSE + "  # neu\n"))


def test_risks():
    load_all_tools()
    for name in ("portainer_containers", "portainer_check_updates", "portainer_logs", "portainer_stacks"):
        assert get_tool(name).risk == SAFE
    for name in ("portainer_update", "portainer_deploy_stack"):
        assert get_tool(name).risk == CONFIRM
    risk, reason = get_tool("portainer_container_action").assess(None, {"action": "remove"})
    assert risk == CONFIRM and "Volumes" in reason


def test_bad_token_and_status(fake, ctx, monkeypatch):
    status = run(pt.portainer_status(ctx.cfg))
    assert status["online"] and status["version"] == "2.21.0" and "nas (online)" in status["environments"]
    monkeypatch.setattr(pt, "TRANSPORT", httpx.MockTransport(lambda r: httpx.Response(401)))
    assert "Zugriffstoken ist ungültig" in run(pt.portainer_containers(ctx))


def test_curl_to_portainer_is_redirected_to_tools(ctx):
    from orbwise.tools.registry import BLOCKED
    from orbwise.tools.shell import _risk
    risk, reason = _risk(ctx, {"command": "curl -k https://nas.local:9443/api/endpoints"})
    assert risk == BLOCKED and "portainer_containers" in reason
