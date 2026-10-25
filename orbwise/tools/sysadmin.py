"""System & Netzwerk: Prozesse, systemd-Dienste, Logs, Netzwerk, Ports, Speicherplatz und Aufräumen."""

from __future__ import annotations

import asyncio
import os
import re
import shutil
from pathlib import Path
from typing import Annotated

from ..lang import T
from . import proc
from .packages import package_manager, privileged
from .registry import BLOCKED, CONFIRM, SAFE, ToolContext, tool

NAME_RE = re.compile(r"^[\w@.:+-]{1,128}$")
HOST_RE = re.compile(r"^[A-Za-z0-9.:_-]{1,253}$")
# Dienste, deren Stopp die laufende Sitzung (und damit Orbwise) abschießt
CRITICAL_SERVICES = {"dbus", "dbus-broker", "systemd-logind", "display-manager", "sddm", "gdm", "gdm3",
                     "lightdm", "systemd-journald", "polkit", "NetworkManager"}
SERVICE_ACTIONS = ("start", "stop", "restart", "reload", "enable", "disable")


def _unit(name: str) -> str:
    return name.strip().removesuffix(".service")


# ---------------------------------------------------------------- Prozesse

@tool("Prozesse mit der höchsten CPU- oder RAM-Last.")
async def top_processes(
    ctx: ToolContext,
    sort: Annotated[str, "cpu | mem"] = "cpu",
    filter: Annotated[str, "Teil von Name/Befehl"] = "",
    limit: Annotated[int, "Standard 10"] = 10,
) -> str:
    key = "-pmem" if sort.strip().lower().startswith(("mem", "ram", "speicher")) else "-pcpu"
    rc, out = await proc.run(ctx, ["ps", "-eo", "pid,user,pcpu,pmem,rss,etime,comm,args", f"--sort={key}"],
                             timeout=15, stream=False)
    if rc != 0:
        return proc.format_result(rc, out, 2000)
    lines = out.splitlines()
    rows = lines[1:]
    if filter.strip():
        f = filter.strip().lower()
        rows = [r for r in rows if f in r.lower()]
    rows = rows[: max(1, min(int(limit), 50))]
    if not rows:
        return "Keine passenden Prozesse gefunden."
    out_lines = ["PID      USER       CPU%  MEM%  RSS      LAUFZEIT     BEFEHL"]
    for r in rows:
        parts = r.split(None, 7)
        if len(parts) < 8:
            continue
        pid, user, cpu, mem, rss, etime, comm, args = parts
        mb = f"{int(rss) / 1024:.0f} MB" if rss.isdigit() else rss
        out_lines.append(f"{pid:<8} {user[:10]:<10} {cpu:>5} {mem:>5}  {mb:<8} {etime:<12} {args[:90]}")
    return "\n".join(out_lines)


def _kill_risk(ctx: ToolContext, args: dict) -> tuple[str, str]:
    target = str(args.get("target", "")).strip()
    if target.isdigit() and int(target) in (0, 1, os.getpid(), os.getppid()):
        return BLOCKED, T("Systemprozess bzw. Orbwise selbst", "system process or Orbwise itself")
    if target.lower() in ("orbwise", "jarvis", "systemd", "init", "kwin_wayland", "gnome-shell", "plasmashell", "xorg", "xwayland"):
        return BLOCKED, T("würde die Sitzung oder Orbwise beenden", "would end the session or Orbwise")
    force = " (erzwungen, SIGKILL)" if args.get("force") else ""
    return CONFIRM, T("Prozess beenden: ", "end process: ") + f"{target}{force}"


@tool("Beendet einen Prozess per PID oder exaktem Namen (erst sanft).", risk=_kill_risk)
async def kill_process(
    ctx: ToolContext,
    target: Annotated[str, "PID oder exakter Name"],
    force: Annotated[bool, "true = SIGKILL"] = False,
) -> str:
    target = target.strip()
    sig = "-KILL" if force else "-TERM"
    if target.isdigit():
        pids = [target]
    else:
        if not NAME_RE.match(target):
            return "Ungültiger Prozessname."
        _, out = await proc.run(ctx, ["pgrep", "-x", target], timeout=10, stream=False)
        pids = [p for p in out.split() if p.isdigit()]
        if not pids:
            return f"Kein laufender Prozess namens '{target}'."
    pids = [p for p in pids if int(p) not in (0, 1, os.getpid(), os.getppid())]
    if not pids:
        return "Diesen Prozess beende ich nicht."
    rc, out = await proc.run(ctx, ["kill", sig, *pids], timeout=10, stream=False)
    if rc != 0:
        extra = " (gehört einem anderen Benutzer – dafür bräuchte es Root-Rechte)" if "not permitted" in out else ""
        return proc.format_result(rc, out, 1000) + extra
    return f"Signal {sig[1:]} an PID {', '.join(pids)} gesendet."


