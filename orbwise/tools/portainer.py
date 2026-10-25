"""Docker-Container und Stacks über die Portainer-API: auflisten, auf Updates prüfen, aktualisieren, starten/stoppen,
Logs lesen und neue Apps per docker-compose installieren.

Einrichtung: in Portainer oben rechts auf den Benutzer → „My account“ → „Access tokens“ → „Add access token“:
    portainer:
      url: https://nas.local:9443
      token: "ptr_…"
      verify_ssl: false     # Portainers eigenes Zertifikat ist selbstsigniert

Updates prüfen lädt nichts herunter: der Digest des Images in der Registry (Docker-API /distribution) wird mit dem
des laufenden Containers verglichen. Aktualisieren: Container eines Portainer-Stacks → Stack mit neuem Image neu
ausrollen; einzelne Container → Portainers „Recreate“ mit „Pull latest image“.
"""

from __future__ import annotations

import re
import struct
from typing import Annotated, Any
from urllib.parse import quote

import httpx

from ..lang import T
from .netutil import client_kwargs, explain, html_instead_of_json, normalize_url
from .registry import CONFIRM, ToolContext, tool

TRANSPORT: httpx.AsyncBaseTransport | None = None  # für Tests (httpx.MockTransport)

CONTAINER_ACTIONS = ("start", "stop", "restart", "kill", "pause", "unpause", "remove")
STACK_ACTIONS = ("start", "stop", "remove")
COMPOSE_PROJECT = "com.docker.compose.project"


class PortainerError(RuntimeError):
    pass


def _enabled(cfg: Any) -> bool:
    return cfg.portainer.enabled


