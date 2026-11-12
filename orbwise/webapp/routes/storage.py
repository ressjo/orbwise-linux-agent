"""Speicherplatz (Einstellungen → Allgemein): was Orbwise belegt, und Zwischenspeicher & Co. leeren (siehe storage.py).
Modelle löscht die Oberfläche einzeln über /api/models/… und /api/image/models/… – die Einträge hier verweisen darauf."""

from __future__ import annotations

import asyncio
from pathlib import Path
from urllib.parse import quote

from fastapi import HTTPException

from ... import imagegen_setup, llamacpp, storage
from ... import models as mdl
from ...lang import T
from ...llm_router import LLMRouter


def register(app, ctx) -> None:
    agent = ctx.agent
    cfg = ctx.cfg
    images = ctx.images
    llm = ctx.llm
    state_file = ctx.state_file

    def model_items() -> list[dict]:
        if not isinstance(llm, LLMRouter):
            return []
        mdir = llamacpp.models_dir(cfg)
        seen_files: set[str] = set()
        out = []
        for name, p in llm.profiles.items():
            rec = llamacpp.local_record(state_file, name)
            removable = mdl.removable(state_file, name)
            label = p.label or name
            if rec is not None:
                files = [f for f in dict.fromkeys(rec.get("files") or []) if f not in seen_files and Path(f).is_file()]
                seen_files.update(files)
                if not files:
                    continue
                own = not rec.get("downloaded")
                out.append({**storage._item(
                    label, sum(Path(f).stat().st_size for f in files), active=name == llm.active,
                    delete="" if own or not removable else f"/api/models/{quote(name, safe='')}",
                    note="Eigene Datei – bleibt beim Entfernen liegen." if own else "",
                    note_en="Your own file – stays when removed." if own else "", path=Path(files[0])),
                    "in_models_dir": all(storage._inside(Path(f), mdir) for f in files)})
        return sorted(out, key=lambda i: -i["bytes"])

    def image_items(directory: Path) -> list[dict]:
        data = imagegen_setup.manifest(directory)
        active = imagegen_setup.active_id(directory)
        seen: set[str] = set()
        out = []
        for mid, entry in (data.get("models") or {}).items():
            if imagegen_setup.is_api(entry):
                continue
            size = 0
            for name in (entry.get("files") or {}).values():
                path = directory / "models" / name
                if name not in seen and path.is_file():
                    seen.add(name)
                    size += path.stat().st_size
            spec = imagegen_setup.spec_of(directory, mid)
            out.append(storage._item(spec.label if spec else mid, size, active=mid == active,
                                     delete=f"/api/image/models/{quote(mid, safe='')}"))
        return sorted(out, key=lambda i: -i["bytes"])

    def image_dir() -> Path:
        return images.directory if images is not None else imagegen_setup.image_dir(cfg)

    @app.get("/api/storage")
    async def storage_overview():
        def build() -> dict:
            directory = image_dir()
            return storage.overview(cfg, models=model_items(), images=image_items(directory),
                                    image_dir=directory)
        return await asyncio.to_thread(build)

    @app.delete("/api/storage/cache")
    async def storage_cache_clear():
        from ...tools import jobs
        busy = {j.log for j in jobs.JOBS.values() if j.log}  # Protokolle dieser Sitzung (job_log liest sie)
        return {"freed": await asyncio.to_thread(storage.clear_cache, busy)}

    @app.delete("/api/storage/llamacpp-old")
    async def storage_llamacpp_old():
        models_api = getattr(ctx, "models_api", None)
        if models_api is not None and models_api.llamacpp_busy():
            raise HTTPException(409, T("llama.cpp wird gerade eingerichtet – bitte warten.",
                                       "llama.cpp is being set up right now – please wait."))
        return {"freed": await asyncio.to_thread(storage.remove_old_llamacpp, cfg)}

    @app.delete("/api/storage/browser")
    async def storage_browser():
        return {"freed": await asyncio.to_thread(storage.remove_browser)}

    @app.delete("/api/storage/snapshots")
    async def storage_snapshots():
        if agent.lock.locked():  # gerade könnte ein Sicherungspunkt entstehen
            raise HTTPException(409, T("Orbwise arbeitet gerade – bitte warten, bis die Antwort fertig ist.",
                                       "Orbwise is working right now – please wait until the reply is done."))
        return {"freed": await asyncio.to_thread(storage.clear_snapshots)}
