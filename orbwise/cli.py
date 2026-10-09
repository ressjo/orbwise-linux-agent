"""Kommandozeile: orbwise [serve|doctor|model|context-test|update|version|reindex|summarize|init-config]"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import shutil
import sys
import webbrowser
from pathlib import Path

from . import bonsai
from .config import config_path, env, load_config
from .lang import T, set_lang

EXAMPLE_CONFIG = Path(__file__).parent / "config.example.yaml"


def cmd_serve(args) -> None:
    import uvicorn

    from .server import create_app, remote_bind_warning

    cfg = load_config()
    warning = remote_bind_warning(cfg)
    if warning and not cfg.allow_remote:
        sys.exit(T("Abgebrochen: ", "Aborted: ") + warning + T(
            "\nWer das wirklich will: allow_remote: true in config.yaml setzen.",
            "\nIf you really want this, set allow_remote: true in config.yaml."))
    if warning:
        logging.getLogger("orbwise").warning("ACHTUNG: %s", warning)
    app = create_app(cfg)
    url = f"http://localhost:{cfg.port}"
    print(T("Orbwise läuft auf ", "Orbwise is running at ") + url)
    if args.open:
        webbrowser.open(url)
    uvicorn.run(app, host=cfg.host, port=cfg.port, log_level="info" if args.verbose else "warning")


def cmd_init_config(args) -> None:
    path = config_path()
    if path.exists() and not args.force:
        print(f"{path} " + T("existiert bereits (--force zum Überschreiben).", "already exists (--force to overwrite)."))
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(EXAMPLE_CONFIG, path)
    print(T("Konfiguration angelegt: ", "Configuration created: ") + str(path))


def cmd_doctor(args) -> None:
    cfg = load_config()

    def line(ok: bool, text: str, hint: str = "") -> None:
        print(f"  {'✔' if ok else '✘'} {text}" + (f"  → {hint}" if hint and not ok else ""))

    from .update import version
    print(f"Version: {version()}")
    print(T("Konfiguration: ", "Configuration: ") + f"{config_path()} ("
          + (T("vorhanden", "found") if config_path().exists() else T("Standardwerte", "defaults")) + ")")
    print(T("Sprache: ", "Language: ") + cfg.language)
    print("LLM:")
    from .llm import OllamaLLM, OpenAICompatLLM
    from .llm_router import LLMRouter
    router = LLMRouter(cfg.llm, state_path=cfg.memory.dir.parent / "state.json")
    ollama = OllamaLLM(cfg.llm)
    st = asyncio.run(ollama.status())
    line(st["online"], f"Ollama {T('unter', 'at')} {cfg.llm.base_url}", "sudo systemctl enable --now ollama")
    if st["online"]:
        line(st["embed_available"], f"{T('Embedding-Modell', 'Embedding model')} {cfg.llm.embed_model}", f"ollama pull {cfg.llm.embed_model}")
    for name, p in router.profiles.items():
        mark = T(" (aktiv)", " (active)") if name == router.active else ""
        print(f"  {T('Profil', 'Profile')} {name}{mark}: {p.backend} · {p.model} · {p.base_url}")
        if p.backend == "ollama":
            ps = asyncio.run(OllamaLLM(cfg.llm.model_copy(update={"base_url": p.base_url, "model": p.model})).status())
            if ps["online"]:
                line(ps["model_available"], f"    {T('Modell', 'Model')} {p.model}", f"ollama pull {p.model}")
        else:
            ps = asyncio.run(OpenAICompatLLM(p).status())
            if p.server:
                script = os.path.expanduser(p.server.command.split()[0])
                line(os.path.exists(script) or bool(shutil.which(script)), f"    {T('Startbefehl', 'Start command')} {script}",
                     T("Pfad in llm.profiles.<name>.server.command prüfen", "check the path in llm.profiles.<name>.server.command"))
                binary = Path(script).parent.parent / bonsai.CUDA_SERVER  # Bonsai-demo-Checkout mit CUDA-Build?
                if binary.exists():
                    missing = bonsai.missing_libs(binary, p.server.env.get("LD_LIBRARY_PATH", ""))
                    line(not missing, f"    {T('Bibliotheken für', 'Libraries for')} {bonsai.CUDA_SERVER}"
                         + (f" – {T('fehlt', 'missing')}: {', '.join(missing)}" if missing else ""),
                         "orbwise model add bonsai")
            line(ps["online"], T("    Server erreichbar", "    Server reachable") if ps["online"]
                 else T("    Server läuft gerade nicht", "    Server is not running"),
                 T("startet automatisch beim Aktivieren", "starts automatically when activated") if p.server
                 else T("Server von Hand starten", "start the server manually"))
            if ps["online"]:
                n_ctx = asyncio.run(OpenAICompatLLM(p).server_context())
                if n_ctx:
                    print(f"    ℹ {T('Kontextfenster laut Server', 'Context window reported by server')}: {n_ctx} Token")
                slots = asyncio.run(OpenAICompatLLM(p).server_props()).get("total_slots")
                if isinstance(slots, int) and slots > 1:  # Anfragen landen in verschiedenen Slots → Cache geteilt
                    print(f"    ℹ {slots} Slots – " + T(
                        "für den Prompt-Cache besser einer: llama-server mit -np 1 starten",
                        "one is better for the prompt cache: start llama-server with -np 1"))
    print(T("Sprache:", "Voice:"))
    from .voice.listen import WakeWordFactory, WhisperSTT
    from .voice.tts import PiperTTS
    stt = WhisperSTT(cfg.voice)
    line(stt.available(), T("faster-whisper (Spracherkennung)", "faster-whisper (speech recognition)"), "uv sync --extra voice")
    tts = PiperTTS(cfg.voice)
    line(tts.available(), f"{T('Piper-Stimme', 'Piper voice')} {cfg.voice.tts_voice.name}", tts.error or "")
    wake = WakeWordFactory(cfg.voice)
    line(wake.available(), f"Wake-Word '{cfg.voice.wakeword_model}'", wake.error or "")
    print(T("Desktop (Dateien/Programme öffnen):", "Desktop (opening files/apps):"))
    from .tools.proc import desktop_env
    env = desktop_env()
    session = env.get("WAYLAND_DISPLAY") or env.get("DISPLAY")
    line(bool(session), f"{T('Grafische Sitzung', 'Graphical session')}: {session or T('nicht gefunden', 'not found')}",
         T("Orbwise aus der Desktop-Sitzung starten (Autostart/Terminal)", "start Orbwise from the desktop session (autostart/terminal)"))
    line(bool(env.get("DBUS_SESSION_BUS_ADDRESS")), T("D-Bus-Sitzung", "D-Bus session"),
         T("Orbwise aus der Desktop-Sitzung starten", "start Orbwise from the desktop session"))
    if shutil.which("xdg-mime"):
        import subprocess
        for mime, label in (("text/plain", T("Texteditor", "Text editor")), ("application/pdf", "PDF"),
                            ("inode/directory", T("Ordner", "Folder"))):
            app = subprocess.run(["xdg-mime", "query", "default", mime], capture_output=True, text=True,
                                 env=env).stdout.strip()
            line(bool(app), f"{T('Standardprogramm', 'Default app')} {label}: {app or T('keins', 'none')}",
                 f"xdg-mime default <app>.desktop {mime}")
    print(T("Werkzeuge:", "Tools:"))
    arch = bool(shutil.which("pacman"))
    tools = [("rg", "ripgrep", "ripgrep"), ("plocate", "plocate", "plocate"), ("xdg-open", "xdg-utils", "xdg-utils"),
             ("gtk-launch", "gtk3", "libgtk-3-bin"), ("sudo", "sudo", "sudo"), ("ip", "iproute2", "iproute2"),
             ("ping", "iputils", "iputils-ping")]
    tools += [("fd", "fd", "")] if arch else [("fdfind", "", "fd-find")]
    tools += [("checkupdates", "pacman-contrib", "")] if arch else []
    for tool, arch_pkg, deb_pkg in tools:
        line(bool(shutil.which(tool)), tool, f"sudo pacman -S {arch_pkg}" if arch else f"sudo apt install {deb_pkg}")
    if arch:
        print(f"  {'✔' if shutil.which('yay') or shutil.which('paru') else '–'} AUR helper (yay/paru, "
              + T("optional", "optional") + ")")
    if cfg.tools.privilege_cmd == "dashboard":
        from .askpass import helper_path, write_helper
        try:
            write_helper()
            line(True, T("Root-Rechte: Passwortfeld in der Oberfläche", "Root privileges: password field in the web UI")
                 + f" (sudo -A, {helper_path()})")
        except OSError as e:
            line(False, T("Askpass-Helfer anlegen", "Create askpass helper"), str(e))
    else:
        print(f"  ℹ {T('Root-Rechte über', 'Root privileges via')} {cfg.tools.privilege_cmd} "
              + T("(tools.privilege_cmd: dashboard = Passwortfeld im Dashboard)", "(tools.privilege_cmd: dashboard = password field in the dashboard)"))
    print("Trilium:")
    from .tools.trilium import trilium_status
    tr = asyncio.run(trilium_status(cfg))
    if not tr["enabled"]:
        print(T("  – nicht konfiguriert", "  – not configured") + " (trilium.url, trilium.token)")
    else:
        from .tools.netutil import normalize_url
        line(tr["online"], f"{normalize_url(cfg.trilium.url, '/etapi')}" + (f" (Version {tr.get('version')})" if tr["online"] else ""),
             tr.get("error", ""))
    print("Obsidian:")
    from .tools.obsidian import obsidian_status
    obs = asyncio.run(obsidian_status(cfg))
    if not obs["enabled"]:
        print(T("  – nicht konfiguriert", "  – not configured") + " (obsidian.vault)")
    else:
        line(obs["online"], f"Vault {obs['vault']}" + (f" – {obs['count']} {T('Notizen', 'notes')}" if obs["online"] else ""),
             obs.get("error", ""))
    proxies = [k for k in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY", "all_proxy") if os.environ.get(k)]
    if proxies and (cfg.trilium.enabled or cfg.paperless.enabled):
        print(f"  ℹ Proxy ({', '.join(proxies)}) – " + T("Heimnetz-Dienste werden bewusst direkt angesprochen", "home network services are contacted directly on purpose"))
    print("Paperless:")
    from .tools.paperless import paperless_status
    ps_ = asyncio.run(paperless_status(cfg))
    if not ps_["enabled"]:
        print(T("  – nicht konfiguriert", "  – not configured") + " (paperless.url, paperless.token)")
    else:
        line(ps_["online"], f"{ps_.get('url') or cfg.paperless.url}" + (f" – {ps_['count']} {T('Dokumente', 'documents')} (Version {ps_['version']})"
                                                       if ps_["online"] else ""), ps_.get("error", ""))
    print("Home Assistant:")
    from .tools.homeassistant import ha_status
    hs = asyncio.run(ha_status(cfg))
    if not hs["enabled"]:
        print(T("  – nicht konfiguriert", "  – not configured") + " (homeassistant.url, homeassistant.token)")
    else:
        line(hs["online"], f"{hs.get('url') or cfg.homeassistant.url}" + (
            f" – {hs['entities']} {T('Entitäten', 'entities')} (Version {hs['version']})" if hs["online"] else ""), hs.get("error", ""))
    print("Portainer:")
    from .tools.portainer import portainer_status
    pt = asyncio.run(portainer_status(cfg))
    if not pt["enabled"]:
        print(T("  – nicht konfiguriert", "  – not configured") + " (portainer.url, portainer.token)")
    else:
        line(pt["online"], f"{pt.get('url') or cfg.portainer.url}" + (
            f" – Version {pt['version']}, {T('Umgebungen', 'environments')}: {', '.join(pt['environments']) or '–'}"
            if pt["online"] else ""), pt.get("error", ""))
    print(T("Bild-Modus:", "Image mode:"))
    from . import imagegen_setup
    image_root = imagegen_setup.image_dir(cfg)
    if not imagegen_setup.server_binary(image_root).exists() and not imagegen_setup.manifest(image_root):
        print(T("  – nicht eingerichtet", "  – not set up") + " (orbwise model add qwen-image)")
    else:
        binary = imagegen_setup.server_binary(image_root)
        missing_bin = bonsai.missing_libs(binary, str(binary.parent)) if binary.exists() else ["sd-server"]
        line(not missing_bin, f"sd-server ({binary})", T("fehlt: ", "missing: ") + ", ".join(missing_bin)
             + " – " + T("Vulkan-Treiber installieren (z. B. vulkan-radeon / mesa-vulkan-drivers)",
                         "install the Vulkan driver (e.g. vulkan-radeon / mesa-vulkan-drivers)"))
        paths = imagegen_setup.model_paths(image_root)
        for role in ("diffusion", "llm", "vae", "vision"):
            p = paths.get(role)
            if p or role != "vision":
                line(bool(p and p.exists()), f"{role}: {p.name if p else '?'}" + (
                    f" ({p.stat().st_size / 1e9:.1f} GB)" if p and p.exists() else ""), "orbwise model add qwen-image")
    print("SSH:")
    from .tools.ssh import ssh_status
    sh = ssh_status()
    line(sh["ok"], sh.get("version") or "ssh", sh.get("error", ""))
    if cfg.ssh.hosts:
        print("  ℹ " + T("Kurznamen: ", "Short names: ") + ", ".join(
            f"{k} → {h.host}" + (f":{h.port}" if h.port != 22 else "") for k, h in cfg.ssh.hosts.items()))
    print(T("E-Mail:", "E-mail:"))
    from .tools.mail import mail_status
    ms = asyncio.run(mail_status(cfg))
    if not ms["enabled"]:
        print(T("  – nicht konfiguriert", "  – not configured") + " (mail.username, mail.password)")
    else:
        line(ms["online"], f"{cfg.mail.username} @ {cfg.mail.host}:{cfg.mail.port}" + (
            f" – {ms['unseen']} {T('ungelesen', 'unread')}, {ms['folders']} {T('Ordner', 'folders')}" if ms["online"] else ""),
            ms.get("error", ""))
    print(T("Websuche:", "Web search:"))
    from .tools.web import search_status
    ws = asyncio.run(search_status(cfg))
    if ws["mode"] == "brave":
        fallback = T(" (Rückfall: Suchseiten auslesen)", " (fallback: scraping)") if cfg.tools.search_fallback else ""
        line(ws["online"], "Brave Search API" + fallback, ws.get("error", ""))
    elif ws["mode"] == "searxng":
        print(f"  ℹ SearXNG {cfg.tools.searxng_url} – " + T("Rückfall: Suchseiten auslesen", "fallback: scraping result pages"))
    else:
        print("  ℹ " + T("Suchseiten werden ausgelesen (ddgs) – ohne Bot-Sperr-Risiko: tools.brave_api_key setzen",
                         "result pages are scraped (ddgs) – to avoid bot blocking set tools.brave_api_key"))
    print(T("Kalender:", "Calendar:"))
    if not cfg.calendar.enabled:
        print(T("  – nicht konfiguriert", "  – not configured") + " (calendar.url, username, password)")
    else:
        from .tools.calendar_tools import calendar_status
        cs = asyncio.run(calendar_status(cfg))
        line(cs["online"], f"{cfg.calendar.url} – " + (", ".join(cs.get("calendars", [])) if cs["online"] else T("Fehler", "error")),
             cs.get("error", ""))
    print(T("Wetter & Erinnerungen:", "Weather & reminders:"))
    if cfg.weather.location:
        from .tools.weather import WeatherError, geocode
        try:
            place = asyncio.run(geocode(cfg.weather.location))
            line(True, f"{T('Wetter-Ort', 'Weather location')}: {place.get('name')} ({place.get('admin1') or place.get('country', '')})")
        except WeatherError as e:
            line(False, f"{T('Wetter-Ort', 'Weather location')} '{cfg.weather.location}'", str(e))
    else:
        print(T("  – kein Standardort (weather.location) – Wetter fragt dann nach dem Ort",
                "  – no default location (weather.location) – weather will ask for a place"))
    line(bool(shutil.which("notify-send")), T("Desktop-Benachrichtigungen (notify-send)", "Desktop notifications (notify-send)"),
         "sudo pacman -S libnotify" if shutil.which("pacman") else "sudo apt install libnotify-bin")
    print("NAS:")
    if not cfg.tools.nas_paths:
        print(T("  – kein NAS-Pfad konfiguriert", "  – no NAS path configured") + " (tools.nas_paths)")
    for p in cfg.tools.nas_paths:
        line(p.exists() and p.is_dir() and any(p.iterdir()), f"{p} " + T("gemountet", "mounted"), T("Mount prüfen", "check the mount"))
    from .memory.index import check_file
    line(check_file(cfg.memory.dir / "index.sqlite"), T("Gedächtnis-Suchindex", "Memory search index"), "orbwise reindex")
    from .server import remote_bind_warning
    line(not remote_bind_warning(cfg), T(f"Dashboard nur lokal ({cfg.host})", f"Dashboard local only ({cfg.host})"),
         T("host: 127.0.0.1 setzen", "set host: 127.0.0.1"))
    if cfg.telegram.secret:
        import httpx

        from .telegram import API, explain, redact
        try:  # echte Verbindung: Token gültig? Webhook gesetzt (blockiert getUpdates)?
            me = httpx.get(f"{API}/bot{cfg.telegram.secret}/getMe", timeout=10).json()
            if not me.get("ok"):
                raise RuntimeError(me.get("description", "Fehler"))
            hook = httpx.get(f"{API}/bot{cfg.telegram.secret}/getWebhookInfo", timeout=10).json()
            url = (hook.get("result") or {}).get("url", "")
            line(True, f"Telegram-Bot @{me['result'].get('username', '?')}")
            line(not url, T("kein Webhook gesetzt", "no webhook set"),
                 T("Orbwise entfernt ihn beim nächsten Start", "Orbwise removes it on the next start"))
        except (httpx.HTTPError, RuntimeError, ValueError) as e:
            line(False, "Telegram-Bot", redact(explain(str(e)), cfg.telegram.secret))
        line(bool(cfg.telegram.chat_id), "Telegram chat_id",
             T("Orbwise starten, dem Bot „/start“ schreiben und telegram.chat_id eintragen",
               "start Orbwise, send the bot “/start” and set telegram.chat_id"))
    if cfg.vision.enabled:
        from .tools.vision import vision_status
        vs = asyncio.run(vision_status(cfg))
        if vs["available"] is not None:
            line(vs["available"], T(f"Vision-Modell {cfg.vision.model} (Bildschirm verstehen)",
                                    f"Vision model {cfg.vision.model} (understand the screen)"),
                 f"ollama pull {cfg.vision.model}")
        line(bool(vs["screenshot"]), T("Screenshot-Programm", "Screenshot program") +
             (f": {vs['screenshot'][0]}" if vs["screenshot"] else ""),
             T("z. B. spectacle (KDE), gnome-screenshot (GNOME), grim (Sway/Hyprland) oder maim (X11) installieren",
               "install e.g. spectacle (KDE), gnome-screenshot (GNOME), grim (Sway/Hyprland) or maim (X11)"))


def cmd_reindex(args) -> None:
    from .llm import OllamaLLM
    from .memory import Memory

    cfg = load_config()

    async def run():
        llm = OllamaLLM(cfg.llm)
        mem = Memory(cfg.memory, llm)
        n = await mem.rebuild_index()
        await llm.close()
        mem.close()
        return n

    print(T("Index neu aufgebaut: ", "Index rebuilt: ") + f"{asyncio.run(run())} " + T("Einträge", "entries"))


def cmd_summarize(args) -> None:
    from .llm_router import LLMRouter
    from .memory import Memory

    cfg = load_config()

    async def run():
        llm = LLMRouter(cfg.llm, state_path=cfg.memory.dir.parent / "state.json")
        mem = Memory(cfg.memory, llm)
        days = [args.day] if args.day else mem.days_needing_summary(include_today=True)
        for d in days:
            print(T("Fasse zusammen: ", "Summarising: ") + f"{d} …")
            print(await mem.summarize_day(d))
        await llm.close()
        mem.close()

    asyncio.run(run())


def cmd_model(args) -> None:
    """Profile anzeigen bzw. umschalten (bei laufendem Server live, sonst für den nächsten Start)."""
    import httpx

    from .llm_router import LLMRouter

    cfg = load_config()
    state = cfg.memory.dir.parent / "state.json"
    args.tag = " ".join(args.tag or []) or None
    if args.name == "search":
        return cmd_model_search(args, cfg)
    if args.name in ("add", "remove", "choose"):
        return cmd_model_manage(args, cfg, state)
    router = LLMRouter(cfg.llm, state_path=state)
    base = f"http://127.0.0.1:{cfg.port}"
    headers = {"Host": f"localhost:{cfg.port}"}
    if not args.name:
        for info in router.describe():
            star = "▶" if info["active"] else " "
            extra = T(" · startet Server selbst", " · starts its own server") if info["managed"] else ""
            print(f"{star} {info['name']:<12} {info['backend']:<7} {info['model']}  ({info['base_url']}){extra}")
        print(T("\nUmschalten: orbwise model <name>   ·   neues Modell laden: orbwise model add   ·   entfernen: "
                "orbwise model remove <name>\nEigene Profile (z. B. llama-server): ~/.config/orbwise/config.yaml → llm.profiles",
                "\nSwitch: orbwise model <name>   ·   download a new model: orbwise model add   ·   remove: "
                "orbwise model remove <name>\nCustom profiles (e.g. llama-server): ~/.config/orbwise/config.yaml → llm.profiles"))
        return
    if args.name not in router.profiles:
        print(T("Unbekanntes Profil", "Unknown profile") + f" '{args.name}'. " + T("Vorhanden: ", "Available: ")
              + ", ".join(router.profiles))
        sys.exit(1)
    try:
        r = httpx.post(f"{base}/api/models/{args.name}/activate", headers=headers, timeout=600)
        if r.status_code == 200:
            print(f"✔ {T('Aktiv', 'Active')}: {args.name}")
            return
        print(f"✘ {T('Umschalten fehlgeschlagen', 'Switching failed')}: {r.json().get('detail', r.text)}")
        sys.exit(1)
    except httpx.ConnectError:
        router.active = args.name
        router._write_state()
        print(T(f"Orbwise läuft gerade nicht – '{args.name}' wird beim nächsten Start verwendet.",
                f"Orbwise is not running – '{args.name}' will be used on the next start."))


def cmd_model_search(args, cfg) -> None:
    """orbwise model search <begriff> → GGUF-Repos auf Hugging Face; orbwise model search <nutzer/repo> → Quantisierungen."""
    import httpx

    from . import models as mdl
    term = (args.tag or "").strip()
    if not term:
        print(T("Wonach suchen? orbwise model search qwen3 27b", "Search for what? orbwise model search qwen3 27b"))
        sys.exit(1)
    tag = mdl.normalize_tag(term)
    repo = tag[6:].split(":", 1)[0] if tag and mdl.is_hf(tag) else \
        term if mdl.REPO_RE.match(term) else None
    try:
        if repo:
            gpu = mdl.detect_gpu()
            items = mdl.hf_quants(repo, gpu["vram_gb"], mdl.installed_models(cfg.llm.base_url))
            if not items:
                print(T("Keine einzeln ladbare GGUF-Datei in diesem Repo.", "No single-file GGUF in this repo."))
                return
            print(T(f"{repo} (Grafikspeicher: {gpu['vram_gb']:g} GB; ✔ passt · ~ teils im RAM · ✘ zu groß)",
                    f"{repo} (video memory: {gpu['vram_gb']:g} GB; ✔ fits · ~ partly in RAM · ✘ too big)"))
            for q in items:
                extra = T("  [EMPFOHLEN]", "  [RECOMMENDED]") if q.get("recommended") else ""
                extra += T("  [installiert]", "  [installed]") if q["installed"] else ""
                print(f"  {mdl.FIT_MARK[q['fit']]} {q['quant']:<14} ~{q['download_gb']:>5.1f} GB   "
                      f"orbwise model add {q['tag']}{extra}")
            return
        results = mdl.hf_search(term)
    except (httpx.HTTPError, ValueError) as e:
        print(T(f"✘ Hugging Face nicht erreichbar: {e}", f"✘ Hugging Face not reachable: {e}"))
        sys.exit(1)
    if not results:
        print(T("Nichts gefunden – anderen Begriff versuchen (z. B. nur 'qwen3.6').",
                "Nothing found – try another term (e.g. just 'qwen3.6')."))
        return
    for r in results:
        print(f"  {r['repo']:<55} ⬇ {r['downloads']:>9,}")
    print(T("\nQuantisierungen eines Repos: orbwise model search <nutzer/repo>",
            "\nQuantizations of a repo: orbwise model search <user/repo>"))


def cmd_model_manage(args, cfg, state: Path) -> None:
    """orbwise model add [ollama-name] · orbwise model remove <name> · orbwise model choose (für den Installer)."""
    import subprocess

    import httpx

    from . import models as mdl

    base = f"http://127.0.0.1:{cfg.port}"
    headers = {"Host": f"localhost:{cfg.port}"}
    if args.name == "choose":
        vram = args.vram if args.vram is not None else mdl.detect_gpu()["vram_gb"]
        if not sys.stdin.isatty():
            tag = mdl.recommend(vram)
        else:
            tag = mdl.choose_interactive(vram, mdl.installed_models(cfg.llm.base_url))
        if args.out:
            Path(args.out).write_text(tag or "", encoding="utf-8")
        else:
            print(tag)
        return
    if args.name == "remove":
        if not args.tag:
            print(T("Welches Modell? orbwise model remove <name>", "Which model? orbwise model remove <name>"))
            sys.exit(1)
        if args.tag.strip().lower() == "qwen-image":
            from . import imagegen_setup
            freed = imagegen_setup.remove(cfg)
            print(T(f"✔ Bildmodell gelöscht ({freed / 1e9:.1f} GB frei).", f"✔ Image model deleted ({freed / 1e9:.1f} GB freed)."))
            return
        tag = mdl.unregister_model(state, args.tag)
        if not tag:
            print(T(f"'{args.tag}' ist kein per 'orbwise model add' geladenes Modell.",
                    f"'{args.tag}' is not a model added with 'orbwise model add'."))
            sys.exit(1)
        print(T(f"✔ '{tag}' aus der Modellliste entfernt.", f"✔ Removed '{tag}' from the model list."))
        from . import bonsai
        if tag in bonsai.VARIANTS:
            files = bonsai.model_files(variant=tag)
            size = sum(f.stat().st_size for f in files) / 1e9
            if files and sys.stdin.isatty() and input(
                    T(f"Auch die Modelldateien löschen (~{size:.1f} GB)? [j/N] ",
                      f"Also delete the model files (~{size:.1f} GB)? [y/N] ")).strip().lower() in ("j", "ja", "y", "yes"):
                bonsai.remove_model_files(variant=tag)
                print(T("✔ Modelldateien gelöscht.", "✔ Model files deleted."))
            return
        if shutil.which("ollama") and sys.stdin.isatty() and \
                input(T("Auch die Modelldatei löschen (ollama rm)? [j/N] ", "Also delete the model files (ollama rm)? [y/N] ")
                      ).strip().lower() in ("j", "ja", "y", "yes"):
            subprocess.run(["ollama", "rm", tag])
        return
    # add
    gpu = mdl.detect_gpu()
    tag = mdl.normalize_tag(args.tag or "") if (args.tag or "").strip() else \
        mdl.choose_interactive(gpu["vram_gb"], mdl.installed_models(cfg.llm.base_url))
    if not tag:
        print(T("Ungültiger Modellname.", "Invalid model name."))
        sys.exit(1)
    if tag == "qwen-image":  # Bild-Modus: stable-diffusion.cpp + Qwen-Image-2.1
        from . import imagegen_setup
        if not imagegen_setup.install(gpu, cfg):
            sys.exit(1)
        return
    if tag in ("bonsai", "bonsai-kompakt"):
        from . import bonsai
        name = bonsai.install(state, gpu=gpu, activate=True, variant=tag)
        if not name:
            sys.exit(1)
        print(T("✔ Bonsai eingerichtet und als Modell aktiviert – Orbwise startet den Server bei Bedarf selbst.",
                "✔ Bonsai is set up and activated – Orbwise starts its server itself when needed."))
        try:
            httpx.post(f"{base}/api/models/reload", headers=headers, timeout=10)
            httpx.post(f"{base}/api/models/{name}/activate", headers=headers, timeout=600)
        except httpx.HTTPError:
            pass
        return
    if not shutil.which("ollama"):
        print(T("ollama ist nicht installiert – erst scripts/install.sh ausführen.",
                "ollama is not installed – run scripts/install.sh first."))
        sys.exit(1)
    print(T(f"Lade {tag} … (das kann dauern)", f"Downloading {tag} … (this can take a while)"))
    if subprocess.run(["ollama", "pull", tag]).returncode != 0:
        print(T(f"✘ '{tag}' konnte nicht geladen werden – Name prüfen (ollama.com/library) oder anderes Modell wählen.",
                f"✘ Could not download '{tag}' – check the name (ollama.com/library) or pick another model."))
        sys.exit(1)
    name = mdl.register_model(state, tag)
    activate = args.yes or not sys.stdin.isatty() or input(T(f"'{tag}' jetzt aktivieren? [J/n] ", f"Activate '{tag}' now? [Y/n] ")
                                                ).strip().lower() not in ("n", "nein", "no")
    try:
        httpx.post(f"{base}/api/models/reload", headers=headers, timeout=10)
        if activate:
            r = httpx.post(f"{base}/api/models/{name}/activate", headers=headers, timeout=600)
            ok = r.status_code == 200
            print(f"✔ {T('Aktiv', 'Active')}: {name}" if ok else f"✘ {r.json().get('detail', r.text)}")
        else:
            print(T(f"✔ '{name}' steht jetzt im Modell-Menü.", f"✔ '{name}' is now in the model menu."))
    except httpx.ConnectError:
        if activate:
            mdl.register_model(state, tag, activate=True)
        print(T(f"✔ '{name}' gespeichert" + (" und wird beim nächsten Start verwendet." if activate else "."),
                f"✔ '{name}' saved" + (" and will be used on the next start." if activate else ".")))


def cmd_update(args) -> None:
    from .update import main_update
    main_update(load_config().port)


def cmd_context_test(args) -> None:
    """Kontext-Selbsttest über den laufenden Orbwise-Server (der hat das Modell geladen und kennt die Einstellungen)."""
    import httpx

    from .context_test import format_report

    cfg = load_config()
    print(T("Kontext-Selbsttest läuft – das volle Fenster einzulesen kann einige Minuten dauern …",
            "Context self-test running – reading the full window can take a few minutes …")
          if not args.quick else T("Kontext-Schnelltest läuft …", "Quick context test running …"), flush=True)
    try:
        r = httpx.post(f"http://127.0.0.1:{cfg.port}/api/context/test", params={"quick": args.quick},
                       headers={"Host": f"localhost:{cfg.port}"}, timeout=3600)
    except httpx.ConnectError:
        sys.exit(T("Orbwise läuft nicht – erst starten (orbwise serve), dann den Test wiederholen.",
                   "Orbwise is not running – start it (orbwise serve), then run the test again."))
    if r.status_code != 200:
        try:
            detail = r.json().get("detail", r.text)
        except ValueError:
            detail = r.text
        sys.exit(f"✘ {detail}")
    result = r.json()
    print(format_report(result))
    if result["status"] == "fail":
        sys.exit(1)


def cmd_version(args) -> None:
    from .update import project_root, version
    print(f"Orbwise {version()}\n{project_root()}")


def _migrate_legacy() -> None:
    """Früher hieß das Projekt „Jarvis“: alte Ordner/Starter einmalig umziehen, alten Befehlsnamen erklären."""
    from .migrate import migrate
    try:
        for line in migrate():
            print(f"[orbwise] {line}", file=sys.stderr)
    except OSError as e:
        print(f"[orbwise] Migration: {e}", file=sys.stderr)
    alias = env("VIA_ALIAS") == "1" or Path(sys.argv[0]).name == "jarvis"
    marker = config_path().parent / ".alias-hint-shown"
    if alias and not marker.exists():
        print(T("[orbwise] Hinweis: Der Befehl heißt jetzt 'orbwise' – 'jarvis' funktioniert noch bis Version 0.2.",
                "[orbwise] Note: the command is now called 'orbwise' – 'jarvis' keeps working until version 0.2."),
              file=sys.stderr)
        try:
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.touch()
        except OSError:
            pass


def main(argv: list[str] | None = None) -> None:
    if argv is None and env("NO_MIGRATE") != "1":
        _migrate_legacy()
    try:
        set_lang(load_config().language)
    except Exception:  # noqa: BLE001 – kaputte Config: doctor/serve melden das selbst
        pass
    if env("LANG"):  # z. B. vom Installer, bevor es eine Config gibt
        set_lang(env("LANG"))
    parser = argparse.ArgumentParser(prog="orbwise", description=T("Orbwise – lokaler KI-Assistent",
                                                                  "Orbwise – local AI assistant"))
    sub = parser.add_subparsers(dest="cmd")
    p = sub.add_parser("serve", help=T("Server starten (Standard)", "start the server (default)"))
    p.add_argument("--open", action="store_true", help=T("Browser öffnen", "open the browser"))
    p.add_argument("-v", "--verbose", action="store_true")
    sub.add_parser("doctor", help=T("Installation prüfen", "check the installation"))
    p = sub.add_parser("model", help=T("Modelle anzeigen, umschalten, laden (add) oder entfernen (remove)",
                                       "list, switch, download (add) or remove (remove) models"))
    p.add_argument("name", nargs="?", help=T("Profilname zum Umschalten – oder add / remove / search (auch: add qwen-image)",
                                             "profile to switch to – or add / remove / search"))
    p.add_argument("tag", nargs="*", help=T("bei add: Ollama-Name oder Hugging-Face-Link · bei search: Suchbegriff",
                                            "with add: Ollama name or Hugging Face link · with search: search term"))
    p.add_argument("-y", "--yes", action="store_true", help=T("ohne Rückfragen", "no questions"))
    p.add_argument("--vram", type=float, help=argparse.SUPPRESS)
    p.add_argument("--out", help=argparse.SUPPRESS)
    p = sub.add_parser("context-test", help=T("Kontextfenster mit dem aktiven Modell prüfen (Größe, Cache, Füllung)",
                                              "check the context window with the active model (size, cache, fill)"))
    p.add_argument("--quick", action="store_true", help=T("ohne das Fenster ganz zu füllen", "without filling the window"))
    sub.add_parser("update", help=T("Auf den neuesten Stand bringen (git pull, Abhängigkeiten, Neustart)",
                                    "update (git pull, dependencies, restart)"))
    sub.add_parser("version", help=T("Installierte Version anzeigen", "show the installed version"))
    sub.add_parser("reindex", help=T("Gedächtnis-Suchindex aus den Markdown-Dateien neu aufbauen",
                                     "rebuild the memory search index from the Markdown files"))
    p = sub.add_parser("summarize", help=T("Tageszusammenfassungen erzeugen", "create daily summaries"))
    p.add_argument("day", nargs="?", help=T("YYYY-MM-DD (Standard: alle fälligen Tage)", "YYYY-MM-DD (default: all due days)"))
    p = sub.add_parser("init-config", help=T("Beispielkonfiguration anlegen", "create the example configuration"))
    p.add_argument("--force", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    for noisy in ("httpx", "httpcore", "quic", "niquests", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    if args.cmd is None:
        args = parser.parse_args(["serve", *(argv or sys.argv[1:])])
    {"serve": cmd_serve, "doctor": cmd_doctor, "update": cmd_update, "model": cmd_model, "version": cmd_version,
     "reindex": cmd_reindex, "summarize": cmd_summarize, "init-config": cmd_init_config,
     "context-test": cmd_context_test}[args.cmd](args)


if __name__ == "__main__":
    main()