class PortainerClient:
    def __init__(self, cfg):
        p = cfg.portainer
        self.cfg = p
        self.url = normalize_url(p.url, "/api")
        self.client = httpx.AsyncClient(base_url=self.url + "/api",
                                        headers={"X-API-Key": p.api_token, "Accept": "application/json"},
                                        transport=TRANSPORT, **client_kwargs(p.verify_ssl, p.timeout))
        self._env: dict | None = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        await self.client.aclose()

    async def request(self, method: str, path: str, raw: bool = False, ok404: bool = False, **kw) -> Any:
        try:
            r = await self.client.request(method, path, **kw)
        except httpx.HTTPError as e:
            raise PortainerError(explain(e, "Portainer", self.url)) from e
        if r.status_code == 401:
            raise PortainerError(T("Der Portainer-Zugriffstoken ist ungültig (My account → Access tokens).",
                                   "The Portainer access token is invalid (My account → Access tokens)."))
        if r.status_code == 403:
            raise PortainerError(T("Portainer verweigert das – dem Token fehlen die Rechte (Administrator bzw. "
                                   "Zugriff auf diese Umgebung).", "Portainer refuses this – the token lacks the "
                                   "rights (administrator or access to this environment)."))
        if r.status_code == 404 and ok404:
            return None
        if r.status_code >= 400:
            try:
                data = r.json()
                detail = data.get("details") or data.get("message") or r.text
            except ValueError:
                detail = r.text
            raise PortainerError(f"Portainer {r.status_code}: {str(detail)[:300]}")
        if raw:
            return r
        if html_instead_of_json(r):
            raise PortainerError(T(f"Unter {self.url} antwortet eine Webseite statt der Portainer-API – Adresse prüfen.",
                                   f"{self.url} answers with a web page instead of the Portainer API – check the URL."))
        if not r.content:
            return None
        return r.json() if "json" in r.headers.get("content-type", "") else r.text

    # ---------- Umgebungen ----------
    async def environments(self) -> list[dict]:
        return await self.request("GET", "/endpoints") or []

    async def environment(self, wanted: str = "") -> dict:
        wanted = (wanted or self.cfg.environment or "").strip()
        if self._env and not wanted:
            return self._env
        envs = await self.environments()
        if not envs:
            raise PortainerError(T("Portainer verwaltet keine Umgebung.", "Portainer manages no environment."))
        if wanted:
            match = [e for e in envs if str(e.get("Id")) == wanted or str(e.get("Name", "")).lower() == wanted.lower()]
            if not match:
                raise PortainerError(T("Unbekannte Umgebung", "Unknown environment") + f" '{wanted}'. " +
                                     T("Vorhanden: ", "Available: ") + ", ".join(e.get("Name", "?") for e in envs))
            env = match[0]
        else:
            env = next((e for e in envs if e.get("Status") == 1), envs[0])
        if not wanted:
            self._env = env
        return env

    def docker(self, env: dict, path: str) -> str:
        return f"/endpoints/{env['Id']}/docker{path}"

    # ---------- Container ----------
    async def containers(self, env: dict, all_: bool = True) -> list[dict]:
        return await self.request("GET", self.docker(env, "/containers/json"), params={"all": int(all_)}) or []

    async def find_container(self, env: dict, name: str) -> dict:
        wanted = name.strip().lstrip("/").lower()
        items = await self.containers(env)

        def names(c: dict) -> list[str]:
            return [n.lstrip("/").lower() for n in c.get("Names") or []]
        exact = [c for c in items if wanted in names(c) or c.get("Id", "").lower().startswith(wanted)]
        if len(exact) == 1 or (exact and len(wanted) >= 12):
            return exact[0]
        partial = [c for c in items if any(wanted in n for n in names(c))]
        if len(partial) == 1:
            return partial[0]
        if not partial and not exact:
            raise PortainerError(T(f"Kein Container „{name}“. Vorhanden: ", f"No container \"{name}\". Available: ")
                                 + ", ".join(sorted(_name(c) for c in items))[:800])
        raise PortainerError(T(f"„{name}“ ist nicht eindeutig: ", f"\"{name}\" is ambiguous: ")
                             + ", ".join(_name(c) for c in (exact or partial)))

    async def stacks(self, env: dict | None = None) -> list[dict]:
        items = await self.request("GET", "/stacks") or []
        return [s for s in items if env is None or s.get("EndpointId") == env["Id"]]

    async def find_stack(self, env: dict, name: str) -> dict | None:
        wanted = name.strip().lower()
        return next((s for s in await self.stacks(env) if str(s.get("Name", "")).lower() == wanted), None)

    async def update_state(self, env: dict, c: dict) -> str:
        """'current' | 'outdated' | 'unknown: Grund' – ohne etwas herunterzuladen (Registry-Digest vs. Image)."""
        image = c.get("Image") or ""
        if not image or image.startswith("sha256:"):
            return T("unbekannt: Container läuft mit einer Image-ID statt eines Namens",
                     "unknown: container runs from an image ID, not a name")
        try:
            local = await self.request("GET", self.docker(env, f"/images/{quote(c.get('ImageID', ''), safe='')}/json"))
            remote = await self.request("GET", self.docker(env, f"/distribution/{quote(image, safe='')}/json"))
        except PortainerError as e:
            return T("unbekannt: ", "unknown: ") + str(e)[:120]
        digest = ((remote or {}).get("Descriptor") or {}).get("digest") or ""
        repo_digests = [d.split("@", 1)[-1] for d in (local or {}).get("RepoDigests") or []]
        if not digest or not repo_digests:
            return T("unbekannt: kein Registry-Digest (lokal gebautes Image?)",
                     "unknown: no registry digest (locally built image?)")
        return "current" if digest in repo_digests else "outdated"


def _name(c: dict) -> str:
    return ((c.get("Names") or ["?"])[0]).lstrip("/")


def _stack_of(c: dict) -> str:
    return (c.get("Labels") or {}).get(COMPOSE_PROJECT, "")


def demux_logs(data: bytes) -> str:
    """Docker-Logs ohne TTY kommen mit 8-Byte-Köpfen je Block (stdout/stderr) – die werden entfernt."""
    out, i = [], 0
    while i + 8 <= len(data) and data[i] in (0, 1, 2) and data[i + 1:i + 4] == b"\0\0\0":
        (size,) = struct.unpack(">I", data[i + 4:i + 8])
        out.append(data[i + 8:i + 8 + size])
        i += 8 + size
    if i == 0:
        return data.decode(errors="replace")
    return b"".join(out).decode(errors="replace") + data[i:].decode(errors="replace")


