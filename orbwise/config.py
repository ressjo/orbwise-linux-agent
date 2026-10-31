"""Konfiguration: Standardwerte + ~/.config/orbwise/config.yaml (oder $ORBWISE_CONFIG)."""

from __future__ import annotations

import os
import re
from pathlib import Path

import yaml
from pydantic import BaseModel, Field, PrivateAttr, field_validator, model_validator

CONFIG_DIR = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "orbwise"
DATA_DIR = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")) / "orbwise"


def wake_phrase(model: str) -> str:
    """Was man sagen muss, aus dem Namen des openWakeWord-Modells: „hey_jarvis_v0.1“ → „Hey Jarvis“."""
    name = re.sub(r"(_v?\d[\w.]*)?(\.onnx|\.tflite)?$", "", os.path.basename(model or ""))
    return " ".join(w.capitalize() for w in name.split("_") if w) or "Wake-Word"


def env(name: str, default: str = "") -> str:
    """$ORBWISE_<name>."""
    return os.environ.get(f"ORBWISE_{name}") or default


class ServerConfig(BaseModel):
    """Optionaler Modell-Server, den Orbwise selbst startet und stoppt (z. B. llama-server)."""
    command: str
    env: dict[str, str] = Field(default_factory=dict)
    cwd: str = ""
    startup_timeout: float = 240.0
    # Standard: <base_url ohne /v1>/health
    health_url: str = ""


class ProfileConfig(BaseModel):
    """Ein Modell-Profil. Leere Felder übernehmen die Werte aus dem llm-Block."""
    label: str = ""
    # "ollama" oder "openai" (OpenAI-kompatibler Server: llama-server, LM Studio, vLLM …)
    backend: str = "ollama"
    base_url: str = ""
    model: str = ""
    api_key: str = ""
    temperature: float | None = None
    num_ctx: int | None = None
    think: bool | None = None
    # Embeddings (Gedächtnis) über Ollama auf der CPU rechnen – spart Grafikspeicher
    embed_on_cpu: bool = False
    # Vor dem Aktivieren alle Ollama-Modelle aus dem Grafikspeicher entladen
    unload_ollama: bool = False
    server: ServerConfig | None = None
    # Sampling je Modus, z. B. {"think": {"temperature": 1.0, "top_p": 0.95}, "fast": {"presence_penalty": 1.5}} –
    # fehlende Werte: Standard des Servers/Modells (temperature: siehe oben); in der Oberfläche einstellbar
    sampling: dict[str, dict[str, float]] = Field(default_factory=dict)

    @field_validator("backend")
    @classmethod
    def _backend(cls, v: str) -> str:
        v = v.strip().lower()
        if v not in ("ollama", "openai"):
            raise ValueError("backend muss 'ollama' oder 'openai' sein")
        return v


class LLMConfig(BaseModel):
    # Adresse von Ollama (für Ollama-Profile und immer für die Embeddings des Gedächtnisses)
    base_url: str = "http://localhost:11434"
    model: str = "qwen3:14b"
    embed_model: str = "bge-m3"
    temperature: float = 0.6
    num_ctx: int = 16384
    # Qwen3 "Thinking" abschalten – deutlich schnellere Antworten
    think: bool = False
    keep_alive: str = "30m"
    request_timeout: float = 300.0
    # Modell-Profile; leer = ein Profil "standard" aus den Werten oben
    profiles: dict[str, ProfileConfig] = Field(default_factory=dict)
    # Profil beim Start (eine Auswahl in der Oberfläche wird gemerkt und hat Vorrang)
    active: str = ""
    # GGUF-Modelle für llama.cpp: Downloads von Hugging Face landen hier, vorhandene Dateien erscheinen im
    # Modell-Menü unter „Auf der Festplatte“
    models_dir: str = "~/models"

    def resolved_profiles(self) -> dict[str, ProfileConfig]:
        """Alle Profile mit aufgefüllten Standardwerten."""
        raw = self.profiles or {"standard": ProfileConfig(backend="ollama")}
        out = {}
        for name, p in raw.items():
            default_url = self.base_url if p.backend == "ollama" else "http://127.0.0.1:8080/v1"
            out[name] = p.model_copy(update={
                "label": p.label or name,
                "base_url": (p.base_url or default_url).rstrip("/"),
                "model": p.model or (self.model if p.backend == "ollama" else "default"),
                "temperature": self.temperature if p.temperature is None else p.temperature,
                "num_ctx": self.num_ctx if p.num_ctx is None else p.num_ctx,
                "think": self.think if p.think is None else p.think,
            })
        return out


