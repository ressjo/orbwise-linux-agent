"""Wo liegt der Kontext (KV-Cache) des Sprachmodells – im Grafikspeicher oder im RAM – und wie groß darf er sein?

- llama-server (z. B. Bonsai): genaue Werte aus seinem Startlog („ROCm0 KV buffer size = 512.00 MiB“,
  „CPU_Mapped model buffer size = …“).
- Ollama: /api/ps (wie viel vom Modell im VRAM liegt) und /api/show (Architektur) – der KV-Cache wird daraus
  geschätzt (f16, wie Ollama ihn standardmäßig anlegt).

Daraus eine Empfehlung: Wie groß kann das Kontextfenster werden, ohne in den RAM auszuweichen (langsam), bzw. wie
weit sollte es herunter, wenn es schon ausweicht.
"""

from __future__ import annotations

import re
from typing import Any

MIB = 1024 ** 2
RESERVE = 600 * MIB  # Rest im Grafikspeicher lassen (Desktop, Rechenpuffer)
STEPS = (2048, 4096, 8192, 12288, 16384, 24576, 32768, 49152, 65536, 98304, 131072, 196608, 262144, 393216,
         524288, 1048576)

_BUF = re.compile(r"(\S+)\s+(KV|model|compute) buffer size\s*=\s*([\d.]+)\s*MiB")
_CTX = re.compile(r"\bn_ctx\s*=\s*(\d+)")


def _is_ram(device: str) -> bool:
    d = device.lower()
    return d.startswith("cpu") or "host" in d


def parse_llama_log(text: str) -> dict | None:
    """Speicherbelegung des letzten Starts aus dem llama-server-Log; None, wenn nichts zu finden ist."""
    start = text.rfind("llama_model_loader: loaded meta data")  # nur der letzte Start zählt
    block = text[start:] if start >= 0 else text
    out = {"kv_vram": 0, "kv_ram": 0, "model_vram": 0, "model_ram": 0, "compute_vram": 0, "compute_ram": 0}
    found = False
    for device, kind, mib in _BUF.findall(block):
        key = f"{kind.lower()}_{'ram' if _is_ram(device) else 'vram'}"
        out[key] += int(float(mib) * MIB)
        found = True
    ctx = _CTX.findall(block)
    if not found:
        return None
    out["ctx"] = int(ctx[-1]) if ctx else None
    layers = re.findall(r"offloaded (\d+)/(\d+) layers to GPU", block)
    # nur wenn Schichten des Modells im RAM liegen, ist der RAM-Anteil ein Problem (Einbettungen liegen immer dort)
    out["model_ram_offload"] = out["model_ram"] if layers and int(layers[-1][0]) < int(layers[-1][1]) else 0
    ctx_n = out["ctx"]
    kv = out["kv_vram"] + out["kv_ram"]
    out["kv_per_token"] = int(kv / ctx_n) if ctx_n and kv else None
    train = re.findall(r"\bn_ctx_train\s*=\s*(\d+)", block)
    out["ctx_train"] = int(train[-1]) if train else None
    out["source"] = "log"
    out["flash_attn"] = flash_attn(block)
    return out


def flash_attn(text: str) -> str | None:
    """Flash-Attention laut llama-server-Log: 'on', 'off' oder None (nicht erkennbar). Neuere Builds melden
    „Flash Attention was auto, set to enabled“, ältere „flash_attn = 1“."""
    decided = re.findall(r"Flash Attention was \w+, set to (enabled|disabled)", text, re.I)
    if decided:
        return "on" if decided[-1].lower() == "enabled" else "off"
    values = re.findall(r"\bflash_attn\s*=\s*(\w+)", text)
    value = values[-1].lower() if values else ""
    if value in ("1", "true", "enabled", "on"):
        return "on"
    if value in ("0", "false", "disabled", "off"):
        return "off"
    return None


