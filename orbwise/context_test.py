"""Kontext-Selbsttest gegen das echte, aktive Modell (`orbwise context-test`, Einstellungen → Kontextfenster).

Prüft, ob das Kontext-Management auf diesem Rechner wirklich so arbeitet, wie Orbwise rechnet:
1. Fenster    – meldet der Modell-Server dieselbe Größe, die eingestellt ist (und mit der Orbwise rechnet)?
2. Speicher   – liegen KV-Cache und Modell im Grafikspeicher?
3. Stufe      – Sparmodus/Normalbetrieb, Komprimierungspunkt, geladene Werkzeuge, Größe des Start-Prompts.
4. Schätzung  – wie weit liegt Orbwises Token-Schätzung neben den echten Token des Servers?
5. Cache      – wird ein gleicher Prompt bzw. ein Folge-Prompt (gleicher Anfang) aus dem Cache gelesen?
6. Füllung    – passt ein Prompt bis zum Komprimierungspunkt, und sieht das Modell noch dessen Anfang?
7. Überlauf   – meldet der Server einen zu großen Prompt als Fehler (statt still zu kürzen)?

Der Test benutzt eigene Nachrichten – der offene Chat bleibt unverändert; danach wird er neu vorgewärmt.
"""

from __future__ import annotations

import json
import random
import re
import time
import uuid
from typing import Awaitable, Callable

from .lang import T
from .llm import ContextOverflow, LLMError, strip_think
from .memory.context import est_tokens

Progress = Callable[[str], Awaitable[None]]

WORDS = ("Kranich", "Leuchtturm", "Ahornblatt", "Bernstein", "Wasserfall", "Kompass", "Morgenrot", "Feldlerche")
TOPICS = ("Wetter", "Zahlen", "Farben", "Bahnhöfe", "Gewürze", "Flüsse", "Werkzeuge", "Vögel")


def _filler_line(i: int) -> str:
    return (f"Zeile {i:05d}: Dieser Absatz füllt nur das Kontextfenster für den Test und handelt von "
            f"{TOPICS[i % len(TOPICS)]}, der Zahl {i * 37 % 1000} und ist sonst bedeutungslos.\n")


def filler(est: int) -> str:
    """Belangloser, nicht wiederholter Text mit ungefähr `est` geschätzten Token (immer gleicher Anfang)."""
    lines, size, i = [], 0, 0
    while size < est * 3:
        line = _filler_line(i)
        lines.append(line)
        size += len(line)
        i += 1
    return "".join(lines)


def _check(status: str, key: str, title: str, detail: str = "") -> dict:
    return {"status": status, "id": key, "title": title, "detail": detail}


def _num(n) -> str:
    return f"{int(n):,}".replace(",", ".") if n is not None else "?"


def _dec(x: float, digits: int = 2) -> str:
    text = f"{x:.{digits}f}"
    return T(text.replace(".", ","), text)


def _start_line() -> str:
    """Startbefehl des zuletzt von Orbwise gestarteten llama-servers (aus seinem Log)."""
    from .llm_router import _log_path
    try:
        text = _log_path().read_text(errors="replace")[-200_000:]
    except OSError:
        return ""
    found = re.findall(r"===== Starte: (.*)", text)
    return found[-1] if found else ""


