"""Befehle im Hintergrund (shell mit background=true): Dev-Server, lange Builds, Testläufe.

Wie bei OpenCode kehrt der Aufruf sofort zurück; ist ein Job fertig, merkt sich Orbwise eine kurze Notiz (Befehl,
Exit-Code, letzte Zeilen). Der Agent hängt sie an das nächste Werkzeug-Ergebnis bzw. an die nächste Frage – das
Modell muss nicht nachfragen. Die Ausgabe liegt vollständig in ~/.cache/orbwise/jobs/.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import subprocess
import time
import uuid
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from .proc import QUIET_ENV

TAIL_LINES = 40
Broadcast = Callable[[dict], Awaitable[None]]


@dataclass
class Job:
    id: str
    command: str
    cwd: str
    log: str
    started: float = field(default_factory=time.time)
    status: str = "running"  # running | exit | stopped
    rc: int | None = None
    ended: float | None = None
    tail: deque = field(default_factory=lambda: deque(maxlen=TAIL_LINES))
    proc: asyncio.subprocess.Process | None = None
    task: asyncio.Task | None = None

    def info(self) -> dict:
        return {"id": self.id, "command": self.command, "cwd": self.cwd, "status": self.status, "rc": self.rc,
                "started": self.started, "ended": self.ended, "log": self.log}

    def summary(self, lines: int = 15) -> str:
        if self.status == "running":
            state = f"läuft seit {int(time.time() - self.started)} s"
        elif self.status == "stopped":
            state = "beendet (gestoppt)"
        else:
            state = f"fertig, Exit-Code {self.rc}"
        tail = "".join(list(self.tail)[-lines:]).rstrip() or "(noch keine Ausgabe)"
        return f"Job {self.id} – {self.command}\nStatus: {state}\nLetzte Ausgabe:\n{tail}\n(Vollständig: {self.log})"


JOBS: dict[str, Job] = {}
_notes: list[str] = []
_broadcast: Broadcast | None = None


def set_broadcast(fn: Broadcast | None) -> None:
    """Server: Job-Ereignisse an alle Fenster schicken (auch nach dem Ende der Antwort, die den Job gestartet hat)."""
    global _broadcast
    _broadcast = fn


def jobs_dir() -> str:
    base = os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache")
    path = os.path.join(base, "orbwise", "jobs")
    os.makedirs(path, mode=0o700, exist_ok=True)
    return path


def running() -> list[Job]:
    return [j for j in JOBS.values() if j.status == "running"]


async def _send(event: dict, emit: Broadcast | None) -> None:
    target = _broadcast or emit
    if target:
        with contextlib.suppress(Exception):
            await target(event)


async def start(command: str, cwd: str, limit: int = 4, emit: Broadcast | None = None) -> Job:
    """Startet den Befehl in einer eigenen Prozessgruppe. Mehr als limit laufende Jobs → RuntimeError."""
    if len(running()) >= limit:
        raise RuntimeError(f"Es laufen schon {limit} Hintergrund-Jobs – erst einen mit job_output(id, stop=true) "
                           "beenden.")
    jid = uuid.uuid4().hex[:6]
    log = os.path.join(jobs_dir(), f"{jid}.log")
    job = Job(id=jid, command=command, cwd=cwd, log=log)
    job.proc = await asyncio.create_subprocess_shell(
        command, executable="/bin/bash", stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT, cwd=cwd, env={**os.environ, **QUIET_ENV}, start_new_session=True)
    JOBS[jid] = job
    job.task = asyncio.create_task(_pump(job, emit))
    await _send({"type": "job", "phase": "start", **job.info()}, emit)
    _prune()
    return job


async def _pump(job: Job, emit: Broadcast | None) -> None:
    assert job.proc and job.proc.stdout
    last = 0.0
    buf: list[str] = []
    with open(job.log, "w", encoding="utf-8") as f:
        while True:
            line = await job.proc.stdout.readline()
            if not line:
                break
            text = line.decode(errors="replace")
            f.write(text)
            f.flush()
            job.tail.append(text)
            buf.append(text)
            if time.monotonic() - last > 0.5:  # gedrosselt an die Oberfläche
                await _send({"type": "job", "phase": "output", "id": job.id, "text": "".join(buf)[-4000:]}, emit)
                buf, last = [], time.monotonic()
    rc = await job.proc.wait()
    if buf:
        await _send({"type": "job", "phase": "output", "id": job.id, "text": "".join(buf)[-4000:]}, emit)
    if job.status == "running":
        job.status, job.rc = "exit", rc
    job.ended = time.time()
    _notes.append(f"Hintergrund-Job {job.id} ist {'gestoppt' if job.status == 'stopped' else 'fertig'}:\n"
                  + job.summary(10))
    await _send({"type": "job", "phase": "done", **job.info()}, emit)


def stop(jid: str) -> Job | None:
    job = JOBS.get(jid)
    if job and job.status == "running" and job.proc:
        job.status = "stopped"
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(job.proc.pid, signal.SIGTERM)
    return job


def stop_all() -> None:
    for job in running():
        stop(job.id)


def take_notes() -> str:
    """Notizen fertiger Jobs, die das Modell noch nicht gesehen hat (danach geleert)."""
    if not _notes:
        return ""
    text = "\n\n".join(_notes)
    _notes.clear()
    return text


def _prune(keep: int = 30) -> None:
    done = [j for j in JOBS.values() if j.status != "running"]
    for job in sorted(done, key=lambda j: j.started)[:-keep] if len(done) > keep else []:
        JOBS.pop(job.id, None)