class MemoryConfig(BaseModel):
    dir: Path = DATA_DIR / "memory"
    # Gesamtbudget (geschätzte Tokens) für den Prompt; muss deutlich unter num_ctx liegen
    context_budget_tokens: int | None = None  # None = automatisch: Kontextfenster des Modells minus Antwortreserve
    retrieval_top_k: int = 6
    # Erinnerungen nur einblenden, wenn sie wirklich passen (Embedding-Ähnlichkeit; ohne Embeddings: ≥ 2 gleiche Wörter)
    retrieval_min_similarity: float = 0.5
    retrieval_max_tokens: int = 1500  # Erinnerungen in der ersten Frage einer Epoche (danach ⅓, nur neue)
    facts_max_tokens: int = 1200
    # Nach einer Antwort schon in Ruhe komprimieren, wenn der Kontext fast voll ist (die nächste Frage wartet dann
    # nicht darauf). Aus = erst komprimieren, wenn es wirklich nötig ist.
    compact_idle: bool = True
    # Tagesübersicht erzeugen, wenn so viele Minuten keine Aktivität war
    summarize_idle_minutes: int = 15


class VoiceConfig(BaseModel):
    enabled: bool = True
    # Sprache der Spracherkennung – leer = wie die globale Einstellung "language"
    language: str = ""
    stt_model: str = "small"
    stt_device: str = "cpu"
    stt_compute_type: str = "int8"
    wakeword_model: str = "hey_jarvis"
    wakeword_threshold: float = 0.5
    silence_ms: int = 900
    max_record_seconds: int = 20
    no_speech_timeout_seconds: float = 5.0


class ToolsConfig(BaseModel):
    search_paths: list[Path] = Field(default_factory=lambda: [Path.home()])
    nas_paths: list[Path] = Field(default_factory=list)
    # "dashboard" (Passwortfeld in der Orbwise-Oberfläche, sudo -A), "pkexec" (Polkit-Dialog des Systems)
    # oder "sudo" (sudo -n, benötigt NOPASSWD-Regel)
    privilege_cmd: str = "dashboard"
    # so lange merkt sich Orbwise das im Dashboard eingegebene Passwort (nur im Arbeitsspeicher, nur für Aufträge
    # am Rechner – nie für Telegram/Routinen); 0 = jedes Mal fragen
    sudo_remember_minutes: int = 15
    # "auto" (erkennen), "pacman" (Arch/Manjaro/EndeavourOS) oder "apt" (Debian/Ubuntu/Mint)
    package_manager: str = "auto"
    # "auto" (yay/paru suchen), "yay", "paru" oder "none"
    aur_helper: str = "auto"
    shell_timeout: int = 120
    # Tools oder ganze Gruppen abschalten (spart Kontext bei kleinen Modellen), z. B. [sysadmin, web, open_ports]
    disabled: list[str] = Field(default_factory=list)
    # Werkzeuge, die standardmäßig aus sind (dünne Hüllen um einzelne Befehle wie ps, systemctl, df – das Modell nutzt
    # dafür run_shell), wieder einschalten: Gruppen- oder Werkzeugnamen, z. B. [sysadmin] oder [list_directory]
    extra: list[str] = Field(default_factory=list)
    # so viele Modellschritte (Tool-Runden) darf eine Aufgabe höchstens brauchen
    max_steps: int = 25
    # „Lesen + Bearbeiten“: Bauen/Tests laufen isoliert ohne Netz (bubblewrap) – zusätzlich schreibbare Ordner
    jail_writable: list[str] = Field(default_factory=list)
    update_timeout: int = 3600
    max_output_chars: int = 6000
    searxng_url: str | None = None
    # Brave Search API (offizielle Schnittstelle, kein Auslesen von Suchseiten); alternativ $ORBWISE_BRAVE_API_KEY
    brave_api_key: str = ""
    # mit Brave-Schlüssel bei Fehlern trotzdem auf das Auslesen der Suchseiten (ddgs) ausweichen?
    search_fallback: bool = False

    @property
    def brave_key(self) -> str:
        return self.brave_api_key or env("BRAVE_API_KEY")

    @field_validator("privilege_cmd")
    @classmethod
    def _privilege(cls, v: str) -> str:
        return "dashboard" if v == "orbwise" else v  # „orbwise“: früherer Name des Passwortfelds

    @field_validator("search_paths", "nas_paths", mode="before")
    @classmethod
    def _paths(cls, v):
        # In YAML wird ein nacktes ~ zu None – als Home-Verzeichnis interpretieren
        if v is None:
            return []
        return ["~" if p is None else p for p in v]