class ContextTest:
    def __init__(self, agent, llm, gpu: dict | None = None, progress: Progress | None = None):
        self.agent, self.llm, self.gpu, self.progress = agent, llm, gpu, progress
        self.client = getattr(llm, "client", llm)
        self.llama = hasattr(self.client, "server_context")  # OpenAI-kompatibler Server (llama-server)
        self.profile = getattr(llm, "profile", None)
        self.checks: dict[str, dict] = {}

    async def _say(self, text: str) -> None:
        if self.progress:
            await self.progress(text)

    async def _ask(self, messages: list[dict], tools: list[dict] | None = None, max_tokens: int = 1) -> dict:
        """Eine Anfrage ohne Denken → Statistik des Servers (+ Antworttext, Dauer)."""
        started, stats, text = time.monotonic(), {}, ""
        async for ev in self.agent._call_llm(messages, tools, think=False, max_tokens=max_tokens):
            if ev["type"] == "done":
                stats = dict(ev.get("stats") or {})
                text = (ev.get("message") or {}).get("content") or ""
        stats["text"] = strip_think(text)
        stats["seconds"] = round(time.monotonic() - started, 1)
        return stats

    # ------------------------------------------------------------------ Ablauf
    async def run(self, quick: bool = False) -> dict:
        agent = self.agent
        window, plan, limit = agent.model_window(), agent.plan(), agent.compact_limit()
        nonce = uuid.uuid4().hex[:10]
        agent.choose_tools(set())  # Werkzeuge wie beim nächsten Schritt im offenen Chat (wie beim Vorwärmen)
        names = {s["function"]["name"] for s in agent.schemas} - {"load_tools"}
        system = agent.system_prompt(names)
        probe = [{"role": "system", "content": f"[Kontexttest {nonce}]\n{system}"},
                 {"role": "user", "content": T("Kontexttest – antworte nur mit OK.", "Context test – reply only with OK.")}]
        est = (est_tokens(probe[0]["content"]) + est_tokens(probe[1]["content"]) + 8
               + est_tokens(json.dumps(agent.schemas, ensure_ascii=False)))
        first = None
        try:
            await self._say(T("Start-Prompt einlesen …", "Reading the start prompt …"))
            first = await self._ask(probe, agent.schemas)
        except ContextOverflow as e:
            self.checks["estimate"] = _check("fail", "estimate", T("Start-Prompt", "Start prompt"), T(
                f"Schon der Start-Prompt passt nicht ins Fenster ({e}).", f"The start prompt alone does not fit ({e})."))
        except LLMError as e:
            return self._result([_check("fail", "model", T("Modell", "Model"), str(e))])

        await self._window(window)
        await self._memory()
        self._stage(plan, limit, window, first, len(names), len(agent.all_schemas))
        if first:
            self._estimate(est, first)
            await self._cache(probe, first)
        if not quick:
            await self._fill(window, limit)
            if self.llama:
                await self._overflow(window)
        order = ("model", "window", "budget", "memory", "flash", "stage", "estimate", "cache", "followup", "fill", "overflow")
        return self._result([self.checks[k] for k in order if k in self.checks])

    def _result(self, checks: list[dict]) -> dict:
        worst = "fail" if any(c["status"] == "fail" for c in checks) else \
            "warn" if any(c["status"] == "warn" for c in checks) else "ok"
        return {"status": worst, "checks": checks, "profile": getattr(self.llm, "active", None),
                "window": self.agent.model_window(), "small": self.agent.plan().small,
                "compact_at": self.agent.compact_limit()}

    # ------------------------------------------------------------------ 1. Fenster
    async def _window(self, window: int) -> None:
        configured = getattr(self.profile, "num_ctx", None)
        server = None
        if self.llama:
            server = await self.client.server_context()
        elif hasattr(self.llm, "memory_info"):
            info, _ = await self.llm.memory_info()
            server = (info or {}).get("ctx")
        title = T("Kontextfenster", "Context window")
        if not server:
            self.checks["window"] = _check("warn", "window", title, T(
                f"Der Server nennt seine Größe nicht – Orbwise rechnet mit {_num(window)} Token (eingestellt).",
                f"The server does not report its size – Orbwise assumes {_num(window)} tokens (configured)."))
        elif configured and server != configured:
            self.checks["window"] = _check("fail", "window", title, T(
                f"Der Server läuft mit {_num(server)} Token, eingestellt sind {_num(configured)}. Kontextfenster in "
                "den Einstellungen noch einmal setzen (startet den Server neu) bzw. -c/BONSAI_CTX im Startbefehl prüfen.",
                f"The server runs with {_num(server)} tokens but {_num(configured)} are configured. Set the context "
                "window again in the settings (restarts the server) or check -c/BONSAI_CTX in the start command."))
        elif server != window:
            self.checks["window"] = _check("fail", "window", title, T(
                f"Der Server hat {_num(server)} Token, Orbwise rechnet aber mit {_num(window)}.",
                f"The server has {_num(server)} tokens but Orbwise assumes {_num(window)}."))
        else:
            self.checks["window"] = _check("ok", "window", title, T(
                f"{_num(server)} Token – Server, Einstellung und Orbwise stimmen überein.",
                f"{_num(server)} tokens – server, setting and Orbwise agree."))
        cap = self.agent.cfg.memory.context_budget_tokens
        if cap:
            self.checks["budget"] = _check("warn", "budget", T("Prompt-Obergrenze", "Prompt cap"), T(
                f"memory.context_budget_tokens = {_num(cap)} begrenzt jeden Prompt, egal wie groß das Fenster ist. "
                "Aus der config.yaml löschen, wenn das ganze Fenster genutzt werden soll.",
                f"memory.context_budget_tokens = {_num(cap)} caps every prompt regardless of the window. "
                "Remove it from config.yaml to use the whole window."))

    # ------------------------------------------------------------------ 2. Speicher
    async def _memory(self) -> None:
        from .llm_memory import MIB, recommend
        title = T("Speicher (KV-Cache)", "Memory (KV cache)")
        if not hasattr(self.llm, "memory_info"):
            return
        info, reason = await self.llm.memory_info()
        if not info:
            self.checks["memory"] = _check("warn", "memory", title, reason or T("Keine Angaben.", "No data."))
            return
        self._flash(info.get("flash_attn"))
        gb = lambda b: f"{b / 1024 ** 3:.1f} GB".replace(".", ",")  # noqa: E731
        kv = info.get("kv_vram", 0) + info.get("kv_ram", 0)
        spill = info.get("kv_ram", 0) + info.get("model_ram_offload", 0)
        rec = recommend(info, self.gpu)
        est = T(" (geschätzt)", " (estimated)") if info.get("source") == "estimate" else ""
        if spill > 64 * MIB:
            self.checks["memory"] = _check("fail", "memory", title, T(
                f"{gb(spill)} von KV-Cache/Modell liegen im RAM{est} – das macht alles deutlich langsamer. "
                f"Empfohlen: höchstens {_num(rec.get('max_ctx'))} Token Kontext oder 4-Bit-KV-Cache.",
                f"{gb(spill)} of KV cache/model sit in RAM{est} – everything gets much slower. "
                f"Recommended: at most {_num(rec.get('max_ctx'))} tokens of context or a 4-bit KV cache."))
            return
        more = ""
        if rec.get("verdict") == "increase":
            more = T(f" Platz wäre für bis zu {_num(rec['max_ctx'])} Token.",
                     f" There is room for up to {_num(rec['max_ctx'])} tokens.")
        self.checks["memory"] = _check("ok", "memory", title, T(
            f"KV-Cache {gb(kv)} komplett im Grafikspeicher{est}.{more}",
            f"KV cache {gb(kv)} fully in VRAM{est}.{more}"))

    def _flash(self, state: str | None) -> None:
        """Flash-Attention (aus dem Startlog des llama-server): spart Speicher für den Kontext, macht lange Prompts
        schneller und ist Voraussetzung für einen q8/q4-KV-Cache."""
        title = "Flash-Attention"
        if state == "on":
            self.checks["flash"] = _check("ok", "flash", title, T("aktiv.", "active."))
        elif state == "off":
            self.checks["flash"] = _check("warn", "flash", title, T(
                "aus – der Kontext braucht mehr Grafikspeicher, lange Prompts werden langsamer eingelesen und ein "
                "q8/q4-KV-Cache geht nicht. Abhilfe: llama.cpp aktualisieren (Einstellungen → Modelle) oder "
                "„-fa on“ im Startbefehl.",
                "off – the context needs more VRAM, long prompts are read more slowly and a q8/q4 KV cache is not "
                "possible. Fix: update llama.cpp (Settings → Models) or add “-fa on” to the start command."))

    # ------------------------------------------------------------------ 3. Stufe
    def _stage(self, plan, limit: int, window: int, first: dict | None, loaded: int, total: int) -> None:
        start = (first or {}).get("prompt_total")
        stage = T("Sparmodus (unter 16k)", "lean mode (below 16k)") if plan.small else \
            T("Normalbetrieb (ab 16k)", "normal mode (16k and up)")
        detail = T(f"{stage} · Platz schaffen ab {_num(limit)} Token · Antwortreserve "
                   f"{_num(self.agent.answer_reserve())} · {loaded} von {total} Werkzeugen geladen",
                   f"{stage} · makes room from {_num(limit)} tokens · answer reserve "
                   f"{_num(self.agent.answer_reserve())} · {loaded} of {total} tools loaded")
        status = "ok"
        if start:
            detail += T(f" · Start-Prompt {_num(start)} Token ({round(100 * start / window)} % des Fensters)",
                        f" · start prompt {_num(start)} tokens ({round(100 * start / window)} % of the window)")
            if start > 0.5 * limit:
                status = "warn"
                detail += T(". Das ist viel – für den Chat bleibt wenig Platz (kleineres Fenster nur mit weniger "
                            "Werkzeugen sinnvoll).", ". That is a lot – little room is left for the chat.")
        self.checks["stage"] = _check(status, "stage", T("Kontext-Stufe", "Context stage"), detail)

    # ------------------------------------------------------------------ 4. Schätzung
    def _estimate(self, est: int, first: dict) -> None:
        real = first.get("prompt_total")
        title = T("Token-Schätzung", "Token estimate")
        if not real:
            self.checks["estimate"] = _check("warn", "estimate", title, T(
                "Der Server meldet keine Prompt-Größe – die Anzeige bleibt eine Schätzung.",
                "The server reports no prompt size – the display stays an estimate."))
            return
        ratio = real / est
        self.agent.learn_tokens(est, real)
        learned = self.agent.token_ratio()
        text = T(f"Geschätzt {_num(est)}, echt {_num(real)} Token (Faktor {_dec(ratio)}; Orbwise gleicht mit "
                 f"{_dec(learned)} aus).", f"Estimated {_num(est)}, real {_num(real)} tokens (factor {_dec(ratio)}; "
                 f"Orbwise corrects with {_dec(learned)}).")
        if ratio > 3 or ratio < 0.5:
            status = "fail"
            text += T(" Die Abweichung ist größer, als Orbwise ausgleichen kann – bitte melden.",
                      " The gap is larger than Orbwise can correct – please report it.")
        elif 0.75 <= ratio <= 1.35:
            status = "ok"
        else:
            status = "warn"
            text += T(" Die Abweichung wird ab der ersten Antwort automatisch ausgeglichen.",
                      " The gap is corrected automatically from the first answer on.")
        self.checks["estimate"] = _check(status, "estimate", title, text)

    # ------------------------------------------------------------------ 5. Cache
    def _cache_hint(self) -> str:
        if not self.llama:
            return T(" Ollama: Wechselt num_ctx oder wird das Modell entladen, ist der Cache weg.",
                     " Ollama: changing num_ctx or unloading the model clears the cache.")
        line = _start_line() if getattr(self.profile, "server", None) else ""
        if line and "--ctx-checkpoints" not in line and "bonsai" in line.lower():
            return T(" Bonsai 2 ist ein Hybrid-Modell und braucht dafür --ctx-checkpoints – der llama-server-Build "
                     "kennt die Option nicht (neuer bauen: orbwise model add bonsai).",
                     " Bonsai 2 is a hybrid model and needs --ctx-checkpoints – this llama-server build does not "
                     "know the option (rebuild: orbwise model add bonsai).")
        return T(" Startbefehl prüfen (cache_prompt, --cache-reuse, bei Hybrid-Modellen --ctx-checkpoints).",
                 " Check the start command (cache_prompt, --cache-reuse, --ctx-checkpoints for hybrid models).")

    async def _cache(self, probe: list[dict], first: dict) -> None:
        total = first.get("prompt_total") or 0
        try:
            await self._say(T("Cache prüfen …", "Checking the cache …"))
            again = await self._ask(probe, self.agent.schemas)
            follow = probe + [{"role": "assistant", "content": "OK"},
                              {"role": "user", "content": T("Noch einmal – antworte nur mit OK.",
                                                            "Once more – reply only with OK.")}]
            after = await self._ask(follow, self.agent.schemas)
        except LLMError as e:
            self.checks["cache"] = _check("fail", "cache", T("Prompt-Cache", "Prompt cache"), str(e))
            return
        for key, stats, title, allowed in (
                ("cache", again, T("Prompt-Cache (gleicher Prompt)", "Prompt cache (same prompt)"), 64),
                ("followup", after, T("Prompt-Cache (Folgefrage)", "Prompt cache (follow-up)"), 160)):
            new = stats.get("prompt_tokens")
            if new is None or not total:
                self.checks[key] = _check("warn", key, title, T("Der Server meldet keine Zahlen dazu.",
                                                                 "The server reports no numbers for this."))
                continue
            secs = T(f" ({_dec(stats['seconds'], 1)} s statt {_dec(first['seconds'], 1)} s)",
                     f" ({_dec(stats['seconds'], 1)} s instead of {_dec(first['seconds'], 1)} s)")
            if new <= max(allowed, 0.15 * total):
                self.checks[key] = _check("ok", key, title, T(
                    f"Nur {_num(new)} von {_num(stats.get('prompt_total') or total)} Token neu eingelesen{secs}.",
                    f"Only {_num(new)} of {_num(stats.get('prompt_total') or total)} tokens read again{secs}."))
            elif first["seconds"] >= 2 and stats["seconds"] <= 0.25 * first["seconds"]:
                # Zahlen sagen „alles neu“, die Zeit sagt „aus dem Cache“ – dann zählt die Zeit
                self.checks[key] = _check("ok", key, title, T(
                    f"Aus dem Cache{secs} – der Server meldet dazu nur keine Cache-Zahlen.",
                    f"Served from the cache{secs} – the server just doesn't report cache numbers."))
            else:
                self.checks[key] = _check("fail", key, title, T(
                    f"{_num(new)} von {_num(stats.get('prompt_total') or total)} Token neu eingelesen – der Anfang "
                    f"wird nicht wiederverwendet, jede Antwort liest alles neu ein{secs}.",
                    f"{_num(new)} of {_num(stats.get('prompt_total') or total)} tokens read again – the prefix is "
                    f"not reused, every answer re-reads everything{secs}.") + self._cache_hint())

    # ------------------------------------------------------------------ 6. Füllung
    async def _fill(self, window: int, limit: int) -> None:
        title = T("Volles Fenster", "Full window")
        word = f"{random.choice(WORDS)}-{random.randint(100, 999)}"
        head = T(f"Merke dir das Codewort: {word}\n\n", f"Remember the code word: {word}\n\n")
        question = T("\n\nWie lautet das Codewort vom Anfang dieser Nachricht? Antworte nur mit dem Codewort.",
                     "\n\nWhat is the code word from the start of this message? Reply only with the code word.")
        def system() -> dict:  # eigener Anfang je Anfrage: Ollama zählt sonst nur die nicht gecachten Token
            return {"role": "system", "content": f"[{uuid.uuid4().hex[:10]}] " + T(
                "Du bist ein Testassistent. Antworte knapp.", "You are a test assistant. Reply briefly.")}
        sys_est = est_tokens(system()["content"])
        try:
            await self._say(T("Fenster füllen – Probe …", "Filling the window – sample …"))
            sample_text = head + filler(1500)
            sample = await self._ask([system(), {"role": "user", "content": sample_text}])
            sample_est = sys_est + est_tokens(sample_text) + 8
            ratio = max(0.5, min(3.0, (sample.get("prompt_total") or sample_est) / sample_est))
            target = min(limit, window - 64) - 40  # so groß wie der größte Prompt, den Orbwise schickt
            est = max(1500, int(target / ratio) - sys_est - 40)
            body = head + filler(est) + question
            await self._say(T(f"Fenster füllen – ca. {_num(target)} Token einlesen …",
                              f"Filling the window – reading about {_num(target)} tokens …"))
            full = await self._ask([system(), {"role": "user", "content": body}], max_tokens=24)
        except ContextOverflow as e:
            self.checks["fill"] = _check("fail", "fill", title, T(
                f"Ein Prompt von ca. {_num(limit)} Token (Komprimierungspunkt) passt nicht ins Fenster: {e} – das "
                "Fenster ist kleiner, als der Server meldet.",
                f"A prompt of about {_num(limit)} tokens (compaction point) does not fit: {e} – the window is "
                "smaller than the server reports."))
            return
        except LLMError as e:
            self.checks["fill"] = _check("fail", "fill", title, str(e))
            return
        real = full.get("prompt_total") or 0
        expected = int((sys_est + est_tokens(body) + 8) * ratio)
        speed = T(f" Eingelesen in {_dec(full['seconds'], 1)} s", f" Read in {_dec(full['seconds'], 1)} s") + \
            (f" ({_num(full['prompt_tps'])} Token/s)." if full.get("prompt_tps") else ".")
        seen = word.lower() in full["text"].lower()
        if real and real < 0.8 * expected:
            self.checks["fill"] = _check("fail", "fill", title, T(
                f"Gesendet ca. {_num(expected)} Token, verarbeitet nur {_num(real)} – der Server kürzt den Prompt "
                "still (Ollama: num_ctx kleiner als gedacht).",
                f"Sent about {_num(expected)} tokens, only {_num(real)} processed – the server silently truncates "
                "the prompt (Ollama: num_ctx smaller than expected).") + speed)
        elif seen:
            self.checks["fill"] = _check("ok", "fill", title, T(
                f"{_num(real or expected)} Token ({round(100 * (real or expected) / window)} % des Fensters) passen, "
                f"und das Modell kennt noch das Codewort vom Anfang.",
                f"{_num(real or expected)} tokens ({round(100 * (real or expected) / window)} % of the window) fit "
                f"and the model still knows the code word from the start.") + speed)
        else:
            answer = full["text"].strip()[:60] or "–"
            self.checks["fill"] = _check("warn", "fill", title, T(
                f"{_num(real or expected)} Token passen, aber das Modell nennt das Codewort „{word}“ nicht "
                f"(Antwort: „{answer}“). Es verarbeitet zwar alles, verliert bei vollem Fenster aber den Anfang aus "
                "dem Blick – bei Problemen ein kleineres Fenster wählen.",
                f"{_num(real or expected)} tokens fit, but the model does not name the code word \"{word}\" "
                f"(answer: \"{answer}\"). Everything is processed, but with a full window it loses track of the "
                "start – choose a smaller window if this causes problems.") + speed)

    # ------------------------------------------------------------------ 7. Überlauf
    async def _overflow(self, window: int) -> None:
        title = T("Überlauf-Erkennung", "Overflow detection")
        ratio = max(0.5, self.agent.token_ratio())
        text = filler(int(window * 1.2 / ratio))
        try:
            await self._say(T("Überlauf prüfen …", "Checking overflow …"))
            stats = await self._ask([{"role": "user", "content": text}])
        except ContextOverflow as e:
            self.checks["overflow"] = _check("ok", "overflow", title, T(
                "Ein zu großer Prompt wird vom Server abgelehnt" + (f" (Fenster {_num(e.n_ctx)})" if e.n_ctx else "")
                + " – Orbwise erkennt das, schafft Platz und versucht es erneut.",
                "An oversized prompt is rejected by the server" + (f" (window {_num(e.n_ctx)})" if e.n_ctx else "")
                + " – Orbwise detects it, makes room and retries."))
            return
        except LLMError as e:
            self.checks["overflow"] = _check("warn", "overflow", title, T(
                f"Der Server meldet einen Fehler, den Orbwise nicht als Überlauf erkennt: {str(e)[:200]}",
                f"The server reports an error Orbwise does not recognise as overflow: {str(e)[:200]}"))
            return
        self.checks["overflow"] = _check("warn", "overflow", title, T(
            f"Ein Prompt über dem Fenster wurde ohne Fehler angenommen ({_num(stats.get('prompt_total'))} Token "
            "verarbeitet) – der Server kürzt oder verschiebt still (z. B. --context-shift). Orbwise bemerkt dann "
            "nicht, dass Teile fehlen.",
            f"A prompt larger than the window was accepted without an error ({_num(stats.get('prompt_total'))} "
            "tokens processed) – the server truncates or shifts silently (e.g. --context-shift). Orbwise then does "
            "not notice that parts are missing."))


async def run_context_test(agent, llm, quick: bool = False, gpu: dict | None = None,
                           progress: Progress | None = None) -> dict:
    return await ContextTest(agent, llm, gpu, progress).run(quick)


def format_report(result: dict) -> str:
    """Textfassung für die Kommandozeile."""
    marks = {"ok": "✔", "warn": "⚠", "fail": "✘", "info": "ℹ"}
    lines = [T(f"Kontext-Selbsttest · Profil {result.get('profile') or '?'} · Fenster {_num(result.get('window'))} Token",
               f"Context self-test · profile {result.get('profile') or '?'} · window {_num(result.get('window'))} tokens"),
             ""]
    for c in result["checks"]:
        lines.append(f"  {marks.get(c['status'], '·')} {c['title']}: {c['detail']}")
    lines.append("")
    lines.append({"ok": T("Alles in Ordnung.", "All good."),
                  "warn": T("Läuft, aber mit Hinweisen (⚠).", "Works, but see the notes (⚠)."),
                  "fail": T("Es gibt Probleme (✘) – siehe oben.", "There are problems (✘) – see above.")}[result["status"]])
    return "\n".join(lines)
