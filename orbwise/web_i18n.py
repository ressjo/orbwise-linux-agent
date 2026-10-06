"""Englische Fassung der Oberfläche: index.html wird beim Ausliefern übersetzt (keine zweite HTML-Datei pflegen).
Die dynamischen Texte übersetzt app.js selbst (L("deutsch", "english"))."""

from __future__ import annotations

HTML_EN = [
    ('<html lang="de">', '<html lang="en">'),
    # Seitenleiste
    ('<span class="brand-sub">Orbwise · lokal</span>', '<span class="brand-sub">Orbwise · local</span>'),
    ('title="Seitenleiste ein-/ausklappen" aria-label="Seitenleiste ein-/ausklappen"',
     'title="Collapse/expand the sidebar" aria-label="Collapse/expand the sidebar"'),
    ('title="Neuen Chat beginnen (Gedächtnis bleibt erhalten)"', 'title="Start a new chat (memory is kept)"'),
    ('<span>Neuer Chat</span>', '<span>New chat</span>'),
    ('placeholder="Chats durchsuchen" aria-label="Chats durchsuchen"', 'placeholder="Search chats" aria-label="Search chats"'),
    ('★ markiert wichtige Chats · Doppelklick auf den Titel benennt um · ✕ löscht den Chat auch aus dem Gedächtnis.',
     '★ marks important chats · double-click the title to rename · ✕ also deletes the chat from memory.'),
    ('title="Planer"><svg class="i"><use href="#i-calendar"/></svg><span>Planer<',
     'title="Planner"><svg class="i"><use href="#i-calendar"/></svg><span>Planner<'),
    ('title="Gedächtnis"><svg class="i"><use href="#i-brain"/></svg><span>Gedächtnis<',
     'title="Memory"><svg class="i"><use href="#i-brain"/></svg><span>Memory<'),
    ('title="Einstellungen"><svg class="i"><use href="#i-settings"/></svg><span>Einstellungen<',
     'title="Settings"><svg class="i"><use href="#i-settings"/></svg><span>Settings<'),
    # Kopfzeile
    ('title="Menü" aria-label="Menü"', 'title="Menu" aria-label="Menu"'),
    ('title="Sprachmodell wechseln"', 'title="Switch language model"'),
    ('title="Sprachausgabe an/aus" aria-label="Sprachausgabe"', 'title="Speech output on/off" aria-label="Speech output"'),
    ('title="Wake-Word „Hey Jarvis“ an/aus" aria-label="Wake-Word"', 'title="Wake word “Hey Jarvis” on/off" aria-label="Wake word"'),
    ('title="Aktivität und Systemauslastung" aria-label="Aktivität"', 'title="Activity and system load" aria-label="Activity"'),
    # Bühne
    ('aria-label="Jarvis Statusanzeige"', 'aria-label="Jarvis status display"'),
    ('<div class="thought-head">Gedankengang</div>', '<div class="thought-head">Thoughts</div>'),
    ('>Wie kann ich helfen?<', '>How can I help?<'),
    ('data-text="Mach ein Systemupdate">Systemupdate<', 'data-text="Run a system update">System update<'),
    ('data-text="Was ist das für eine Fehlermeldung auf meinem Bildschirm?">Bildschirm erklären<',
     'data-text="What is that error message on my screen?">Explain my screen<'),
    ('data-text="Guten Morgen – was steht heute an?">Briefing<', 'data-text="Good morning – what\'s on today?">Briefing<'),
    ('data-text="Wie viel Speicherplatz ist noch frei?">Speicherplatz<',
     'data-text="How much disk space is left?">Disk space<'),
    # Eingabe
    ('title="Halten zum Sprechen (Leertaste) · kurz tippen = zuhören bis Stille" aria-label="Mikrofon"',
     'title="Hold to talk (space bar) · tap = listen until silence" aria-label="Microphone"'),
    ('placeholder="Frag Jarvis oder sag „Hey Jarvis“ …"', 'placeholder="Ask Jarvis or say “Hey Jarvis” …"'),
    ('aria-label="Modus"', 'aria-label="Mode"'),
    ('title="Tools: Fragen, Websuche, Paperless, Mail, System bedienen – mit Sprache"',
     'title="Tools: questions, web search, Paperless, mail, running the system – with voice"'),
    ('title="Coding: nur Dateien, Shell, Web und Gedächtnis – mehr Kontext für Code, ohne Sprache"',
     'title="Coding: only files, shell, web and memory – more context for code, no voice"'),
    ('title="Projektordner für diesen Coding-Chat – Shell und relative Pfade starten dort"',
     'title="Project folder for this coding chat – shell and relative paths start there"'),
    ('<em id="project-label">Projektordner wählen<', '<em id="project-label">Choose project folder<'),
    ('data-text="Gib mir einen Überblick über dieses Projekt: Aufbau, Stack, wie man es baut und testet.">Projekt '
     'verstehen<',
     'data-text="Give me an overview of this project: structure, stack, how to build and test it.">Understand the '
     'project<'),
    ('data-text="Führe die Tests aus und behebe, was fehlschlägt.">Tests reparieren<',
     'data-text="Run the tests and fix what fails.">Fix the tests<'),
    ('data-text="Was hat sich seit dem letzten Commit geändert? Prüfe die Änderungen auf Fehler.">Änderungen prüfen<',
     'data-text="What changed since the last commit? Review the changes for bugs.">Review changes<'),
    ('data-text="Schreib mir ein kleines Python-Skript, das ">Skript schreiben<',
     'data-text="Write me a small Python script that ">Write a script<'),
    ('title="Planmodus: Jarvis schaut nur lesend nach und legt erst einen Plan vor – ausgeführt wird nach '
     'deiner Freigabe."><svg class="i"><use href="#i-plan"/></svg><span>Plan<',
     'title="Plan mode: Jarvis only looks things up and presents a plan first – it is carried out once you '
     'approve it."><svg class="i"><use href="#i-plan"/></svg><span>Plan<'),
    ('title="Denkmodus: Jarvis denkt vor der Antwort nach – langsamer, dafür gründlicher."><svg class="i"><use '
     'href="#i-sparkles"/></svg><span id="think-label">Denken<',
     'title="Thinking mode: Jarvis reasons before answering – slower but more thorough."><svg class="i"><use '
     'href="#i-sparkles"/></svg><span id="think-label">Think<'),
    ('<b>Nicht denken</b><small>Direkte Antworten – am schnellsten.</small>',
     '<b>No thinking</b><small>Direct answers – fastest.</small>'),
    ('<b>Kurz</b><small>Kurz überlegen, dann handeln (Denkkette höchstens ~500 Token).</small>',
     '<b>Brief</b><small>Think briefly, then act (reasoning up to ~500 tokens).</small>'),
    ('<b>Normal</b><small>Zügig durchdenken (höchstens ~2.000 Token).</small>',
     '<b>Normal</b><small>Think it through efficiently (up to ~2,000 tokens).</small>'),
    ('<b>Gründlich</b><small>So lange wie nötig – am langsamsten.</small>',
     '<b>Thorough</b><small>As long as needed – slowest.</small>'),
    ('title="Auto-Modus: was ohne Rückfrage laufen darf"', 'title="Auto mode: what may run without asking"'),
    ('<span id="auto-label">Lesen<', '<span id="auto-label">Read<'),
    ('<b>Aus</b><small>Jeder Shell-Befehl fragt vorher.</small>',
     '<b>Off</b><small>Every shell command asks first.</small>'),
    ('<b>Nur lesen</b><small>Erkannte lesende Befehle (ls, df, docker ps, git status …) laufen ohne Rückfrage.</small>',
     '<b>Read only</b><small>Recognised read-only commands (ls, df, docker ps, git status …) run without asking.</small>'),
    ('<b>Lesen + Dateien bearbeiten</b><small>Zusätzlich Dateien im eigenen Home anlegen, schreiben, kopieren, '
     'verschieben – ohne Root. Löschen und versteckte Dateien fragen weiter.</small>',
     '<b>Read + edit files</b><small>Also create, write, copy and move files in your own home – without root. '
     'Deleting and hidden files still ask.</small>'),
    ('<b>Auto</b><small>Alles ohne Root: Shell-Befehle (python, git, make, pip --user …) und Dateien laufen ohne '
     'Rückfrage. Löschen, sudo, Ausschalten, Senden ins Netz (git push, ssh, Uploads), Autostart/Startdateien und '
     'Zugangsdaten fragen weiter.</small>',
     '<b>Auto</b><small>Everything without root: shell commands (python, git, make, pip --user …) and files run '
     'without asking. Deleting, sudo, shutting down, sending over the network (git push, ssh, uploads), '
     'autostart/start-up files and credentials still ask.</small>'),
    ('title="Senden" aria-label="Senden"', 'title="Send" aria-label="Send"'),
    ('title="Aktuelle Aufgabe abbrechen (Esc)" aria-label="Abbrechen"', 'title="Cancel the current task (Esc)" aria-label="Cancel"'),
    ('>Leertaste halten zum Sprechen · Esc bricht ab · Änderungen am System fragt Jarvis vorher<',
     '>Hold the space bar to talk · Esc cancels · Jarvis asks before changing your system<'),
    # Aktivität
    ('<span>Aktivität</span>', '<span>Activity</span>'),
    ('title="Schließen" aria-label="Schließen"', 'title="Close" aria-label="Close"'),
    ('aria-label="Systemauslastung"', 'aria-label="System load"'),
    ('title="Kontext: wie voll der Prompt im Verhältnis zum Budget ist"',
     'title="Context: how full the prompt is relative to the budget"'),
    ('>Kontext<', '>Context<'),
    ('>Leistung<', '>Power<'),
    ('>Noch keine Aktivität – Werkzeuge und Befehle erscheinen hier live.<',
     '>No activity yet – tools and commands show up here live.<'),
    # Planer
    ('aria-label="Planer">', 'aria-label="Planner">'),
    ('<h2>Planer</h2>', '<h2>Planner</h2>'),
    ('<h3>Routinen</h3>', '<h3>Routines</h3>'),
    ('title="Neue Routine anlegen">+ Routine<', 'title="Create a routine">+ Routine<'),
    ('placeholder="Name, z. B. Linux-News"', 'placeholder="Name, e.g. Linux news"'),
    ('placeholder="Aufgabe, z. B. Suche die wichtigsten Linux-News von heute und fasse sie zusammen."',
     'placeholder="Task, e.g. Search today\'s most important Linux news and summarise them."'),
    ('aria-label="Uhrzeit"', 'aria-label="Time"'),
    ('>nur einmal am <', '>only once on <'),
    ('id="rt-save">Speichern<', 'id="rt-save">Save<'),
    ('id="rt-cancel">Abbrechen<', 'id="rt-cancel">Cancel<'),
    ('<h3>Erinnerungen</h3>', '<h3>Reminders</h3>'),
    ('<h3>Briefing-Inhalt</h3>', '<h3>Briefing content</h3>'),
    ('>Punkte &amp; Reihenfolge<', '>Items &amp; order<'),
    ('Vorausschau für Termine und Erinnerungen (Tage)', 'Look-ahead for events and reminders (days)'),
    ('Nachrichten-Themen (mit Komma getrennt)', 'News topics (comma-separated)'),
    ('placeholder="z. B. Linux, Freiburg, KI"', 'placeholder="e.g. Linux, Berlin, AI"'),
    ('Schlagzeilen je Thema', 'Headlines per topic'),
    ('Paperless-Posteingangs-Tag', 'Paperless inbox tag'),
    ('placeholder="leer = Posteingangs-Tags aus Paperless"', 'placeholder="empty = inbox tags from Paperless"'),
    ('Eigener Wunsch fürs Briefing', 'Your wish for the briefing'),
    ('placeholder="z. B. Halte dich kurz und fang mit den Terminen an."',
     'placeholder="e.g. Keep it short and start with my appointments."'),
    ('id="brief-preview">Vorschau<', 'id="brief-preview">Preview<'),
    ('title="Einstellungen aus der Config-Datei verwenden">Zurücksetzen<',
     'title="Use the settings from the config file">Reset<'),
    # Gedächtnis
    ('aria-label="Gedächtnis">', 'aria-label="Memory">'),
    ('<h2>Gedächtnis</h2>', '<h2>Memory</h2>'),
    ('<h3>Fakten</h3>', '<h3>Facts</h3>'),
    ('<h3>Tagebuch</h3>', '<h3>Journal</h3>'),
    ('<h3>Erfahrungen</h3>', '<h3>Experience</h3>'),
    ('Kurze Lektionen, die Jarvis aus Fehlern, deinen Korrekturen und 👎 gelernt hat. Zu einer Frage gehen nur die '
     'passenden mit (feste kleine Grenze) – das Kontextfenster wächst dadurch nicht.',
     'Short lessons Jarvis learned from errors, your corrections and 👎. Only matching ones go along with a question '
     '(small fixed limit) – the context window does not grow.'),
    ('placeholder="Eigene Regel hinzufügen, z. B. „Docker auf dem NAS immer mit sudo“"',
     'placeholder="Add your own rule, e.g. “always use sudo for docker on the NAS”"'),
    ('<button class="btn small" type="submit">Hinzufügen</button>', '<button class="btn small" type="submit">Add</button>'),
    ('title="Bild: Bilder erzeugen und bearbeiten mit Qwen-Image-2.1 – lokal"><svg class="i"><use href="#i-image"/></svg><span>Bild<',
     'title="Image: generate and edit images with Qwen-Image-2.1 – locally"><svg class="i"><use href="#i-image"/></svg><span>Image<'),
    ('title="Bild anhängen (auch einfügen mit Strg+V oder hineinziehen)" aria-label="Bild anhängen"',
     'title="Attach an image (or paste with Ctrl+V or drag it in)" aria-label="Attach image"'),
    # Einstellungen
    ('aria-label="Einstellungen">', 'aria-label="Settings">'),
    ('<h2>Einstellungen</h2>', '<h2>Settings</h2>'),
    ('data-section="models">Modelle<', 'data-section="models">Models<'),
    ('data-section="voice">Stimme<', 'data-section="voice">Voice<'),
    ('data-section="general">Allgemein<', 'data-section="general">General<'),
    ('>Wähle das Sprachmodell, lade neue herunter oder lösche nicht mehr benötigte.<',
     '>Choose the language model, download new ones or delete those you no longer need.<'),
    ('<div class="mm-title">Jarvis-Effekt</div>', '<div class="mm-title">Jarvis effect</div>'),
    ('id="fx-toggle">Aus<', 'id="fx-toggle">Off<'),
    ('aria-label="Effektstärke"', 'aria-label="Effect strength"'),
    ('>Tiefere, sonore Stimme mit leichtem Hall und digitalem Schimmer.<',
     '>Deeper, sonorous voice with a light reverb and a digital shimmer.<'),
    ('<b>Sprachausgabe</b><small>Antworten werden vorgelesen.</small>',
     '<b>Speech output</b><small>Answers are read out.</small>'),
    ('<b>Wake-Word</b><small>„Hey Jarvis“ startet die Spracheingabe.</small>',
     '<b>Wake word</b><small>“Hey Jarvis” starts voice input.</small>'),
    ('<b>Planmodus</b><small>Erst einen Plan vorlegen, ausführen nach Freigabe.</small>',
     '<b>Plan mode</b><small>Present a plan first, carry it out once approved.</small>'),
    ('<b>Auto-Modus</b><small>Was ohne Rückfrage laufen darf.</small>',
     '<b>Auto mode</b><small>What may run without asking.</small>'),
    ('data-auto="off">Aus</button><button type="button" data-auto="read">Nur lesen</button><button type="button" '
     'data-auto="files">+ Dateien<',
     'data-auto="off">Off</button><button type="button" data-auto="read">Read only</button><button type="button" '
     'data-auto="files">+ Files<'),
    ('<b>Root-Passwort</b><em>nicht gemerkt</em><button type="button" class="btn-link hidden" '
     'id="btn-sudo-forget">Vergessen<',
     '<b>Root password</b><em>not remembered</em><button type="button" class="btn-link hidden" '
     'id="btn-sudo-forget">Forget<'),
    ('<span class="tele-label">Kontext im Speicher</span>', '<span class="tele-label">Context in memory</span>'),
    ('<b>Denkmodus</b><small>Gründlicher, dafür langsamer.</small>',
     '<b>Thinking mode</b><small>More thorough, but slower.</small>'),
    ('data-mirror="btn-tts">Aus<', 'data-mirror="btn-tts">Off<'),
    ('data-mirror="btn-wake">Aus<', 'data-mirror="btn-wake">Off<'),
    ('data-mirror="btn-plan">Aus<', 'data-mirror="btn-plan">Off<'),
    ('data-think="off">Aus</button><button type="button" data-think="low">Kurz</button><button type="button" '
     'data-think="medium">Normal</button><button type="button" data-think="high">Gründlich<',
     'data-think="off">Off</button><button type="button" data-think="low">Brief</button><button type="button" '
     'data-think="medium">Normal</button><button type="button" data-think="high">Thorough<'),
    ('>Weitere Einstellungen (Integrationen, Telegram, Mail …) stehen in ',
     '>Further settings (integrations, Telegram, mail …) live in '),
    (' – <code>orbwise doctor</code> prüft sie.<', ' – <code>orbwise doctor</code> checks them.<'),
    ('<b>Stimme</b><em>', '<b>Voice</b><em>'),
    ('<b>Gedächtnis</b><em>', '<b>Memory</b><em>'),
    # Dialoge
    ('id="boot-btn" disabled>Orbwise starten<', 'id="boot-btn" disabled>Start Orbwise<'),
    ('id="boot-sub">Orbwise startet …<', 'id="boot-sub">Orbwise is starting …<'),
    ('>Startet von selbst, sobald alles geladen ist<', '>Starts by itself once everything has loaded<'),
    ('Bestätigung erforderlich', 'Confirmation required'),
    ('Abbrechen <kbd>', 'Cancel <kbd>'),
    ('Ausführen <kbd>', 'Run <kbd>'),
    ('oder sag „Ja“ bzw. „Nein“', 'or say “yes” or “no”'),
    ('Root-Rechte benötigt', 'Root privileges required'),
    ('>Root-Passwort (sudo)<', '>Root password (sudo)<'),
    ('placeholder="Passwort" aria-label="Passwort"', 'placeholder="Password" aria-label="Password"'),
    ('>Geht direkt an sudo – nie auf die Platte und nie an das Sprachmodell.<',
     '>Goes straight to sudo – never written to disk and never passed to the language model.<'),
    ('<span id="pw-remember-text">15 Minuten merken<', '<span id="pw-remember-text">Remember for 15 minutes<'),
    ('id="day-close">Schließen<', 'id="day-close">Close<'),
    ('id="reminder-kind">Erinnerung<', 'id="reminder-kind">Reminder<'),
]


def translate_index(html: str, language: str) -> str:
    if language != "en":
        return html
    for de, en in HTML_EN:
        html = html.replace(de, en)
    return html