class ObsidianConfig(BaseModel):
    # Pfad zum Obsidian-Vault (normaler Ordner mit Markdown-Dateien), z. B. ~/Obsidian/Notes
    vault: str = ""
    # Ordner im Vault für neue Notizen
    inbox: str = "Inbox"
    # so viel Notiztext geht höchstens an das Modell (größere Notizen: abschnittsweise bzw. nur relevante Stellen)
    max_chars: int = 8000

    @property
    def path(self) -> Path | None:
        return Path(self.vault).expanduser() if self.vault.strip() else None

    @property
    def enabled(self) -> bool:
        return bool(self.path and self.path.is_dir())


class TriliumConfig(BaseModel):
    # z. B. http://localhost:8080 oder die Adresse auf dem NAS
    url: str = ""
    # ETAPI-Token (Trilium: Optionen → ETAPI); alternativ $ORBWISE_TRILIUM_TOKEN
    token: str = ""
    timeout: float = 20.0
    max_chars: int = 8000
    # HTTPS mit selbstsigniertem Zertifikat: false – oder Pfad zur CA-/Zertifikatsdatei
    verify_ssl: bool | str = True

    @property
    def api_token(self) -> str:
        return self.token or env("TRILIUM_TOKEN")

    @property
    def enabled(self) -> bool:
        return bool(self.url and self.api_token)


class PaperlessConfig(BaseModel):
    # z. B. http://nas.local:8000
    url: str = ""
    # API-Token (Paperless → Profil oben rechts → API-Auth-Token); alternativ $ORBWISE_PAPERLESS_TOKEN
    token: str = ""
    timeout: float = 30.0
    # HTTPS mit selbstsigniertem Zertifikat: false – oder Pfad zur CA-/Zertifikatsdatei
    verify_ssl: bool | str = True
    # so viel Dokumenttext geht höchstens an das Modell (größere Dokumente: nur relevante Stellen)
    max_chars: int = 5000

    @property
    def api_token(self) -> str:
        return self.token or env("PAPERLESS_TOKEN")

    @property
    def enabled(self) -> bool:
        return bool(self.url and self.api_token)


class PortainerConfig(BaseModel):
    # z. B. https://nas.local:9443
    url: str = ""
    # Zugriffstoken (Portainer → Benutzer oben rechts → My account → Access tokens); alternativ $ORBWISE_PORTAINER_TOKEN
    token: str = ""
    # Umgebung (Name oder ID), wenn Portainer mehrere verwaltet; leer = die erste erreichbare
    environment: str = ""
    timeout: float = 60.0
    verify_ssl: bool | str = True

    @property
    def api_token(self) -> str:
        return self.token or env("PORTAINER_TOKEN")

    @property
    def enabled(self) -> bool:
        return bool(self.url and self.api_token)