async def _guard(coro) -> str:
    try:
        return await coro
    except PortainerError as e:
        return str(e)


# ---------------------------------------------------------------- Werkzeuge
@tool("Listet Docker-Container (Portainer) mit Zustand, Image und Stack.", enabled=_enabled)
async def portainer_containers(
    ctx: ToolContext,
    filter: Annotated[str, "Teil von Name/Image/Stack"] = "",
    environment: Annotated[str, "Umgebung (Name/ID)"] = "",
) -> str:
    async def go() -> str:
        async with PortainerClient(ctx.cfg) as pc:
            env = await pc.environment(environment)
            items = await pc.containers(env)
            want = filter.strip().lower()
            rows = []
            for c in sorted(items, key=lambda c: (_stack_of(c), _name(c))):
                text = f"{_name(c)} {c.get('Image', '')} {_stack_of(c)}".lower()
                if want and want not in text:
                    continue
                stack = f" · Stack {_stack_of(c)}" if _stack_of(c) else ""
                rows.append(f"- {_name(c)}: {c.get('State', '?')} ({c.get('Status', '')}) · {c.get('Image', '?')}{stack}")
            head = T(f"{len(rows)} Container in {env.get('Name')}:", f"{len(rows)} containers in {env.get('Name')}:")
            return head + "\n" + ("\n".join(rows) if rows else T("(keine)", "(none)"))
    return await _guard(go())


@tool("Prüft, ob es neuere Images gibt (lädt nichts, ändert nichts).",
      enabled=_enabled)
async def portainer_check_updates(
    ctx: ToolContext,
    container: Annotated[str, "Leer = alle laufenden"] = "",
    environment: Annotated[str, "Umgebung (Name/ID)"] = "",
) -> str:
    async def go() -> str:
        async with PortainerClient(ctx.cfg) as pc:
            env = await pc.environment(environment)
            items = [await pc.find_container(env, container)] if container else \
                [c for c in await pc.containers(env, all_=False)]
            outdated, current, unknown = [], [], []
            for c in items:
                state = await pc.update_state(env, c)
                label = f"{_name(c)} ({c.get('Image')})" + (f" · Stack {_stack_of(c)}" if _stack_of(c) else "")
                (outdated if state == "outdated" else current if state == "current" else unknown).append(
                    label if state in ("outdated", "current") else f"{label}: {state}")
            parts = []
            if outdated:
                parts.append(T("Update verfügbar:\n", "Update available:\n") + "\n".join(f"- {x}" for x in outdated))
            if current:
                parts.append(T("Aktuell: ", "Up to date: ") + ", ".join(x.split(" (")[0] for x in current))
            if unknown:
                parts.append(T("Nicht prüfbar:\n", "Could not check:\n") + "\n".join(f"- {x}" for x in unknown))
            if outdated:
                parts.append(T("Aktualisieren mit portainer_update(name) – bei Stacks den Stack-Namen.",
                               "Update with portainer_update(name) – for stacks use the stack name."))
            return "\n".join(parts) or T("Keine laufenden Container.", "No running containers.")
    return await _guard(go())


@tool("Zeigt die letzten Log-Zeilen eines Containers.", enabled=_enabled)
async def portainer_logs(
    ctx: ToolContext,
    container: str,
    lines: Annotated[int, "Standard 100"] = 100,
    environment: Annotated[str, "Umgebung (Name/ID)"] = "",
) -> str:
    async def go() -> str:
        async with PortainerClient(ctx.cfg) as pc:
            env = await pc.environment(environment)
            c = await pc.find_container(env, container)
            r = await pc.request("GET", pc.docker(env, f"/containers/{c['Id']}/logs"), raw=True,
                                 params={"stdout": 1, "stderr": 1, "tail": max(1, min(lines or 100, 2000))})
            text = demux_logs(r.content).strip() or T("(keine Ausgabe)", "(no output)")
            from .proc import clip_saved
            return f"Logs {_name(c)}:\n" + clip_saved(text, ctx.limit(), f"logs-{_name(c)}")
    return await _guard(go())


