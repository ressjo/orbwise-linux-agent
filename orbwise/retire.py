"""Aufräumen nach entfernten Modell-Engines.

Strata (eigene MoE-Engine für Qwen3.8-Flash-Next) und Bonsai (PrismML-Fork von llama.cpp mit eigenem Setup) gibt es
in Orbwise nicht mehr. Wer sie angelegt hatte, bekommt beim ersten Start danach: Profile raus aus state.json (samt
ihren Einstellungen); bei Strata außerdem im Hintergrund einen noch laufenden Strata-Server stoppen, das Modell
(~/models/strata/<variante>) und die von Orbwise installierte Engine (~/.local/share/strata) löschen. ~/bonsai bleibt
liegen (kann von Hand gelöscht werden). Läuft nur, solange es etwas zu tun gibt.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path

from . import state

log = logging.getLogger(__name__)

Runner = Callable[..., subprocess.CompletedProcess]

# Einstellungen, die state.json je Profil führt – mit dem Profil verschwinden sie auch
PER_PROFILE = ("context", "context_coding", "sampling", "kv_cache", "ubatch", "speculative", "prefill_tps", "tuning",
               "strata_reserve")
_STRATA = re.compile(r"(^|/)strata\s+launch\b")


def strata_data_dir() -> Path:
    base = os.environ.get("STRATA_DATA_DIR") or os.path.join(
        os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share"), "strata")
    return Path(base)


def _command(raw: dict) -> str:
    return str(((raw or {}).get("server") or {}).get("command") or "")


def clean_state(state_path: Path) -> dict | None:
    """Profile entfernter Engines aus state.json nehmen → {"profiles", "strata", "strata_dirs"}, None wenn nichts zu
    tun war."""
    data = state.load(state_path)
    added = data.get("added_profiles") or {}
    local = data.get("local_models") or {}
    strata = [n for n, raw in added.items() if _STRATA.search(_command(raw)) or (local.get(n) or {}).get("engine") == "strata"]
    strata += [n for n, rec in local.items() if rec.get("engine") == "strata" and n not in strata]
    bonsai = [n for n, raw in added.items() if n in ("bonsai", "bonsai-kompakt") and "start_llama_server.sh" in _command(raw)]
    names = strata + bonsai
    if not names and "strata_reserve" not in data:
        return None
    dirs = [str(local[n]["dir"]) for n in strata if (local.get(n) or {}).get("dir")]
    for n in names:
        added.pop(n, None)
        local.pop(n, None)
        for key in PER_PROFILE:
            if isinstance(data.get(key), dict):
                data[key].pop(n, None)
    data.pop("strata_reserve", None)
    if data.get("active_profile") in names:  # dann gilt llm.active bzw. das erste Profil – geladen wie gewohnt
        data.pop("active_profile")
        data.pop("unloaded", None)
    state.save(state_path, data)
    return {"profiles": names, "strata": strata, "strata_dirs": dirs}


def _size(path: Path) -> int:
    total = 0
    for root, _dirs, files in os.walk(path):
        for f in files:
            try:
                total += os.lstat(os.path.join(root, f)).st_size
            except OSError:
                pass
    return total


def remove_strata_files(dirs: list[str], run: Runner = subprocess.run, home: Path | None = None) -> int:
    """Laufenden Strata-Server stoppen, Modell-Ordner und Engine löschen → freigegebene Bytes."""
    data = strata_data_dir()
    exe = data / "terminal" / "current" / "bin" / "strata"
    if exe.exists():
        try:  # gibt den Grafikspeicher frei, falls das Modell noch geladen ist
            run([str(exe), "launch", "--stop"], capture_output=True, text=True, timeout=60)
        except (OSError, subprocess.SubprocessError) as e:
            log.warning("Strata-Server ließ sich nicht stoppen: %s", e)
    freed = 0
    for raw in dirs:
        folder = Path(os.path.expanduser(raw))
        if folder.is_dir():
            freed += _size(folder)
            shutil.rmtree(folder, ignore_errors=True)
        parent = folder.parent
        if parent.name == "strata" and parent.is_dir() and not any(parent.iterdir()):
            parent.rmdir()
    if data.is_dir():
        freed += _size(data)
        shutil.rmtree(data, ignore_errors=True)
    link = (home or Path.home()) / ".local" / "bin" / "strata"
    if link.is_symlink() and os.readlink(link).startswith(str(data)):  # nur den Verweis auf diese Engine
        link.unlink()
    return freed