class ImageConfig(BaseModel):
    """Bild-Modus: stable-diffusion.cpp (sd-server) mit Qwen-Image 2.1, Z-Image Turbo, FLUX.1 schnell oder SDXL – laden im
    Bild-Modus unter „Bildmodelle“ oder mit `orbwise model add qwen-image` (z-image-turbo, flux-schnell, sdxl)."""
    # Ordner mit bin/sd-server und models/ (leer = ~/orbwise-image bzw. $ORBWISE_IMAGE_DIR)
    dir: str = ""
    # wohin die Bilder kommen (leer = ~/Bilder/Orbwise bzw. ~/Pictures/Orbwise)
    output_dir: str = ""
    port: int = 7861
    steps: int = 20
    cfg_scale: float = 6.0
    size: str = "1:1"  # Seitenverhältnis: 1:1, 4:3, 3:4, 16:9, 9:16, 3:2, 2:3 – oder z. B. "1280x720"
    megapixels: float = 1.0  # Auflösung: 0.5 schnell · 1 Standard · 1.5 fein · 2 groß (mehr = langsamer, mehr VRAM)
    # Immer nur ein Modell im Speicher: das Sprachmodell (eigener llama-server, Ollama) geht vor dem ersten Bild aus dem
    # Grafikspeicher – always (Standard); auto = nur bei weniger als 16 GB VRAM; never = beide geladen lassen
    unload_llm: str = "always"
    # so lange bleibt das Bildmodell nach dem letzten Bild geladen (danach kommt das Sprachmodell zurück)
    idle_minutes: int = 10
    # zusätzliche Startoptionen für sd-server (leer = passend zum VRAM: --offload-to-cpu, --vae-tiling …)
    extra_args: str = ""
    startup_timeout: float = 600.0


class SshHost(BaseModel):
    host: str
    port: int = 22
    user: str = ""  # nur Vorschlag im Anmeldedialog – gefragt wird trotzdem jedes Mal


class SshConfig(BaseModel):
    # Kurznamen für ssh_connect, z. B. nas: {host: 192.168.1.10, user: admin}; andere Adressen gehen auch direkt
    hosts: dict[str, SshHost] = Field(default_factory=dict)
    # so lange bleibt eine Verbindung nach dem letzten Befehl offen (danach wird neu nach dem Passwort gefragt)
    keep_minutes: int = 15
    connect_timeout: int = 15


class HomeAssistantConfig(BaseModel):
    # z. B. http://homeassistant.local:8123
    url: str = ""
    # Langlebiges Zugriffstoken (Profil → Sicherheit); alternativ $ORBWISE_HA_TOKEN
    token: str = ""
    timeout: float = 15.0
    verify_ssl: bool | str = True

    @property
    def api_token(self) -> str:
        return self.token or env("HA_TOKEN")

    @property
    def enabled(self) -> bool:
        return bool(self.url and self.api_token)


class CalendarConfig(BaseModel):
    # iCloud: https://caldav.icloud.com – funktioniert mit jedem CalDAV-Server (Nextcloud, Radicale, …)
    url: str = ""
    username: str = ""
    # App-spezifisches Passwort (Apple-ID → Anmeldung und Sicherheit); alternativ $ORBWISE_CALENDAR_PASSWORD
    password: str = ""
    # Kalender, die gelesen werden (leer = alle)
    calendars: list[str] = Field(default_factory=list)
    # Kalender für neue Termine (leer = erster gelesener)
    default_calendar: str = ""
    timeout: float = 20.0

    @property
    def api_password(self) -> str:
        return self.password or env("CALENDAR_PASSWORD")

    @property
    def enabled(self) -> bool:
        return bool(self.url and self.username and self.api_password)