@tool("Listet die Stacks; mit name auch dessen Compose-Datei.", enabled=_enabled)
async def portainer_stacks(
    ctx: ToolContext,
    name: str = "",
    environment: Annotated[str, "Umgebung (Name/ID)"] = "",
) -> str:
    async def go() -> str:
        async with PortainerClient(ctx.cfg) as pc:
            env = await pc.environment(environment)
            if name:
                stack = await pc.find_stack(env, name)
                if not stack:
                    raise PortainerError(T(f"Kein Stack „{name}“.", f"No stack \"{name}\"."))
                data = await pc.request("GET", f"/stacks/{stack['Id']}/file") or {}
                env_vars = ", ".join(f"{v.get('name')}=…" for v in stack.get("Env") or [])
                return (f"Stack {stack['Name']} ({'aktiv' if stack.get('Status') == 1 else 'gestoppt'})"
                        + (f" · Variablen: {env_vars}" if env_vars else "") + "\n"
                        + str(data.get("StackFileContent", ""))[:ctx.limit()])
            stacks = await pc.stacks(env)
            containers = await pc.containers(env)
            rows = []
            for s in sorted(stacks, key=lambda s: s.get("Name", "")):
                own = [c for c in containers if _stack_of(c).lower() == str(s.get("Name", "")).lower()]
                running = sum(1 for c in own if c.get("State") == "running")
                git = " · Git" if s.get("GitConfig") else ""
                rows.append(f"- {s.get('Name')}: {T('aktiv', 'active') if s.get('Status') == 1 else T('gestoppt', 'stopped')}"
                            f" · {running}/{len(own)} Container laufen{git}")
            return T(f"{len(rows)} Stacks:\n", f"{len(rows)} stacks:\n") + ("\n".join(rows) or T("(keine)", "(none)"))
    return await _guard(go())


def _confirm(ctx: ToolContext, args: dict) -> tuple[str, str]:
    action = str(args.get("action") or "")
    if action == "remove":
        return CONFIRM, T("entfernt – Daten in benannten Volumes bleiben erhalten",
                          "removes it – data in named volumes is kept")
    return CONFIRM, ""


@tool(f"Startet, stoppt oder entfernt einen Docker-Container: action = {', '.join(CONTAINER_ACTIONS)}.",
      risk=_confirm, enabled=_enabled)
async def portainer_container_action(
    ctx: ToolContext,
    container: str,
    action: Annotated[str, "start | stop | restart | kill | pause | unpause | remove"],
    environment: Annotated[str, "Umgebung (Name/ID)"] = "",
) -> str:
    async def go() -> str:
        act = action.strip().lower()
        if act not in CONTAINER_ACTIONS:
            raise PortainerError(T("Unbekannte Aktion – erlaubt: ", "Unknown action – allowed: ")
                                 + ", ".join(CONTAINER_ACTIONS))
        async with PortainerClient(ctx.cfg) as pc:
            env = await pc.environment(environment)
            c = await pc.find_container(env, container)
            if act == "remove":
                await pc.request("DELETE", pc.docker(env, f"/containers/{c['Id']}"), params={"force": "true"})
                return T(f"Container {_name(c)} entfernt.", f"Container {_name(c)} removed.") + (
                    T(f" Er gehört zum Stack {_stack_of(c)} – beim nächsten Ausrollen kommt er wieder.",
                      f" It belongs to stack {_stack_of(c)} – it comes back on the next deploy.") if _stack_of(c) else "")
            await pc.request("POST", pc.docker(env, f"/containers/{c['Id']}/{act}"), raw=True)
            fresh = await pc.find_container(env, c["Id"])
            return f"{_name(c)}: {act} ✔ – {fresh.get('State')} ({fresh.get('Status')})"
    return await _guard(go())


