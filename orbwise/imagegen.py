"""Bilder erzeugen und bearbeiten mit Qwen-Image-2.1 über stable-diffusion.cpp (sd-server, eigene Prozessgruppe).

- Der Server wird erst beim ersten Bild gestartet und bleibt geladen, solange Bilder kommen (image.idle_minutes).
- Grafikspeicher: Auf kleinen Karten passen Sprachmodell und Bildmodell nicht gleichzeitig. Dann wird der
  llama-server des Sprachmodells vor dem ersten Bild beendet; braucht Orbwise das Sprachmodell wieder (Chat,
  Modellwechsel) oder ist der Bild-Modus eine Weile ungenutzt, gibt der Bildserver den Speicher frei und das
  Sprachmodell startet neu (LLMRouter.gpu_borrowed).
- Bilder landen in ~/Bilder/Orbwise (Prompt und Einstellungen stehen in den PNG-Metadaten und in einem Index).
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import logging
import random
import re
import time
from collections.abc import Awaitable, Callable
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx

from .config import ServerConfig
from .imagegen_setup import image_dir, is_set_up, manifest, model_paths, server_binary
from .lang import T

log = logging.getLogger(__name__)

SIZES = {"1:1": (1024, 1024), "4:3": (1152, 864), "3:4": (864, 1152), "16:9": (1344, 768), "9:16": (768, 1344),
         "3:2": (1216, 800), "2:3": (800, 1216)}
INDEX = ".orbwise-images.jsonl"
_STEP = re.compile(r"(\d+)/(\d+)\s*-\s*([\d.]+)\s*(s/it|it/s)")

Progress = Callable[[dict], Awaitable[None]]


class ImageError(RuntimeError):
    pass


def output_dir(cfg) -> Path:
    raw = cfg.image.output_dir
    if raw:
        return Path(raw).expanduser()
    home = Path.home()
    base = home / "Bilder" if (home / "Bilder").is_dir() else home / "Pictures"
    return base / "Orbwise"


def size_of(size: str) -> tuple[int, int]:
    """"16:9" oder "1024x768" → (Breite, Höhe), auf 32 gerundet (verlangt das Modell)."""
    if size in SIZES:
        return SIZES[size]
    m = re.fullmatch(r"\s*(\d{3,4})\s*[x×*]\s*(\d{3,4})\s*", size or "")
    if m:
        w, h = (max(256, min(2048, int(v) // 32 * 32)) for v in m.groups())
        return w, h
    return SIZES["1:1"]


def parse_progress(text: str) -> dict | None:
    """Letzter Fortschritt aus der sd.cpp-Ausgabe („ 5/20 - 3.21s/it“)."""
    found = _STEP.findall(text or "")
    if not found:
        return None
    step, total, rate, unit = found[-1]
    per_step = float(rate) if unit == "s/it" else (1 / float(rate) if float(rate) else 0)
    return {"step": int(step), "steps": int(total), "eta_s": round(per_step * (int(total) - int(step)), 1)}


class ImageEngine:
    def __init__(self, cfg, llm=None, gpu_info: Callable[[], dict | None] | None = None,
                 transport: httpx.AsyncBaseTransport | None = None):
        self.cfg = cfg
        self.llm = llm
        self.gpu_info = gpu_info or (lambda: None)
        self.transport = transport
        self.lock = asyncio.Lock()
        self.server = None  # ManagedServer
        self.stopped_llm = False  # haben wir das Sprachmodell für den Bildserver beendet?
        self.job: str | None = None
        self._idle: asyncio.TimerHandle | None = None
        self.started_at = 0.0

    # ---------- Zustand ----------
    @property
    def directory(self) -> Path:
        return image_dir(self.cfg)

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.cfg.image.port}"

    def available(self) -> bool:
        return is_set_up(self.directory)

    def can_edit(self) -> bool:
        vision = model_paths(self.directory).get("vision")
        return bool(vision and vision.exists())

    def vram_gb(self) -> float:
        gpu = self.gpu_info() or {}
        if gpu.get("vram_total"):
            return gpu["vram_total"] / 1024 ** 3
        return float(manifest(self.directory).get("vram_gb") or 0)

    def loaded(self) -> bool:
        return bool(self.server and self.server.running())

    def status(self) -> dict:
        return {"available": self.available(), "loaded": self.loaded(), "busy": self.lock.locked(),
                "can_edit": self.can_edit(), "sizes": list(SIZES), "size": self.cfg.image.size,
                "steps": self.cfg.image.steps, "cfg_scale": self.cfg.image.cfg_scale,
                "llm_unloaded": self.stopped_llm, "dir": str(self.directory)}

    # ---------- Server ----------
    def command(self) -> str:
        paths = model_paths(self.directory)
        parts = [str(server_binary(self.directory)), "--diffusion-model", str(paths["diffusion"]),
                 "--vae", str(paths["vae"]), "--llm", str(paths["llm"])]
        if paths.get("vision") and paths["vision"].exists():
            parts += ["--llm_vision", str(paths["vision"])]
        parts += ["--listen-ip", "127.0.0.1", "--listen-port", str(self.cfg.image.port), "--fa"]
        if self.cfg.image.extra_args.strip():
            parts += self.cfg.image.extra_args.split()
        else:
            vram = self.vram_gb()
            if vram < 16:  # Gewichte im RAM halten und nur bei Bedarf in den Grafikspeicher (lädt etwas langsamer)
                parts.append("--offload-to-cpu")
            if 0 < vram < 10:  # 8-GB-Karten: Text-Encoder auf der CPU, VAE in Kacheln
                parts += ["--clip-on-cpu", "--vae-tiling"]
        return " ".join(_quote(p) for p in parts)

    def _should_unload(self) -> bool:
        mode = (self.cfg.image.unload_llm or "auto").lower()
        if mode == "never":
            return False
        if mode == "always":
            return True
        vram = self.vram_gb()
        return vram == 0 or vram < 16

    async def _borrow_gpu(self, progress: Progress | None) -> None:
        """Sprachmodell aus dem Grafikspeicher nehmen (nur ein von Orbwise verwalteter llama-server bzw. Ollama)."""
        router = self.llm
        if router is None or not hasattr(router, "server_for") or not self._should_unload():
            return
        server = router.server_for(router.active)
        if server is not None and (server.running() or await server.healthy()):
            if progress:
                await progress({"phase": "unload_llm"})
            await server.stop()
            await server.stop_foreign()
            self.stopped_llm = True
        with contextlib.suppress(Exception):
            await router.ollama.unload_all()
        router.gpu_borrowed = self.release

    async def ensure(self, progress: Progress | None = None) -> None:
        if not self.available():
            raise ImageError(T("Der Bild-Modus ist noch nicht eingerichtet: orbwise model add qwen-image",
                               "Image mode is not set up yet: orbwise model add qwen-image"))
        from .llm_router import ManagedServer
        if self.server is None:
            bin_dir = str(server_binary(self.directory).parent)
            self.server = ManagedServer(
                ServerConfig(command=self.command(), env={"LD_LIBRARY_PATH": bin_dir}, cwd=bin_dir,
                             startup_timeout=self.cfg.image.startup_timeout,
                             health_url=f"{self.base_url}/sdcpp/v1/capabilities"),
                self.base_url, transport=self.transport)
        if await self.server.healthy():
            return
        await self._borrow_gpu(progress)
        if progress:
            await progress({"phase": "loading"})
        self.started_at = time.monotonic()

        async def say(text: str) -> None:
            if progress:
                await progress({"phase": "loading", "text": text})
        try:
            await self.server.ensure_running(say)
        except Exception as e:  # noqa: BLE001 – Sprachmodell zurückholen, Fehler weitergeben
            await self._release_now()
            raise ImageError(T(f"Bildserver startet nicht: {e}", f"Image server does not start: {e}")) from e

    async def release(self, restart: bool = True) -> None:
        """Bildserver beenden und – falls wir es beendet hatten – das Sprachmodell wieder starten. Läuft gerade ein
        Bild, wird es erst fertig gemalt (eine Chat-Frage wartet so lange, statt das Bild abzubrechen)."""
        async with self.lock:
            await self._release_now(restart)

    async def _release_now(self, restart: bool = True) -> None:
        if self._idle:
            self._idle.cancel()
            self._idle = None
        if self.server is not None:
            await self.server.stop()
        router = self.llm
        if router is not None and getattr(router, "gpu_borrowed", None) == self.release:
            router.gpu_borrowed = None
        if self.stopped_llm:
            self.stopped_llm = False
            if restart and router is not None:
                with contextlib.suppress(Exception):
                    await router._prepare(router.active, None)

    def _schedule_idle(self) -> None:
        if self._idle:
            self._idle.cancel()
        minutes = max(1, self.cfg.image.idle_minutes)
        loop = asyncio.get_running_loop()
        self._idle = loop.call_later(minutes * 60, lambda: loop.create_task(self._idle_release()))

    async def _idle_release(self) -> None:
        if not self.lock.locked():
            log.info("Bild-Modus ungenutzt – Bildmodell entladen")
            await self.release()

    # ---------- Erzeugen ----------
    async def generate(self, prompt: str, negative: str = "", size: str = "", steps: int = 0, cfg_scale: float = 0,
                       seed: int = -1, count: int = 1, ref_images: list[bytes] | None = None,
                       progress: Progress | None = None) -> list[dict]:
        prompt = (prompt or "").strip()
        if not prompt:
            raise ImageError(T("Bitte beschreiben, was auf das Bild soll.", "Please describe the image."))
        if ref_images and not self.can_edit():
            raise ImageError(T("Zum Bearbeiten fehlt die Bild-Erkennung des Text-Encoders (mmproj) – "
                               "orbwise model add qwen-image erneut ausführen.",
                               "Editing needs the text encoder's vision part (mmproj) – run orbwise model add "
                               "qwen-image again."))
        width, height = size_of(size or self.cfg.image.size)
        steps = max(1, min(80, steps or self.cfg.image.steps))
        cfg_scale = cfg_scale or self.cfg.image.cfg_scale
        seed = seed if seed is not None and seed >= 0 else random.randint(0, 2 ** 31 - 1)
        count = max(1, min(4, count or 1))
        async with self.lock:
            if self._idle:
                self._idle.cancel()
            await self.ensure(progress)
            started = time.monotonic()
            body: dict[str, Any] = {
                "prompt": prompt, "negative_prompt": negative or "", "width": width, "height": height,
                "seed": seed, "batch_count": count, "embed_image_metadata": True, "output_format": "png",
                "ref_images": [base64.b64encode(b).decode() for b in ref_images or []],
                "sample_params": {"sample_method": "euler", "sample_steps": steps,
                                  "guidance": {"txt_cfg": cfg_scale}},
            }
            if self.vram_gb() and self.vram_gb() < 10:
                body["vae_tiling_params"] = {"enabled": True}
            async with httpx.AsyncClient(base_url=self.base_url, timeout=60, transport=self.transport) as c:
                r = await c.post("/sdcpp/v1/img_gen", json=body)
                if r.status_code >= 400:
                    raise ImageError(T("Bildserver lehnt ab: ", "Image server refused: ") + r.text[:300])
                job = r.json()["id"]
                self.job = job
                result = await self._wait(c, job, progress)
            self.job = None
            seconds = round(time.monotonic() - started, 1)
            saved = self._save(result, {"prompt": prompt, "negative": negative, "width": width, "height": height,
                                        "steps": steps, "cfg_scale": cfg_scale, "seed": seed,
                                        "edit": bool(ref_images), "seconds": seconds})
            self._schedule_idle()
            return saved

    async def _wait(self, client: httpx.AsyncClient, job: str, progress: Progress | None) -> dict:
        from .llm_router import _log_path
        try:
            start = _log_path().stat().st_size
        except OSError:
            start = 0
        last = None
        while True:
            await asyncio.sleep(0.7)
            r = await client.get(f"/sdcpp/v1/jobs/{job}")
            if r.status_code == 404 or r.status_code == 410:
                raise ImageError(T("Der Auftrag ist verschwunden (Bildserver neu gestartet?).",
                                   "The job disappeared (image server restarted?)."))
            data = r.json()
            status = data.get("status")
            if status == "completed":
                return data.get("result") or {}
            if status in ("failed", "cancelled"):
                err = (data.get("error") or {}).get("message") or status
                raise ImageError(T("abgebrochen" if status == "cancelled" else f"Bild fehlgeschlagen: {err}",
                                   "cancelled" if status == "cancelled" else f"Image failed: {err}"))
            if progress:
                try:
                    with _log_path().open("rb") as f:
                        f.seek(start)
                        step = parse_progress(f.read()[-4000:].decode(errors="replace"))
                except OSError:
                    step = None
                state = {"phase": "queued" if status == "queued" else "generating", **(step or {})}
                if state != last:
                    last = state
                    await progress(state)

    async def cancel(self) -> bool:
        job = self.job
        if not job:
            return False
        with contextlib.suppress(httpx.HTTPError):
            async with httpx.AsyncClient(base_url=self.base_url, timeout=10, transport=self.transport) as c:
                await c.post(f"/sdcpp/v1/jobs/{job}/cancel")
        return True

    def _save(self, result: dict, meta: dict) -> list[dict]:
        folder = output_dir(self.cfg)
        folder.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        saved = []
        for i, img in enumerate(result.get("images") or []):
            data = base64.b64decode(img.get("b64_json") or "")
            if not data:
                continue
            seed = meta["seed"] + i
            path = folder / f"{stamp}-{seed}.png"
            path.write_bytes(data)
            entry = {**meta, "seed": seed, "file": path.name, "created": datetime.now().isoformat(timespec="seconds")}
            with (folder / INDEX).open("a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
            saved.append({**entry, "path": str(path)})
        if not saved:
            raise ImageError(T("Der Bildserver hat kein Bild geliefert.", "The image server returned no image."))
        return saved

    # ---------- Galerie ----------
    def list(self, limit: int = 60) -> list[dict]:
        folder = output_dir(self.cfg)
        try:
            lines = (folder / INDEX).read_text(encoding="utf-8").splitlines()
        except OSError:
            return []
        out, seen = [], set()
        for line in reversed(lines):
            try:
                e = json.loads(line)
            except ValueError:
                continue
            name = e.get("file", "")
            if name in seen or not (folder / name).exists():
                continue
            seen.add(name)
            out.append(e)
            if len(out) >= limit:
                break
        return out

    def path_of(self, name: str) -> Path | None:
        """Datei einer Galerie-Liste – nur Namen aus dem Bilderordner (kein Pfad von außen)."""
        if not re.fullmatch(r"[\w.-]+\.png", name or ""):
            return None
        path = output_dir(self.cfg) / name
        return path if path.is_file() else None

    def delete(self, name: str) -> bool:
        path = self.path_of(name)
        if not path:
            return False
        path.unlink()
        return True


def _quote(part: str) -> str:
    return part if re.fullmatch(r"[\w./:=+-]+", part) else "'" + part.replace("'", "'\\''") + "'"
