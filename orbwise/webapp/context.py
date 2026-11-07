"""Gemeinsamer Zustand der Web-App (früher die lokalen Variablen von create_app).

server.create_app legt die Kernobjekte an; die Teile (lifecycle, background, routes/…) holen sich, was sie brauchen,
und hängen ihre eigenen Helfer an, die andere Teile aufrufen (z. B. idle_if_free, mode_info, chat_switched).
"""

from __future__ import annotations

from types import SimpleNamespace


class AppContext(SimpleNamespace):
    """Kern (aus create_app): cfg, llm, memory, agent, hub, images, startup, broker, reminders, routines, stt, tts,
    wake, background (laufende Hintergrund-Aufgaben), state_file, boot_id, auto_file, en, retired, bg_jobs,
    apply_reply_language, ImageError, web_token.

    Helfer der Teile:
    - lifecycle: lifespan, idle_if_free, model_progress, heal_index_soon, keep, setup_piper, piper_jobs
    - telegram_link: telegram_bot, reminder_loop
    - background: start_routine, routine_loop, metrics_loop, summary_loop, sandbox_idle_loop, llm_resting,
      model_resting, wake_model, waking, app_state_resting
    - routes.chats: mode_info, chat_switched, not_busy, settle, settings_info
    - routes.sandbox: coding_chat · routes.modes: mode_models · routes.models: models_api · routes.web: from_jail
    """
