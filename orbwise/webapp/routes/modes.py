"""Modi Tools (Assistent) und Coding: umschalten, Modell und Kontextfenster je Modus."""

from __future__ import annotations

import asyncio
import contextlib
import json

from fastapi import HTTPException, Request

from ...llm import LLMError
from ...llm_router import LLMRouter


def register(app, ctx) -> None:
    # gemeinsame Objekte und Helfer anderer Teile (siehe webapp/context.py)
    agent = ctx.agent
    background = ctx.background
    chat_switched = ctx.chat_switched
    hub = ctx.hub
    idle_if_free = ctx.idle_if_free
    llm = ctx.llm
    memory = ctx.memory
    mode_info = ctx.mode_info
    model_progress = ctx.model_progress
    models_api = ctx.models_api
    settle = ctx.settle

    # ---------- Modi: Tools (Assistent) und Coding ----------
    mode_models_file = memory.chats.dir / "mode-models.json"

    def mode_models() -> dict:
        try:
            return json.loads(mode_models_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def remember_mode_model(mode: str, name: str) -> None:
        data = mode_models()
        data[mode] = name
        mode_models_file.write_text(json.dumps(data), encoding="utf-8")

    @app.post("/api/mode")
    async def set_mode(request: Request):
        mode = str((await request.json()).get("mode", ""))
        if mode not in ("tools", "coding"):
            raise HTTPException(400, "Unbekannter Modus")
        await settle()
        old = mode_info()["mode"]
        if isinstance(llm, LLMRouter) and old != mode and old not in mode_models():
            remember_mode_model(old, llm.active)  # damit der Rückweg wieder das bisherige Modell nimmt
        memory.switch_mode(mode)
        await chat_switched()
        await hub.broadcast({"type": "chats_changed"})
        # Eigenes Modell für diesen Modus gewählt? Dann im Hintergrund umschalten
        target = mode_models().get(mode)
        if isinstance(llm, LLMRouter) and target and target != llm.active and target in llm.profiles:
            await llm.set_mode(mode, restart=False)  # das neue Modell startet gleich mit dem Fenster des Modus
            background.append(asyncio.create_task(switch_model_quietly(target)))
        elif isinstance(llm, LLMRouter):
            background.append(asyncio.create_task(switch_context_quietly(mode)))
        return mode_info()

    async def switch_context_quietly(mode: str) -> None:
        """Eigenes Kontextfenster des Modus: Server des aktiven Modells damit neu starten (wenn es abweicht)."""
        wanted = llm.wanted_context(llm.active, mode)
        if not wanted or wanted == llm.profile.num_ctx or llm.profile.backend == "ollama" and not llm.profile.server:
            await llm.set_mode(mode)  # Ollama: neues num_ctx gilt ab der nächsten Anfrage, kein Neustart nötig
            return
        name = llm.active
        await hub.broadcast({"type": "model_switching", "name": name, "label": llm.profile.label})
        llm.switching = name
        try:
            async with agent.lock:
                await llm.set_mode(mode, model_progress)
        except LLMError as e:
            await hub.broadcast({"type": "model_error", "name": name, "text": str(e)})
            return
        finally:
            llm.switching = None
            await idle_if_free()
        await hub.broadcast({"type": "model_active", "name": name})
        agent._cache_owner = None
        agent.build_messages()
        await hub.broadcast({"type": "context", **(agent.last_context or {})})
        await hub.broadcast({"type": "models_changed"})
        hub.prewarm_soon()

    async def switch_model_quietly(name: str) -> None:
        with contextlib.suppress(HTTPException):
            await models_api.activate_model(name)

    # für andere Teile der App
    ctx.mode_models = mode_models
    ctx.remember_mode_model = remember_mode_model