async def _redeploy(pc: PortainerClient, env: dict, stack: dict) -> None:
    """Stack mit aktuellen Images neu ausrollen (Compose-Datei bleibt; Git-Stacks: neu aus dem Repository)."""
    sid, params = stack["Id"], {"endpointId": env["Id"]}
    if stack.get("GitConfig"):
        await pc.request("PUT", f"/stacks/{sid}/git/redeploy", params=params,
                         json={"pullImage": True, "prune": False, "env": stack.get("Env") or [],
                               "repositoryReferenceName": (stack["GitConfig"] or {}).get("ReferenceName", "")})
        return
    data = await pc.request("GET", f"/stacks/{sid}/file") or {}
    await pc.request("PUT", f"/stacks/{sid}", params=params,
                     json={"stackFileContent": data.get("StackFileContent", ""), "env": stack.get("Env") or [],
                           "prune": False, "pullImage": True})


@tool("Aktualisiert einen Container oder Stack auf das neueste Image (Container eines Stacks: ganzer Stack).", risk=CONFIRM, enabled=_enabled)
async def portainer_update(
    ctx: ToolContext,
    name: Annotated[str, "Container oder Stack"],
    environment: Annotated[str, "Umgebung (Name/ID)"] = "",
) -> str:
    async def go() -> str:
        async with PortainerClient(ctx.cfg) as pc:
            env = await pc.environment(environment)
            stack = await pc.find_stack(env, name)
            container = None
            if not stack:
                container = await pc.find_container(env, name)
                if _stack_of(container):
                    stack = await pc.find_stack(env, _stack_of(container))
            if stack:
                await _redeploy(pc, env, stack)
                own = [c for c in await pc.containers(env) if _stack_of(c).lower() == stack["Name"].lower()]
                state = ", ".join(f"{_name(c)}: {c.get('State')}" for c in own)
                return T(f"Stack {stack['Name']} mit aktuellen Images neu ausgerollt – {state}.",
                         f"Stack {stack['Name']} redeployed with current images – {state}.")
            if _stack_of(container):
                raise PortainerError(T(f"{_name(container)} gehört zum Compose-Projekt „{_stack_of(container)}“, das "
                                       "nicht in Portainer verwaltet wird – dort per docker compose pull && up -d "
                                       "aktualisieren.", f"{_name(container)} belongs to compose project "
                                       f"\"{_stack_of(container)}\" which Portainer doesn't manage – update it with "
                                       "docker compose pull && up -d."))
            r = await pc.request("POST", f"/docker/{env['Id']}/containers/{container['Id']}/recreate",
                                 json={"PullImage": True}, ok404=True)
            if r is None:
                raise PortainerError(T("Diese Portainer-Version kann Container nicht per API neu erstellen – in "
                                       "Portainer beim Container „Recreate“ mit „Re-pull image“ wählen oder "
                                       "Portainer aktualisieren.", "This Portainer version cannot recreate containers "
                                       "via the API – use \"Recreate\" with \"Re-pull image\" in Portainer or update "
                                       "Portainer."))
            fresh = await pc.find_container(env, _name(container))
            return T(f"{_name(container)} mit aktuellem Image neu erstellt – {fresh.get('State')} "
                     f"({fresh.get('Status')}).", f"{_name(container)} recreated with the current image – "
                     f"{fresh.get('State')} ({fresh.get('Status')}).")
    return await _guard(go())


def _parse_env(text: str) -> list[dict]:
    out = []
    for line in re.split(r"[\n;]", text or ""):
        key, sep, value = line.strip().partition("=")
        if sep and key.strip():
            out.append({"name": key.strip(), "value": value.strip()})
    return out


