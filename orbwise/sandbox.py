"""Coding-Sandbox: die Befehle des Agenten laufen in einem Container (Podman bevorzugt, sonst Docker) auf einer
Arbeitskopie des Projekts (workspace.py) – wie Claude im Web. Der Agent darf dort frei bauen, testen und
installieren; das System und der Projektordner bleiben unberührt.

- Ein Container je Coding-Chat (orbwise-<chat>), gestartet beim ersten Befehl, nach Leerlauf gestoppt.
- Die Arbeitskopie ist unter demselben Pfad eingebunden – Pfade in Ausgaben stimmen mit den Dateiwerkzeugen überein.
- Keine Zugangsdaten im Container (kein Token, kein ~/.ssh, kein Docker-Socket); Netz je Chat an oder aus.
- Rootless Podman: root im Container ist der Nutzer auf dem Host (apt geht, Dateien gehören dem Nutzer).
  Docker: läuft als der Nutzer (--user), Pakete per apt gehen dort nicht.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import shlex
import shutil
import subprocess
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

DEFAULT_IMAGE = "orbwise-sandbox:1"
CONTAINERFILE = Path(__file__).parent / "sandbox_image" / "Containerfile"
CAPS = ("CHOWN", "DAC_OVERRIDE", "FOWNER", "SETUID", "SETGID", "KILL")
_engine_cache: dict[str, tuple[float, str | None, str]] = {}
last_used: dict[str, float] = {}


class SandboxError(RuntimeError):
    pass


# ---------------------------------------------------------------- Engine
def _info_ok(binary: str) -> tuple[bool, str]:
    try:
        r = subprocess.run([binary, "info", "--format", "{{.Host.Arch}}" if "podman" in binary else "{{.OSType}}"],
                           capture_output=True, text=True, timeout=60)
    except subprocess.TimeoutExpired:
        return False, "timeout"
    except (OSError, subprocess.SubprocessError) as e:
        return False, str(e)
    return r.returncode == 0, (r.stderr or r.stdout).strip()[-300:]


def _runtime_dir() -> None:
    """Als Dienst gestartet fehlt oft XDG_RUNTIME_DIR – rootless Podman braucht es (für alle späteren Aufrufe)."""
    run_dir = f"/run/user/{os.getuid()}" if hasattr(os, "getuid") else ""
    if not os.environ.get("XDG_RUNTIME_DIR") and run_dir and os.path.isdir(run_dir):
        os.environ["XDG_RUNTIME_DIR"] = run_dir


def _podman_hint(msg: str) -> str:
    """Konkreter Rat zu häufigen Podman-Fehlern."""
    low = msg.lower()
    if msg == "timeout":
        return "Podman antwortet nicht (der erste Start dauert manchmal) – gleich nochmal versuchen."
    if "subuid" in low or "subgid" in low or "newuidmap" in low or "uid mapping" in low:
        return ("Für deinen Benutzer fehlen UID-Bereiche: sudo usermod --add-subuids 100000-165535 "
                "--add-subgids 100000-165535 $USER && podman system migrate")
    if any(w in low for w in ("overlay", "graph driver", "mount_program", "storage driver")):
        return ("Podman-Speicher (z. B. auf btrfs): fuse-overlayfs installieren, in ~/.config/containers/storage.conf "
                '[storage] driver = "overlay" und [storage.options.overlay] mount_program = "/usr/bin/fuse-overlayfs" '
                "setzen, dann podman system reset.")
    if "xdg_runtime_dir" in low or "/run/user" in low:
        return "XDG_RUNTIME_DIR fehlt – Orbwise in der Desktop-Sitzung starten oder loginctl enable-linger $USER."
    return "Im Terminal `podman info` ausführen, um den Grund zu sehen."


def engine(cfg: Any) -> tuple[str | None, str]:
    """(Programm, Fehlertext). auto: Podman vor Docker. Treffer eine Minute, Fehlschläge 15 s zwischengespeichert.
    Scheitern beide, steht Podmans Grund vorn (Docker ist nur der Rückfall)."""
    want = getattr(getattr(cfg, "sandbox", None), "engine", "auto")
    hit = _engine_cache.get(want)
    if hit and time.monotonic() - hit[0] < (60 if hit[1] else 15):
        return hit[1], hit[2]
    _runtime_dir()
    names = ["podman", "docker"] if want == "auto" else [want]
    found, errors = None, []
    for name in names:
        binary = shutil.which(name)
        if not binary:
            continue
        ok, msg = _info_ok(binary)
        if ok:
            found = binary
            break
        if name == "podman":
            errors.append(f"podman: {'keine Antwort' if msg == 'timeout' else msg} – {_podman_hint(msg)}")
        else:
            errors.append(f"{name}: {msg}")
    error = "" if found else (" · ".join(errors) or
                              "Weder Podman noch Docker ist installiert (z. B. sudo apt install podman).")
    _engine_cache[want] = (time.monotonic(), found, error)
    return found, error


def is_podman(binary: str) -> bool:
    return "podman" in os.path.basename(binary)


def image(cfg: Any) -> str:
    return str(getattr(getattr(cfg, "sandbox", None), "image", "") or "").strip() or DEFAULT_IMAGE


def image_ready(binary: str, name: str) -> bool:
    r = subprocess.run([binary, "image", "inspect", name], capture_output=True, timeout=30)
    return r.returncode == 0


async def build_image(binary: str, progress: Callable[[str], Awaitable[None]] | None = None) -> None:
    """orbwise-sandbox:1 aus sandbox_image/Containerfile bauen (einmalig, einige Minuten)."""
    proc = await asyncio.create_subprocess_exec(
        binary, "build", "-t", DEFAULT_IMAGE, "-f", str(CONTAINERFILE), str(CONTAINERFILE.parent),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    tail: list[str] = []
    assert proc.stdout
    while True:
        line = await proc.stdout.readline()
        if not line:
            break
        text = line.decode(errors="replace").rstrip()
        tail = (tail + [text])[-20:]
        if progress and text:
            await progress(text[:200])
    if await proc.wait() != 0:
        raise SandboxError("Image-Bau fehlgeschlagen:\n" + "\n".join(tail))


# ---------------------------------------------------------------- Container
def container_name(chat_id: str) -> str:
    return "orbwise-" + re.sub(r"[^a-zA-Z0-9_.-]", "", chat_id)[:40]


def run_argv(binary: str, cfg: Any, name: str, ws: str, network: bool, img: str) -> list[str]:
    sb = getattr(cfg, "sandbox", None)
    argv = [binary, "run", "-d", "--name", name, "--label", "orbwise=sandbox", "--hostname", "sandbox",
            "-v", f"{ws}:{ws}:Z", "-w", ws,
            "--memory", str(getattr(sb, "memory", "4g")), "--cpus", str(getattr(sb, "cpus", 2.0)),
            "--pids-limit", "512", "--cap-drop", "ALL"]
    for cap in CAPS:
        argv += ["--cap-add", cap]
    argv += ["--security-opt", "no-new-privileges"]
    if not network:
        argv += ["--network", "none"]
    if not is_podman(binary):
        # Docker-Daemon läuft als root: im Container als der Nutzer arbeiten, sonst gehörten neue Dateien root
        argv += ["--user", f"{os.getuid()}:{os.getgid()}", "-e", "HOME=/tmp"]
    argv += ["-e", "TERM=dumb", "-e", "PAGER=cat", "-e", "GIT_PAGER=cat", "-e", "NO_COLOR=1", img,
             "sleep", "infinity"]
    return argv


def exec_argv(binary: str, name: str, workdir: str, command: str, timeout: float | None = None) -> list[str]:
    """Befehl im Container; mit timeout bricht `timeout` ihn auch drinnen ab (nicht nur den exec-Client)."""
    inner = command if not timeout else f"timeout -k 5 {int(timeout)} bash -lc {shlex.quote(command)}"
    return [binary, "exec", "-w", workdir, name, "bash", "-lc", inner]


def job_argv(binary: str, name: str, workdir: str, command: str, jid: str) -> tuple[list[str], list[str]]:
    """Hintergrund-Job im Container: (Start, Stopp). Die Prozesse tragen ORBWISE_JOB=<id> in der Umgebung – so
    lassen sie sich samt Kindern beenden, auch wenn der exec-Client schon weg ist."""
    start = [binary, "exec", "-w", workdir, "-e", f"ORBWISE_JOB={jid}", name, "bash", "-lc", command]
    kill = ("for p in /proc/[0-9]*; do grep -qz 'ORBWISE_JOB=%s' $p/environ 2>/dev/null && "
            "kill -TERM ${p#/proc/} 2>/dev/null; done; true" % jid)
    return start, [binary, "exec", name, "sh", "-c", kill]


def strip_sudo(command: str) -> str:
    """Im Container ist man root (Podman) – sudo gibt es dort nicht."""
    return re.sub(r"(^|[;&|]\s*|\$\(\s*)sudo\s+(-\S+\s+)*", r"\1", command)


def state_of(binary: str, name: str) -> str:
    """running | exited | created | missing"""
    r = subprocess.run([binary, "inspect", "-f", "{{.State.Status}}", name], capture_output=True, text=True,
                       timeout=20)
    return r.stdout.strip() if r.returncode == 0 else "missing"


def _run(argv: list[str], timeout: float = 120) -> str:
    r = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    if r.returncode != 0:
        raise SandboxError((r.stderr or r.stdout).strip()[-500:])
    return r.stdout


async def ensure_running(cfg: Any, sb: dict, progress: Callable[[str], Awaitable[None]] | None = None) -> str:
    """Container des Chats starten (bei Bedarf Image bauen und Container anlegen). Liefert das Engine-Programm."""
    binary, error = engine(cfg)
    if not binary:
        raise SandboxError(error)
    name = sb["name"]
    status = await asyncio.to_thread(state_of, binary, name)
    if status != "running":
        if status == "missing":
            img = sb.get("image") or image(cfg)
            if not await asyncio.to_thread(image_ready, binary, img):
                if img != DEFAULT_IMAGE:
                    if progress:
                        await progress(f"Lade Image {img} …")
                    await asyncio.to_thread(_run, [binary, "pull", img], 1800)
                else:
                    if progress:
                        await progress("Baue das Sandbox-Image (einmalig, einige Minuten) …")
                    await build_image(binary, progress)
            await asyncio.to_thread(_run, run_argv(binary, cfg, name, sb["path"], sb.get("network", True), img))
        else:
            await asyncio.to_thread(_run, [binary, "start", name])
    last_used[name] = time.monotonic()
    return binary


def stop(cfg: Any, name: str) -> None:
    binary, _ = engine(cfg)
    if binary:
        subprocess.run([binary, "stop", "-t", "3", name], capture_output=True, timeout=60)
    last_used.pop(name, None)


def remove(cfg: Any, name: str) -> None:
    binary, _ = engine(cfg)
    if binary:
        subprocess.run([binary, "rm", "-f", name], capture_output=True, timeout=60)
    last_used.pop(name, None)


def idle(cfg: Any) -> list[str]:
    """Container, die länger als sandbox.idle_minutes keinen Befehl hatten."""
    limit = 60 * max(1, int(getattr(getattr(cfg, "sandbox", None), "idle_minutes", 30)))
    now = time.monotonic()
    return [name for name, t in last_used.items() if now - t > limit]


def status(cfg: Any, sb: dict | None) -> dict:
    binary, error = engine(cfg)
    out = {"engine": os.path.basename(binary) if binary else None, "error": "" if binary else error,
           "image": (sb or {}).get("image") or image(cfg)}
    if binary and sb and sb.get("name"):
        out["container"] = state_of(binary, sb["name"])
    return out
