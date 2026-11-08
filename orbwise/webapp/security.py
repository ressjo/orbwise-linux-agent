"""Wer darf den Server erreichen? Nur localhost, Warnung beim Lauschen im Netz."""

from __future__ import annotations

from ..config import Config
from ..lang import T


def is_loopback(host: str) -> bool:
    import ipaddress
    if host in ("localhost", "ip6-localhost"):
        return True
    try:
        return ipaddress.ip_address(host.strip("[]")).is_loopback
    except ValueError:
        return False


def remote_bind_warning(cfg: Config) -> str:
    """Leer bei 127.0.0.1. Sonst ist das Dashboard – ohne Anmeldung, mit Befehlsausführung – im Netz erreichbar;
    die Host-Prüfung hält Browser-Angriffe ab, aber kein Gerät im LAN, das den Host-Header selbst setzt."""
    if is_loopback(cfg.host):
        return ""
    return T(f"host: {cfg.host} macht das Dashboard ohne Anmeldung im Netzwerk erreichbar – jedes Gerät dort könnte "
             "Befehle auf diesem PC auslösen. Für unterwegs lieber Telegram, VPN (z. B. WireGuard/Tailscale) oder "
             "einen SSH-Tunnel (ssh -L 8765:localhost:8765 pc) nutzen.",
             f"host: {cfg.host} makes the dashboard reachable on the network without a login – any device there could "
             "run commands on this PC. For remote use prefer Telegram, a VPN (e.g. WireGuard/Tailscale) or an SSH "
             "tunnel (ssh -L 8765:localhost:8765 pc).")


def check_host(host: str | None, port: int) -> bool:
    if not host:
        return False
    name = host.rsplit(":", 1)[0] if not host.startswith("[") else host.split("]")[0] + "]"
    return name in ("localhost", "127.0.0.1", "[::1]")
