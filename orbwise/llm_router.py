"""Modell-Profile: aktives Chat-Modell (Ollama oder OpenAI-kompatibler Server) umschalten, optional den
Modell-Server (z. B. llama-server für Bonsai) selbst starten/stoppen. Embeddings laufen immer über Ollama."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import signal
import subprocess
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path

import httpx

from .config import LLMConfig, ProfileConfig, ServerConfig
from .lang import T
from .llm import ContextOverflow, LLMError, OllamaLLM, OpenAICompatLLM, clean_sampling
from .llm_memory import cache_type

log = logging.getLogger(__name__)

Progress = Callable[[str], Awaitable[None]]


def listening_pids(port: int) -> list[int]:
    """Prozesse, die auf diesem TCP-Port lauschen (aus /proc, ohne ss/lsof)."""
    inodes = set()
    for table in ("/proc/net/tcp", "/proc/net/tcp6"):
        try:
            lines = Path(table).read_text().splitlines()[1:]
        except OSError:
            continue
        for line in lines:
            parts = line.split()
            if len(parts) > 9 and parts[3] == "0A" and int(parts[1].rsplit(":", 1)[1], 16) == port:
                inodes.add(parts[9])
    pids = []
    if not inodes:
        return pids
    for proc in Path("/proc").iterdir():
        if not proc.name.isdigit():
            continue
        try:
            for fd in (proc / "fd").iterdir():
                if os.readlink(fd).removeprefix("socket:[").removesuffix("]") in inodes:
                    pids.append(int(proc.name))
                    break
        except OSError:
            continue
    return pids


def _log_path() -> Path:
    state = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state"))
    state.mkdir(parents=True, exist_ok=True)
    return state / "orbwise-llm.log"


class ManagedServer:
    """Startet einen Modell-Server als eigene Prozessgruppe und wartet, bis er bereit ist."""

    def __init__(self, server: ServerConfig, base_url: str, transport: httpx.AsyncBaseTransport | None = None):
        self.cfg = server
        self.base_url = base_url.rstrip("/")
        self.proc: subprocess.Popen | None = None
        self.transport = transport

    @property
    def health_url(self) -> str:
        if self.cfg.health_url:
            return self.cfg.health_url
        root = self.base_url[:-3] if self.base_url.endswith("/v1") else self.base_url
        return root + "/health"

    async def healthy(self) -> bool:
        try:
            async with httpx.AsyncClient(timeout=3, transport=self.transport) as c:
                r = await c.get(self.health_url)
            return r.status_code == 200
        except httpx.HTTPError:
            return False

    async def port_in_use(self) -> bool:
        """Antwortet überhaupt etwas (auch 503 = lädt noch)?"""
        try:
            async with httpx.AsyncClient(timeout=3, transport=self.transport) as c:
                await c.get(self.health_url)
            return True
        except httpx.HTTPError:
            return False

    def running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    async def ensure_running(self, progress: Progress | None = None) -> bool:
        """True, wenn Orbwise den Server gestartet hat; False, wenn er bereits lief."""
        if await self.healthy():
            return False
        if not self.running() and not await self.port_in_use():
            command = os.path.expanduser(self.cfg.command)
            env = {**os.environ, **{k: os.path.expanduser(str(v)) for k, v in self.cfg.env.items()}}
            if "LD_LIBRARY_PATH" in self.cfg.env and os.environ.get("LD_LIBRARY_PATH"):  # ergänzen statt ersetzen
                env["LD_LIBRARY_PATH"] += ":" + os.environ["LD_LIBRARY_PATH"]
            try:  # Bonsai (Hybrid-Modell): Prompt-Cache über Checkpoints – sonst liest er nach Änderungen viel neu ein
                from .bonsai import cache_flags
                extra = await asyncio.to_thread(cache_flags, command, {k: env[k] for k in self.cfg.env})
            except Exception:  # noqa: BLE001 – nur eine Beschleunigung, der Start darf nicht daran scheitern
                extra = ""
            if extra:
                command = f"{command} {extra}"
            cwd = os.path.expanduser(self.cfg.cwd) if self.cfg.cwd else str(Path.home())
            log_file = _log_path().open("ab")
            log_file.write(f"\n===== Starte: {command}\n".encode())
            log_file.flush()
            self.proc = subprocess.Popen(["bash", "-c", command], env=env, cwd=cwd, stdin=subprocess.DEVNULL,
                                         stdout=log_file, stderr=subprocess.STDOUT, start_new_session=True)
            log_file.close()
            if progress:
                await progress("Modell-Server wird gestartet …")
        waited = 0.0
        while waited < self.cfg.startup_timeout:
            if await self.healthy():
                return True
            if self.proc is not None and self.proc.poll() is not None:
                raise LLMError(f"Modell-Server ist beim Start beendet worden (Exit-Code {self.proc.returncode}).\n"
                               + tail_log() + library_hint(self.cfg.command))
            await asyncio.sleep(1)
            waited += 1
            if progress and int(waited) % 10 == 0:
                await progress(f"Modell wird geladen … {int(waited)} s")
        await self.stop()
        raise LLMError(f"Modell-Server nicht rechtzeitig bereit ({int(self.cfg.startup_timeout)} s).\n" + tail_log())

    def port(self) -> int | None:
        m = re.search(r":(\d+)(?:/|$)", self.base_url)
        return int(m.group(1)) if m else None

    async def stop_foreign(self) -> bool:
        """Einen llama-server beenden, der schon vor Orbwise lief (z. B. aus einer früheren Sitzung) – sonst würde
        ein Neustart mit neuer Kontextgröße einfach den alten Server weiterverwenden. Nur Prozesse, deren
        Befehlszeile nach llama-server aussieht."""
        pids = listening_pids(self.port()) if self.port() else []
        killed = False
        for pid in pids:
            try:
                cmd = Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace")
            except OSError:
                continue
            if "llama" not in cmd.lower():
                continue
            try:
                os.kill(pid, signal.SIGTERM)
                killed = True
            except (ProcessLookupError, PermissionError):
                pass
        for _ in range(100):  # warten, bis der Port frei ist
            if not killed or not await self.port_in_use():
                break
            await asyncio.sleep(0.1)
        return killed

    async def stop(self) -> None:
        if not self.running():
            self.proc = None
            return
        try:
            os.killpg(self.proc.pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass
        for _ in range(100):
            if self.proc.poll() is not None:
                break
            await asyncio.sleep(0.1)
        else:
            try:
                os.killpg(self.proc.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
        self.proc = None


def library_hint(command: str) -> str:
    """Fehlt dem Server eine Bibliothek (typisch: CUDA-Laufzeit libcudart.so.12), sagen, wie man es repariert."""
    try:
        text = _log_path().read_text(errors="replace")[-20000:].rsplit("===== Starte:", 1)[-1]  # nur letzter Start
    except OSError:
        return ""
    arch = re.search(r"unknown model architecture: '?([\w.-]+)", text)
    if arch:
        return T(f"\n→ Dieser llama-server kennt den Modelltyp „{arch.group(1)}“ noch nicht. Für Modelle aus der "
                 "Oberfläche: Einstellungen → Modelle → llama.cpp AKTUALISIEREN (oder orbwise model llamacpp --update).",
                 f"\n→ This llama-server does not know the model type “{arch.group(1)}” yet. For models added in the "
                 "web UI: Settings → Models → update llama.cpp (or orbwise model llamacpp --update).")
    m = re.search(r"error while loading shared libraries: ([^:\s]+)", text)
    if not m:
        return ""
    fix = "orbwise model add bonsai" if "bonsai" in command else T(
        "das CUDA-/ROCm-Laufzeitpaket der Distribution installieren", "install your distribution's CUDA/ROCm runtime")
    return T(f"\n→ Es fehlt die Bibliothek {m.group(1)}. Reparatur: {fix}",
             f"\n→ The library {m.group(1)} is missing. Fix: {fix}")


def tail_log(lines: int = 15) -> str:
    try:
        text = _log_path().read_text(errors="replace").splitlines()[-lines:]
    except OSError:
        return ""
    return "Letzte Zeilen aus ~/.local/state/orbwise-llm.log:\n" + "\n".join(text)


KV_TYPES = ("f16", "q8_0", "q4_0")


def _server_ctx(server: ServerConfig) -> int | None:
    """Kontextgröße laut Startbefehl (BONSAI_CTX oder -c/--ctx-size)."""
    env = str((server.env or {}).get("BONSAI_CTX", "")).strip()
    if env.isdigit():
        return int(env)
    m = re.findall(r"(?:^|\s)(?:-c|--ctx-size)\s+(\d+)", server.command)
    return int(m[-1]) if m else None


def _help_text(command: str, env: dict) -> str:
    """--help des llama-server, den dieser Startbefehl nutzt (direkt oder über den Bonsai-Starter) – leer, wenn
    unbekannt. Einmal je Programm (bonsai._server_help merkt es sich)."""
    try:
        from .bonsai import _server_help
        m = re.match(r"\s*(?:\S+=\S*\s+)*(\S*llama-server)(\s|$)", command)
        if m:
            binary = Path(os.path.expanduser(m.group(1)))
            text = _server_help(binary.parent, {k: os.path.expanduser(str(v)) for k, v in env.items()},
                                subprocess.run, [binary])
        elif "start_llama_server.sh" in command:
            script = re.search(r"(\S*start_llama_server\.sh)", command).group(1)
            text = _server_help(Path(os.path.expanduser(script)).parent.parent, env, subprocess.run)
        else:
            text = ""
    except Exception:  # noqa: BLE001 – unbekannt
        text = ""
    return text


def kv_types(command: str, env: dict) -> list[str]:
    """Angebotene KV-Cache-Stufen: f16/q8_0/q4_0 (falls der Server sie kennt) und zusätzlich 2-/3-Bit-Typen, die ein
    Build anbietet (Forks) – laut „allowed values“ von --cache-type-k."""
    text = _help_text(command, env)
    m = re.search(r"--cache-type-k[^\n]*(?:\n[^\n-][^\n]*)*?allowed values:\s*([\w ,]+)", text)
    if not m:
        return list(KV_TYPES)
    allowed = [t.strip().lower() for t in m.group(1).split(",") if t.strip()]
    base = [t for t in KV_TYPES if t in allowed] or list(KV_TYPES)
    low = [t for t in allowed if re.match(r"^(?:t|i)?q[23](?!\d)", t) and t not in base]  # q2_0, tq3_0, iq2_xs …
    return base + low


def _flash_attn_flag(command: str, env: dict) -> str:
    """-fa on (neuere llama-server) bzw. -fa (ältere) – je nachdem, was die Hilfe des Servers nennt."""
    text = _help_text(command, env)
    line = next((x for x in text.splitlines() if "--flash-attn" in x), "")
    if line and not re.search(r"\b(on|off|auto)\b", line):  # alte Builds: nur ein Schalter ohne Wert
        return "-fa"
    return "-fa on"


class LLMRouter:
    """Bietet dieselbe Schnittstelle wie OllamaLLM (chat_stream, chat, embed, status, close)."""

    def __init__(self, cfg: LLMConfig, state_path: Path | None = None,
                 transport: httpx.AsyncBaseTransport | None = None):
        self.cfg = cfg
        self.state_path = state_path
        self.profiles = cfg.resolved_profiles()
        self.add_downloaded_models()
        self.transport = transport
        self.ollama = OllamaLLM(cfg, transport=transport)  # Embeddings + Entladen
        self.active = self._initial_profile()
        self.client = None
        self.servers: dict[str, ManagedServer] = {}
        self.switching: str | None = None
        self._lock = asyncio.Lock()
        self.detected_ctx: dict[str, int] = {}  # vom Server gemeldete Kontextgröße je Profil
        # Hat der Bild-Modus den Grafikspeicher geliehen (Sprachmodell beendet)? Dann gibt dieser Aufruf ihn zurück
        # (release(restart=…)), bevor das Sprachmodell wieder gebraucht wird – siehe imagegen.py
        self.gpu_borrowed = None
        state = self._read_state()
        self.mode = "tools"  # Tools oder Coding – Coding kann je Modell ein eigenes Kontextfenster haben
        for p in self.profiles.values():  # eigener Server: die Größe steht im Startbefehl (BONSAI_CTX / -c)
            if p.server and (n := _server_ctx(p.server)):
                p.num_ctx = n
        self._base_ctx = {name: p.num_ctx for name, p in self.profiles.items()}  # ohne Oberflächen-Einstellung
        for name, n in (state.get("context") or {}).items():  # in der Oberfläche eingestellt
            if name in self.profiles and isinstance(n, int):
                self._apply_context(name, n)
        for name, p in self.profiles.items():  # aus der config.yaml: Namen vereinheitlichen, Unsinn melden
            if p.sampling:
                try:
                    p.sampling = {m: clean_sampling(p.sampling.get(m)) for m in ("think", "fast")}
                except ValueError as e:
                    log.warning("Sampling-Werte im Profil '%s' ungültig: %s", name, e)
                    p.sampling = {}
        for name, sets in (state.get("sampling") or {}).items():
            if name in self.profiles and isinstance(sets, dict):
                try:
                    self.profiles[name].sampling = {m: clean_sampling(sets.get(m)) for m in ("think", "fast")}
                except ValueError as e:
                    log.warning("Gespeicherte Sampling-Werte für '%s' ungültig: %s", name, e)
        for name, kv in (state.get("kv_cache") or {}).items():
            if name in self.profiles and kv in KV_TYPES and self.profiles[name].server:
                self._apply_kv(name, kv)
        self._build_client()

    def add_downloaded_models(self) -> list[str]:
        """Per `orbwise model add` bzw. Oberfläche geladene Ollama-Modelle als Profile ergänzen."""
        if not self.state_path:
            return []
        from .models import added_models, added_profiles, label_of, slug
        new = []
        for name, raw in added_profiles(self.state_path).items():
            if name in self.profiles:
                continue
            try:
                extra = self.cfg.model_copy(update={"profiles": {name: ProfileConfig.model_validate(raw)}})
            except ValueError as e:
                log.warning("Gespeichertes Profil '%s' ungültig: %s", name, e)
                continue
            self.profiles[name] = extra.resolved_profiles()[name]
            new.append(name)
        for tag in added_models(self.state_path):
            name = slug(tag)
            if name in self.profiles:
                continue
            label = label_of(tag)
            extra = self.cfg.model_copy(update={"profiles": {name: ProfileConfig(backend="ollama", model=tag,
                                                                                  label=label)}})
            self.profiles[name] = extra.resolved_profiles()[name]
            new.append(name)
        return new

    # ---------- Auswahl ----------
    def _initial_profile(self) -> str:
        remembered = self._read_state().get("active_profile")
        for name in (remembered, self.cfg.active):
            if name and name in self.profiles:
                return name
        if self.cfg.active and self.cfg.active not in self.profiles:
            log.warning("Profil '%s' aus llm.active existiert nicht", self.cfg.active)
        return next(iter(self.profiles))

    def _read_state(self) -> dict:
        if self.state_path and self.state_path.exists():
            try:
                return json.loads(self.state_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                return {}
        return {}

    def _write_state(self) -> None:
        if not self.state_path:
            return
        data = self._read_state()
        data["active_profile"] = self.active
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text(json.dumps(data, indent=1), encoding="utf-8")

    @property
    def profile(self) -> ProfileConfig:
        return self.profiles[self.active]

    async def supports_images(self) -> bool:
        """Kann das aktive Modell Bilder direkt sehen? (je Profil gemerkt; nach Wechsel/Neustart neu gefragt)"""
        key = (self.active, id(self.client))
        cache = getattr(self, "_vision_cache", {})
        if key not in cache:
            try:
                cache[key] = bool(await self.client.supports_images())
            except Exception:  # noqa: BLE001 – im Zweifel: Bild über das Vision-Werkzeug
                return False
            self._vision_cache = cache
        return cache[key]

    @property
    def context_size(self) -> int:
        """Kontextfenster des aktiven Modells: vom Server gemeldet > Profil (num_ctx) > llm.num_ctx."""
        return self.detected_ctx.get(self.active) or self.profile.num_ctx or self.cfg.num_ctx

    async def detect_context(self) -> None:
        if isinstance(self.client, OpenAICompatLLM):
            n = await self.client.server_context()
            if n:
                if self.profile.num_ctx and n != self.profile.num_ctx:
                    log.info("Profil '%s': Server meldet %d Token Kontext (Config: %d) – nutze %d",
                             self.active, n, self.profile.num_ctx, n)
                self.detected_ctx[self.active] = n

    def _client_for(self, p: ProfileConfig):
        if p.backend == "openai":
            return OpenAICompatLLM(p, timeout=self.cfg.request_timeout, transport=self.transport)
        client = OllamaLLM(self.cfg.model_copy(update={
            "base_url": p.base_url, "model": p.model, "temperature": p.temperature,
            "num_ctx": p.num_ctx, "think": p.think}), transport=self.transport)
        client.sampling = p.sampling
        return client

    def _build_client(self) -> None:
        self.client = self._client_for(self.profile)
        self.ollama.embed_on_cpu = self.profile.embed_on_cpu

    def server_for(self, name: str) -> ManagedServer | None:
        p = self.profiles[name]
        if not p.server:
            return None
        if name not in self.servers:
            self.servers[name] = ManagedServer(p.server, p.base_url, transport=self.transport)
        return self.servers[name]

    async def _prepare(self, name: str, progress: Progress | None) -> None:
        p = self.profiles[name]
        if p.unload_ollama:
            unloaded = await self.ollama.unload_all()
            if unloaded and progress:
                await progress("Grafikspeicher freigegeben (" + ", ".join(unloaded) + ")")
        server = self.server_for(name)
        if server:
            await server.ensure_running(progress)

    async def start(self, progress: Progress | None = None) -> None:
        """Beim Start von Orbwise: aktives Profil vorbereiten (Server starten usw.). Fehler gehen an den Aufrufer
        (Startanzeige), statt nur im Log zu landen."""
        await self._prepare(self.active, progress)
        await self.detect_context()

    async def warm(self) -> None:
        """Ollama-Modell vorladen (bei einem eigenen Modell-Server ist es nach dem Start schon geladen)."""
        if isinstance(self.client, OllamaLLM):
            await self.client.preload()

    async def remove_profile(self, name: str) -> None:
        """Profil aus der Auswahl nehmen (vorher `orbwise.models.unregister_model`); ein gestarteter Server wird beendet."""
        if name == self.active:
            raise LLMError("Das aktive Modell kann nicht entfernt werden – erst ein anderes wählen.")
        server = self.servers.pop(name, None)
        if server:
            await server.stop()
        self.profiles.pop(name, None)

    async def activate(self, name: str, progress: Progress | None = None) -> None:
        if name not in self.profiles:
            raise LLMError(f"Unbekanntes Profil '{name}'. Vorhanden: {', '.join(self.profiles)}")
        if self.gpu_borrowed:  # Bildmodell zuerst entladen – das neue Modell startet gleich ohnehin
            await self.gpu_borrowed(restart=False)
        async with self._lock:
            if name == self.active and (not self.server_for(name) or await self.server_for(name).healthy()):
                return
            previous = self.active
            self.switching = name
            try:
                # Zuerst den bisherigen, von Orbwise gestarteten Server beenden (gibt VRAM frei)
                old = self.servers.get(previous)
                if old and previous != name:
                    await old.stop()
                n = self.wanted_context(name)  # Fenster des aktuellen Modus (Coding kann ein eigenes haben)
                if previous != name and n and n != self.profiles[name].num_ctx:
                    self._apply_context(name, n)
                await self._prepare(name, progress)
            except LLMError:
                # Zurück zum vorherigen Profil
                self.switching = None
                try:
                    await self._prepare(previous, None)
                except LLMError:
                    pass
                raise
            finally:
                self.switching = None
            old_client = self.client
            self.active = name
            self._build_client()
            self._write_state()
            if old_client is not None and old_client is not self.ollama:
                await old_client.close()
            await self.detect_context()

    # ---------- LLM-Schnittstelle ----------
    async def chat_stream(self, messages: list[dict], tools: list[dict] | None = None,
                          think: bool | None = None, **opts) -> AsyncIterator[dict]:
        """opts: max_tokens, tool_choice (für Komprimierung und Vorwärmen)."""
        if self.gpu_borrowed:  # Bild-Modus hat den Grafikspeicher – zurückgeben, Sprachmodell starten
            await self.gpu_borrowed()
        try:
            async for ev in self.client.chat_stream(messages, tools, think=think, **opts):
                yield ev
        except ContextOverflow as e:
            if e.n_ctx:  # Server kennt seine echte Größe – ab jetzt die verwenden
                self.detected_ctx[self.active] = e.n_ctx
            raise

    async def chat(self, messages: list[dict]) -> str:
        if self.gpu_borrowed:
            await self.gpu_borrowed()
        return await self.client.chat(messages)

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return await self.ollama.embed(texts)

    async def status(self) -> dict:
        p = self.profile
        st = await self.client.status()
        if p.backend == "openai":
            emb = await self.ollama.status()
            st.update({"embed_model": self.cfg.embed_model, "embed_available": emb.get("embed_available", False)})
        st.update({"context": self.context_size, "profile": self.active, "label": p.label, "backend": p.backend, "switching": self.switching,
                   "model": p.model})
        return st

    # ---------- Kontextgröße (Einstellungen → Modelle) ----------
    def _apply_context(self, name: str, n: int) -> None:
        """Kontextfenster eines Profils setzen: Ollama über num_ctx, ein eigener llama-server über BONSAI_CTX bzw.
        -c im Startbefehl (wirkt beim nächsten Start)."""
        p = self.profiles[name]
        p.num_ctx = n
        if p.server:
            env = dict(p.server.env or {})
            command = p.server.command
            if "BONSAI_CTX" in env:
                env["BONSAI_CTX"] = str(n)
            elif re.search(r"(^|\s)(-c|--ctx-size)\s+\d+", command):
                command = re.sub(r"(^|\s)(-c|--ctx-size)\s+\d+", rf"\1\2 {n}", command)
            else:
                command = f"{command} -c {n}"
            p.server = p.server.model_copy(update={"env": env, "command": command})
            self.servers.pop(name, None)  # neuer Startbefehl

    def _apply_kv(self, name: str, kv: str) -> None:
        """KV-Cache-Stufe eines eigenen llama-server setzen (wirkt beim nächsten Start): -ctk/-ctv im Startbefehl,
        beim Bonsai-Starter q4_0 über BONSAI_KV4. Ein quantisierter V-Cache braucht Flash-Attention."""
        p = self.profiles[name]
        if not p.server:
            raise LLMError("Die KV-Cache-Stufe lässt sich nur bei einem eigenen Modell-Server einstellen")
        env = dict(p.server.env or {})
        command = re.sub(r"\s+(?:-ctk|-ctv|--cache-type-k|--cache-type-v)\s+\S+", "", p.server.command)
        env.pop("BONSAI_KV4", None)
        if kv != "f16":
            if "start_llama_server.sh" in command and kv == "q4_0":
                env["BONSAI_KV4"] = "1"  # der Starter setzt dann selbst alles Nötige
            else:
                command += f" -ctk {kv} -ctv {kv}"
                if not re.search(r"(^|\s)(-fa|--flash-attn)(\s|=|$)", command):
                    command += " " + _flash_attn_flag(command, env)
        p.server = p.server.model_copy(update={"env": env, "command": command})
        self.servers.pop(name, None)  # neuer Startbefehl

    async def _reconfigure(self, name: str, apply, key: str, value, progress: Progress | None) -> None:
        """Startoptionen eines Profils ändern und merken (state.json[key]); beim aktiven Profil wird ein eigener
        Server neu gestartet (Ollama lädt bei der nächsten Anfrage neu)."""
        if self.gpu_borrowed and name == self.active:
            await self.gpu_borrowed(restart=False)
        async with self._lock:
            running = self.servers.get(name) or (self.server_for(name) if name == self.active else None)
            if running:
                await running.stop()  # gibt den Grafikspeicher frei, bevor mit neuen Optionen gestartet wird
                if name == self.active:
                    await running.stop_foreign()  # lief schon vor Orbwise → sonst blieben die alten Optionen
            apply()
            self.detected_ctx.pop(name, None)
            if self.state_path and key:
                data = self._read_state()
                data.setdefault(key, {})[name] = value
                self.state_path.parent.mkdir(parents=True, exist_ok=True)
                self.state_path.write_text(json.dumps(data, indent=1), encoding="utf-8")
            if name == self.active:
                await self._prepare(name, progress)
                old_client = self.client
                self._build_client()
                if old_client is not None and old_client is not self.ollama:
                    await old_client.close()
                await self.detect_context()

    # ---------- Kontext je Modus (Tools / Coding) ----------
    def wanted_context(self, name: str, mode: str | None = None) -> int | None:
        """Gewünschtes Fenster eines Profils im Modus: Coding eigener Wert, sonst der Tools-Wert bzw. der Start-Wert."""
        state = self._read_state()
        own = (state.get("context_coding") or {}).get(name) if (mode or self.mode) == "coding" else None
        return own or (state.get("context") or {}).get(name) or self._base_ctx.get(name)

    def apply_mode(self, mode: str) -> None:
        """Beim Start (bevor ein Server läuft): Fenster aller Profile für den Modus setzen."""
        self.mode = mode
        for name, p in self.profiles.items():
            n = self.wanted_context(name, mode)
            if n and n != p.num_ctx:
                self._apply_context(name, n)

    async def set_mode(self, mode: str, progress: Progress | None = None, restart: bool = True) -> bool:
        """Modus gewechselt: hat das aktive Modell dort ein anderes Fenster, startet sein Server damit neu → True.
        restart=False: das aktive Modell nicht anfassen (gleich wird ohnehin ein anderes aktiviert)."""
        self.mode = mode
        for name, p in self.profiles.items():
            n = self.wanted_context(name, mode)
            if name != self.active and n and n != p.num_ctx:
                self._apply_context(name, n)  # läuft nicht – gilt beim nächsten Start
        n = self.wanted_context(self.active, mode)
        if not restart or not n or n == self.profile.num_ctx:
            return False
        name = self.active
        await self._reconfigure(name, lambda: self._apply_context(name, n), None, None, progress)
        return True

    async def set_context(self, name: str, n: int, progress: Progress | None = None, mode: str | None = None) -> None:
        """Kontextfenster ändern und merken (state.json; Coding getrennt). Beim aktiven Profil im aktuellen Modus wird
        ein eigener Server neu gestartet, Ollama lädt das Modell bei der nächsten Anfrage mit der neuen Größe.
        n = 0 im Coding-Modus: eigene Größe aufheben (wieder wie Tools)."""
        if name not in self.profiles:
            raise LLMError(f"Unbekanntes Profil '{name}'")
        mode = mode or self.mode
        key = "context_coding" if mode == "coding" else "context"
        if n == 0 and mode == "coding":
            data = self._read_state()
            (data.get(key) or {}).pop(name, None)
            self.state_path and self.state_path.write_text(json.dumps(data, indent=1), encoding="utf-8")
            n = self.wanted_context(name, "tools") or 0
            if mode != self.mode or not n or n == self.profiles[name].num_ctx:
                return
            await self._reconfigure(name, lambda: self._apply_context(name, n), None, None, progress)
            return
        if not 1024 <= n <= 1048576:
            raise LLMError("Kontextfenster bitte zwischen 1.024 und 1.048.576 Token")
        if mode != self.mode:  # anderer Modus: nur merken – gilt beim Wechsel dorthin
            data = self._read_state()
            data.setdefault(key, {})[name] = n
            if self.state_path:
                self.state_path.parent.mkdir(parents=True, exist_ok=True)
                self.state_path.write_text(json.dumps(data, indent=1), encoding="utf-8")
            return
        await self._reconfigure(name, lambda: self._apply_context(name, n), key, n, progress)
        got = self.detected_ctx.get(name) if name == self.active else None
        if got and got != n:
            raise LLMError(f"Der Modell-Server meldet weiterhin {got} Token Kontext statt {n} – er wurde "
                           "nicht neu gestartet oder übernimmt die Größe nicht (Startbefehl prüfen).")

    async def set_sampling(self, name: str, think: dict | None, fast: dict | None) -> dict:
        """Sampling-Werte je Modus festlegen und merken – gilt ab der nächsten Anfrage, ohne Neustart."""
        if name not in self.profiles:
            raise LLMError(f"Unbekanntes Profil '{name}'")
        try:
            sets = {"think": clean_sampling(think), "fast": clean_sampling(fast)}
        except ValueError as e:
            raise LLMError(str(e)) from e
        self.profiles[name].sampling = sets
        if self.state_path:
            data = self._read_state()
            data.setdefault("sampling", {})[name] = sets
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            self.state_path.write_text(json.dumps(data, indent=1), encoding="utf-8")
        if name == self.active and isinstance(self.client, OllamaLLM):
            self.client.sampling = sets  # OpenAI-Client liest das Profil selbst
        return sets

    async def set_kv(self, name: str, kv: str, progress: Progress | None = None) -> None:
        """KV-Cache-Stufe (f16/q8_0/q4_0) ändern und merken; beim aktiven Profil startet der Server neu."""
        if name not in self.profiles:
            raise LLMError(f"Unbekanntes Profil '{name}'")
        p = self.profiles[name]
        allowed = kv_types(p.server.command, p.server.env) if p.server else list(KV_TYPES)
        if kv not in allowed:
            raise LLMError(f"KV-Cache-Stufe bitte eine von: {', '.join(allowed)}")
        if not self.profiles[name].server:
            raise LLMError("Bei Ollama gilt die KV-Cache-Stufe für den ganzen Ollama-Dienst: sudo systemctl edit ollama "
                           "→ Environment=\"OLLAMA_FLASH_ATTENTION=1\" \"OLLAMA_KV_CACHE_TYPE=q8_0\"")
        await self._reconfigure(name, lambda: self._apply_kv(name, kv), "kv_cache", kv, progress)

    async def memory_info(self) -> tuple[dict | None, str]:
        """Speicher des aktiven Modells samt Kontext (VRAM/RAM) → (Werte, Grund falls keine).
        llama-server: genau aus dem Startlog, sonst geschätzt aus der GGUF-Datei (Pfad laut /props); Ollama: API."""
        from .llm_memory import estimate_from_gguf, parse_llama_log
        if isinstance(self.client, OllamaLLM):
            info = await self.client.memory_info()
            return info, "" if info else "Modell ist gerade nicht in Ollama geladen"
        p = self.profile
        info = None
        if p.server:
            try:
                text = _log_path().read_text(encoding="utf-8", errors="replace")[-500_000:]
                # nur der letzte Start dieses Servers – ältere Einträge gehören zu einer anderen Größe
                info = parse_llama_log(text[text.rfind("===== Starte:"):] if "===== Starte:" in text else text)
            except OSError:
                info = None
        if info:
            return info, ""
        props = await self.client.server_props() if isinstance(self.client, OpenAICompatLLM) else {}
        path = os.path.expanduser(str(props.get("model_path") or ""))
        if not path or not os.path.isfile(path):
            return None, ("Der Modell-Server nennt keine Modelldatei (/props) und sein Log enthält keine "
                          "Speicherangaben" if p.server else "Fremder Modell-Server ohne Angabe der Modelldatei")
        server = p.server
        est = estimate_from_gguf(path, self.context_size, server.command if server else "",
                                 dict(server.env) if server else {})
        return est, "" if est else "Modelldatei ohne lesbare Architektur-Angaben"

    def describe(self) -> list[dict]:
        state = self._read_state()
        ctx, coding = state.get("context") or {}, state.get("context_coding") or {}
        return [{"name": n, "label": p.label, "backend": p.backend, "model": p.model, "base_url": p.base_url,
                 "managed": p.server is not None, "active": n == self.active,
                 "num_ctx": self.detected_ctx.get(n) or p.num_ctx or self.cfg.num_ctx,
                 "ctx_tools": ctx.get(n) or self._base_ctx.get(n), "ctx_coding": coding.get(n),
                 "kv": cache_type(p.server.command, p.server.env) if p.server else None,
                 "kv_settable": p.server is not None,
                 "kv_options": kv_types(p.server.command, p.server.env) if p.server else []}
                for n, p in self.profiles.items()]

    async def close(self) -> None:
        for server in self.servers.values():
            await server.stop()
        if self.client is not None:
            await self.client.close()
        await self.ollama.close()