@tool("Installiert eine App als Stack aus einer docker-compose-Datei oder ersetzt die eines vorhandenen Stacks.", risk=CONFIRM, enabled=_enabled, editable=("compose", "env"))
async def portainer_deploy_stack(
    ctx: ToolContext,
    name: Annotated[str, "Klein, ohne Leerzeichen"],
    compose: Annotated[str, "Vollständige docker-compose.yml"],
    env: Annotated[str, "Je Zeile NAME=wert"] = "",
    environment: Annotated[str, "Umgebung (Name/ID)"] = "",
) -> str:
    async def go() -> str:
        stack_name = re.sub(r"[^a-z0-9_-]", "", name.strip().lower().replace(" ", "-"))
        if not stack_name:
            raise PortainerError(T("Ungültiger Stack-Name.", "Invalid stack name."))
        if "services:" not in compose:
            raise PortainerError(T("Das ist keine docker-compose-Datei (services: fehlt).",
                                   "This is not a docker-compose file (services: missing)."))
        variables = _parse_env(env)
        async with PortainerClient(ctx.cfg) as pc:
            target = await pc.environment(environment)
            existing = await pc.find_stack(target, stack_name)
            params = {"endpointId": target["Id"]}
            if existing:
                await pc.request("PUT", f"/stacks/{existing['Id']}", params=params,
                                 json={"stackFileContent": compose, "env": variables or existing.get("Env") or [],
                                       "prune": False, "pullImage": True})
                verb = T("aktualisiert", "updated")
            else:
                body = {"name": stack_name, "stackFileContent": compose, "env": variables}
                r = await pc.request("POST", "/stacks/create/standalone/string", params=params, json=body, ok404=True)
                if r is None:  # Portainer vor 2.19
                    await pc.request("POST", "/stacks", params={"type": 2, "method": "string", **params},
                                     json={"Name": stack_name, "StackFileContent": compose, "Env": variables})
                verb = T("installiert", "installed")
            own = [c for c in await pc.containers(target) if _stack_of(c).lower() == stack_name]
            state = ", ".join(f"{_name(c)}: {c.get('State')}" for c in own) or T("noch keine Container",
                                                                                  "no containers yet")
            return T(f"Stack {stack_name} {verb} – {state}.", f"Stack {stack_name} {verb} – {state}.")
    return await _guard(go())


@tool(f"Startet, stoppt oder entfernt einen ganzen Portainer-Stack: action = {', '.join(STACK_ACTIONS)}.",
      risk=_confirm, enabled=_enabled)
async def portainer_stack_action(
    ctx: ToolContext,
    stack: str,
    action: Annotated[str, "start | stop | remove"],
    environment: Annotated[str, "Umgebung (Name/ID)"] = "",
) -> str:
    async def go() -> str:
        act = action.strip().lower()
        if act not in STACK_ACTIONS:
            raise PortainerError(T("Unbekannte Aktion – erlaubt: ", "Unknown action – allowed: ") + ", ".join(STACK_ACTIONS))
        async with PortainerClient(ctx.cfg) as pc:
            env = await pc.environment(environment)
            s = await pc.find_stack(env, stack)
            if not s:
                raise PortainerError(T(f"Kein Stack „{stack}“.", f"No stack \"{stack}\"."))
            params = {"endpointId": env["Id"]}
            if act == "remove":
                await pc.request("DELETE", f"/stacks/{s['Id']}", params=params, raw=True)
                return T(f"Stack {s['Name']} entfernt (benannte Volumes bleiben).",
                         f"Stack {s['Name']} removed (named volumes are kept).")
            await pc.request("POST", f"/stacks/{s['Id']}/{act}", params=params, raw=True)
            return f"Stack {s['Name']}: {act} ✔"
    return await _guard(go())


async def portainer_status(cfg) -> dict:
    """Für orbwise doctor."""
    if not cfg.portainer.enabled:
        return {"enabled": False, "online": False}
    try:
        async with PortainerClient(cfg) as pc:
            status = await pc.request("GET", "/status") or {}
            envs = await pc.environments()
        return {"enabled": True, "online": True, "version": status.get("Version", "?"), "url": pc.url,
                "environments": [f"{e.get('Name')} ({'online' if e.get('Status') == 1 else 'offline'})" for e in envs]}
    except PortainerError as e:
        return {"enabled": True, "online": False, "error": str(e)}