# ---------------------------------------------------------------- Dienste

@tool("Status eines systemd-Dienstes; ohne Namen alle fehlgeschlagenen.")
async def service_status(
    ctx: ToolContext,
    name: Annotated[str, "z. B. 'sshd'"] = "",
    user: Annotated[bool, "true = Benutzerdienst"] = False,
) -> str:
    scope = ["--user"] if user else []
    if not name.strip():
        rc, out = await proc.run(ctx, ["systemctl", *scope, "--failed", "--no-pager", "--no-legend"], timeout=15,
                                 stream=False)
        return out.strip() or "Keine fehlgeschlagenen Dienste."
    unit = _unit(name)
    if not NAME_RE.match(unit):
        return "Ungültiger Dienstname."
    rc, out = await proc.run(ctx, ["systemctl", *scope, "status", "--no-pager", "-n", "8", unit], timeout=15,
                             stream=False)
    if rc == 4 or "could not be found" in out:
        return f"Dienst '{unit}' nicht gefunden." + ("" if user else " (Benutzerdienst? dann user=true)")
    return proc.clip(out.strip(), ctx.limit())


def _service_risk(ctx: ToolContext, args: dict) -> tuple[str, str]:
    unit = _unit(str(args.get("name", "")))
    action = str(args.get("action", "")).lower()
    if action not in SERVICE_ACTIONS or not NAME_RE.match(unit or "-"):
        return SAFE, T("ungültige Angaben werden abgelehnt", "invalid input is rejected")
    if unit in CRITICAL_SERVICES and action in ("stop", "restart", "disable"):
        return BLOCKED, T(f"'{unit}' zu {action}en würde die Sitzung (und Orbwise) beenden",
                          f"{action} '{unit}' would end the session (and Orbwise)")
    return CONFIRM, T(f"Dienst {unit}: {action}", f"service {unit}: {action}") + (
        T(" (Benutzerdienst)", " (user service)") if args.get("user") else T(" (Root-Rechte)", " (root privileges)"))


@tool("Steuert einen systemd-Dienst.", risk=_service_risk)
async def service_control(
    ctx: ToolContext,
    name: str,
    action: Annotated[str, "start | stop | restart | reload | enable | disable"],
    user: Annotated[bool, "true = Benutzerdienst"] = False,
) -> str:
    unit, action = _unit(name), action.strip().lower()
    if action not in SERVICE_ACTIONS:
        return f"Unbekannte Aktion '{action}'. Möglich: {', '.join(SERVICE_ACTIONS)}."
    if not NAME_RE.match(unit):
        return "Ungültiger Dienstname."
    argv = ["systemctl", "--user", action, unit] if user else privileged(ctx, ["systemctl", action, unit])
    rc, out = await proc.run(ctx, argv, timeout=90, stream=False)
    if rc != 0:
        return proc.format_result(rc, out, 2000)
    _, state = await proc.run(ctx, ["systemctl", *(["--user"] if user else []), "is-active", unit], timeout=10,
                              stream=False)
    return f"Erledigt: {unit} {action} – Zustand jetzt: {state.strip() or 'unbekannt'}."


@tool("Letzte Log-Zeilen eines Dienstes (journalctl).")
async def service_logs(
    ctx: ToolContext,
    name: Annotated[str, "Leer = ganzes Journal"] = "",
    lines: Annotated[int, "Standard 40"] = 40,
    errors_only: bool = False,
    since: Annotated[str, "z. B. 'today', '1 hour ago'"] = "",
    user: Annotated[bool, "true = Benutzerdienst"] = False,
) -> str:
    argv = ["journalctl", "--no-pager", "-o", "short-iso", "-n", str(max(1, min(int(lines), 400)))]
    if user:
        argv.append("--user")
    if name.strip():
        unit = _unit(name)
        if not NAME_RE.match(unit):
            return "Ungültiger Dienstname."
        argv += ["-u", unit]
    if errors_only:
        argv += ["-p", "warning"]
    if since.strip():
        if not re.fullmatch(r"[\w :.-]{1,40}", since.strip()):
            return "Ungültige Zeitangabe."
        argv += ["--since", since.strip()]
    rc, out = await proc.run(ctx, argv, timeout=30, stream=False)
    text = out.strip()
    if not text or "-- No entries --" in text[-40:]:
        return "Keine Log-Einträge gefunden (für System-Logs muss der Benutzer ggf. in der Gruppe 'systemd-journal' " \
               "oder 'wheel'/'adm' sein)."
    return proc.clip_saved(text, ctx.limit(), "logs")