class WeatherConfig(BaseModel):
    # Standardort, z. B. "Freiburg im Breisgau" – leer = Orbwise fragt nach dem Ort
    location: str = ""


class MailConfig(BaseModel):
    # IMAP-Postfach – für Proton Mail über die lokale Proton Mail Bridge (Standard: 127.0.0.1:1143, STARTTLS)
    host: str = "127.0.0.1"
    port: int = 1143
    security: str = "starttls"  # starttls | ssl | none
    username: str = ""
    # Bei Proton: das Bridge-Passwort (nicht das Proton-Passwort); alternativ $ORBWISE_MAIL_PASSWORD
    password: str = ""
    # Zertifikat prüfen? Leer = nur bei fremden Servern (die Bridge auf localhost nutzt ein eigenes Zertifikat)
    verify_ssl: bool | None = None
    archive_folder: str = "Archive"
    trash_folder: str = "Trash"
    timeout: float = 30.0
    # so viel Mailtext geht höchstens an das Modell
    max_chars: int = 6000
    # Mails senden (optional, standardmäßig aus): immer erst nach Bestätigung in einem Fenster, in dem Empfänger,
    # Betreff und Text noch bearbeitet werden können. Proton Mail Bridge: SMTP auf 127.0.0.1:1025 mit STARTTLS.
    send_enabled: bool = False
    smtp_host: str = ""          # leer = wie host
    smtp_port: int = 1025
    smtp_security: str = "starttls"  # starttls | ssl | none
    from_address: str = ""       # leer = username

    @field_validator("security", "smtp_security")
    @classmethod
    def _security(cls, v: str) -> str:
        v = (v or "starttls").strip().lower()
        return v if v in ("starttls", "ssl", "none") else "starttls"

    @property
    def smtp_server(self) -> str:
        return self.smtp_host or self.host

    @property
    def sender(self) -> str:
        return self.from_address or self.username

    @property
    def secret(self) -> str:
        return self.password or env("MAIL_PASSWORD")

    @property
    def enabled(self) -> bool:
        return bool(self.host and self.username and self.secret)

    @property
    def verify(self) -> bool:
        if self.verify_ssl is not None:
            return self.verify_ssl
        return self.host.strip().lower() not in ("127.0.0.1", "localhost", "::1")


BRIEFING_SECTIONS = ("weather", "calendar", "reminders", "mail", "paperless_inbox", "news", "updates", "storage")


class TelegramConfig(BaseModel):
    # Telegram-Bot (optional): vom Handy fragen, Erinnerungen aufs Handy, Rückfragen per Knopf.
    # Token von @BotFather (oder $ORBWISE_TELEGRAM_TOKEN); chat_id nennt der Bot nach „/start“.
    token: str = ""
    chat_id: int = 0
    # Hierhin speichert der Bot Dateien und Fotos, die du vom Handy schickst
    inbox_dir: Path | None = None

    @property
    def inbox(self) -> Path:
        return (self.inbox_dir or Path.home() / "Downloads" / "Orbwise-Telegram").expanduser()

    @property
    def secret(self) -> str:
        return self.token or env("TELEGRAM_TOKEN")

    @property
    def enabled(self) -> bool:
        return bool(self.secret and self.chat_id)


class VisionConfig(BaseModel):
    # Bildschirm und Bilder verstehen („Was ist das für eine Fehlermeldung?“) – mit dem geladenen Modell, wenn es Bilder
    # sehen kann (llama-server mit Bildmodul/mmproj, Ollama-Modell mit „vision“). Ein zweites Bildmodell gibt es nicht
    # mehr; alte Schlüssel (backend, base_url, model, api_key, keep_alive) werden ignoriert.
    enabled: bool = True
    timeout: float = 180.0
    # Eigener Screenshot-Befehl, {file} = Zieldatei (leer = automatisch: grim, spectacle, gnome-screenshot, …)
    screenshot_command: str = ""


