"""Die Web-App: Zusammenbau in server.create_app, gemeinsamer Zustand in context.AppContext.

- lifecycle.py      Start/Ende (Modell, Stimme, Suchindex), lifespan
- telegram_link.py  Telegram-Bot, fällige Erinnerungen
- background.py     Routinen und Hintergrund-Schleifen
- routes/           HTTP- und WebSocket-Endpunkte, ein Modul je Bereich (register(app, ctx))
- startup.py, security.py, static.py  Startanzeige, Zugriff nur von localhost, Oberflächen-Dateien
"""