# ---------------------------------------------------------------- Netzwerk

@tool("IP-Adressen, Gateway, DNS, WLAN und optional die öffentliche IP.")
async def network_info(ctx: ToolContext, public_ip: bool = False) -> str:
    parts = []
    _, addrs = await proc.run(ctx, ["ip", "-brief", "address"], timeout=10, stream=False)
    parts.append("Adressen:\n" + "\n".join(ln for ln in addrs.splitlines() if not ln.startswith("lo ")))
    _, route = await proc.run(ctx, ["ip", "route", "show", "default"], timeout=10, stream=False)
    parts.append("Standard-Route: " + (route.strip() or "keine"))
    dns = []
    try:
        dns = [ln.split()[1] for ln in Path("/etc/resolv.conf").read_text().splitlines()
               if ln.startswith("nameserver") and len(ln.split()) > 1]
    except OSError:
        pass
    parts.append("DNS: " + (", ".join(dns) or "unbekannt"))
    if shutil.which("nmcli"):
        _, wifi = await proc.run(ctx, ["nmcli", "-t", "-f", "active,ssid,signal,freq", "dev", "wifi"], timeout=15,
                                 stream=False)
        active = [ln for ln in wifi.splitlines() if ln.startswith(("yes:", "ja:"))]
        if active:
            _, ssid, signal, freq = (active[0].split(":") + ["", "", ""])[:4]
            parts.append(f"WLAN: {ssid} (Signal {signal} %, {freq})")
    if public_ip:
        import httpx
        try:
            async with httpx.AsyncClient(timeout=8) as client:
                r = await client.get("https://api.ipify.org")
            parts.append("Öffentliche IP: " + r.text.strip())
        except httpx.HTTPError as e:
            parts.append(f"Öffentliche IP: nicht ermittelbar ({type(e).__name__})")
    return "\n".join(parts)


@tool("Pingt einen Host.")
async def ping_host(
    ctx: ToolContext,
    host: Annotated[str, "Hostname oder IP"],
    count: Annotated[int, "Standard 4"] = 4,
) -> str:
    host = host.strip()
    if not HOST_RE.match(host) or host.startswith("-"):
        return "Ungültiger Hostname."
    rc, out = await proc.run(ctx, ["ping", "-c", str(max(1, min(int(count), 10))), "-W", "2", host], timeout=30,
                             stream=False)
    lines = out.strip().splitlines()
    summary = "\n".join(lines[-3:]) if lines else "(keine Ausgabe)"
    return ("Erreichbar.\n" if rc == 0 else "Nicht erreichbar.\n") + summary


@tool("Offene (lauschende) TCP/UDP-Ports mit Programm.")
async def open_ports(ctx: ToolContext) -> str:
    rc, out = await proc.run(ctx, ["ss", "-tulpnH"], timeout=15, stream=False)
    if rc != 0:
        return proc.format_result(rc, out, 2000)
    rows = []
    for ln in out.splitlines():
        cols = ln.split()
        if len(cols) < 5:
            continue
        prog = re.search(r'users:\(\("([^"]+)"', ln)
        rows.append(f"{cols[0]:<4} {cols[4]:<28} {prog.group(1) if prog else '(anderer Benutzer)'}")
    return "PROTO LOKALE ADRESSE                PROGRAMM\n" + "\n".join(sorted(set(rows))) if rows else "Keine offenen Ports."


@tool("Prüft, ob ein TCP-Port auf einem Host erreichbar ist.")
async def check_port(
    ctx: ToolContext,
    host: str,
    port: int,
) -> str:
    host = host.strip()
    if not HOST_RE.match(host) or not 0 < int(port) < 65536:
        return "Ungültiger Host oder Port."
    try:
        _, writer = await asyncio.wait_for(asyncio.open_connection(host, int(port)), timeout=4)
        writer.close()
        return f"{host}:{port} ist erreichbar (TCP-Verbindung aufgebaut)."
    except (OSError, asyncio.TimeoutError) as e:
        reason = "Zeitüberschreitung" if isinstance(e, asyncio.TimeoutError) else (e.strerror or type(e).__name__)
        return f"{host}:{port} ist nicht erreichbar ({reason})."


# ---------------------------------------------------------------- Speicherplatz

