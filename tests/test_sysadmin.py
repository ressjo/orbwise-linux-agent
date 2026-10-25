import os

import pytest
from conftest import run

from orbwise.tools import sysadmin as sa
from orbwise.tools.registry import BLOCKED, CONFIRM, SAFE, ToolContext, get_tool, load_all_tools


@pytest.fixture
def calls(monkeypatch):
    seen = []
    replies = {}

    async def fake_run(ctx, cmd, timeout, stream=True, cwd=None):
        seen.append(cmd)
        key = " ".join(cmd)
        for prefix, reply in replies.items():
            if key.startswith(prefix):
                return reply
        return 0, ""

    monkeypatch.setattr(sa.proc, "run", fake_run)
    return seen, replies


def ctx(cfg):
    cfg.tools.package_manager = "pacman"
    return ToolContext(cfg=cfg, memory=None)


def test_registered_and_risks(cfg):
    load_all_tools()
    c = ctx(cfg)
    for name in ("top_processes", "service_status", "service_logs", "network_info", "ping_host", "open_ports",
                 "check_port", "disk_usage"):
        assert get_tool(name).assess(c, {"name": "x", "host": "h", "port": 1})[0] == SAFE
    assert get_tool("kill_process").assess(c, {"target": "firefox"})[0] == CONFIRM
    assert get_tool("kill_process").assess(c, {"target": "1"})[0] == BLOCKED
    assert get_tool("kill_process").assess(c, {"target": str(os.getpid())})[0] == BLOCKED
    assert get_tool("service_control").assess(c, {"name": "docker", "action": "restart"})[0] == CONFIRM
    assert get_tool("service_control").assess(c, {"name": "sddm.service", "action": "stop"})[0] == BLOCKED
    assert get_tool("service_control").assess(c, {"name": "sddm", "action": "start"})[0] == CONFIRM
    assert get_tool("cleanup_system").assess(c, {})[0] == SAFE
    assert get_tool("cleanup_system").assess(c, {"apply": True})[0] == CONFIRM


def test_top_processes_formats_and_filters(cfg, calls):
    _, replies = calls
    replies["ps"] = (0, "PID USER %CPU %MEM RSS ELAPSED COMMAND COMMAND\n"
                        "  42 alex 55.0 3.1 204800 01:02 firefox /usr/lib/firefox/firefox -contentproc\n"
                        "  77 alex  2.0 0.5 10240 10:00 kate kate notes.txt\n")
    out = run(sa.top_processes(ctx(cfg), "cpu", "fire"))
    assert "firefox" in out and "200 MB" in out and "kate" not in out


def test_kill_by_name_and_service_control(cfg, calls):
    seen, replies = calls
    replies["pgrep"] = (0, "4242\n4243\n")
    assert "PID 4242, 4243" in run(sa.kill_process(ctx(cfg), "firefox"))
    assert seen[-1] == ["kill", "-TERM", "4242", "4243"]
    run(sa.service_control(ctx(cfg), "docker.service", "restart"))
    assert ["sudo", "-A", "systemctl", "restart", "docker"] in seen
    run(sa.service_control(ctx(cfg), "syncthing", "start", user=True))
    assert ["systemctl", "--user", "start", "syncthing"] in seen
    assert "Ungültiger" in run(sa.service_control(ctx(cfg), "x; rm -rf /", "stop"))


def test_logs_ping_validation(cfg, calls):
    seen, replies = calls
    replies["journalctl"] = (0, "2026-09-28T10:00:00 host sshd[1]: Accepted key\n")
    out = run(sa.service_logs(ctx(cfg), "sshd", 20, errors_only=True, since="1 hour ago"))
    assert "Accepted" in out
    assert seen[-1][-6:] == ["-u", "sshd", "-p", "warning", "--since", "1 hour ago"]
    assert "Ungültiger" in run(sa.ping_host(ctx(cfg), "-f 8.8.8.8"))
    assert "Ungültige Zeitangabe" in run(sa.service_logs(ctx(cfg), "sshd", since="$(reboot)"))


def test_cleanup_pacman_apply(cfg, calls, monkeypatch):
    seen, replies = calls
    replies["du -sh /var/cache/pacman/pkg"] = (0, "3,2G\t/var/cache/pacman/pkg")
    replies["pacman -Qdtq"] = (0, "libfoo\nlibbar\n")
    replies["journalctl --disk-usage"] = (0, "Archived and active journals take up 1.2G in the file system.")
    monkeypatch.setattr(sa.shutil, "which", lambda n: "/usr/bin/paccache" if n == "paccache" else None)
    out = run(sa.cleanup_system(ctx(cfg)))
    assert "3,2G" in out and "Verwaiste Pakete: 2" in out and "1.2G" in out and "apply=true" in out
    run(sa.cleanup_system(ctx(cfg), apply=True))
    assert seen[-1] == ["sudo", "-A", "sh", "-c",
                        "paccache -rk2 && pacman -Rns --noconfirm libfoo libbar && journalctl --vacuum-time=2weeks"]


def test_check_port_real_socket(cfg):
    import socket
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen()
    port = srv.getsockname()[1]
    assert "ist erreichbar" in run(sa.check_port(ctx(cfg), "127.0.0.1", port))
    srv.close()
    assert "nicht erreichbar" in run(sa.check_port(ctx(cfg), "127.0.0.1", port))


def test_disable_tools_and_groups(cfg):
    from orbwise.tools.registry import tool_schemas
    load_all_tools()
    names = lambda: {s["function"]["name"] for s in tool_schemas(cfg)}  # noqa: E731
    assert {"top_processes", "ping_host", "web_search"} <= names()
    cfg.tools.disabled = ["sysadmin", "web_search"]
    assert not {"top_processes", "ping_host", "web_search"} & names()
    assert "run_shell" in names()


def test_small_context_sends_only_relevant_tool_groups(cfg, memory):
    import json

    from orbwise.agent import Agent
    from orbwise.config import ProfileConfig

    class Small:
        profile = ProfileConfig(backend="openai", base_url="http://x/v1", model="llama", num_ctx=8192)
        context_size = 8192
        sent = []

        async def chat_stream(self, messages, tools=None):
            self.sent.append({t["function"]["name"] for t in tools or []})
            yield {"type": "done", "message": {"role": "assistant", "content": "ok"}, "stats": {}}

    cfg.homeassistant.url, cfg.homeassistant.token = "http://ha", "t"
    cfg.paperless.url, cfg.paperless.token = "http://pl", "t"
    llm = Small()
    agent = Agent(cfg, llm, memory)

    async def noop(e):
        pass

    run(agent.run("Mach bitte das Licht im Wohnzimmer an", noop, noop))
    tools = llm.sent[-1]
    assert "ha_control" in tools and "run_shell" in tools and "remember" in tools
    assert "paperless_search" not in tools and "top_processes" not in tools
    run(agent.run("Wie wird das Wetter?", noop, noop))
    assert "weather" in llm.sent[-1] and "ha_control" in llm.sent[-1]  # vorherige Frage zählt mit, solange Platz ist
    run(agent.run("Warum ist mein Rechner so langsam?", noop, noop))
    assert "top_processes" in llm.sent[-1] and "ha_control" not in llm.sent[-1]  # Grenze: Älteres fällt weg
    assert agent.schema_tokens < agent.all_schema_tokens

    big = Agent(cfg, type("Big", (Small,), {"context_size": 65536})(), memory)
    big.choose_tools()
    everything = [s for s in big._mode_schemas()[0] if s["function"]["name"] != "load_tools"]  # nichts nachzuladen
    assert big.schemas == everything and json.dumps(big.schemas)