class BriefingConfig(BaseModel):
    # Punkte in dieser Reihenfolge (Datum/Uhrzeit steht immer am Anfang); nicht eingerichtete Dienste entfallen
    sections: list[str] = Field(default_factory=lambda: list(BRIEFING_SECTIONS))
    # Termine und Erinnerungen/Fristen der nächsten Tage (0 = nur heute; 1 = heute + morgen …)
    lookahead_days: int = 2
    # Schlagzeilen zu diesen Themen (leer = keine Nachrichten), je Thema so viele
    news_topics: list[str] = Field(default_factory=list)
    news_count: int = 3
    # Paperless: Tag des Posteingangs – leer = die in Paperless als Posteingang markierten Tags
    inbox_tag: str = ""
    # eigener Wunsch an das Modell, z. B. "Halte dich kurz und fang mit den Terminen an."
    instructions: str = ""

    @field_validator("sections")
    @classmethod
    def _sections(cls, v: list[str]) -> list[str]:
        return list(dict.fromkeys(s for s in v if s in BRIEFING_SECTIONS))

    @field_validator("lookahead_days", "news_count")
    @classmethod
    def _small(cls, v: int) -> int:
        return max(0, min(int(v), 14))


class CodingConfig(BaseModel):
    # Coding-Modus (Arbeitsweise nach OpenCode 2)
    snapshots: bool = True          # vor/nach jeder Antwort Schnappschuss des Projekts → „Rückgängig“
    subagent_steps: int = 15        # höchstens so viele Schritte je Unteragent (explore/general)
    max_steps: int = 60             # Werkzeug-Schritte je Antwort im Coding-Modus (Tools-Modus: tools.max_steps)
    max_jobs: int = 4               # gleichzeitig laufende Hintergrund-Befehle (shell mit background=true)
    # Projektanweisungen aus dem Projektordner (die erste vorhandene Datei kommt in den Systemprompt)
    instructions: list[str] = Field(default_factory=lambda: ["AGENTS.md", "CLAUDE.md"])
    # Ziel-Modus (/goal): arbeitet ohne „mach weiter“ – höchstens so viele Werkzeug-Schritte bzw. Minuten
    goal_max_steps: int = 300
    # Code-Check (🛡): so viele Runden „beheben → erneut prüfen“ höchstens
    review_rounds: int = 2
    goal_max_minutes: int = 120

    @field_validator("subagent_steps", "max_jobs")
    @classmethod
    def _small(cls, v: int) -> int:
        return max(1, min(int(v), 50))

    @field_validator("max_steps")
    @classmethod
    def _steps(cls, v: int) -> int:
        return max(1, min(int(v), 500))


class SandboxConfig(BaseModel):
    # Coding-Sandbox: Befehle laufen in einem Container (Podman bevorzugt, sonst Docker) auf einer Arbeitskopie
    engine: str = "auto"            # auto | podman | docker
    image: str = ""                 # leer = orbwise-sandbox:4 (wird beim ersten Einschalten gebaut)
    memory: str = "4g"
    cpus: float = 2.0
    network: bool = True            # Standard für neue Sandboxen (je Chat umschaltbar)
    idle_minutes: int = 30          # danach wird der Container gestoppt (beim nächsten Befehl wieder gestartet)

    @field_validator("engine")
    @classmethod
    def _engine(cls, v: str) -> str:
        v = (v or "auto").strip().lower()
        return v if v in ("auto", "podman", "docker") else "auto"


class GitHubConfig(BaseModel):
    # Token: leer = $ORBWISE_GITHUB_TOKEN / $GH_TOKEN / $GITHUB_TOKEN, sonst das Token einer angemeldeten GitHub-CLI (gh)
    token: str = ""
    clone_dir: str = ""             # wohin geklont wird (leer = ~/Projekte bzw. ~/Projects)


