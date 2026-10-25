"""Modellverwaltung im Web-Server: Modellliste, Hinzufügen (Ollama-Vorauswahl, Hugging Face, Dateien in ~/models),
Downloads als Hintergrund-Aufgaben, llama.cpp-Build, Laden/Entladen/Löschen und die Einstellungen je Modell
(Kontextfenster, Sampling, KV-Cache, Batch-Größe, spekulatives Decoding, Messen der schnellsten Einstellung).

Aus server.create_app ausgelagert – `register` hängt die Endpunkte an die App, wie api_server.register."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import tarfile
import time
import zipfile
from pathlib import Path
from types import SimpleNamespace

import httpx
from fastapi import FastAPI, HTTPException, Request

from . import metrics
from .lang import T
from .llm import LLMError
from .llm_router import LLMRouter

log = logging.getLogger(__name__)


def register(app: FastAPI, *, cfg, llm, agent, memory, hub, state_file: Path, background: list, model_progress,
             idle_if_free, mode_info, mode_models, remember_mode_model) -> SimpleNamespace:
    """Endpunkte /api/models*, /api/llamacpp*, /api/llm/memory und /api/context/test anlegen → was server.py weiter
    braucht (activate_model für den Moduswechsel)."""

    def router_profile(name: str):
        """Einstellungen je Modell gibt es nur mit dem Router (nicht im Demo-Modus) und für bekannte Profile."""
        if not isinstance(llm, LLMRouter):
            raise HTTPException(400, "Im Demo-Modus nicht verfügbar")
        if name not in llm.profiles:
            raise HTTPException(404, "Unbekanntes Profil")
        return llm.profiles[name]

    @app.get("/api/models")
    async def get_models():
        if not isinstance(llm, LLMRouter):
            return {"active": "demo", "switching": None,
                    "profiles": [{"name": "demo", "label": "Demo (Fake-LLM)", "backend": "fake", "model": "fake",
                                  "base_url": "", "managed": False, "active": True}]}
        from . import llamacpp as llamacpp_mod
        from . import llamacpp_tune
        from . import models as mdl
        profiles = llm.describe()
        sizes: dict[str, int] = {}
        # Größen nur von Ollama-Servern holen, die ein Profil wirklich nutzt (llama-server kennt /api/tags nicht)
        for base in {p.get("base_url") or cfg.llm.base_url for p in profiles if p["backend"] == "ollama"}:
            try:
                async with httpx.AsyncClient(base_url=base, timeout=2) as client:
                    for m in (await client.get("/api/tags")).json().get("models", []):
                        sizes.setdefault(m.get("name", ""), int(m.get("size") or 0))
            except (httpx.HTTPError, ValueError):
                pass
        added = mdl.added_profiles(state_file)  # state.json nur einmal lesen
        added_slugs = {mdl.slug(t) for t in mdl.added_models(state_file)}
        gpu = await gpu_info()
        for p in profiles:
            p["deletable"] = p["name"] in added or p["name"] in added_slugs
            size = sizes.get(p["model"]) or sizes.get(f"{p['model']}:latest") if p["backend"] == "ollama" else None
            p["engine"] = "Ollama" if p["backend"] == "ollama" else "llama.cpp" if p["managed"] else "OpenAI-API"
            files: list[str] = []
            if (local := llamacpp_mod.local_record(state_file, p["name"])) is not None:
                files = [f for f in local.get("files", []) if Path(f).is_file()]
                p["keeps_files"] = not local.get("downloaded")
                p["repo"] = local.get("repo") or ""
            if files:
                size = sum(Path(f).stat().st_size for f in files)
                weights = [f for f in files if not mdl.is_sidecar(Path(f).name)]
                p["path"] = llamacpp_mod._tilde(weights[0] if weights else files[0])
                p["vision"] = len(weights) < len(files)
                q = mdl.QUANT_RE.search(mdl.SPLIT_RE.sub(".gguf", Path(weights[0]).name)) if weights else None
                p["quant"] = q.group(1) if q else None
            elif p["backend"] == "ollama":
                q = re.search(r":((?:UD-)?I?Q\d\w*|[bf]f16)$", p["model"], re.I)
                p["quant"] = q.group(1) if q else None
            p["size_gb"] = round(size / 1e9, 1) if size else None
            if size:
                p["vram_gb"] = mdl.need_gb(round(size / 1e9, 1))
                p["fit"] = mdl.fit_need(p["vram_gb"], gpu["vram_gb"]) if gpu["vram_gb"] else None
            p["prefill_tps"] = agent.prefill_speed(p["name"])
            server = llm.profiles[p["name"]].server
            if server and llamacpp_tune.uses_own_build(cfg, server.command):  # „Schnellste Einstellung messen“
                p["tunable"] = True
                p["tuning"] = llamacpp_tune.saved(state_file, p["name"])
        return {"active": llm.active, "switching": llm.switching, "unloaded": llm.unloaded, "profiles": profiles, "gpu": gpu,
                "pulls": [{"tag": t, **pull_state.get(t, {})} for t in sorted(pulls)]}

    gpu_cache: dict = {}

    async def gpu_info() -> dict:
        """Grafikkarte einmal erkennen (nvidia-smi/sysfs) – ändert sich nicht, solange Orbwise läuft."""
        if not gpu_cache:
            from . import models as mdl
            gpu_cache.update(await asyncio.to_thread(mdl.detect_gpu))
        return gpu_cache

    @app.get("/api/models/log")
    async def model_server_log(lines: int = 200):
        """Ausgabe des letzten Modell-Server-Starts (llama-server) – zum Nachsehen, warum etwas nicht lädt."""
        from .llm_router import _log_path
        try:
            text = (await asyncio.to_thread(_log_path().read_text, errors="replace"))[-200_000:]
        except OSError:
            return {"lines": [], "file": "~/.local/state/orbwise-llm.log"}
        last = text.rsplit("===== Starte:", 1)
        body = ("===== Starte:" + last[1]) if len(last) == 2 else text
        body = re.sub(r"(--api-key\s+)\S+", r"\1•••", body)
        return {"lines": body.splitlines()[-max(1, min(lines, 2000)):], "file": "~/.local/state/orbwise-llm.log"}

    # ---------- Modelle hinzufügen (Vorauswahl + ollama pull mit Fortschritt) ----------
    pulls: dict[str, asyncio.Task] = {}
    pull_state: dict[str, dict] = {}  # letzter Fortschritt je Download (für das Modell-Menü)

    @app.get("/api/models/presets")
    async def model_presets():
        from . import models as mdl
        gpu = await asyncio.to_thread(mdl.detect_gpu)
        installed = await asyncio.to_thread(mdl.installed_models, cfg.llm.base_url)
        return {"gpu": gpu, "presets": mdl.preset_list(gpu["vram_gb"], installed),
                "pulling": sorted(pulls)}

    @app.get("/api/models/hf/search")
    async def hf_search(q: str = ""):
        from . import models as mdl
        if not q.strip():
            return {"results": []}
        tag = mdl.normalize_tag(q)
        if tag and mdl.is_hf(tag):  # Link eingefügt → gleich dieses Repo
            return {"results": [{"repo": tag[6:].split(":", 1)[0], "tag": tag if ":" in tag else None}]}
        try:
            return {"results": await asyncio.to_thread(mdl.hf_search, q)}
        except (httpx.HTTPError, ValueError) as e:
            raise HTTPException(502, f"Hugging Face nicht erreichbar: {e}") from e

    @app.get("/api/models/hf/quants")
    async def hf_quants(repo: str):
        from . import llamacpp
        from . import models as mdl
        gpu = await asyncio.to_thread(mdl.detect_gpu)
        try:
            items = await asyncio.to_thread(mdl.hf_quants, repo, gpu["vram_gb"], llamacpp.models_dir(cfg))
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
        except httpx.HTTPStatusError as e:
            raise HTTPException(404 if e.response.status_code in (401, 404) else 502,
                                "Repo nicht gefunden oder nicht öffentlich") from e
        except httpx.HTTPError as e:
            raise HTTPException(502, f"Hugging Face nicht erreichbar: {e}") from e
        return {"repo": repo, "gpu": gpu, "quants": items, "pulling": sorted(pulls)}

    # ---------- Eigene Modelle über llama.cpp (Hugging Face, ~/models) ----------
    llamacpp_lock = asyncio.Lock()
    add_lock = asyncio.Lock()  # Profile nacheinander anlegen – sonst bekämen zwei fertige Downloads denselben Port

    def hf_base() -> str:
        from . import models as mdl
        return mdl.HF_API[:-4] if mdl.HF_API.endswith("/api") else "https://huggingface.co"

    async def pull_event(tag: str, **data) -> None:
        pull_state[tag] = data
        await hub.broadcast({"type": "model_pull", "tag": tag, **data})

    async def ensure_llamacpp(tag: str, gpu: dict, update: bool = False) -> dict:
        """llama.cpp einrichten (einmalig) bzw. aktualisieren – Statuszeilen gehen als model_pull-Fortschritt raus."""
        from . import llamacpp
        async with llamacpp_lock:
            if llamacpp.installed(cfg) and not update:
                return llamacpp.manifest(cfg)
            loop = asyncio.get_running_loop()
            lines: list[str] = []

            def out(line: str) -> None:
                lines.append(line)
                asyncio.run_coroutine_threadsafe(pull_event(tag, status=line.strip()), loop)
            await pull_event(tag, status="llama.cpp wird eingerichtet …")
            try:
                result = await asyncio.to_thread(llamacpp.install, cfg, gpu, out, update)
            except (httpx.HTTPError, OSError, ValueError, zipfile.BadZipFile, tarfile.TarError) as e:
                raise LLMError(f"llama.cpp konnte nicht geladen werden: {e}") from e
            if not result:
                raise LLMError("llama.cpp konnte nicht eingerichtet werden" + (f": {lines[-1].strip()}" if lines else ""))
            return result

    async def run_job(tag: str, job) -> None:
        """Download/Einrichtung als Hintergrund-Aufgabe mit model_pull-Events (Fortschritt, fertig, Fehler, Abbruch)."""
        try:
            result = await job()
            await hub.broadcast({"type": "model_pull", "tag": tag, "done": True, **result})
        except asyncio.CancelledError:
            await hub.broadcast({"type": "model_pull", "tag": tag, "cancelled": True})
            raise
        except (httpx.HTTPError, LLMError, ValueError, OSError) as e:
            text = str(e)
            if isinstance(e, httpx.HTTPStatusError) and e.response.status_code in (401, 403):
                text = "Hugging Face verlangt eine Anmeldung für dieses Modell (HF_TOKEN setzen)"
            await hub.broadcast({"type": "model_pull", "tag": tag, "error": text or type(e).__name__})
        except Exception as e:  # noqa: BLE001 – unerwartet: trotzdem melden, sonst hinge die Anzeige
            log.exception("Modell-Aufgabe %s fehlgeschlagen", tag)
            await hub.broadcast({"type": "model_pull", "tag": tag, "error": f"{type(e).__name__}: {e}"})
        finally:
            pulls.pop(tag, None)
            pull_state.pop(tag, None)

    async def pull_gguf(tag: str) -> dict:
        """hf.co/<repo>:<quant> → Dateien nach ~/models laden, llama.cpp bereitstellen, Profil anlegen."""
        from . import llamacpp
        from . import models as mdl
        repo, _, quant = tag[6:].partition(":")
        gpu = await asyncio.to_thread(mdl.detect_gpu)
        items = await asyncio.to_thread(mdl.hf_quants, repo, gpu["vram_gb"], llamacpp.models_dir(cfg))
        item = next((x for x in items if x["quant"].lower() == quant.lower()), None) if quant else \
            next((x for x in items if x.get("recommended")), items[0] if items else None)
        if not item:
            raise LLMError(f"Keine GGUF-Variante {quant} in {repo} gefunden".replace("  ", " "))
        await ensure_llamacpp(tag, gpu)
        last = 0.0

        async def progress(done: int, total: int) -> None:
            nonlocal last
            now = time.monotonic()
            if now - last > 0.5 or done >= total:
                last = now
                await pull_event(tag, status="downloading", completed=done, total=total)
        first, mmproj, _ = await llamacpp.download_quant(cfg, repo, item, progress, hf_base())
        async with add_lock:
            name = await asyncio.to_thread(llamacpp.add_model, cfg, state_file, first, mmproj, gpu, llm.profiles,
                                           True, repo)
            llm.add_downloaded_models()
        return {"profile": name}

    async def add_local(tag: str, path: Path) -> dict:
        from . import llamacpp
        from . import models as mdl
        gpu = await asyncio.to_thread(mdl.detect_gpu)
        await ensure_llamacpp(tag, gpu)
        async with add_lock:
            name = await asyncio.to_thread(llamacpp.add_model, cfg, state_file, path, None, gpu, llm.profiles, False)
            llm.add_downloaded_models()
        return {"profile": name}

    def start_job(tag: str, job) -> str:
        existing = next((k for k in pulls if k.lower() == tag.lower()), None)
        if existing:
            return existing
        pulls[tag] = asyncio.create_task(run_job(tag, job))
        return tag

    @app.get("/api/models/local")
    async def models_local():
        """GGUF-Dateien in ~/models (llm.models_dir) – mit dem Profil, das sie schon nutzt."""
        from . import llamacpp
        from . import models as mdl
        gpu = await asyncio.to_thread(mdl.detect_gpu)
        profiles = llm.profiles if isinstance(llm, LLMRouter) else {}
        items = await asyncio.to_thread(llamacpp.scan, cfg, profiles, gpu["vram_gb"])
        return {"dir": llamacpp._short(llamacpp.models_dir(cfg)), "items": items, "gpu": gpu, "pulling": sorted(pulls)}

    @app.post("/api/models/local")
    async def models_local_add(request: Request):
        from . import llamacpp
        from . import models as mdl
        if not isinstance(llm, LLMRouter):
            raise HTTPException(400, "Im Demo-Modus nicht verfügbar")
        raw = str((await request.json()).get("path", "")).strip()
        path = Path(os.path.expanduser(raw))
        if not raw or not path.is_file() or not path.name.lower().endswith(".gguf") or mdl.is_sidecar(path.name):
            raise HTTPException(400, "Keine GGUF-Modelldatei gefunden: " + raw[:200])
        tag = start_job(llamacpp.label_for(path), lambda: add_local(llamacpp.label_for(path), path))
        return {"ok": True, "tag": tag}

    @app.get("/api/llamacpp")
    async def llamacpp_status(check: bool = False, force: bool = False):
        """Installierter Build; mit check: auch die neueste Version (llama.cpp und ROCm, je 3 h gemerkt).
        ROCm wird nur angezeigt – es ist ein Systempaket mit Treibern, das aktualisiert der Nutzer selbst."""
        from . import llamacpp
        from . import models as mdl
        m = llamacpp.manifest(cfg)
        gpu = await asyncio.to_thread(mdl.detect_gpu)
        flavors = [f for f in llamacpp.flavors_for(gpu) if f != "cpu"]
        out = {"installed": llamacpp.installed(cfg), "tag": m.get("tag"), "flavor": m.get("flavor"),
               "updating": "llama.cpp" in pulls,
               # Schalter ROCm/Vulkan bzw. CUDA/Vulkan – ready: schon geladen (sonst lädt das Umschalten ihn erst)
               "flavors": [{"flavor": f, "ready": f == m.get("flavor") or llamacpp.flavor_ready(cfg, f)}
                           for f in flavors] if len(flavors) > 1 else []}
        latest = await asyncio.to_thread(llamacpp.latest_tag, gpu, None, force) if check else None
        if check:
            out["latest"] = latest
            out["update"] = bool(latest and latest != m.get("tag") and _build_no(latest) > _build_no(m.get("tag")))
        if gpu.get("vendor") == "amd" or llamacpp.rocm_version():
            rocm_latest = await asyncio.to_thread(llamacpp.latest_rocm, None, force) if check else None
            out["rocm"] = llamacpp.rocm_status(cfg, rocm_latest)
        return out

    def _build_no(tag) -> int:
        m = re.search(r"\d+", str(tag or ""))
        return int(m.group(0)) if m else 0

    @app.post("/api/llamacpp/update")
    async def llamacpp_update():
        """Neuesten llama.cpp-Build laden; ein geladenes eigenes Modell startet danach gleich mit ihm neu."""
        from . import llamacpp
        from . import models as mdl

        async def job() -> dict:
            before = llamacpp.manifest(cfg).get("dir")
            gpu = await asyncio.to_thread(mdl.detect_gpu)
            m = await ensure_llamacpp("llama.cpp", gpu, update=True)
            changed = m.get("dir") != before
            restart = changed and reload_own_model()
            return {"kind": "llamacpp", "version": m.get("tag"), "flavor": m.get("flavor"), "changed": changed,
                    "restarted": restart}
        start_job("llama.cpp", job)
        return {"ok": True}

    async def pull_model(tag: str) -> None:
        from . import models as mdl
        last = 0.0
        try:
            async with httpx.AsyncClient(base_url=cfg.llm.base_url, timeout=None) as client:
                async with client.stream("POST", "/api/pull", json={"model": tag, "stream": True}) as resp:
                    if resp.status_code != 200:
                        raise LLMError(f"Ollama {resp.status_code}: {(await resp.aread()).decode(errors='replace')[:200]}")
                    async for line in resp.aiter_lines():
                        if not line.strip():
                            continue
                        ev = json.loads(line)
                        if ev.get("error"):
                            raise LLMError(ev["error"])
                        now = time.monotonic()
                        if now - last > 0.5 or ev.get("status") == "success":
                            last = now
                            pull_state[tag] = {"status": ev.get("status", ""), "completed": ev.get("completed"),
                                               "total": ev.get("total")}
                            await hub.broadcast({"type": "model_pull", "tag": tag, **pull_state[tag]})
            name = mdl.register_model(state_file, tag)
            if isinstance(llm, LLMRouter):
                llm.add_downloaded_models()
            await hub.broadcast({"type": "model_pull", "tag": tag, "done": True, "profile": name})
        except asyncio.CancelledError:
            # Stream geschlossen → Ollama bricht ab; Teilstücke bleiben im Ollama-Cache (erneuter Start setzt fort)
            await hub.broadcast({"type": "model_pull", "tag": tag, "cancelled": True})
            raise
        except (httpx.HTTPError, LLMError, ValueError) as e:
            text = str(e) if not isinstance(e, httpx.ConnectError) else "Ollama ist nicht erreichbar"
            await hub.broadcast({"type": "model_pull", "tag": tag, "error": text})
        finally:
            pulls.pop(tag, None)
            pull_state.pop(tag, None)

    @app.post("/api/models/pull")
    async def model_pull(request: Request):
        from . import models as mdl
        if not isinstance(llm, LLMRouter):
            raise HTTPException(400, "Im Demo-Modus nicht verfügbar")
        body = await request.json()
        tag = mdl.normalize_tag(str(body.get("tag", "")))
        if not tag:
            raise HTTPException(400, "Ungültiger Modellname oder Hugging-Face-Link (geteilte GGUF-Dateien gehen nicht)")
        if mdl.is_hf(tag):  # Hugging Face: Orbwise lädt die GGUF-Datei selbst und startet sie mit llama.cpp
            return {"ok": True, "tag": start_job(tag, lambda: pull_gguf(tag))}
        if tag not in pulls:
            pulls[tag] = asyncio.create_task(pull_model(tag))
        return {"ok": True, "tag": tag}

    @app.delete("/api/models/pull/{tag:path}")
    async def model_pull_cancel(tag: str):
        task = pulls.get(tag) or next((t for k, t in pulls.items() if k.lower() == tag.lower()), None)
        if task is None:
            raise HTTPException(404, "Kein laufender Download")
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        return {"ok": True}

    @app.delete("/api/models/{name}")
    async def model_delete(name: str):
        """Per Oberfläche/CLI hinzugefügtes Modell entfernen und seine Dateien löschen (gibt Speicher frei)."""
        from . import llamacpp as llamacpp_mod
        from . import models as mdl
        if not isinstance(llm, LLMRouter):
            raise HTTPException(400, "Im Demo-Modus nicht verfügbar")
        if name not in llm.profiles:
            raise HTTPException(404, "Unbekanntes Modell")
        if not mdl.removable(state_file, name):
            raise HTTPException(400, "Dieses Modell ist in der config.yaml eingetragen – dort entfernen.")
        profile = llm.profiles[name]
        if llm.switching:
            raise HTTPException(409, T("Gerade wird ein Modell geladen – bitte kurz warten.",
                                       "A model is loading right now – please wait a moment."))
        if profile.backend == "ollama" and profile.model in pulls:
            raise HTTPException(409, "Das Modell wird gerade geladen – erst den Download abbrechen.")
        if name == llm.active:  # erst entladen, ein anderes wählen (ohne es zu laden), dann löschen
            others = [n for n in llm.profiles if n != name]
            if not others:
                raise HTTPException(409, T("Das ist das einzige Modell – erst ein anderes hinzufügen.",
                                           "This is the only model – add another one first."))
            remembered = [n for n in mode_models().values() if n in others]
            async with agent.lock:  # eine laufende Antwort läuft zu Ende
                await llm.unload()
                await llm.select((remembered or others)[0])
            agent._cache_owner = None
            await hub.broadcast({"type": "model_active", "name": llm.active, "unloaded": True})
        await llm.remove_profile(name)
        mdl.unregister_model(state_file, name)
        freed = 0
        if llamacpp_mod.local_record(state_file, name) is not None:  # nur selbst geladene Dateien löschen
            freed = await asyncio.to_thread(llamacpp_mod.forget, cfg, state_file, name)
        elif profile.backend == "ollama" and not any(
                p.backend == "ollama" and p.model == profile.model for p in llm.profiles.values()):
            try:  # nur löschen, wenn kein anderes Profil dasselbe Ollama-Modell nutzt
                async with httpx.AsyncClient(base_url=cfg.llm.base_url, timeout=30) as client:
                    await client.request("DELETE", "/api/delete", json={"model": profile.model})
            except httpx.HTTPError as e:
                log.warning("Ollama-Modell %s nicht gelöscht: %s", profile.model, e)
        await hub.broadcast({"type": "models_changed"})
        return {"ok": True, "freed_gb": round(freed / 1e9, 1), "active": llm.active}

    @app.post("/api/models/unload")
    async def model_unload():
        """Aktives Modell aus dem Grafikspeicher nehmen – die nächste Frage lädt es wieder."""
        if not isinstance(llm, LLMRouter):
            raise HTTPException(400, "Im Demo-Modus nicht verfügbar")
        if llm.switching:
            raise HTTPException(409, T("Gerade wird ein Modell geladen – bitte kurz warten.",
                                       "A model is loading right now – please wait a moment."))
        async with agent.lock:  # eine laufende Antwort läuft zu Ende
            await llm.unload()
        agent._cache_owner = None  # Server aus = Prompt-Cache weg
        await hub.broadcast({"type": "model_unloaded", "name": llm.active})
        await hub.broadcast({"type": "models_changed"})
        await idle_if_free()
        return {"ok": True, "active": llm.active}

    @app.post("/api/models/reload")
    async def models_reload():
        added = llm.add_downloaded_models() if isinstance(llm, LLMRouter) else []
        return {"added": added}

    @app.post("/api/models/{name}/activate")
    async def activate_model(name: str):
        router_profile(name)
        await hub.broadcast({"type": "model_switching", "name": name, "label": llm.profiles[name].label})
        llm.switching = name
        try:
            async with agent.lock:  # wartet, bis eine laufende Antwort fertig ist
                await llm.activate(name, model_progress)
        except LLMError as e:
            await hub.broadcast({"type": "model_error", "name": name, "text": str(e)})
            raise HTTPException(502, str(e)) from e
        finally:
            llm.switching = None
            await idle_if_free()
        await hub.broadcast({"type": "model_active", "name": name})
        remember_mode_model(mode_info()["mode"], name)  # gilt ab jetzt für diesen Modus
        agent._cache_owner = None  # neues Modell = leerer Cache → offenen Chat gleich einlesen
        hub.prewarm_soon()
        return {"ok": True, "active": llm.active}

    @app.get("/api/llm/memory")
    async def llm_memory():
        """Wo liegt der Kontext (VRAM/RAM), und wie groß darf das Kontextfenster werden?"""
        from . import llm_memory
        gpu = (await asyncio.to_thread(metrics.collect)).get("gpu")
        if not isinstance(llm, LLMRouter):
            return {"available": False}
        info, reason = await llm.memory_info()
        if not info:
            return {"available": False, "profile": llm.active, "ctx": llm.context_size, "reason": reason,
                    "managed": llm.profile.server is not None, "backend": llm.profile.backend}
        return {"available": True, "profile": llm.active, "backend": llm.profile.backend,
                **llm_memory.summary(info), "recommend": llm_memory.recommend(info, gpu), "gpu": gpu}

    @app.post("/api/context/test")
    async def context_test(quick: bool = False):
        """Kontext-Selbsttest gegen das aktive Modell (siehe context_test.py) – der offene Chat bleibt unverändert."""
        from .context_test import run_context_test
        if not isinstance(llm, LLMRouter):
            raise HTTPException(400, "Im Demo-Modus nicht verfügbar")
        if agent.busy() or llm.switching:
            raise HTTPException(409, "Gerade läuft eine Anfrage oder ein Modellwechsel – bitte gleich noch einmal.")
        gpu = (await asyncio.to_thread(metrics.collect)).get("gpu")
        async with agent.lock:
            result = await run_context_test(agent, llm, quick=quick, gpu=gpu)
        agent._cache_owner = None  # der Test hat den Cache des Servers belegt → offenen Chat neu einlesen
        hub.prewarm_soon()
        return result

    async def restart_with(name: str, change, live: bool | None = None, show_context: bool = False,
                           status=lambda e: 502) -> bool:
        """Einstellung ändern (`change`, wartet auf eine laufende Antwort). Ist das Modell gerade geladen, startet sein
        Server damit neu: die Oberfläche zeigt „lädt …“, danach ist der Prompt-Cache leer und wird vorgewärmt.
        → ob das Modell geladen war."""
        active = (name == llm.active and not llm.unloaded) if live is None else live
        if active:
            await hub.broadcast({"type": "model_switching", "name": name, "label": llm.profiles[name].label})
            llm.switching = name
        try:
            async with agent.lock:
                await change()
        except LLMError as e:
            await hub.broadcast({"type": "model_error", "name": name, "text": str(e)})
            raise HTTPException(status(e), str(e)) from e
        finally:
            llm.switching = None
            await idle_if_free()
        if active:
            await hub.broadcast({"type": "model_active", "name": name})
            agent._cache_owner = None  # neu gestartet = leerer Cache
            if show_context:  # Kontext-Kachel: neue Fenstergröße und Komprimierungspunkt sofort zeigen
                agent.build_messages()
                await hub.broadcast({"type": "context", **(agent.last_context or {})})
            hub.prewarm_soon()
        await hub.broadcast({"type": "models_changed"})
        return active

    @app.post("/api/models/{name}/context")
    async def set_model_context(name: str, request: Request):
        router_profile(name)
        body = await request.json()
        try:
            ctx = int(body.get("ctx", 0))
        except (TypeError, ValueError):
            raise HTTPException(400, "ctx fehlt") from None
        mode = str(body.get("mode") or llm.mode)
        if mode not in ("tools", "coding"):
            raise HTTPException(400, "Unbekannter Modus")
        if ctx == 0 and mode != "coding":
            raise HTTPException(400, "ctx fehlt")
        live = name == llm.active and mode == llm.mode and not llm.unloaded

        async def change() -> None:
            await llm.set_context(name, ctx, model_progress, mode=mode)
            if live:
                await llm.warm()  # Ollama: gleich mit der neuen Größe laden
        active = await restart_with(name, change, live=live, show_context=True,
                                    status=lambda e: 400 if "zwischen" in str(e) else 502)
        return {"ok": True, "ctx": llm.context_size if active else ctx}

    @app.get("/api/models/{name}/sampling")
    async def get_model_sampling(name: str):
        from .llm import SAMPLING_LIMITS, SAMPLING_PRESETS
        router_profile(name)
        p = llm.profiles[name]
        return {"think": p.sampling.get("think") or {}, "fast": p.sampling.get("fast") or {},
                "temperature": p.temperature, "presets": SAMPLING_PRESETS,
                "limits": {k: [lo, hi] for k, (lo, hi, _) in SAMPLING_LIMITS.items()}}

    @app.post("/api/models/{name}/sampling")
    async def set_model_sampling(name: str, request: Request):
        """Sampling-Werte je Modus (mit/ohne Denken) – gilt ab der nächsten Antwort."""
        router_profile(name)
        body = await request.json()
        try:
            sets = await llm.set_sampling(name, body.get("think"), body.get("fast"))
        except LLMError as e:
            raise HTTPException(400, str(e)) from e
        await hub.broadcast({"type": "models_changed"})
        return {"ok": True, **sets}

    @app.post("/api/models/{name}/kv")
    async def set_model_kv(name: str, request: Request):
        """KV-Cache-Stufe (f16/q8_0/q4_0) eines eigenen llama-server – kleinerer Cache = mehr Kontext im VRAM."""
        from .llm_router import KV_TYPES
        router_profile(name)
        kv = str((await request.json()).get("kv", ""))
        if kv not in KV_TYPES:
            raise HTTPException(400, "KV-Cache-Stufe bitte f16, q8_0 oder q4_0")
        if not llm.profiles[name].server:
            try:
                await llm.set_kv(name, kv)
            except LLMError as e:
                raise HTTPException(400, str(e)) from e
        await restart_with(name, lambda: llm.set_kv(name, kv, model_progress), show_context=True)
        return {"ok": True, "kv": kv}

    @app.post("/api/models/{name}/ubatch")
    async def set_model_ubatch(name: str, request: Request):
        """Batch-Größe (-ub) eines eigenen llama-server – größer = langer Text wird schneller eingelesen, mehr VRAM."""
        from .llm_router import UBATCH_SIZES
        router_profile(name)
        try:
            size = int((await request.json()).get("size", 0))
        except (TypeError, ValueError):
            size = 0
        if size not in UBATCH_SIZES:
            raise HTTPException(400, "Batch-Größe bitte 512, 1024 oder 2048")
        if not llm.profiles[name].server:
            raise HTTPException(400, T("Die Batch-Größe lässt sich nur bei llama.cpp einstellen, nicht bei Ollama.",
                                       "The batch size can only be set for llama.cpp, not for Ollama."))
        await restart_with(name, lambda: llm.set_ubatch(name, size, model_progress))
        agent.forget_prefill(name)  # die alte Geschwindigkeit gilt nicht mehr – die nächste Antwort misst neu
        return {"ok": True, "ubatch": size}

    @app.post("/api/models/{name}/tune")
    async def tune_model(name: str):
        """Schnellste verlustfreie llama.cpp-Einstellung messen (Build, Batch-Größe) – siehe llamacpp_tune.py."""
        from . import llamacpp_tune
        from . import models as mdl
        router_profile(name)
        server = llm.profiles[name].server
        if not server or not llamacpp_tune.uses_own_build(cfg, server.command):
            raise HTTPException(400, T("Messen geht nur bei Modellen mit Orbwises llama.cpp.",
                                       "Measuring only works for models running on Orbwise's llama.cpp."))
        if llm.switching:
            raise HTTPException(409, T("Gerade wird ein Modell geladen – bitte kurz warten.",
                                       "A model is loading right now – please wait a moment."))

        async def job() -> dict:
            async def step(text: str) -> None:
                pull_state["Tuning"] = {"status": text}
                await hub.broadcast({"type": "model_pull", "tag": "Tuning", "status": text})
            gpu = await asyncio.to_thread(mdl.detect_gpu)
            async with agent.lock:  # keine Antwort gleichzeitig – das Modell ist währenddessen entladen
                result = await llamacpp_tune.tune(cfg, llm, name, gpu, step)
            agent.forget_prefill(name)  # die Batch-Größe kann sich geändert haben – neu messen
            agent._cache_owner = None
            await hub.broadcast({"type": "models_changed"})
            other = result.get("other") or {}
            return {"kind": "tuning", "profile": name, "applied_ubatch": result.get("applied_ubatch"),
                    "gain_pct": other.get("gain_pct"), "other_flavor": other.get("flavor")}
        return {"ok": True, "tag": start_job("Tuning", job)}

    @app.get("/api/models/{name}/tune")
    async def tune_result(name: str):
        from . import llamacpp_tune
        return {"result": llamacpp_tune.saved(state_file, name)}

    @app.post("/api/llamacpp/flavor")
    async def llamacpp_flavor(request: Request):
        """Anderen Build (z. B. Vulkan statt ROCm) für alle llama.cpp-Modelle übernehmen – fehlt er noch, wird er
        derselben Version zuerst geladen (Hintergrund-Aufgabe „llama.cpp“); ein geladenes Modell startet neu."""
        from . import llamacpp
        from . import models as mdl
        flavor = str((await request.json()).get("flavor") or "")
        if flavor not in llamacpp.FLAVORS:
            raise HTTPException(400, T("Unbekannte Variante", "Unknown variant"))
        if not llamacpp.installed(cfg):
            raise HTTPException(400, T("llama.cpp ist noch nicht eingerichtet.", "llama.cpp is not set up yet."))
        if not llamacpp.flavor_ready(cfg, flavor) and flavor != llamacpp.manifest(cfg).get("flavor"):
            async def job() -> dict:
                gpu = await asyncio.to_thread(mdl.detect_gpu)
                loop = asyncio.get_running_loop()

                def out(line: str) -> None:
                    asyncio.run_coroutine_threadsafe(pull_event("llama.cpp", status=line.strip()), loop)
                async with llamacpp_lock:
                    try:
                        await asyncio.to_thread(llamacpp.fetch_flavor, cfg, gpu, flavor, out)
                    except (httpx.HTTPError, OSError, zipfile.BadZipFile, tarfile.TarError) as e:
                        raise LLMError(T(f"{flavor}-Build konnte nicht geladen werden: {e}",
                                         f"Could not download the {flavor} build: {e}")) from e
                    except ValueError as e:
                        raise LLMError(str(e)) from e
                    m = await asyncio.to_thread(llamacpp.use_flavor, cfg, flavor)
                restart = reload_own_model()
                await hub.broadcast({"type": "models_changed"})
                return {"kind": "llamacpp", "version": m.get("tag"), "flavor": flavor, "changed": True,
                        "restarted": restart}
            return {"ok": True, "job": start_job("llama.cpp", job), "flavor": flavor}
        try:
            m = await asyncio.to_thread(llamacpp.use_flavor, cfg, flavor)
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
        restart = reload_own_model()
        await hub.broadcast({"type": "models_changed"})
        return {"ok": True, "flavor": m.get("flavor"), "tag": m.get("tag"), "restart": restart}

    def reload_own_model() -> bool:
        """Läuft gerade ein Modell auf Orbwise' eigenem llama.cpp: im Hintergrund mit dem neuen Build neu starten
        (wartet auf eine laufende Antwort). → ob neu gestartet wird."""
        if not (isinstance(llm, LLMRouter) and not llm.unloaded and own_llamacpp(llm.active)):
            return False

        async def reload() -> None:
            async with agent.lock:
                await llm.unload()
                try:
                    await llm.ensure_loaded()
                except LLMError as e:
                    await hub.broadcast({"type": "model_error", "name": llm.active, "text": str(e)})
            agent._cache_owner = None
            await idle_if_free()
            hub.prewarm_soon()
            await hub.broadcast({"type": "models_changed"})
        background.append(asyncio.create_task(reload()))
        return True

    def own_llamacpp(name: str) -> bool:
        from . import llamacpp_tune
        server = llm.profiles[name].server
        return bool(server) and llamacpp_tune.uses_own_build(cfg, server.command)

    @app.post("/api/models/{name}/spec")
    async def set_model_spec(name: str, request: Request):
        """Spekulatives Decoding: off / mtp (eigene Vorhersage-Schichten des Modells) / ngram – schneller antworten."""
        from .llm_router import SPEC_MODES, spec_options
        router_profile(name)
        mode = str((await request.json()).get("mode", ""))
        if mode not in SPEC_MODES:
            raise HTTPException(400, "Bitte off, mtp oder ngram")
        option = next(o for o in spec_options(llm.profiles[name].server) if o["mode"] == mode)
        if not option["ok"]:
            raise HTTPException(400, option.get("why") or "nicht möglich")
        await restart_with(name, lambda: llm.set_spec(name, mode, model_progress))
        return {"ok": True, "spec": mode}

    @app.get("/api/models/spec/stats")
    async def model_spec_stats():
        """Trefferquote der Vorschläge (laut Log des laufenden llama-server) – zeigt, ob es etwas bringt."""
        from .llm_router import _log_path, spec_stats
        try:
            text = (await asyncio.to_thread(_log_path().read_text, errors="replace"))[-400_000:]
        except OSError:
            return {"stats": None}
        return {"stats": spec_stats(text.rsplit("===== Starte:", 1)[-1])}

    return SimpleNamespace(activate_model=activate_model)
