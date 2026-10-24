"""Schnellste verlustfreie llama.cpp-Einstellung für ein Modell messen.

Gemessen wird mit `llama-bench` (liegt in den offiziellen Builds neben llama-server): Einlesen (Prefill) eines
2k-Prompts am Chat-Anfang und bei langem Chat (Tiefe 16k) sowie die Ausgabe, je Batch-Größe (-ub 512/1024/2048) und
je Build (ROCm ↔ Vulkan bzw. CUDA ↔ Vulkan). Alles davon ändert nur das Tempo, nicht die Antworten. Die beste
Batch-Größe wird übernommen; ist der andere Build schneller, schlägt die Modellseite den Wechsel vor (gilt für alle
llama.cpp-Modelle, darum nicht automatisch).

Ein dichtes Modell (z. B. 27B) rechnet beim Einlesen pro Token alle Parameter – ein MoE-Modell nur einen Bruchteil.
Das Tuning holt das Maximum aus der Karte, erreicht aber nicht das Tempo eines MoE."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import shlex
import signal
import subprocess
import time
from collections.abc import Awaitable, Callable
from pathlib import Path

from . import llamacpp, state
from .lang import T
from .llm_memory import cache_type

log = logging.getLogger(__name__)

UBATCHES = (512, 1024, 2048)
DEPTH = 16384          # Einlesen „bei langem Chat“: so viele Token stehen schon im Cache
PROMPT = 2048          # gemessener Prompt-Teil
GEN = 32               # gemessene Ausgabe-Token
TIMEOUT_S = 900        # je Lauf (eine Batch-Größe, ein Build)
TIE = 0.03             # innerhalb von 3 % gilt als gleich schnell
FLAVOR_NAMES = {"rocm": "ROCm", "vulkan": "Vulkan", "cuda": "CUDA", "cpu": "CPU"}

Progress = Callable[[str], Awaitable[None]]
Runner = Callable[[list[str], dict, float], Awaitable[tuple[int, str, str]]]


def uses_own_build(cfg, command: str) -> bool:
    """Startet das Profil Orbwises eigenen llama-server (keinen fremden Server)?"""
    try:
        exe = os.path.expanduser(shlex.split(command)[0])
    except (ValueError, IndexError):
        return False
    return Path(exe) == llamacpp.binary(cfg) or Path(exe).resolve() == llamacpp.binary(cfg).resolve()


def bench_args(bench: Path, command: str, gguf: Path, ubatch: int, depth: int, help_text: str = "") -> list[str]:
    """llama-bench-Aufruf mit den Einstellungen des Profils (KV-Cache, Flash-Attention wie im Server)."""
    kv = cache_type(command, {})
    fa = "on" if re.search(r"(-fa|--flash-attn)[^\n]*\bon\b", help_text) else "1"
    args = [str(bench), "-m", str(gguf), "-ngl", "99", "-fa", fa, "-ub", str(ubatch), "-b", str(max(2048, ubatch)),
            "-p", str(PROMPT), "-n", str(GEN), "-r", "1", "-o", "json"]
    if kv != "f16":
        args += ["-ctk", kv, "-ctv", kv]
    if depth and (not help_text or re.search(r"(-d,|--n-depth)", help_text)):
        args += ["-d", f"0,{depth}"]
    return args


def parse_bench(text: str) -> dict | None:
    """llama-bench -o json → {pp_short, pp_deep, pp, tg} in Token/s (pp = Mittel aus Chat-Anfang und langem Chat)."""
    try:
        start = text.index("[")
        rows = json.loads(text[start:text.rindex("]") + 1])
    except (ValueError, json.JSONDecodeError):
        return None
    pp: dict[int, float] = {}
    tg: dict[int, float] = {}
    for row in rows if isinstance(rows, list) else []:
        ts, depth = float(row.get("avg_ts") or 0), int(row.get("n_depth") or 0)
        if row.get("n_gen"):
            tg.setdefault(depth, ts)
        elif row.get("n_prompt"):
            pp[depth] = ts
    if not pp:
        return None
    short = pp.get(0) or next(iter(pp.values()))
    deep = max(pp.items())[1] if len(pp) > 1 else None
    gen = tg.get(0) or next(iter(tg.values()), None)
    return {"pp_short": round(short, 1), "pp_deep": round(deep, 1) if deep else None,
            "pp": round((short + deep) / 2 if deep else short, 1), "tg": round(gen, 1) if gen else None}


def best(runs: list[dict]) -> dict | None:
    """Schnellstes Einlesen; was innerhalb von 3 % liegt, gilt als gleich schnell – davon die mit schnellerer Ausgabe
    (ebenfalls ±3 %) und dann die kleinere Batch (weniger Grafikspeicher)."""
    ok = [r for r in runs if r.get("pp")]
    if not ok:
        return None
    top = max(r["pp"] for r in ok)
    near = [r for r in ok if r["pp"] >= top * (1 - TIE)]
    fastest = max(r.get("tg") or 0 for r in near)
    near = [r for r in near if (r.get("tg") or 0) >= fastest * (1 - TIE)]
    return min(near, key=lambda r: (r["ubatch"], -r["pp"]))


def quant_hint(gguf: Path) -> str:
    from .models import QUANT_RE, SPLIT_RE
    m = QUANT_RE.search(SPLIT_RE.sub(".gguf", gguf.name))
    quant = (m.group(1) if m else "").upper()
    if "IQ" not in quant:
        return ""
    return T(f"{quant} ist ein IQ-Quant – die brauchen beim Einlesen mehr Rechenarbeit. Ein K-Quant wie Q4_K_M bzw. "
             "UD-Q4_K_XL hat mindestens gleiche Qualität und liest meist schneller ein, braucht aber ~1–2 GB mehr "
             "Grafikspeicher.",
             f"{quant} is an IQ quant – these need more compute when reading the prompt. A K quant such as Q4_K_M or "
             "UD-Q4_K_XL has at least the same quality and usually reads faster, but needs ~1–2 GB more video memory.")


async def run_process(argv: list[str], env: dict, timeout: float) -> tuple[int, str, str]:
    proc = await asyncio.create_subprocess_exec(*argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                                                env=env, start_new_session=True)
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout)
    except (asyncio.TimeoutError, asyncio.CancelledError) as e:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(proc.pid, signal.SIGKILL)  # samt Kindprozessen – sonst hielte einer den Grafikspeicher
        await proc.wait()
        if isinstance(e, asyncio.CancelledError):
            raise
        return -1, "", T("Zeitlimit überschritten", "timed out")
    return proc.returncode or 0, out.decode(errors="replace"), err.decode(errors="replace")


def _why(code: int, err: str) -> str:
    low = err.lower()
    if "out of memory" in low or "failed to allocate" in low or "oom" in low:
        return T("zu wenig Grafikspeicher", "not enough video memory")
    lines = [x.strip() for x in err.splitlines() if x.strip()]
    return (lines[-1][:160] if lines else T(f"Exit-Code {code}", f"exit code {code}"))


def _help(bench: Path, env: dict) -> str:
    try:
        r = subprocess.run([str(bench), "--help"], capture_output=True, text=True, timeout=30, env=env)
        return (r.stdout or "") + (r.stderr or "")
    except (OSError, subprocess.SubprocessError):
        return ""


async def tune(cfg, router, name: str, gpu: dict, progress: Progress | None = None, run: Runner = run_process,
               fetch=None) -> dict:
    """Messen, beste Batch-Größe übernehmen, Ergebnis in state.json["tuning"][name] merken → Ergebnis."""
    from .llm_router import ubatch_of
    p = router.profiles[name]
    if not p.server or not uses_own_build(cfg, p.server.command):
        raise ValueError(T("Messen geht nur bei Modellen mit Orbwises llama.cpp.",
                           "Measuring only works for models running on Orbwise's llama.cpp."))
    files = [f for f in llamacpp.profile_paths(p) if f.is_file()]
    if not files:
        raise ValueError(T("Modelldatei nicht gefunden.", "Model file not found."))
    gguf = files[0]
    say = progress or (lambda text: asyncio.sleep(0))
    current = llamacpp.manifest(cfg).get("flavor") or "?"
    builds = [(current, (llamacpp.root(cfg) / "current").resolve())]
    notes = []
    other = llamacpp.other_flavor(gpu, current)
    if other:
        await say(T(f"Lade den {FLAVOR_NAMES.get(other, other)}-Build zum Vergleich …",
                    f"Downloading the {FLAVOR_NAMES.get(other, other)} build for comparison …"))
        try:
            fetch = fetch or (lambda: asyncio.to_thread(llamacpp.fetch_flavor, cfg, gpu, other, log.info))
            builds.append((other, await fetch()))
        except Exception as e:  # noqa: BLE001 – Vergleich fällt weg, der Rest läuft
            notes.append(T(f"{FLAVOR_NAMES.get(other, other)} nicht gemessen: {e}",
                           f"{FLAVOR_NAMES.get(other, other)} not measured: {e}"))
    n_ctx = p.num_ctx or 0
    depth = min(DEPTH, n_ctx // 2) if n_ctx else DEPTH
    if router.gpu_borrowed:  # Bild-Modus hält den Grafikspeicher
        await router.gpu_borrowed(restart=False)
    was_loaded = not router.unloaded
    if was_loaded:
        await say(T("Entlade das Modell, damit der Grafikspeicher frei ist …", "Unloading the model to free video memory …"))
        await router.unload()
    runs: list[dict] = []
    try:
        steps = [(f, d, ub) for f, d in builds for ub in UBATCHES]
        for i, (flavor, folder, ub) in enumerate(steps, 1):
            label = FLAVOR_NAMES.get(flavor, flavor)
            await say(T(f"Messe {label} · Batch {ub} ({i}/{len(steps)}) …", f"Measuring {label} · batch {ub} ({i}/{len(steps)}) …"))
            bench = folder / "llama-bench"
            if not bench.exists():
                runs.append({"flavor": flavor, "ubatch": ub, "error": T("llama-bench fehlt im Build",
                                                                         "llama-bench is missing from the build")})
                continue
            env = llamacpp._env(folder)
            args = bench_args(bench, p.server.command, gguf, ub, depth, await asyncio.to_thread(_help, bench, env))
            code, out, err = await run(args, env, TIMEOUT_S)
            got = parse_bench(out) if code == 0 else None
            runs.append({"flavor": flavor, "ubatch": ub, **got} if got else
                        {"flavor": flavor, "ubatch": ub, "error": _why(code, err) if code else
                         T("keine Messwerte", "no results")})
    finally:
        cur_ub = ubatch_of(p.server.command)
        top_here = best([r for r in runs if r["flavor"] == current])
        applied = None
        if top_here and top_here["ubatch"] != cur_ub:
            await router.set_ubatch(name, top_here["ubatch"])  # Modell ist entladen: gilt beim nächsten Laden
            applied = top_here["ubatch"]
        if was_loaded:
            await say(T("Lade das Modell wieder …", "Loading the model again …"))
            try:
                await router.ensure_loaded()
            except Exception as e:  # noqa: BLE001 – Ergebnis trotzdem melden
                notes.append(T(f"Neu laden fehlgeschlagen: {e}", f"Reloading failed: {e}"))
    top = best(runs)
    other_best = best([r for r in runs if r["flavor"] != current])
    result = {"at": time.time(), "flavor": current, "depth": depth, "runs": runs, "best": top,
              "before": next((r for r in runs if r["flavor"] == current and r["ubatch"] == cur_ub and r.get("pp")), None),
              "applied_ubatch": applied, "ubatch": applied or cur_ub, "notes": notes, "quant_hint": quant_hint(gguf),
              "other": ({"flavor": other_best["flavor"], "gain_pct": round((other_best["pp"] / top_here["pp"] - 1) * 100)}
                        if other_best and top_here else None)}
    if router.state_path:
        def remember(data: dict) -> None:
            data.setdefault("tuning", {})[name] = result
        state.update(router.state_path, remember)
    return result


def saved(state_path: Path | None, name: str) -> dict | None:
    if not state_path:
        return None
    return (state.load(state_path).get("tuning") or {}).get(name)