class Config(BaseModel):
    # Abschnitte, wie sie aus config.yaml kamen – Grundlage für die Skills (skills.apply), nicht Teil der Datei
    _skills_base: dict | None = PrivateAttr(default=None)
    # Sprache von Orbwise und der Oberfläche: "de" (Deutsch) oder "en" (English)
    language: str = "de"
    host: str = "127.0.0.1"
    port: int = 8765
    # Nur mit true startet Orbwise auf einer anderen Adresse als 127.0.0.1 (Dashboard hat keine Anmeldung!)
    allow_remote: bool = False
    assistant_name: str = "Orbwise"
    user_name: str = ""
    # Zusätzliche Persönlichkeits-/Verhaltensanweisungen für den System-Prompt
    persona_extra: str = ""
    # Bundesland für regionale Feiertage (date_info), z. B. "BW"; leer = nur bundesweite
    holiday_region: str = ""
    llm: LLMConfig = Field(default_factory=LLMConfig)
    memory: MemoryConfig = Field(default_factory=MemoryConfig)
    voice: VoiceConfig = Field(default_factory=VoiceConfig)
    tools: ToolsConfig = Field(default_factory=ToolsConfig)
    trilium: TriliumConfig = Field(default_factory=TriliumConfig)
    obsidian: ObsidianConfig = Field(default_factory=ObsidianConfig)
    paperless: PaperlessConfig = Field(default_factory=PaperlessConfig)
    homeassistant: HomeAssistantConfig = Field(default_factory=HomeAssistantConfig)
    portainer: PortainerConfig = Field(default_factory=PortainerConfig)
    ssh: SshConfig = Field(default_factory=SshConfig)
    image: ImageConfig = Field(default_factory=ImageConfig)
    weather: WeatherConfig = Field(default_factory=WeatherConfig)
    calendar: CalendarConfig = Field(default_factory=CalendarConfig)
    briefing: BriefingConfig = Field(default_factory=BriefingConfig)
    mail: MailConfig = Field(default_factory=MailConfig)
    telegram: TelegramConfig = Field(default_factory=TelegramConfig)
    vision: VisionConfig = Field(default_factory=VisionConfig)
    coding: CodingConfig = Field(default_factory=CodingConfig)
    sandbox: SandboxConfig = Field(default_factory=SandboxConfig)
    github: GitHubConfig = Field(default_factory=GitHubConfig)
    # Zusätzliche Websites für open_website: Name → URL; "{q}" wird durch die Suche ersetzt
    websites: dict[str, str] = Field(default_factory=dict)

    @field_validator("language")
    @classmethod
    def _lang(cls, v: str) -> str:
        v = (v or "de").strip().lower()[:2]
        return v if v in ("de", "en") else "de"

    @model_validator(mode="after")
    def _language_defaults(self) -> "Config":
        if not self.voice.language:
            self.voice.language = self.language
        return self

    def expand_paths(self) -> "Config":
        self.memory.dir = self.memory.dir.expanduser()
        self.tools.search_paths = [p.expanduser() for p in self.tools.search_paths]
        self.tools.nas_paths = [p.expanduser() for p in self.tools.nas_paths]
        return self


def config_path() -> Path:
    return Path(env("CONFIG") or CONFIG_DIR / "config.yaml").expanduser()


def load_config(path: Path | None = None, skills: bool = True) -> Config:
    """config.yaml lesen; skills=True legt die eingerichteten Skills darüber (Werte, Schalter, Geheimnisse aus dem
    Schlüsselbund) und zieht Geheimnisse, die noch im Klartext in der Datei stehen, dorthin um."""
    path = path or config_path()
    data = {}
    if path.exists():
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    cfg = Config.model_validate(data).expand_paths()
    if skills:
        from . import skills as skill_store
        try:
            skill_store.migrate_plaintext(cfg, path)
        except OSError as e:  # z. B. schreibgeschützte config.yaml – dann bleibt sie, wie sie ist
            import logging
            logging.getLogger(__name__).warning("Zugangsdaten nicht umgezogen: %s", e)
        skill_store.apply(cfg)
    return cfg
