"""Schritt-Limit je Antwort (Werkzeug-Runden, danach Zwischenbilanz + „mach weiter“) – in den Einstellungen wählbar,
getrennt für Tools- und Coding-Modus, auch unbegrenzt (0). Dazu, wann mit Denkmodus gedacht wird (think_steps: vor
jedem Schritt oder nur wo nötig). Die Wahl liegt in state.json und gilt vor der config.yaml."""

from __future__ import annotations

from pathlib import Path

from . import state
from .config import MAX_STEPS_LIMIT, clamp_steps

MODES = ("tools", "coding")
THINK_STEPS = ("all", "lean")


def _section(cfg, mode: str):
    return cfg.coding if mode == "coding" else cfg.tools


def apply_saved(cfg, state_path: Path) -> None:
    """Beim Start: gespeicherte Wahl aus der Oberfläche übernehmen."""
    data = state.load(state_path)
    if data.get("think_steps") in THINK_STEPS:
        cfg.llm.think_steps = data["think_steps"]
    saved = data.get("max_steps")
    if not isinstance(saved, dict):
        return
    for mode in MODES:
        if isinstance(saved.get(mode), int) and not isinstance(saved.get(mode), bool):
            _section(cfg, mode).max_steps = clamp_steps(saved[mode])


def info(cfg) -> dict:
    return {"tools": cfg.tools.max_steps, "coding": cfg.coding.max_steps, "max": MAX_STEPS_LIMIT,
            "think_steps": cfg.llm.think_steps}


def set_limits(cfg, state_path: Path, values: dict) -> dict:
    """{tools?, coding?} setzen (0 = unbegrenzt), {think_steps?} ("all" | "lean") – und merken → neuer Stand."""
    changed = {}
    think = values.get("think_steps")
    if think is not None and think not in THINK_STEPS:
        raise ValueError("think_steps")
    for mode in MODES:
        v = values.get(mode)
        if v is None:
            continue
        if isinstance(v, bool) or not isinstance(v, int | float | str):
            raise ValueError(mode)
        try:
            changed[mode] = clamp_steps(int(v))
        except OverflowError as e:  # float("inf")
            raise ValueError(mode) from e
        _section(cfg, mode).max_steps = changed[mode]

    def change(data: dict) -> None:
        data["max_steps"] = {**(data.get("max_steps") if isinstance(data.get("max_steps"), dict) else {}), **changed}
        if think is not None:
            data["think_steps"] = think
    if think is not None:
        cfg.llm.think_steps = think
    if changed or think is not None:
        state.update(state_path, change)
    return info(cfg)
