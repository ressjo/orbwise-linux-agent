"""Bild-Modus: Bilder erzeugen und verwalten, Bildmodelle (Katalog, Hugging Face, eigenes Backend), Bildserver."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import logging
import re
from pathlib import Path

import httpx
from fastapi import HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse

from ...lang import T

log = logging.getLogger(__name__)


def register(app, ctx) -> None:
    # gemeinsame Objekte und Helfer anderer Teile (siehe webapp/context.py)
    ImageError = ctx.ImageError
    agent = ctx.agent
    background = ctx.background
    cfg = ctx.cfg
    hub = ctx.hub
    images = ctx.images
    telegram_bot = ctx.telegram_bot

    # ---------- Bild-Modus (Qwen-Image-2.1 über stable-diffusion.cpp, siehe imagegen.py) ----------
    image_task: list[asyncio.Task] = []

    @app.get("/api/image/status")
    async def image_status():
        return {**images.status(), "telegram": bool(telegram_bot is not None and cfg.telegram.chat_id)}

    @app.get("/api/image/list")
    async def image_list(limit: int = 60):
        return {"images": images.list(limit)}

    @app.get("/api/image/file/{name}")
    async def image_file(name: str):
        path = images.path_of(name)
        if not path:
            raise HTTPException(404, "Bild nicht gefunden")
        return FileResponse(path, media_type="image/png", headers={"Cache-Control": "private, max-age=86400"})

    @app.delete("/api/image/file/{name}")
    async def image_delete(name: str):
        if not images.delete(name):
            raise HTTPException(404, "Bild nicht gefunden")
        return {"ok": True}

    @app.post("/api/image/generate")
    async def image_generate(request: Request):
        """Startet ein Bild im Hintergrund; Fortschritt und Ergebnis kommen per WebSocket (image_progress/_done)."""
        data = await request.json()
        if images.lock.locked():
            raise HTTPException(409, "Es wird gerade ein Bild erzeugt – bitte warten oder abbrechen.")
        if agent.busy():
            raise HTTPException(409, "Orbwise arbeitet gerade – bitte kurz warten.")
        # Vorlagen in Reihenfolge (Bild 1, 2, 3): {"file": Galerie-Name} oder {"data": data:-URL eines hochgeladenen Bilds}
        sources = list(data.get("refs") or [])
        sources += [{"file": n} for n in data.get("ref_files") or []] + [{"data": d} for d in data.get("ref_images") or []]
        refs: list[bytes] = []
        for src in sources:
            if not isinstance(src, dict):
                continue
            if src.get("file"):
                path = images.path_of(str(src["file"]))
                if not path:
                    raise HTTPException(400, f"Bild nicht gefunden: {src['file']}")
                refs.append(path.read_bytes())
            elif src.get("data"):
                try:
                    refs.append(base64.b64decode(str(src["data"]).split(",", 1)[-1]))
                except ValueError:
                    raise HTTPException(400, "Bild nicht lesbar") from None
        from ...imagegen import MAX_REFS
        if len(refs) > MAX_REFS or sum(len(r) for r in refs) > 40_000_000:
            raise HTTPException(400, f"Höchstens {MAX_REFS} Vorlagen bis zusammen 40 MB")

        async def progress(state: dict) -> None:
            await hub.broadcast({"type": "image_progress", **state})

        async def run() -> None:
            try:
                saved = await images.generate(
                    str(data.get("prompt") or ""), negative=str(data.get("negative") or ""),
                    size=str(data.get("size") or ""), steps=int(data.get("steps") or 0),
                    cfg_scale=float(data.get("cfg_scale") or 0), seed=int(-1 if data.get("seed") in (None, "") else data["seed"]),
                    count=int(data.get("count") or 1), ref_images=refs, progress=progress,
                    megapixels=float(data.get("megapixels") or 0))
                await hub.broadcast({"type": "image_done", "images": saved})
            except ImageError as e:
                await hub.broadcast({"type": "image_error", "text": str(e)})
            except Exception as e:  # noqa: BLE001
                log.exception("Bild fehlgeschlagen")
                await hub.broadcast({"type": "image_error", "text": str(e)})
        image_task[:] = [asyncio.create_task(run())]
        return JSONResponse({"started": True}, status_code=202)

    # ---------- Bildmodelle: auswählen, laden, löschen (Katalog: imagegen_setup.CATALOG) ----------
    image_install: dict = {"model": "", "stop": None, "text": "", "pct": None}

    runtime_update = [False]

    @app.post("/api/image/runtime/update")
    async def image_runtime_update():
        """sd-server auf den neuesten Build bringen (neuere Bildmodelle) – ein laufendes Bild wird erst fertig."""
        from ... import imagegen_setup
        if runtime_update[0]:
            raise HTTPException(409, T("Wird schon aktualisiert.", "Already updating."))
        runtime_update[0] = True
        lines: list[str] = []
        try:
            async with images.lock:
                await images.drop_server()
                directory = images.directory

                def run() -> bool:
                    with imagegen_setup._client() as client:
                        return imagegen_setup.install_binary(directory, client, lines.append, update=True)
                ok = await asyncio.to_thread(run)
        except httpx.HTTPError as e:
            ok, lines = False, [*lines, T(f"✘ GitHub nicht erreichbar: {e}", f"✘ GitHub not reachable: {e}")]
        finally:
            runtime_update[0] = False
        if not ok:
            raise HTTPException(502, (lines[-1] if lines else T("Aktualisieren fehlgeschlagen", "Update failed")).lstrip("✘ "))
        return {"ok": True, "version": imagegen_setup.runtime_version(images.directory), "text": lines[-1] if lines else ""}

    @app.get("/api/image/models")
    async def image_models():
        from ... import imagegen_setup
        vram = await asyncio.to_thread(images.vram_gb)
        items = await asyncio.to_thread(imagegen_setup.catalog, cfg, vram)
        home = str(Path.home())
        folder = str(images.directory)
        return {"models": items, "vram_gb": round(vram, 1),
                "runtime": {"installed": imagegen_setup.server_binary(images.directory).exists(),
                            "version": imagegen_setup.runtime_version(images.directory), "updating": runtime_update[0]},
                "dir": "~" + folder[len(home):] if folder.startswith(home + "/") else folder,
                "installing": image_install["model"], "text": image_install["text"], "pct": image_install["pct"]}

    def start_image_install(model_id: str, source: dict | None = None) -> dict:
        """Bildmodell im Hintergrund laden (Fortschritt per WebSocket: image_install) – danach ist es aktiv."""
        import threading

        from ... import imagegen_setup
        from ... import models as mdl
        if image_install["model"]:
            raise HTTPException(409, T("Es wird schon ein Bildmodell geladen.", "An image model is already downloading."))
        stop = threading.Event()
        image_install.update(model=model_id, stop=stop, text="", pct=None)
        loop = asyncio.get_running_loop()

        def out(line: str) -> None:
            m = re.search(r"\((\d+) %\)", line)
            image_install["text"], image_install["pct"] = line.strip(), int(m.group(1)) if m else None
            asyncio.run_coroutine_threadsafe(hub.broadcast({"type": "image_install", "model": model_id,
                                                            "text": line.strip(), "pct": image_install["pct"]}), loop)

        async def run() -> None:
            ok, cancelled = False, False
            try:
                gpu = await asyncio.to_thread(mdl.detect_gpu)
                gpu = {**gpu, "vram_gb": images.vram_gb() or gpu.get("vram_gb", 0)}  # eine Karte rechnet
                ok = await asyncio.to_thread(imagegen_setup.install, gpu, cfg, out, None, model_id, stop, source)
            except imagegen_setup.InstallCancelled:
                cancelled = True
            except Exception as e:  # noqa: BLE001 – melden statt hängen lassen
                log.exception("Bildmodell %s nicht geladen", model_id)
                out(f"✘ {e}")
            finally:
                last = image_install["text"]
                image_install.update(model="", stop=None, text="", pct=None)
            if ok:
                await images.select(model_id)  # neuer Startbefehl beim nächsten Bild
            await hub.broadcast({"type": "image_install", "model": model_id, "done": True, "ok": ok,
                                 "cancelled": cancelled, "text": last})
        background.append(asyncio.create_task(run()))
        return {"started": True, "model": model_id}

    @app.post("/api/image/models/{model_id}/install")
    async def image_model_install(model_id: str):
        from ... import imagegen_setup
        if model_id not in imagegen_setup.BY_ID:
            raise HTTPException(404, T("Unbekanntes Bildmodell", "Unknown image model"))
        return start_image_install(model_id)

    # ---------- Alle Text-zu-Bild-Modelle auf Hugging Face: suchen, Datei wählen, laden ----------
    @app.get("/api/image/hf/search")
    async def image_hf_search(q: str = "", sort: str = "downloads"):
        from ... import imagegen_setup
        try:
            return {"results": await asyncio.to_thread(imagegen_setup.hf_image_search, q, sort)}
        except (httpx.HTTPError, ValueError) as e:
            raise HTTPException(502, T(f"Hugging Face nicht erreichbar: {e}", f"Hugging Face not reachable: {e}")) from e

    @app.get("/api/image/hf/files")
    async def image_hf_files(repo: str):
        from ... import imagegen_setup
        try:
            return await asyncio.to_thread(imagegen_setup.hf_image_files, repo, images.vram_gb(), images.directory)
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
        except httpx.HTTPStatusError as e:
            if e.response.status_code in (401, 403):
                raise HTTPException(403, T(
                    "Zugangsbeschränktes Modell – Lizenz auf Hugging Face akzeptieren und unter Einstellungen → "
                    "Allgemein → Hugging Face ein Token eintragen.",
                    "Gated model – accept the license on Hugging Face and enter a token under Settings → General → "
                    "Hugging Face.")) from e
            raise HTTPException(404 if e.response.status_code == 404 else 502,
                                T("Repo nicht gefunden", "Repo not found")) from e
        except httpx.HTTPError as e:
            raise HTTPException(502, T(f"Hugging Face nicht erreichbar: {e}", f"Hugging Face not reachable: {e}")) from e

    @app.post("/api/image/hf/install")
    async def image_hf_install(request: Request):
        """{repo, file}: diese Datei als Bildmodell laden – Familie wird erkannt, Text-Encoder/VAE kommen dazu."""
        from ... import imagegen_setup
        data = await request.json()
        repo, file = str(data.get("repo") or ""), str(data.get("file") or "")
        try:
            listing = await asyncio.to_thread(imagegen_setup.hf_image_files, repo, images.vram_gb())
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
        except httpx.HTTPError as e:
            raise HTTPException(502, T(f"Hugging Face nicht erreichbar: {e}", f"Hugging Face not reachable: {e}")) from e
        if not listing["family"]:
            raise HTTPException(400, listing["reason"] or T("nicht unterstützt", "not supported"))
        variant = next((v for v in listing["variants"] if v["file"] == file), None)
        if variant is None:  # nur Dateien aus der eigenen Liste – kein beliebiger Pfad
            raise HTTPException(400, T("Diese Datei gibt es in dem Repo nicht.", "That file is not in the repo."))
        label = repo.split("/")[-1] + (f" {variant['quant']}" if variant["quant"] else "")
        return start_image_install(variant["id"], {"repo": repo, "file": file,
                                                   "family": variant.get("family") or listing["family"],
                                                   "label": label})

    # ---------- Eigenes Bild-Backend (Experten): OpenAI Images API, Automatic1111/Forge, externer sd-server ----------
    async def probe_image_backend(kind: str, base: str, key: str) -> dict:
        """Verbindung prüfen → {"ok", "models", "error"} (Modelle, soweit die Schnittstelle sie nennt)."""
        base = base.strip().rstrip("/")
        headers = {"Authorization": f"Bearer {key}"} if key else {}
        path = {"openai": "/models", "a1111": "/sdapi/v1/sd-models", "sdcpp": "/sdcpp/v1/capabilities"}.get(kind)
        if not path or not re.match(r"^https?://[^\s/]+", base):
            return {"ok": False, "models": [], "error": T("Adresse bitte mit http:// oder https://",
                                                          "Address please with http:// or https://")}
        try:
            async with httpx.AsyncClient(timeout=10) as c:
                r = await c.get(base + path, headers=headers)
        except httpx.HTTPError as e:
            return {"ok": False, "models": [], "error": T(f"nicht erreichbar: {e}", f"not reachable: {e}")}
        if r.status_code == 401:
            return {"ok": False, "models": [], "error": T("API-Schlüssel falsch oder fehlt", "API key wrong or missing")}
        if r.status_code >= 400:
            return {"ok": False, "models": [], "error": f"HTTP {r.status_code}: {r.text[:200]}"}
        try:
            data = r.json()
        except ValueError:
            data = {}
        models = [m.get("id", "") for m in data.get("data") or []] if kind == "openai" and isinstance(data, dict) else \
            [m.get("title") or m.get("model_name", "") for m in data] if kind == "a1111" and isinstance(data, list) else []
        return {"ok": True, "models": [m for m in models if m][:200], "error": ""}

    @app.post("/api/image/backend/probe")
    async def image_backend_probe(request: Request):
        data = await request.json()
        return await probe_image_backend(str(data.get("kind") or ""), str(data.get("base_url") or ""),
                                         str(data.get("api_key") or ""))

    @app.post("/api/image/backend")
    async def image_backend_add(request: Request):
        """Eigenes Bild-Backend eintragen und aktiv machen – Orbwise startet dort nichts (der Server läuft selbst)."""
        from ... import imagegen_setup, keystore
        data = await request.json()
        kind, base = str(data.get("kind") or ""), str(data.get("base_url") or "")
        label = str(data.get("label") or "").strip() or imagegen_setup.API_KINDS.get(kind, "Server")
        try:
            async with images.lock:  # ein laufendes Bild rechnet erst fertig
                await images.drop_server()  # immer nur ein Modell: der eigene Bildserver geht aus
                model_id = await asyncio.to_thread(imagegen_setup.add_api_model, cfg, label, kind, base,
                                                   str(data.get("model") or ""))
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
        key = str(data.get("api_key") or "").strip()
        await asyncio.to_thread(lambda: keystore.set("image_api_key:" + model_id, key) if key
                                else keystore.delete("image_api_key:" + model_id))
        return {"id": model_id, **images.status()}

    @app.delete("/api/image/models/install")
    async def image_model_install_cancel():
        stop = image_install.get("stop")
        if stop is not None:
            stop.set()  # die .part-Datei bleibt – erneut laden macht dort weiter
        return {"ok": stop is not None}

    @app.post("/api/image/models/{model_id}/activate")
    async def image_model_activate(model_id: str):
        from ... import imagegen_setup
        if not imagegen_setup.model_installed(images.directory, model_id):
            raise HTTPException(404, T("Dieses Bildmodell ist nicht installiert.", "This image model is not installed."))
        await images.select(model_id)
        return images.status()

    @app.delete("/api/image/models/{model_id}")
    async def image_model_delete(model_id: str):
        """Bildmodell löschen (gemeinsam genutzte Dateien bleiben). Läuft gerade ein Bild, wird es erst fertig."""
        from ... import imagegen_setup
        if model_id == image_install["model"]:
            raise HTTPException(409, T("Wird gerade geladen – erst abbrechen.", "Downloading right now – cancel first."))
        async with images.lock:
            if model_id == imagegen_setup.active_id(images.directory):
                await images.drop_server()
            freed = await asyncio.to_thread(imagegen_setup.remove_model, cfg, model_id)
        if model_id.startswith("api-"):  # Schlüssel eines eigenen Backends mit entfernen
            from ... import keystore
            with contextlib.suppress(Exception):
                await asyncio.to_thread(keystore.delete, "image_api_key:" + model_id)
        return {"freed_gb": round(freed / 1e9, 1), **images.status()}

    @app.post("/api/image/cancel")
    async def image_cancel():
        return {"ok": await images.cancel()}

    @app.post("/api/image/enter")
    async def image_enter():
        """Bild-Ansicht geöffnet: Sprachmodell jetzt entladen (im Hintergrund – eine laufende Antwort endet erst).
        Das passende Bildmodell lädt beim ersten Bild."""
        async def run() -> None:
            async with agent.lock:  # nie mitten in einer Antwort entladen
                unloaded = await images.enter()
            if unloaded:
                await hub.broadcast({"type": "image_llm_unloaded"})
        background.append(asyncio.create_task(run()))
        return {"ok": True}

    @app.post("/api/image/release")
    async def image_release():
        """Bild-Modus verlassen: Bildmodell entladen, Sprachmodell zurück (im Hintergrund; ein laufendes Bild wird
        erst fertig)."""
        background.append(asyncio.create_task(images.leave()))
        return {"ok": True}

    @app.post("/api/image/telegram/{name}")
    async def image_telegram(name: str):
        path = images.path_of(name)
        if not path:
            raise HTTPException(404, "Bild nicht gefunden")
        bot = agent.services.get("telegram")
        if bot is None:
            raise HTTPException(400, "Telegram ist nicht eingerichtet")
        try:
            await bot.send_file(path.read_bytes(), path.name)
        except Exception as e:  # noqa: BLE001
            raise HTTPException(502, f"Senden fehlgeschlagen: {bot.redact(e)}") from e
        return {"ok": True}
