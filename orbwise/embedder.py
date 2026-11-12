"""Embeddings für die Gedächtnis-Suche über llama.cpp – ein eigener kleiner llama-server im Embedding-Modus.

Das Embedding-Modell (Standard: bge-m3) ist klein und läuft auf der CPU: so nimmt es dem Sprachmodell keinen
Grafikspeicher weg und stürzt nicht ab, wenn der knapp ist. Der Server startet erst, wenn zum ersten Mal etwas
einzubetten ist, und bleibt dann stehen.

Woher die Modelldatei kommt (die erste, die es gibt):
1. `llm.embed_model` ist selbst ein Pfad zu einer .gguf-Datei,
2. eine .gguf-Datei mit diesem Namen unter `llm.models_dir` (dorthin lädt `orbwise model embed` sie),
3. das gleichnamige Modell aus einem früheren Ollama: dessen Dateien sind GGUF und liegen auf der Platte – Orbwise
   liest sie direkt, Ollama muss dafür nicht laufen. Wer umsteigt, behält so dieselben Vektoren im Suchindex.

Ohne Modelldatei oder ohne llama.cpp gibt es keine Embeddings; die Suche fällt dann auf Stichworte zurück (wie
bisher, wenn Ollama aus war). `orbwise doctor` sagt, was fehlt.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
import shlex
import time
from pathlib import Path
from typing import Any

import httpx

from .config import ServerConfig
from .llm import LLMError

log = logging.getLogger(__name__)

PORT = 8079            # neben den Sprachmodellen (ab 8080)
CONTEXT = 8192         # bge-m3 liest bis 8192 Token
MAX_CHARS = 12_000     # längere Texte werden gekürzt (≈ 4.000 Token) – ein Eintrag soll den Server nie sprengen
BATCH = 16             # so viele Texte je Anfrage
MISSING_TTL = 30.0    # so lange gilt „Modelldatei fehlt“, bevor wieder gesucht wird (Suche liest Ordner)
RETRY_AFTER = 300.0   # nach einem gescheiterten Start so lange nicht erneut versuchen – Fragen sollen nicht warten
OLLAMA_DIRS = ("~/.ollama/models", "/usr/share/ollama/.ollama/models", "/var/lib/ollama/.ollama/models",
               "/var/lib/ollama/models")


# ---------------------------------------------------------------- Modelldatei finden
def ollama_blob(tag: str, dirs: tuple[str, ...] | None = None) -> Path | None:
    """GGUF-Datei eines Ollama-Modells (`bge-m3`, `bge-m3:latest`, `nutzer/modell:tag`) – Ollama legt je Modell ein
    Manifest ab, das auf die Datei unter blobs/ zeigt. None, wenn es das Modell dort nicht gibt."""
    name, _, version = tag.partition(":")
    version = version or "latest"
    owner, _, model = name.rpartition("/")
    roots = [os.environ["OLLAMA_MODELS"]] if os.environ.get("OLLAMA_MODELS") else []
    for base in (*roots, *(dirs or OLLAMA_DIRS)):
        root = Path(os.path.expanduser(base))
        manifest = root / "manifests" / "registry.ollama.ai" / (owner or "library") / model / version
        try:
            layers = json.loads(manifest.read_text(encoding="utf-8")).get("layers") or []
        except (OSError, ValueError):
            continue
        for layer in layers:
            if str(layer.get("mediaType", "")).endswith("image.model"):
                blob = root / "blobs" / str(layer.get("digest", "")).replace(":", "-")
                if blob.is_file() and os.access(blob, os.R_OK):
                    return blob
    return None


def find_model(embed_model: str, models_dir: str | Path) -> Path | None:
    """Die Modelldatei nach der Reihenfolge oben – oder None."""
    name = (embed_model or "").strip()
    if not name:
        return None
    direct = Path(os.path.expanduser(name))
    if name.lower().endswith(".gguf"):
        return direct if direct.is_file() else None
    key = name.split("/")[-1].split(":")[0].lower()
    folder = Path(os.path.expanduser(str(models_dir or "~/models")))
    if folder.is_dir():
        hits = sorted(p for p in folder.rglob("*.gguf") if key in p.name.lower() and "mmproj" not in p.name.lower())
        if hits:
            return hits[0]
    return ollama_blob(name)


# ---------------------------------------------------------------- Server und Anfragen
class Embedder:
    """embed(texts) → Vektoren. Startet den Embedding-Server bei Bedarf selbst."""

    def __init__(self, embed_model: str, models_dir: str | Path, binary: Path | None, lib_dir: Path | None = None,
                 port: int = PORT, transport: httpx.AsyncBaseTransport | None = None, gpu: bool = False):
        self.embed_model, self.models_dir = embed_model, models_dir
        self.binary, self.lib_dir, self.port = binary, lib_dir, port
        self.gpu = gpu                    # llm.embed_gpu: im Grafikspeicher statt auf der CPU
        self._path: Path | None = None    # gefundene Modelldatei (gemerkt, solange es sie gibt)
        self._looked = 0.0                # wann zuletzt vergeblich gesucht
        self._start_failed = 0.0          # wann der Server zuletzt nicht starten wollte
        self.transport = transport
        self.key = secrets.token_hex(16)  # llama-server erlaubt Anfragen von jeder Webseite (CORS *) – nur mit Schlüssel
        self.server = None                # ManagedServer, sobald gestartet
        self._lock = asyncio.Lock()
        self._client: httpx.AsyncClient | None = None
        self._failed = ""                 # letzter Grund, warum es keine Embeddings gibt (für doctor/Status)

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}/v1"

    def model_path(self) -> Path | None:
        """Die Modelldatei – gemerkt, damit nicht jede Frage die Ordner durchsucht; fehlt sie, wird höchstens alle
        MISSING_TTL Sekunden neu gesucht (nach `orbwise model embed` ist sie also gleich da)."""
        if self._path is not None and self._path.is_file():
            return self._path
        now = time.monotonic()
        if self._looked and now - self._looked < MISSING_TTL:
            return None
        self._path = find_model(self.embed_model, self.models_dir)
        self._looked = 0.0 if self._path else now
        return self._path

    def missing(self) -> str:
        """Leer, wenn alles da ist – sonst, was fehlt (mit Rat)."""
        from .lang import T
        if self.transport is None and (self.binary is None or not self.binary.is_file()):
            return T("llama.cpp ist nicht installiert (Modelle → Installieren).",
                     "llama.cpp is not installed (Models → Install).")
        if self.model_path() is None:
            return T(f"Embedding-Modell „{self.embed_model}“ fehlt – `orbwise model embed` lädt es.",
                     f"Embedding model \"{self.embed_model}\" is missing – `orbwise model embed` downloads it.")
        return ""

    def command(self, gguf: Path) -> str:
        """Startbefehl: Embedding-Modus, auf der CPU (oder mit llm.embed_gpu im Grafikspeicher), nur lokal
        erreichbar. Die physische Batch-Größe muss einen ganzen Text fassen – sonst lehnt der Server ihn ab."""
        layers = 99 if self.gpu else 0
        return (f"{shlex.quote(str(self.binary))} -m {shlex.quote(str(gguf))} --embedding -ngl {layers} -c {CONTEXT} "
                f"-b {CONTEXT} -ub {CONTEXT} -np 1 --host 127.0.0.1 --port {self.port} --api-key {self.key}")

    async def _ensure(self) -> None:
        why = self.missing()
        if why:
            self._failed = why
            raise LLMError(why)
        if self.transport is not None:  # Tests: kein Prozess
            return
        async with self._lock:
            if self._start_failed and time.monotonic() - self._start_failed < RETRY_AFTER:
                raise LLMError(self._failed or "Embedding-Server startet nicht")
            if self.server is None:
                from .llm_router import ManagedServer
                env = {"LD_LIBRARY_PATH": str(self.lib_dir)} if self.lib_dir else {}
                cfg = ServerConfig(command=self.command(self.model_path()), env=env, startup_timeout=60)
                self.server = ManagedServer(cfg, self.base_url, plain=True)
            try:
                await self.server.ensure_running()
            except LLMError as e:  # nicht bei jeder Frage wieder bis zum Zeitlimit warten
                self._start_failed, self._failed = time.monotonic(), str(e)
                raise
            self._start_failed = 0.0

    async def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        await self._ensure()
        if self._client is None:
            self._client = httpx.AsyncClient(base_url=self.base_url, timeout=120, transport=self.transport,
                                             headers={"Authorization": f"Bearer {self.key}"})
        out: list[list[float]] = []
        for start in range(0, len(texts), BATCH):
            chunk = [t[:MAX_CHARS] or " " for t in texts[start:start + BATCH]]
            try:
                resp = await self._client.post("/embeddings", json={"input": chunk, "model": self.embed_model})
            except httpx.HTTPError as e:
                self._failed = f"Embedding-Server nicht erreichbar ({type(e).__name__})"
                raise LLMError(self._failed) from e
            if resp.status_code != 200:
                self._failed = f"Embedding fehlgeschlagen ({resp.status_code}): {resp.text[:200]}"
                raise LLMError(self._failed)
            try:
                data = sorted(resp.json()["data"], key=lambda d: d.get("index", 0))
                out += [d["embedding"] for d in data]
            except (KeyError, TypeError, ValueError) as e:
                raise LLMError("Embedding-Server antwortet unerwartet") from e
        if len(out) != len(texts):
            raise LLMError("Embedding-Server liefert nicht für jeden Text einen Vektor")
        self._failed = ""
        return out

    async def warm(self) -> None:
        """Beim Start von Orbwise im Hintergrund: Server starten und das Modell laden, damit die erste Frage nicht
        darauf wartet. Fehlt etwas, passiert nichts (die Suche nimmt dann Stichworte)."""
        if self.missing():
            return
        try:
            await self.embed(["warm"])
        except LLMError as e:
            log.warning("Embedding-Server nicht vorgeladen: %s", e)

    def status(self) -> dict[str, Any]:
        path, why = self.model_path(), self.missing()
        return {"model": self.embed_model, "path": str(path) if path else "", "available": not why,
                "running": bool(self.server and self.server.running()), "problem": why or self._failed}

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None
        if self.server is not None:
            await self.server.stop()
            self.server = None