@tool("Freier Platz aller Laufwerke, optional die größten Ordner unter path.")
async def disk_usage(
    ctx: ToolContext,
    path: Annotated[str, "z. B. '~/Downloads'"] = "",
    top: Annotated[int, "Standard 10"] = 10,
) -> str:
    _, df = await proc.run(ctx, ["df", "-h", "--output=target,size,used,avail,pcent", "-x", "tmpfs", "-x", "devtmpfs",
                                 "-x", "squashfs", "-x", "overlay", "-x", "efivarfs"], timeout=20, stream=False)
    result = "Laufwerke:\n" + df.strip()
    if path.strip():
        target = Path(os.path.expanduser(path.strip()))
        if not target.is_dir():
            return result + f"\n\n'{path}' ist kein Ordner."
        _, du = await proc.run(ctx, ["du", "-xh", "--max-depth=1", str(target)], timeout=120, stream=False)

        def size(line: str) -> float:
            num = line.split("\t")[0]
            units = {"K": 1, "M": 1e3, "G": 1e6, "T": 1e9}
            try:
                return float(num[:-1].replace(",", ".")) * units.get(num[-1], 0.001)
            except ValueError:
                return 0.0

        rows = sorted((ln for ln in du.splitlines() if "\t" in ln and not ln.startswith("du:")), key=size, reverse=True)
        result += f"\n\nGrößte Ordner in {target}:\n" + "\n".join(rows[1: max(2, min(int(top), 30) + 1)])
    return result


def _cleanup_risk(ctx: ToolContext, args: dict) -> tuple[str, str]:
    return (CONFIRM, T("Paket-Cache, verwaiste Pakete und alte Journal-Logs löschen",
                       "delete package cache, orphaned packages and old journal logs")) if args.get("apply") \
        else (SAFE, "nur Analyse")


@tool("Findet Platz zum Aufräumen (Paket-Cache, verwaiste Pakete, Journal). Erst ohne apply zeigen, dann "
      "apply=true.", risk=_cleanup_risk)
async def cleanup_system(ctx: ToolContext, apply: bool = False) -> str:
    pm = package_manager(ctx)
    lines = []
    cache = "/var/cache/pacman/pkg" if pm == "pacman" else "/var/cache/apt/archives"
    _, du = await proc.run(ctx, ["du", "-sh", cache], timeout=60, stream=False)
    lines.append(f"Paket-Cache ({cache}): {du.split()[0] if du.split() else '?'}")
    if pm == "pacman":
        _, orphans = await proc.run(ctx, ["pacman", "-Qdtq"], timeout=30, stream=False)
        orphan_list = [o for o in orphans.split() if re.match(r"^[a-z0-9@._+-]+$", o)]
        lines.append(f"Verwaiste Pakete: {len(orphan_list)}" + (f" ({', '.join(orphan_list[:15])})" if orphan_list else ""))
    else:
        _, sim = await proc.run(ctx, ["apt-get", "-s", "autoremove"], timeout=60, stream=False)
        orphan_list = re.findall(r"^Remv (\S+)", sim, re.M)
        lines.append(f"Nicht mehr benötigte Pakete: {len(orphan_list)}"
                     + (f" ({', '.join(orphan_list[:15])})" if orphan_list else ""))
    _, jdu = await proc.run(ctx, ["journalctl", "--disk-usage"], timeout=20, stream=False)
    lines.append("System-Journal: " + (jdu.strip().splitlines()[-1] if jdu.strip() else "?"))
    home_cache = Path.home() / ".cache"
    if home_cache.exists():
        _, hdu = await proc.run(ctx, ["du", "-sh", str(home_cache)], timeout=60, stream=False)
        lines.append(f"Eigener Cache (~/.cache, wird nicht automatisch gelöscht): {hdu.split()[0] if hdu.split() else '?'}")
    if not apply:
        return "\n".join(lines) + "\n\nZum Aufräumen erneut mit apply=true aufrufen."
    if pm == "pacman":
        steps = ["paccache -rk2" if shutil.which("paccache") else "pacman -Sc --noconfirm"]
        if orphan_list:
            steps.append("pacman -Rns --noconfirm " + " ".join(orphan_list))
    else:
        steps = ["DEBIAN_FRONTEND=noninteractive apt-get -y autoremove", "apt-get clean"]
    steps.append("journalctl --vacuum-time=2weeks")
    rc, out = await proc.run(ctx, privileged(ctx, ["sh", "-c", " && ".join(steps)]), timeout=600)
    return "Vorher:\n" + "\n".join(lines) + "\n\nAufräumen: " + proc.format_result(rc, out, 3000)
