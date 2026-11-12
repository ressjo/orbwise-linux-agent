import subprocess

from conftest import run

from orbwise import metrics
from orbwise.agent import Agent


def make_amd(root, total=8 * 1024**3, used=3 * 1024**3, busy=57, power_uw=123_000_000, temp=64000):
    dev = root / "card1" / "device"
    hw = dev / "hwmon" / "hwmon3"
    hw.mkdir(parents=True)
    (dev / "mem_info_vram_total").write_text(str(total))
    (dev / "mem_info_vram_used").write_text(str(used))
    (dev / "gpu_busy_percent").write_text(str(busy))
    (dev / "product_name").write_text("Radeon RX 7600")
    (hw / "power1_average").write_text(str(power_uw))
    (hw / "temp1_input").write_text(str(temp))
    # integrierte GPU mit wenig VRAM soll ignoriert werden
    igpu = root / "card0" / "device"
    igpu.mkdir(parents=True)
    (igpu / "mem_info_vram_total").write_text(str(512 * 1024**2))
    (igpu / "gpu_busy_percent").write_text("1")


def test_amd_from_sysfs(tmp_path):
    make_amd(tmp_path)
    g = metrics.amd_gpu(tmp_path)
    assert g["name"] == "Radeon RX 7600" and g["util"] == 57
    assert g["vram_total"] == 8 * 1024**3 and g["vram_used"] == 3 * 1024**3
    assert g["power"] == 123.0 and g["temp"] == 64.0


def test_no_gpu(tmp_path):
    assert metrics.amd_gpu(tmp_path) is None


def test_nvidia_parsing(monkeypatch):
    monkeypatch.setattr(metrics.shutil, "which", lambda n: "/usr/bin/nvidia-smi")

    def runner(*a, **k):
        return subprocess.CompletedProcess(a, 0, stdout="NVIDIA GeForce RTX 4080 SUPER, 87, 11234, 16376, 285.40, 66\n")

    g = metrics.nvidia_gpu(runner)
    assert g["util"] == 87 and g["power"] == 285.4 and g["temp"] == 66
    assert round(g["vram_used"] / 1024**2) == 11234 and round(g["vram_total"] / 1024**3) == 16

    def runner_na(*a, **k):
        return subprocess.CompletedProcess(a, 0, stdout="NVIDIA GeForce RTX 4080 SUPER, 3, 500, 16376, [N/A], 40\n")

    assert metrics.nvidia_gpu(runner_na)["power"] is None


def test_cpu_percent(tmp_path, monkeypatch):
    stat = tmp_path / "stat"
    monkeypatch.setattr(metrics, "_last_cpu", None)
    stat.write_text("cpu  100 0 100 800 0 0 0 0 0 0\n")
    assert metrics.cpu_percent(stat) is None
    stat.write_text("cpu  150 0 150 900 0 0 0 0 0 0\n")  # +100 busy, +100 idle
    assert metrics.cpu_percent(stat) == 50.0


def test_collect_has_ram():
    data = metrics.collect()
    assert data["ram"]["total"] > 0 and data["ram"]["used"] > 0


def test_agent_emits_llm_stats(cfg, llm, memory):
    events = []

    async def emit(e):
        events.append(e)

    async def confirm(*a):
        return True

    run(Agent(cfg, llm, memory).run("Hallo", emit, confirm))
    stats = [e for e in events if e["type"] == "llm_stats"]
    assert stats and stats[0]["tps"] == 42.0
