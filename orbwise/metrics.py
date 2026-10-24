"""Hardware-Telemetrie für die Oberfläche: GPU-Last, VRAM, Leistung, Temperatur, RAM und CPU.

NVIDIA wird über `nvidia-smi` gelesen, AMD (amdgpu) direkt aus sysfs – keine zusätzlichen Pakete nötig.
Alle Werte sind optional; was nicht messbar ist, fehlt bzw. ist None.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from .tools.system import _meminfo

DRM_ROOT = Path("/sys/class/drm")
PROC_STAT = Path("/proc/stat")

_last_cpu: tuple[int, int] | None = None


def _num(value: str) -> float | None:
    try:
        return float(value.strip())
    except (ValueError, AttributeError):
        return None  # z. B. "[N/A]"


def _read(path: Path) -> str | None:
    try:
        return path.read_text().strip()
    except OSError:
        return None


def nvidia_gpu(runner=subprocess.run) -> dict | None:
    if not shutil.which("nvidia-smi"):
        return None
    try:
        out = runner(
            ["nvidia-smi", "--query-gpu=name,utilization.gpu,memory.used,memory.total,power.draw,temperature.gpu",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=3,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    gpus = []
    for line in out.strip().splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 6:
            continue
        used, total = _num(parts[2]), _num(parts[3])
        gpus.append({
            "vendor": "nvidia",
            "name": parts[0],
            "util": _num(parts[1]),
            "vram_used": used * 1024 ** 2 if used is not None else None,   # MiB → Bytes
            "vram_total": total * 1024 ** 2 if total is not None else None,
            "power": _num(parts[4]),
            "temp": _num(parts[5]),
        })
    return max(gpus, key=lambda g: g["vram_total"] or 0) if gpus else None


def amd_gpu(root: Path = DRM_ROOT) -> dict | None:
    best, best_total = None, 0
    for dev in sorted(root.glob("card*/device")):
        total = _num(_read(dev / "mem_info_vram_total") or "")
        if total and total > best_total and (dev / "gpu_busy_percent").exists():
            best, best_total = dev, total
    if not best:
        return None
    power = temp = None
    for hw in sorted(best.glob("hwmon/hwmon*")):
        for name in ("power1_average", "power1_input"):
            val = _num(_read(hw / name) or "")
            if val is not None and power is None:
                power = val / 1e6  # µW → W
        t = _num(_read(hw / "temp1_input") or "")
        if t is not None and temp is None:
            temp = t / 1000  # m°C → °C
    return {
        "vendor": "amd",
        "name": _read(best / "product_name") or "AMD GPU",
        "util": _num(_read(best / "gpu_busy_percent") or ""),
        "vram_used": _num(_read(best / "mem_info_vram_used") or ""),
        "vram_total": best_total,
        "power": power,
        "temp": temp,
    }


def cpu_percent(stat_path: Path = PROC_STAT) -> float | None:
    """CPU-Auslastung seit dem letzten Aufruf (erster Aufruf liefert None)."""
    global _last_cpu
    line = (_read(stat_path) or "").splitlines()[:1]
    if not line or not line[0].startswith("cpu "):
        return None
    values = [int(v) for v in line[0].split()[1:]]
    idle = values[3] + (values[4] if len(values) > 4 else 0)
    total = sum(values)
    prev, _last_cpu = _last_cpu, (idle, total)
    if not prev or total <= prev[1]:
        return None
    return round(100 * (1 - (idle - prev[0]) / (total - prev[1])), 1)


def collect() -> dict:
    gpu = nvidia_gpu() or amd_gpu()
    mem = _meminfo()
    ram = None
    if mem.get("MemTotal"):
        ram = {"used": mem["MemTotal"] - mem.get("MemAvailable", 0), "total": mem["MemTotal"]}
    return {"gpu": gpu, "ram": ram, "cpu": cpu_percent()}
