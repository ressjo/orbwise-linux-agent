"""LLM-Anbindung: Ollama (Chat mit Tool-Calls, Streaming, Embeddings) und ein FakeLLM für Tests/Demo."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import math
import re
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx

from .config import LLMConfig

log = logging.getLogger(__name__)


class LLMError(RuntimeError):
    pass


class ContextOverflow(LLMError):
    """Der Prompt passt nicht ins Kontextfenster des Modell-Servers."""

    def __init__(self, message: str, n_ctx: int | None = None, n_prompt: int | None = None):
        super().__init__(message)
        self.n_ctx = n_ctx
        self.n_prompt = n_prompt


def _check_overflow(status: int, body: str) -> None:
    """llama-server meldet einen zu großen Prompt als 400 exceed_context_size_error."""
    if status not in (400, 500) or "exceed" not in body or "context" not in body:
        return
    n_ctx = n_prompt = None
    try:
        err = json.loads(body).get("error") or {}
        n_ctx, n_prompt = err.get("n_ctx"), err.get("n_prompt_tokens")
    except (json.JSONDecodeError, AttributeError):
        pass
    from .lang import T
    raise ContextOverflow(T(
        f"Das passt nicht mehr ins Kontextfenster des Modells ({n_prompt or '?'} von {n_ctx or '?'} Token). "
        "Möglichkeiten: „mach weiter“ (ältere Schritte werden gefaltet) oder einen neuen Chat beginnen; "
        "memory.retrieval_max_tokens verkleinern; dauerhaft mehr Platz mit BONSAI_CTX bzw. --ctx-size im "
        "Startbefehl (grob +1 GB Grafikspeicher je +8k Token bei 7–9B-Modellen).",
        f"This no longer fits into the model's context window ({n_prompt or '?'} of {n_ctx or '?'} tokens). "
        "Options: say “continue” (older steps get folded) or start a new chat; lower memory.retrieval_max_tokens; "
        "for more room raise BONSAI_CTX or --ctx-size in the start command (roughly +1 GB VRAM per +8k tokens "
        "for 7–9B models)."), n_ctx, n_prompt)


class OllamaLLM:
    def __init__(self, cfg: LLMConfig, transport: httpx.AsyncBaseTransport | None = None):
        self.cfg = cfg
        # Embeddings auf der CPU rechnen (spart Grafikspeicher, wenn ein anderes Modell die GPU belegt)
        self.embed_on_cpu = False
        self._client = httpx.AsyncClient(base_url=cfg.base_url, timeout=cfg.request_timeout, transport=transport)

    async def close(self) -> None:
        await self._client.aclose()

    def _payload(self, messages: list[dict], tools: list[dict] | None, stream: bool,
                 think: bool | None = None, max_tokens: int | None = None, effort: str | None = None) -> dict:
        think = self.cfg.think if think is None else think
        if think and effort and supports_effort(self.cfg.model):  # gpt-oss: echte Stufen low/medium/high
            think = effort
        payload: dict[str, Any] = {
            "model": self.cfg.model,
            "messages": ollama_messages(messages),
            "stream": stream,
            "keep_alive": self.cfg.keep_alive,
            "options": {"temperature": self.cfg.temperature, "num_ctx": self.cfg.num_ctx},
            "think": think,
        }
        if max_tokens:
            payload["options"]["num_predict"] = max_tokens
        if tools:
            payload["tools"] = tools
        return payload

    async def chat_stream(self, messages: list[dict], tools: list[dict] | None = None,
                          think: bool | None = None, max_tokens: int | None = None,
                          tool_choice: str | None = None, effort: str | None = None) -> AsyncIterator[dict]:
        """Liefert {"type": "token", "text": ...} und abschließend {"type": "done", "message": {...}}.
        think: Denkmodus für diese Anfrage (None = Einstellung aus der Config). max_tokens begrenzt die Antwort;
        tool_choice kennt Ollama nicht (die Werkzeuge bleiben trotzdem im Prompt, damit der Cache passt)."""
        content: list[str] = []
        tool_calls: list[dict] = []
        stats: dict[str, Any] = {}
        payload = self._payload(messages, tools, True, think, max_tokens, effort)
        try:
            async with self._client.stream("POST", "/api/chat", json=payload) as resp:
                if resp.status_code != 200:
                    body = (await resp.aread()).decode(errors="replace")
                    raise LLMError(f"Ollama antwortet mit {resp.status_code}: {body[:300]}")
                async for line in resp.aiter_lines():
                    if not line.strip():
                        continue
                    chunk = json.loads(line)
                    if "error" in chunk:
                        raise LLMError(chunk["error"])
                    msg = chunk.get("message") or {}
                    if msg.get("thinking"):
                        yield {"type": "reasoning", "text": msg["thinking"]}
                    if msg.get("content"):
                        content.append(msg["content"])
                        yield {"type": "token", "text": msg["content"]}
                    if msg.get("tool_calls"):
                        tool_calls.extend(msg["tool_calls"])
                    if chunk.get("done"):
                        stats = generation_stats(chunk)
                        break
        except httpx.ConnectError as e:
            raise LLMError(
                f"Ollama unter {self.cfg.base_url} nicht erreichbar – läuft 'ollama serve'?"
            ) from e
        message: dict[str, Any] = {"role": "assistant", "content": "".join(content)}
        if tool_calls:
            message["tool_calls"] = tool_calls
        yield {"type": "done", "message": message, "stats": stats}

    async def chat(self, messages: list[dict]) -> str:
        try:
            resp = await self._client.post("/api/chat", json=self._payload(messages, None, False))
        except httpx.ConnectError as e:
            raise LLMError(f"Ollama unter {self.cfg.base_url} nicht erreichbar") from e
        if resp.status_code != 200:
            raise LLMError(f"Ollama antwortet mit {resp.status_code}: {resp.text[:300]}")
        return strip_think(resp.json().get("message", {}).get("content", ""))

    async def preload(self) -> None:
        """Modell schon beim Start in den (Grafik-)Speicher laden – sonst wartet die erste Frage darauf."""
        try:
            resp = await self._client.post("/api/generate", json={
                "model": self.cfg.model, "prompt": "", "keep_alive": self.cfg.keep_alive}, timeout=600)
        except httpx.ConnectError as e:
            raise LLMError(f"Ollama unter {self.cfg.base_url} nicht erreichbar – läuft 'ollama serve'?") from e
        except httpx.HTTPError as e:
            raise LLMError(f"Ollama: {e}") from e
        if resp.status_code == 404 or "not found" in resp.text.lower():
            raise LLMError(f"Modell {self.cfg.model} fehlt – einmalig laden: ollama pull {self.cfg.model}")
        if resp.status_code != 200:
            raise LLMError(f"Ollama antwortet mit {resp.status_code}: {resp.text[:200]}")

    async def embed(self, texts: list[str]) -> list[list[float]]:
        payload: dict[str, Any] = {"model": self.cfg.embed_model, "input": texts, "keep_alive": self.cfg.keep_alive}
        if self.embed_on_cpu:
            payload["options"] = {"num_gpu": 0}
        try:
            resp = await self._client.post("/api/embed", json=payload)
        except httpx.HTTPError as e:
            raise LLMError(f"Ollama für Embeddings nicht erreichbar: {e}") from e
        if resp.status_code != 200:
            raise LLMError(f"Embedding fehlgeschlagen ({resp.status_code}): {resp.text[:200]}")
        return resp.json()["embeddings"]

    async def loaded_models(self) -> list[str]:
        try:
            resp = await self._client.get("/api/ps", timeout=5)
            return [m["name"] for m in resp.json().get("models", [])]
        except (httpx.HTTPError, ValueError, KeyError):
            return []

    async def memory_info(self) -> dict | None:
        """Wie viel des geladenen Modells (samt Kontext) im Grafikspeicher liegt – aus /api/ps und /api/show.
        Der KV-Cache wird aus der Architektur geschätzt (f16). None, wenn das Modell gerade nicht geladen ist."""
        from .llm_memory import kv_per_token_from_info
        try:
            models = (await self._client.get("/api/ps", timeout=5)).json().get("models", [])
            want = self.cfg.model if ":" in self.cfg.model else f"{self.cfg.model}:latest"
            entry = next((m for m in models if m.get("name") in (self.cfg.model, want)
                          or m.get("model") in (self.cfg.model, want)), None)
            if not entry:
                return None
            show = (await self._client.post("/api/show", json={"model": self.cfg.model}, timeout=10)).json()
        except (httpx.HTTPError, ValueError):
            return None
        info_ = show.get("model_info") or {}
        size, in_vram = int(entry.get("size") or 0), int(entry.get("size_vram") or 0)
        ctx = int(entry.get("context_length") or self.cfg.num_ctx)
        per_token = kv_per_token_from_info(info_)
        kv = per_token * ctx if per_token else 0
        share = in_vram / size if size else 1.0
        train = next((int(v) for k, v in info_.items() if k.endswith(".context_length") and isinstance(v, int)), None)
        model = max(0, size - kv)
        return {"ctx": ctx, "ctx_train": train, "kv_per_token": per_token, "source": "estimate",
                "kv_vram": int(kv * share), "kv_ram": int(kv * (1 - share)),
                "model_vram": int(model * share), "model_ram": int(model * (1 - share)),
                "model_ram_offload": int(model * (1 - share))}

    async def supports_images(self) -> bool:
        """Kann das Modell Bilder sehen? (Ollama: /api/show → capabilities enthält "vision")"""
        try:
            show = (await self._client.post("/api/show", json={"model": self.cfg.model}, timeout=10)).json()
        except (httpx.HTTPError, ValueError):
            return False
        return "vision" in (show.get("capabilities") or [])

    async def unload_all(self) -> list[str]:
        """Alle geladenen Ollama-Modelle aus dem (Grafik-)Speicher entfernen."""
        names = await self.loaded_models()
        for name in names:
            try:
                await self._client.post("/api/generate", json={"model": name, "keep_alive": 0}, timeout=30)
            except httpx.HTTPError:
                pass
        return names

    async def status(self) -> dict:
        try:
            resp = await self._client.get("/api/tags", timeout=3)
            names = [m["name"] for m in resp.json().get("models", [])]
        except Exception as e:  # noqa: BLE001
            return {"online": False, "error": str(e), "model": self.cfg.model}
        def has(model: str) -> bool:
            return any(n == model or n == f"{model}:latest" or n.split(":")[0] == model for n in names)
        return {
            "online": True,
            "model": self.cfg.model,
            "model_available": has(self.cfg.model),
            "embed_model": self.cfg.embed_model,
            "embed_available": has(self.cfg.embed_model),
        }


def to_openai_messages(messages: list[dict]) -> list[dict]:
    """Nachrichten im internen (Ollama-)Format → OpenAI-Format: Tool-Calls bekommen IDs, Tool-Ergebnisse werden
    in Reihenfolge ihrer Aufrufe zugeordnet, Argumente als JSON-String."""
    out: list[dict] = []
    pending: list[str] = []
    counter = 0
    for m in messages:
        role = m.get("role")
        if role == "assistant" and m.get("tool_calls"):
            calls = []
            for call in m["tool_calls"]:
                fn = call.get("function", {})
                args = fn.get("arguments", {})
                counter += 1
                cid = f"call_{counter}"
                pending.append(cid)
                calls.append({"id": cid, "type": "function", "function": {
                    "name": fn.get("name", ""),
                    "arguments": args if isinstance(args, str) else json.dumps(args, ensure_ascii=False)}})
            out.append({"role": "assistant", "content": m.get("content") or "", "tool_calls": calls})
        elif role == "tool":
            if pending:
                cid = pending.pop(0)
            else:
                counter += 1
                cid = f"call_{counter}"
            out.append({"role": "tool", "tool_call_id": cid, "content": m.get("content", "")})
        elif m.get("images"):  # Bilder für ein Modell mit Bildunterstützung (llama-server mit mmproj)
            parts: list[dict] = [{"type": "text", "text": m.get("content", "")}]
            for path in m["images"]:
                url = _data_url(path)
                if url:
                    parts.append({"type": "image_url", "image_url": {"url": url}})
            out.append({"role": role, "content": parts})
        else:
            out.append({"role": role, "content": m.get("content", "")})
    return out


def _data_url(path: str) -> str | None:
    from .attachments import data_url
    try:
        return data_url(Path(path))
    except OSError:
        return None


def ollama_messages(messages: list[dict]) -> list[dict]:
    """Bildpfade → Base64 (Ollama erwartet die Bilder direkt in der Nachricht)."""
    out = []
    for m in messages:
        if m.get("images"):
            images = []
            for path in m["images"]:
                try:
                    images.append(base64.b64encode(Path(path).read_bytes()).decode())
                except OSError:
                    continue
            m = {**m, "images": images}
        out.append(m)
    return out


def _parse_args(raw: str) -> Any:
    if not raw or not raw.strip():
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return raw  # coerce_args versucht es erneut und ignoriert Unbrauchbares


class OpenAICompatLLM:
    """OpenAI-kompatibler Server (llama-server aus llama.cpp, LM Studio, vLLM …) mit Streaming und Tool-Calls."""

    def __init__(self, profile, timeout: float = 300.0, transport: httpx.AsyncBaseTransport | None = None):
        self.profile = profile
        headers = {"Authorization": f"Bearer {profile.api_key}"} if profile.api_key else {}
        self._client = httpx.AsyncClient(base_url=profile.base_url, timeout=timeout, headers=headers,
                                         transport=transport)

    async def close(self) -> None:
        await self._client.aclose()

    def _payload(self, messages: list[dict], tools: list[dict] | None, stream: bool,
                 think: bool | None = None, max_tokens: int | None = None, tool_choice: str | None = None,
                 effort: str | None = None) -> dict:
        p = self.profile
        payload: dict[str, Any] = {
            "model": p.model,
            "messages": to_openai_messages(messages),
            "stream": stream,
            "temperature": p.temperature,
            "cache_prompt": True,
        }
        # Qwen-basierte Modelle (auch Bonsai): Denkmodus aus – deutlich schneller; per Anfrage einschaltbar
        thinking = bool(p.think if think is None else think)
        payload["chat_template_kwargs"] = {"enable_thinking": thinking}
        if thinking and effort:  # Vorlagen mit Stufen (gpt-oss) nutzen es, alle anderen ignorieren es
            payload["chat_template_kwargs"]["reasoning_effort"] = effort
        if stream:
            payload["stream_options"] = {"include_usage": True}
            payload["return_progress"] = True  # neuere llama-server melden den Fortschritt beim Einlesen
        if max_tokens:
            payload["max_tokens"] = max_tokens
        if tools:
            payload["tools"] = tools
            if tool_choice:  # "none": Werkzeuge bleiben im Prompt (Cache passt), aber das Modell ruft keins auf
                payload["tool_choice"] = tool_choice
        return payload

    _warned_reasoning = False

    def _warn_reasoning(self) -> None:
        if not OpenAICompatLLM._warned_reasoning:
            OpenAICompatLLM._warned_reasoning = True
            log.warning("Modell-Server %s denkt trotz think: false (Reasoning-Tokens kosten Zeit). "
                        "Tipp: '--reasoning-budget 0' an den llama-server-Befehl anhängen (dann wirkt allerdings "
                        "auch der DENKEN-Knopf nicht mehr).", self.profile.base_url)

    def _error(self, e: Exception) -> LLMError:
        return LLMError(f"Modell-Server {self.profile.base_url} nicht erreichbar ({type(e).__name__}) – "
                        "läuft llama-server?")

    async def chat_stream(self, messages: list[dict], tools: list[dict] | None = None,
                          think: bool | None = None, max_tokens: int | None = None,
                          tool_choice: str | None = None, effort: str | None = None) -> AsyncIterator[dict]:
        content: list[str] = []
        calls: dict[int, dict] = {}
        timings: dict = {}
        usage: dict = {}
        first_token = None
        started = time.monotonic()
        payload = self._payload(messages, tools, True, think, max_tokens, tool_choice, effort)
        try:
            async with self._client.stream("POST", "/chat/completions", json=payload) as resp:
                if resp.status_code != 200:
                    body = (await resp.aread()).decode(errors="replace")
                    _check_overflow(resp.status_code, body)
                    raise LLMError(f"Modell-Server antwortet mit {resp.status_code}: {body[:300]}")
                async for line in resp.aiter_lines():
                    line = line.strip()
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    chunk = json.loads(data)
                    if chunk.get("error"):
                        raise LLMError(str(chunk["error"]))
                    timings = chunk.get("timings") or timings
                    usage = chunk.get("usage") or usage
                    progress = chunk.get("prompt_progress")
                    if isinstance(progress, dict) and progress.get("total"):
                        yield {"type": "prompt_progress", "total": progress.get("total"),
                               "cache": progress.get("cache") or 0, "processed": progress.get("processed") or 0}
                    for choice in chunk.get("choices") or []:
                        delta = choice.get("delta") or {}
                        reasoning = delta.get("reasoning_content") or delta.get("reasoning")
                        if reasoning:
                            # Modell „denkt“ (Thinking-Tokens): nicht Teil der Antwort, aber sichtbar machen
                            first_token = first_token or time.monotonic()
                            if not (self.profile.think if think is None else think):
                                self._warn_reasoning()
                            yield {"type": "reasoning", "text": reasoning}
                        text = delta.get("content")
                        if text:
                            first_token = first_token or time.monotonic()
                            content.append(text)
                            yield {"type": "token", "text": text}
                        for tc in delta.get("tool_calls") or []:
                            first_token = first_token or time.monotonic()
                            slot = calls.setdefault(tc.get("index", len(calls)), {"name": "", "arguments": ""})
                            fn = tc.get("function") or {}
                            if fn.get("name"):
                                slot["name"] += fn["name"]
                            if fn.get("arguments"):
                                slot["arguments"] += fn["arguments"]
                            # Werkzeug-Aufruf entsteht (z. B. write_file mit langem Inhalt) – sichtbar machen
                            yield {"type": "tool_delta", "name": slot["name"], "chars": len(slot["arguments"])}
        except httpx.HTTPError as e:
            raise self._error(e) from e
        message: dict[str, Any] = {"role": "assistant", "content": "".join(content)}
        if calls:
            message["tool_calls"] = [{"function": {"name": c["name"], "arguments": _parse_args(c["arguments"])}}
                                     for _, c in sorted(calls.items()) if c["name"]]
        yield {"type": "done", "message": message, "stats": openai_stats(timings, usage, first_token, started)}

    async def chat(self, messages: list[dict]) -> str:
        try:
            resp = await self._client.post("/chat/completions", json=self._payload(messages, None, False))
        except httpx.HTTPError as e:
            raise self._error(e) from e
        if resp.status_code != 200:
            _check_overflow(resp.status_code, resp.text)
            raise LLMError(f"Modell-Server antwortet mit {resp.status_code}: {resp.text[:300]}")
        choices = resp.json().get("choices") or [{}]
        return strip_think((choices[0].get("message") or {}).get("content") or "")

    async def server_props(self) -> dict:
        """Eigenschaften des llama-servers (GET /props: Kontextgröße, Anzahl Slots …), sonst {}."""
        root = str(self._client.base_url).rstrip("/")
        root = root[:-3] if root.endswith("/v1") else root
        try:
            resp = await self._client.get(root + "/props", timeout=3)
            data = resp.json() if resp.status_code == 200 else {}
        except (httpx.HTTPError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    async def supports_images(self) -> bool:
        """Kann der Server Bilder annehmen? (llama-server mit Bildmodul: /props → modalities.vision)"""
        data = await self.server_props()
        return bool((data.get("modalities") or {}).get("vision"))

    async def server_context(self) -> int | None:
        """Tatsächliche Kontextgröße des llama-servers (GET /props), sonst None."""
        data = await self.server_props()
        try:
            n = (data.get("default_generation_settings") or {}).get("n_ctx") or data.get("n_ctx")
            return int(n) if n else None
        except (ValueError, TypeError, AttributeError):
            return None

    async def status(self) -> dict:
        try:
            resp = await self._client.get("/models", timeout=3)
            ok = resp.status_code == 200
        except httpx.HTTPError as e:
            return {"online": False, "error": str(e), "model": self.profile.model}
        return {"online": ok, "model": self.profile.model, "model_available": ok,
                **({} if ok else {"error": f"HTTP {resp.status_code}"})}


def openai_stats(timings: dict, usage: dict, first_token: float | None, started: float) -> dict:
    """Token/s aus llama.cpp-Timings, sonst aus usage + gemessener Zeit."""
    # Gesamtgröße des Prompts: neu verarbeitete + aus dem Cache wiederverwendete Token
    total = usage.get("prompt_tokens")
    if timings.get("prompt_n") is not None:
        total = (timings.get("prompt_n") or 0) + (timings.get("cache_n") or 0) or total
    # Auch ohne Generier-Geschwindigkeit (max_tokens=1 beim Vorwärmen/Test: predicted_per_second fehlt oder ist 0)
    # zählen die Prompt-Zahlen aus den Timings – usage.prompt_tokens enthält bei llama-server die Cache-Token mit
    if timings.get("predicted_per_second") or timings.get("prompt_n") is not None:
        tps = timings.get("predicted_per_second")
        return {"tokens": timings.get("predicted_n"), "tps": round(tps, 1) if tps else None,
                "prompt_tokens": timings.get("prompt_n"), "prompt_total": total,
                **({"prompt_cached": timings["cache_n"]} if timings.get("cache_n") is not None else {}),
                "prompt_tps": round(timings["prompt_per_second"], 1) if timings.get("prompt_per_second") else None,
                "prompt_ms": round(timings["prompt_ms"]) if timings.get("prompt_ms") else None}
    tokens = usage.get("completion_tokens")
    elapsed = time.monotonic() - (first_token or started)
    return {"tokens": tokens, "tps": round(tokens / elapsed, 1) if tokens and elapsed > 0 else None,
            "prompt_tokens": usage.get("prompt_tokens"), "prompt_total": total, "prompt_tps": None}


def generation_stats(chunk: dict) -> dict:
    """Token-Statistik aus dem letzten Ollama-Chunk (Dauern in Nanosekunden)."""
    def rate(count, duration):
        return round(count / (duration / 1e9), 1) if count and duration else None
    return {
        "tokens": chunk.get("eval_count"),
        "tps": rate(chunk.get("eval_count"), chunk.get("eval_duration")),
        "prompt_tokens": chunk.get("prompt_eval_count"),
        "prompt_total": chunk.get("prompt_eval_count"),
        "prompt_tps": rate(chunk.get("prompt_eval_count"), chunk.get("prompt_eval_duration")),
        "prompt_ms": round(chunk["prompt_eval_duration"] / 1e6) if chunk.get("prompt_eval_duration") else None,
        "load_ms": round(chunk["load_duration"] / 1e6) if chunk.get("load_duration") else None,
    }


def supports_effort(model: str) -> bool:
    """Kennt das Modell echte Denkstufen (low/medium/high)? Bisher gpt-oss; andere können nur an/aus."""
    return "gpt-oss" in (model or "").lower()


_THINK_RE = re.compile(r"<think>.*?</think>\s*", re.S)


def strip_context_note_text(text: str) -> str:
    from .prompts import strip_context_note
    return strip_context_note(text)


def strip_think(text: str) -> str:
    return _THINK_RE.sub("", text).strip()


class FakeLLM:
    """Deterministisches Ersatz-LLM (ORBWISE_FAKE_LLM=1) für Tests und UI-Demo ohne Ollama.

    - "/tool <name> <json-args>" erzeugt einen Tool-Call
    - "systemupdate"/"update my system" löst system_update aus, "welche dateien ..." find_files,
      "search the web for …"/"such im web nach …" web_search (für Demo-Screenshots)
    - nach einem Tool-Ergebnis wird dieses kurz zusammengefasst
    """

    def __init__(self, delay: float = 0.02, language: str = "de"):
        self.en = language == "en"
        self.delay = delay
        self.calls: list[list[dict]] = []
        self.opts: list[dict] = []  # Optionen je Aufruf (Werkzeuge, max_tokens, tool_choice) – für Tests
        self.thought_repeat = 1  # Tests: wie lang die Denkkette wird (Denk-Budget)

    async def close(self) -> None:
        pass

    def _decide(self, messages: list[dict]) -> dict:
        last = messages[-1]
        user = next((m.get("content") or "" for m in reversed(messages) if m["role"] == "user"), "")
        planning = "PLANMODUS:" in user or "PLAN MODE:" in user
        if planning and (last["role"] == "tool" or not strip_context_note_text(user).startswith("/tool ")):
            return {"role": "assistant", "content": self._demo_plan(strip_context_note_text(user))}
        if last["role"] == "tool":
            done = "Done. Result" if self.en else "Erledigt. Ergebnis"
            return {"role": "assistant", "content": f"{done}: {last['content'][:200]}"}
        from .prompts import strip_context_note
        text = strip_context_note(last.get("content", ""))
        if text.startswith("/tool "):
            parts = text.split(" ", 2)
            args = json.loads(parts[2]) if len(parts) > 2 else {}
            return {"role": "assistant", "content": "",
                    "tool_calls": [{"function": {"name": parts[1], "arguments": args}}]}
        low = text.lower()
        web = re.match(r"(search the web for|such im web nach)\s+(.+)", low)
        if web:
            return {"role": "assistant", "content": "",
                    "tool_calls": [{"function": {"name": "web_search", "arguments": {"query": web.group(2)}}}]}
        if "systemupdate" in low or "update my system" in low:
            return {"role": "assistant", "content": "I'll start the system update." if self.en
                    else "Ich starte das Systemupdate.",
                    "tool_calls": [{"function": {"name": "system_update", "arguments": {}}}]}
        if low.startswith("welche dateien"):
            return {"role": "assistant", "content": "",
                    "tool_calls": [{"function": {"name": "find_files", "arguments": {"query": text.split()[-1]}}}]}
        return {"role": "assistant",
                "content": f"Certainly. You said: {text}. How else may I help?" if self.en
                else f"Sehr wohl. Sie sagten: {text}. Wie kann ich sonst behilflich sein?"}

    def _demo_plan(self, request: str) -> str:
        if self.en:
            return (f"## Plan\n1. Check free disk space with `df -h` (read-only, no confirmation)\n"
                    f"2. Clear the package cache and old journal logs with `cleanup_system` (asks for confirmation)\n"
                    f"3. Show the result and how much space was freed\n\n**Risks/assumptions:** nothing is deleted "
                    f"outside caches and logs. (Request: {request})")
        return (f"## Plan\n1. Freien Speicher mit `df -h` prüfen (nur lesend, keine Rückfrage)\n"
                f"2. Paket-Cache und alte Journal-Logs mit `cleanup_system` leeren (mit Rückfrage)\n"
                f"3. Ergebnis zeigen und wie viel Platz frei wurde\n\n**Risiken/Annahmen:** Gelöscht werden nur "
                f"Caches und Logs. (Anfrage: {request})")

    def _demo_summary(self, messages: list[dict]) -> str:
        """Antwort auf die Komprimierungs-Anweisung: listet, was im Verlauf stand (für Tests und die Demo)."""
        users = [strip_context_note_text(m.get("content") or "") for m in messages[:-1] if m["role"] == "user"]
        tools = [m.get("tool_name", "") for m in messages if m["role"] == "tool"]
        lines = ["1. Anliegen: " + (users[-1][:120] if users else "–"),
                 "3. Erledigt: " + (", ".join(tools[-6:]) or "–"),
                 "6. Alle Nutzernachrichten: " + " | ".join(u[:60] for u in users[-8:]),
                 "9. Nächster Schritt: weiter mit „" + (users[-1][:80] if users else "") + "“"]
        return "\n".join(lines)

    async def chat_stream(self, messages: list[dict], tools: list[dict] | None = None,
                          think: bool | None = None, max_tokens: int | None = None,
                          tool_choice: str | None = None, effort: str | None = None) -> AsyncIterator[dict]:
        self.calls.append(messages)
        self.opts.append({"tools": tools, "think": think, "max_tokens": max_tokens, "tool_choice": tool_choice,
                          "effort": effort})
        last = messages[-1].get("content") or "" if messages else ""
        if max_tokens == 1:  # Vorwärmen: nur einlesen
            msg = {"role": "assistant", "content": "."}
        elif "Kontextfenster ist fast voll" in last or "context window is almost full" in last:
            msg = {"role": "assistant", "content": self._demo_summary(messages)}
        else:
            msg = self._decide(messages)
        for name in [c["function"]["name"] for c in msg.get("tool_calls") or []]:
            yield {"type": "tool_delta", "name": name, "chars": 24}
        if think:
            thought = "Der Nutzer möchte etwas wissen. Ich überlege kurz, welche Werkzeuge passen, und antworte dann knapp. "
            for word in re.findall(r"\S+\s*", thought * self.thought_repeat):
                if self.delay:
                    await asyncio.sleep(self.delay)
                yield {"type": "reasoning", "text": word}
        for word in re.findall(r"\S+\s*", msg["content"]):
            if self.delay:
                await asyncio.sleep(self.delay)
            yield {"type": "token", "text": word}
        n = max(1, len(msg["content"].split()))
        yield {"type": "done", "message": msg,
               "stats": {"tokens": n, "tps": 42.0, "prompt_tokens": 800, "prompt_tps": 950.0, "prompt_ms": 842}}

    async def chat(self, messages: list[dict]) -> str:
        self.calls.append(messages)
        body = messages[-1]["content"]
        return "Zusammenfassung: " + " ".join(body.split()[:60])

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [fake_embedding(t) for t in texts]

    async def status(self) -> dict:
        return {"online": True, "model": "fake", "model_available": True,
                "embed_model": "fake", "embed_available": True}


def fake_embedding(text: str, dim: int = 256) -> list[float]:
    vec = [0.0] * dim
    for word in re.findall(r"\w+", text.lower()):
        h = int(hashlib.md5(word.encode()).hexdigest(), 16)
        vec[h % dim] += 1.0
    norm = math.sqrt(sum(v * v for v in vec)) or 1.0
    return [v / norm for v in vec]
