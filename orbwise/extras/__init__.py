"""Zusätze: Anbindungen an Dienste, die nicht jeder hat – Paperless, Mail, Kalender, Portainer, Trilium, Obsidian,
Home Assistant, Telegram.

Der Kern von Orbwise (den Rechner bedienen, Coding, Gedächtnis, Sprache, Freigaben) kommt ohne sie aus und hängt nicht
von ihnen ab: Kern-Code importiert Zusätze nur dort, wo er sie einbindet (Werkzeuge laden, Skills einrichten,
`orbwise doctor`, das Morgen-Briefing). Umgekehrt nutzen die Zusätze die Bausteine des Kerns (tools.registry,
tools.netutil, tools.proc …).

Jeder Zusatz trägt seine Werkzeuge beim Import ein; angeboten werden sie dem Modell erst, wenn der Dienst unter
Einstellungen → Skills eingerichtet ist (`enabled` am Werkzeug). Die Tests dazu liegen in tests/extras/.
"""
