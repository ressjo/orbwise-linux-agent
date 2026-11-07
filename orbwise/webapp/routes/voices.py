"""Stimmen: Auswahl, Vorschau, Piper und Pocket TTS installieren/entfernen."""

from __future__ import annotations

import asyncio

from fastapi import HTTPException, Request
from fastapi.responses import Response

from ...lang import T
from ...voice import piper, pocket


def register(app, ctx) -> None:
    # gemeinsame Objekte und Helfer anderer Teile (siehe webapp/context.py)
    apply_reply_language = ctx.apply_reply_language
    background = ctx.background
    cfg = ctx.cfg
    hub = ctx.hub
    piper_jobs = ctx.piper_jobs
    setup_piper = ctx.setup_piper
    startup = ctx.startup
    tts = ctx.tts

    @app.get("/api/voices")
    async def voices():
        if not tts:
            return {"available": False, "voices": [], "languages": []}
        return {"available": True, "current": tts.voice, "language": tts.reply_language, "engine": tts.engine(),
                "voices": pocket.voice_list(cfg.language, tts.voice, tts.languages),
                "languages": pocket.language_list(cfg.language == "en"), "pocket": tts.pocket.describe(),
                "piper": {**tts.piper.describe(), "downloading": piper_jobs["voice"],
                          "voices": piper.voice_list(cfg.language, tts.voice, tts.piper.data_dir)}}

    @app.post("/api/piper/voices/{voice_id}")
    async def piper_voice_download(voice_id: str):
        """Piper (falls nötig) einrichten und eine Stimme laden – im Hintergrund; danach ist sie gewählt."""
        if not tts:
            raise HTTPException(400, "Sprachausgabe ist deaktiviert")
        if voice_id not in piper.BY_ID:
            raise HTTPException(404, T("Unbekannte Piper-Stimme", "Unknown Piper voice"))
        if piper_jobs["voice"]:
            raise HTTPException(409, T("Es wird schon eine Stimme geladen.", "A voice is already downloading."))
        piper_jobs["voice"] = voice_id

        async def run() -> None:
            try:
                ok = await setup_piper(voice_id)
            finally:
                piper_jobs.update(voice="", stop=None)
            if ok and await asyncio.to_thread(tts.select, "piper:" + voice_id):
                apply_reply_language()
                await hub.broadcast({"type": "voice_changed", "voice": tts.voice, "language": tts.reply_language})
                await startup.set("tts", "ok", f"{tts.label_of(tts.voice)} · "
                                  f"{pocket.language_name(tts.reply_language, cfg.language == 'en')}")
            await hub.broadcast({"type": "piper_progress", "voice": voice_id, "done": True, "ok": ok})
        background.append(asyncio.create_task(run()))
        return {"started": True}

    @app.delete("/api/piper/download")
    async def piper_download_cancel():
        stop = piper_jobs.get("stop")
        if stop is not None:
            stop.set()
        return {"ok": stop is not None}

    @app.delete("/api/piper")
    async def piper_remove():
        if not tts:
            raise HTTPException(400, "Sprachausgabe ist deaktiviert")
        if tts.is_piper(tts.voice):
            raise HTTPException(409, T("Piper ist gerade aktiv – erst eine Pocket-Stimme wählen.",
                                       "Piper is active – choose a Pocket voice first."))
        await asyncio.to_thread(tts.piper.stop)
        freed = await asyncio.to_thread(piper.remove, tts.piper.data_dir)
        return {"ok": True, "freed_mb": round(freed / 1e6)}

    @app.delete("/api/piper/voices/{voice_id}")
    async def piper_voice_delete(voice_id: str):
        if not tts or voice_id not in piper.BY_ID:
            raise HTTPException(404, T("Unbekannte Piper-Stimme", "Unknown Piper voice"))
        if tts.voice == "piper:" + voice_id:
            raise HTTPException(409, T("Diese Stimme ist gerade aktiv – erst eine andere wählen.",
                                       "This voice is active – choose another one first."))
        freed = await asyncio.to_thread(piper.remove_voice, tts.piper.data_dir, voice_id)
        return {"ok": True, "freed_mb": round(freed / 1e6)}

    @app.post("/api/pocket/install")
    async def install_pocket():
        """Pocket TTS installieren (Programm von GitHub), danach im Hintergrund starten – das lädt beim ersten Mal
        Modell und Stimme von Hugging Face."""
        if not tts:
            raise HTTPException(400, "Sprachausgabe ist deaktiviert")
        lines: list[str] = []
        if not await asyncio.to_thread(pocket.install, tts.pocket.data_dir, lines.append):
            raise HTTPException(502, (lines[-1] if lines else "Installation fehlgeschlagen").lstrip("✘ "))
        if not tts.is_piper(tts.voice):
            tts.pocket.warm(tts.voice)
            await startup.set("tts", "ok", tts.label_of(tts.voice))
        return {"ok": True, "pocket": tts.pocket.describe()}

    @app.delete("/api/pocket")
    async def remove_pocket():
        if not tts:
            raise HTTPException(400, "Sprachausgabe ist deaktiviert")
        await asyncio.to_thread(tts.pocket.stop)
        freed = await asyncio.to_thread(pocket.remove, tts.pocket.data_dir)
        await startup.set("tts", "warn", T("Browser-Stimme", "browser voice"))
        return {"ok": True, "freed_mb": round(freed / 1e6)}

    @app.post("/api/voices/select")
    async def select_voice(request: Request):
        """Stimme wählen, optional mit ihrer Sprache – die gilt dann auch für die Antworten."""
        data = await request.json()
        if not tts:
            raise HTTPException(400, "Sprachausgabe ist deaktiviert")
        voice, language = str(data.get("voice") or ""), (str(data["language"]) if data.get("language") else None)
        if not await asyncio.to_thread(tts.select, voice, language):
            raise HTTPException(400, "Unbekannte Stimme oder Sprache")
        apply_reply_language()
        await hub.broadcast({"type": "voice_changed", "voice": tts.voice, "language": tts.reply_language})
        return {"ok": True, "current": tts.voice, "language": tts.reply_language}

    @app.get("/api/voices/{name}/preview")
    async def preview_voice(name: str, text: str = ""):
        if not tts or not tts.known(name):
            raise HTTPException(404, "Stimme nicht verfügbar")
        lang = tts.language_of(name)
        if tts.is_piper(name):  # Piper: jede geladene Stimme lässt sich anhören
            if not tts.piper.installed() or not piper.has_voice(tts.piper.data_dir, tts.piper_id(name)):
                raise HTTPException(404, T("Diese Stimme ist noch nicht geladen.", "This voice is not downloaded yet."))
        elif not tts.pocket.installed() or not pocket.cpu_supported():
            raise HTTPException(404, "Stimme nicht verfügbar")
        elif lang != tts.pocket.language:  # Pocket hat nur das Modell einer Sprache geladen
            en = cfg.language == "en"
            raise HTTPException(409, T(
                f"{tts.label_of(name)} spricht {pocket.language_name(lang, en)} – zum Anhören die Stimme wählen.",
                f"{tts.label_of(name)} speaks {pocket.language_name(lang, en)} – select the voice to hear it."))
        sample = text.strip()[:200] or pocket.SAMPLES.get(lang, pocket.SAMPLES["en"])
        try:
            wav = await asyncio.to_thread(tts.synth, sample, name)
        except Exception as e:  # noqa: BLE001
            raise HTTPException(502, f"Probe fehlgeschlagen: {e}") from e
        return Response(wav, media_type="audio/wav")