def kv_per_token_from_info(model_info: dict, bytes_per: float = 2.0) -> int | None:
    """KV-Cache pro Token aus den GGUF-Metadaten von Ollama (/api/show → model_info)."""
    def find(suffix: str) -> int | None:
        for key, value in model_info.items():
            if key.endswith(suffix) and isinstance(value, (int, float)):
                return int(value)
        return None
    layers = find(".block_count")
    heads = find(".attention.head_count")
    kv_heads = find(".attention.head_count_kv") or heads
    key_len = find(".attention.key_length")
    value_len = find(".attention.value_length")
    if not key_len and heads and find(".embedding_length"):
        key_len = find(".embedding_length") // heads
    value_len = value_len or key_len
    # Hybrid-Modelle (Bonsai 2/Qwen 3.5, Qwen3-Next …): nur ein Teil der Schichten hat Attention und damit einen
    # KV-Cache – je Schicht angegeben (0 = rekurrente Schicht) oder als „jede n-te Schicht“
    per_layer = next((v for k, v in model_info.items() if k.endswith(".attention.head_count_kv")
                      and isinstance(v, list)), None)
    if per_layer and key_len:
        return int(sum(int(x) for x in per_layer if isinstance(x, (int, float))) * (key_len + value_len) * bytes_per)
    interval = find(".full_attention_interval")
    if layers and interval and interval > 1:
        layers = max(1, layers // interval)
    if not (layers and kv_heads and key_len):
        return None
    return int(layers * kv_heads * (key_len + value_len) * bytes_per)


def recommend(info: dict, gpu: dict | None) -> dict:
    """Empfohlenes Kontextfenster: so groß, wie der freie Grafikspeicher erlaubt – oder kleiner, wenn schon etwas
    in den RAM ausweicht."""
    ctx, per_token = info.get("ctx"), info.get("kv_per_token")
    if not ctx or not per_token:
        return {}
    spill = info.get("kv_ram", 0) + info.get("model_ram_offload", 0)
    if spill > 64 * MIB:  # Kontext oder Modell weichen in den RAM aus → kleiner
        target = ctx - int(spill / per_token)
        verdict = "reduce"
    else:
        free = (gpu or {}).get("vram_total", 0) - (gpu or {}).get("vram_used", 0) - RESERVE \
            if gpu and gpu.get("vram_total") else 0
        target = ctx + int(max(0, free) / per_token)
        verdict = "increase" if target >= ctx * 1.25 else "ok"
    if info.get("ctx_train"):  # mehr als trainiert bringt nichts (das Modell versteht es nicht)
        target = min(target, info["ctx_train"])
    fitting = [s for s in STEPS if s <= max(target, STEPS[0])]
    return {"verdict": verdict, "max_ctx": fitting[-1], "options": [
        {"ctx": s, "kv_bytes": s * per_token, "fits": s <= max(target, 0)} for s in STEPS
        if not info.get("ctx_train") or s <= info["ctx_train"]]}


def summary(info: dict[str, Any]) -> dict:
    """Zusammenfassung für die Oberfläche (Bytes)."""
    kv = info.get("kv_vram", 0) + info.get("kv_ram", 0)
    model = info.get("model_vram", 0) + info.get("model_ram", 0)
    return {**info, "kv_bytes": kv, "model_bytes": model,
            "kv_vram_share": round(info.get("kv_vram", 0) / kv, 3) if kv else None,
            "model_vram_share": round(info.get("model_vram", 0) / model, 3) if model else None}


# ---------------------------------------------------------------- Schätzung aus der GGUF-Datei (llama-server)
# Wenn das Startlog nichts hergibt (eigener Log des Startskripts, Server lief schon): Architektur aus dem Kopf der
# Modelldatei lesen und den KV-Cache daraus berechnen – wie bei Ollama.
_GGUF_SCALARS = {0: "<B", 1: "<b", 2: "<H", 3: "<h", 4: "<I", 5: "<i", 6: "<f", 7: "<?", 10: "<Q", 11: "<q", 12: "<d"}
CACHE_BYTES = {"f32": 4.0, "f16": 2.0, "bf16": 2.0, "q8_0": 34 / 32, "q5_1": 24 / 32, "q5_0": 22 / 32,
               "q4_1": 20 / 32, "q4_0": 18 / 32, "iq4_nl": 18 / 32}


def gguf_metadata(path: str, wanted: tuple[str, ...] = (".block_count", ".attention.head_count",
                                                         ".attention.head_count_kv", ".attention.key_length",
                                                         ".attention.value_length", ".embedding_length",
                                                         ".context_length", ".full_attention_interval")) -> dict:
    """Ausgewählte Zahlen-Metadaten aus dem Kopf einer GGUF-Datei (liest nur so weit wie nötig)."""
    import struct

    out: dict[str, int] = {}
    with open(path, "rb") as f:
        if f.read(4) != b"GGUF":
            return {}
        _version, _tensors, n_kv = struct.unpack("<IQQ", f.read(20))

        def string() -> str:
            (n,) = struct.unpack("<Q", f.read(8))
            return f.read(n).decode("utf-8", "replace")

        def skip(kind: int) -> None:
            if kind == 8:
                (n,) = struct.unpack("<Q", f.read(8))
                f.seek(n, 1)
            elif kind == 9:
                elem, count = struct.unpack("<IQ", f.read(12))
                if elem in _GGUF_SCALARS:
                    f.seek(struct.calcsize(_GGUF_SCALARS[elem]) * count, 1)
                else:
                    for _ in range(count):
                        skip(elem)
            else:
                f.seek(struct.calcsize(_GGUF_SCALARS[kind]), 1)

        for _ in range(min(n_kv, 10_000)):
            key = string()
            (kind,) = struct.unpack("<I", f.read(4))
            if kind in _GGUF_SCALARS and kind not in (6, 7, 12) and key.endswith(wanted):
                (out[key],) = struct.unpack(_GGUF_SCALARS[kind], f.read(struct.calcsize(_GGUF_SCALARS[kind])))
            elif kind == 9 and key.endswith(".attention.head_count_kv"):  # je Schicht (Hybrid-Modelle)
                elem, count = struct.unpack("<IQ", f.read(12))
                if elem in _GGUF_SCALARS and elem not in (6, 7, 12) and count <= 4096:
                    fmt = _GGUF_SCALARS[elem]
                    out[key] = [struct.unpack(fmt, f.read(struct.calcsize(fmt)))[0] for _ in range(count)]
                else:
                    f.seek(-12, 1)
                    skip(kind)
            else:
                skip(kind)
            if len(out) >= len(wanted):
                break
    return out


def type_bytes(kind: str) -> float:
    """Bytes je KV-Element; unbekannte Typen (z. B. 2-/3-Bit aus Forks) aus der Bit-Zahl im Namen geschätzt."""
    kind = kind.lower()
    if kind in CACHE_BYTES:
        return CACHE_BYTES[kind]
    m = re.search(r"q(\d)", kind)
    return (int(m.group(1)) * 32 + 16) / 8 / 32 if m else 2.0  # Bits + Skalierung je 32er-Block


def cache_type(command: str, env: dict) -> str:
    """KV-Cache-Stufe aus dem Startbefehl (V-Cache, sonst K-Cache) bzw. BONSAI_KV4 – Standard f16."""
    v = re.findall(r"(?:-ctv|--cache-type-v)\s+(\S+)", command)
    k = re.findall(r"(?:-ctk|--cache-type-k)\s+(\S+)", command)
    if v or k:
        return (v or k)[-1].lower()
    return "q4_0" if str(env.get("BONSAI_KV4", "")).strip() in ("1", "true", "yes") else "f16"


def cache_bytes(command: str, env: dict) -> float:
    """Bytes pro KV-Element aus dem Startbefehl (-ctk/-ctv bzw. --cache-type-k/v) – Bonsai: BONSAI_KV4=1 → q4_0."""
    found = re.findall(r"(?:-ctk|-ctv|--cache-type-[kv])\s+(\S+)", command)
    if not found and str(env.get("BONSAI_KV4", "")).strip() in ("1", "true", "yes"):
        found = ["q4_0"]
    sizes = [type_bytes(t) for t in found] or [2.0]
    return sum(sizes) / len(sizes)


def estimate_from_gguf(path: str, ctx: int, command: str = "", env: dict | None = None) -> dict | None:
    """KV-Cache und Modellgröße aus der GGUF-Datei; Ablage im VRAM, außer der Befehl hält den Cache im RAM."""
    import os

    try:
        meta = gguf_metadata(path)
        size = os.path.getsize(path)
    except (OSError, ValueError, Exception):  # noqa: BLE001 – ungewöhnliche Datei: keine Schätzung
        return None
    per_token = kv_per_token_from_info(meta, cache_bytes(command, env or {}))
    if not per_token or not ctx:
        return None
    kv = per_token * ctx
    kv_in_ram = bool(re.search(r"(?:^|\s)(-nkvo|--no-kv-offload)\b", command))
    ngl = re.findall(r"(?:-ngl|--n-gpu-layers|--gpu-layers)\s+(\d+)", command)
    layers = next((v for k, v in meta.items() if k.endswith(".block_count")), 0)
    share = 1.0 if not ngl or not layers else min(1.0, int(ngl[-1]) / layers)
    train = next((v for k, v in meta.items() if k.endswith(".context_length")), None)
    return {"ctx": ctx, "ctx_train": train, "kv_per_token": per_token, "source": "estimate",
            "kv_vram": 0 if kv_in_ram else int(kv * share), "kv_ram": kv if kv_in_ram else int(kv * (1 - share)),
            "model_vram": int(size * share), "model_ram": int(size * (1 - share)),
            "model_ram_offload": int(size * (1 - share))}
