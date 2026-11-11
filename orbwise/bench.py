"""Agent-Benchmark (`orbwise bench`): Löst der Coding-Agent mit dem geladenen Modell echte kleine Aufgaben – und wie
aufwendig? Die Tests des Projekts prüfen den Code mit einem Ersatz-Modell; ob ein bestimmtes Modell mit Systemprompt
und Werkzeugen wirklich zum Ziel kommt, zeigt erst ein Lauf wie dieser.

Je Aufgabe (bench_tasks.py): ein Wegwerf-Projekt in einem Temp-Ordner, ein frischer Chat im Coding-Modus, die Bitte,
danach eine objektive Prüfung. Gemessen werden gelöst/nicht gelöst, Schritte, Zeit, Token, fehlgeschlagene Edits,
abgebrochene Denkschleifen und was die Schleifenerkennung übersprungen hat.

Freigaben: Der Agent läuft mit Auto „Lesen + Dateien bearbeiten“. Was dort nachfragen würde, entscheidet statt eines
Menschen eine feste Regel: erlaubt ist, was auch der Käfig dieser Stufe zuließe (Befehle ohne Root, Löschen, Senden
ins Netz, Startdateien; Dateien nur im Projekt) – alles andere wird abgelehnt und gezählt. Ein Modell kann im Benchmark
also nichts tun, was es im Alltag auf dieser Stufe nicht ohne dich dürfte.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import statistics
import subprocess
import tempfile
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

from .bench_tasks import BY_NAME, CONFTEST, TASKS, Task

TIMEOUT = 900  # Sekunden je Aufgabe, danach zählt sie als nicht gelöst


def _prepare(task: Task, project: Path) -> None:
    for rel, content in {**task.files, "conftest.py": CONFTEST}.items():
        target = project / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    if shutil.which("git"):  # ein Repository wie im echten Projekt (Schnappschüsse, git status/diff für das Modell)
        env = {"GIT_AUTHOR_NAME": "bench", "GIT_AUTHOR_EMAIL": "bench@localhost", "GIT_COMMITTER_NAME": "bench",
               "GIT_COMMITTER_EMAIL": "bench@localhost", "PATH": __import__("os").environ.get("PATH", ""),
               "HOME": str(project.parent)}
        for argv in (["git", "init", "-q"], ["git", "add", "-A"], ["git", "commit", "-q", "-m", "Ausgangsstand"]):
            subprocess.run(argv, cwd=project, env=env, capture_output=True, timeout=30)


def verdict(task: Task, project: Path, answer: str) -> tuple[bool, str]:
    """Prüfung der Aufgabe. Geschützte Dateien (die Tests) gelten im Ausgangszustand; eine Aufgabe, die nichts ändern
    darf, ist mit geänderten Dateien nicht gelöst."""
    originals = {**task.files, "conftest.py": CONFTEST}
    if task.read_only:
        changed = [rel for rel, content in originals.items()
                   if not (project / rel).is_file() or (project / rel).read_text(encoding="utf-8") != content]
        if changed:
            return False, "hat Dateien geändert: " + ", ".join(changed[:3])
    for rel in (*task.protect, "conftest.py"):
        (project / rel).parent.mkdir(parents=True, exist_ok=True)
        (project / rel).write_text(originals[rel], encoding="utf-8")
    try:
        return task.check(project, answer or "")
    except Exception as e:  # noqa: BLE001 – eine Prüfung, die abstürzt, ist „nicht gelöst“ mit Grund
        return False, f"Prüfung fehlgeschlagen: {type(e).__name__}: {e}"


class Recorder:
    """Zählt aus den Ereignissen eines Laufs, was der Agent getan hat."""

    def __init__(self):
        self.steps = self.edit_fails = self.repeats = self.think_cuts = self.denied = self.errors = 0
        self.tools: dict[str, int] = {}
        self.calls: dict[str, str] = {}  # Aufruf-ID → Werkzeug
        self.checked = ""                # Zeile der Prüf-Schranke („✓ Geprüft …“)

    async def emit(self, ev: dict) -> None:
        kind = ev.get("type")
        if kind == "llm_phase":
            if ev.get("phase") == "done" and not ev.get("prewarm") and "tools_added" not in ev:
                self.steps += 1
            elif ev.get("phase") == "think_cut":
                self.think_cuts += 1
        elif kind == "tool_call" and not ev.get("sub"):
            self.calls[ev["id"]] = ev.get("name", "")
            self.tools[ev.get("name", "")] = self.tools.get(ev.get("name", ""), 0) + 1
        elif kind == "tool_result" and not ev.get("sub"):
            name, text = self.calls.get(ev.get("id"), ""), str(ev.get("text") or "")
            if name in ("edit", "write") and ev.get("status") == "ok" and not text.startswith(("Geändert", "Angelegt")):
                self.edit_fails += 1
        elif kind == "error":
            self.errors += 1
        elif kind == "token" and str(ev.get("text", "")).lstrip().startswith(("✓", "⚠", "○")):
            self.checked = str(ev["text"]).strip()[:120]


def contained(agent: Any, project: str) -> Callable:
    """Die Freigabe-Regel des Benchmarks (siehe oben): entscheidet Rückfragen ohne Menschen."""
    from .agent_safety import SafetyMixin

    async def confirm(call_id: str, name: str, args: dict, reason: str) -> bool:  # noqa: ARG001
        ok = name in ("shell", "edit", "write") and SafetyMixin._auto_ok(name, args, project, project)[0]
        if not ok:
            agent._bench_denied = getattr(agent, "_bench_denied", 0) + 1
        return ok
    return confirm


async def run_task(cfg: Any, llm: Any, task: Task, think: Any = None, timeout: float = TIMEOUT,
                   keep: Path | None = None) -> dict:
    """Eine Aufgabe einmal lösen lassen → Kennzahlen. keep: Ordner, in dem das Projekt danach liegen bleibt."""
    from .agent import Agent
    from .memory import Memory

    root = Path(tempfile.mkdtemp(prefix=f"orbwise-bench-{task.name}-"))
    project = root / "project"
    project.mkdir()
    _prepare(task, project)
    bcfg = cfg.model_copy(deep=True)
    bcfg.memory.dir = root / "memory"
    bcfg.voice.enabled = False
    memory = Memory(bcfg.memory, llm)
    memory.switch_mode("coding")
    memory.set_project(str(project))
    agent = Agent(bcfg, llm, memory)
    agent.auto_mode = "files"
    rec = Recorder()
    en = getattr(cfg, "language", "de") == "en"
    started, status, answer = time.monotonic(), "ok", ""
    try:
        answer = await asyncio.wait_for(
            agent.run(task.prompt[1 if en else 0], rec.emit, contained(agent, str(project)), think=think), timeout)
        if rec.errors and not answer:
            status = "error"
    except asyncio.TimeoutError:
        status = "timeout"
    except Exception as e:  # noqa: BLE001 – ein Absturz ist ein Ergebnis, kein Grund, den Benchmark abzubrechen
        status, answer = "crash", f"{type(e).__name__}: {e}"
    seconds = time.monotonic() - started
    solved, why = await asyncio.to_thread(verdict, task, project, answer)
    usage = agent.chat_usage()
    marks = ("bereits ausgeführt", "already made with the same arguments")  # Hinweis der Schleifenerkennung
    skipped = sum(1 for m in memory.conversation.history
                  if m.get("role") == "tool" and any(k in str(m.get("content", ""))[:200] for k in marks))
    memory.close()
    if keep:
        shutil.copytree(project, keep / f"{task.name}-{int(time.time())}", dirs_exist_ok=True)
    shutil.rmtree(root, ignore_errors=True)
    return {"task": task.name, "solved": bool(solved) and status != "crash", "why": why, "status": status,
            "seconds": round(seconds, 1), "steps": rec.steps, "tool_calls": sum(rec.tools.values()), "tools": rec.tools,
            "edit_fails": rec.edit_fails, "repeats_skipped": skipped, "think_cuts": rec.think_cuts,
            "denied": getattr(agent, "_bench_denied", 0), "checked": rec.checked,
            "tokens_read": usage["read"], "tokens_cached": usage["cached"], "tokens_written": usage["written"]}


async def run_bench(cfg: Any, llm: Any, names: list[str] | None = None, runs: int = 1, think: Any = None,
                    timeout: float = TIMEOUT, keep: Path | None = None, progress: Callable[[dict], None] | None = None,
                    model: str = "") -> dict:
    """Alle (oder die genannten) Aufgaben je `runs`-mal → {model, window, results: [...], summary}."""
    unknown = [n for n in names or [] if n not in BY_NAME]
    if unknown:
        raise ValueError("Unbekannte Aufgabe: " + ", ".join(unknown))
    tasks = [BY_NAME[n] for n in names] if names else TASKS
    results = []
    for task in tasks:
        for run in range(1, max(1, runs) + 1):
            r = {**await run_task(cfg, llm, task, think, timeout, keep), "run": run}
            results.append(r)
            if progress:
                progress(r)
    return {"model": model, "window": getattr(llm, "context_size", None) or cfg.llm.num_ctx, "think": think,
            "date": datetime.now().isoformat(timespec="seconds"), "runs": max(1, runs), "results": results,
            "summary": summary(results)}


def summary(results: list[dict]) -> dict:
    if not results:
        return {"solved": 0, "total": 0, "rate": 0.0}
    solved = [r for r in results if r["solved"]]

    def median(key: str, rows: list[dict]) -> float:
        return round(statistics.median(r[key] for r in rows), 1) if rows else 0.0
    return {"solved": len(solved), "total": len(results), "rate": round(len(solved) / len(results), 3),
            "seconds": round(sum(r["seconds"] for r in results), 1), "median_steps": median("steps", results),
            "median_seconds": median("seconds", results), "edit_fails": sum(r["edit_fails"] for r in results),
            "think_cuts": sum(r["think_cuts"] for r in results),
            "repeats_skipped": sum(r["repeats_skipped"] for r in results), "denied": sum(r["denied"] for r in results),
            "timeouts": sum(1 for r in results if r["status"] == "timeout"),
            "tokens_written": sum(r["tokens_written"] for r in results),
            "tokens_read": sum(r["tokens_read"] for r in results)}


def format_report(result: dict, en: bool = False) -> str:
    """Tabelle je Aufgabe und eine Summenzeile – für die Konsole."""
    rows: dict[str, list[dict]] = {}
    for r in result["results"]:
        rows.setdefault(r["task"], []).append(r)
    head = (("task", "solved", "steps", "time", "edits failed", "note") if en
            else ("Aufgabe", "gelöst", "Schritte", "Zeit", "Edits fehlgeschl.", "Hinweis"))
    table = [head]
    for name, runs in rows.items():
        ok = sum(1 for r in runs if r["solved"])
        note = next((r["why"] for r in runs if not r["solved"]), "")
        if any(r["status"] != "ok" for r in runs):
            note = ", ".join(sorted({r["status"] for r in runs if r["status"] != "ok"})) + (f" – {note}" if note else "")
        table.append((name, f"{ok}/{len(runs)}", f"{statistics.median(r['steps'] for r in runs):g}",
                      f"{statistics.median(r['seconds'] for r in runs):.0f} s", str(sum(r["edit_fails"] for r in runs)),
                      note[:60]))
    widths = [max(len(row[i]) for row in table) for i in range(len(head))]
    lines = ["  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row)).rstrip() for row in table]
    lines.insert(1, "  ".join("-" * w for w in widths))
    s = result["summary"]
    model = f"{result.get('model') or '?'} · {result.get('window') or '?'} Token"
    think = result.get("think")
    model += (" · thinking " if en else " · Denken ") + (str(think) if think not in (None, False) else ("off" if en else "aus"))
    total = (f"{s['solved']}/{s['total']} solved ({s['rate']:.0%}) · {s['seconds']:.0f} s · median {s['median_steps']:g} "
             f"steps · {s['edit_fails']} failed edits · {s['think_cuts']} thinking cut · {s['repeats_skipped']} repeats "
             f"skipped · {s['denied']} denied · {s['timeouts']} timeouts" if en else
             f"{s['solved']}/{s['total']} gelöst ({s['rate']:.0%}) · {s['seconds']:.0f} s · Median {s['median_steps']:g} "
             f"Schritte · {s['edit_fails']} fehlgeschlagene Edits · {s['think_cuts']}× Denken abgebrochen · "
             f"{s['repeats_skipped']} Wiederholungen übersprungen · {s['denied']} abgelehnt · {s['timeouts']} Zeitlimit")
    return "\n".join([model, "", *lines, "", total])


def save(result: dict, folder: Path) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    slug = "".join(c if c.isalnum() else "-" for c in str(result.get("model") or "modell"))[:40].strip("-") or "modell"
    path = folder / f"{datetime.now():%Y%m%d-%H%M%S}-{slug}.json"
    path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return path
