"""Wie genau ist eine Modell-Variante? Gemessen mit `llama-perplexity` (liegt in den offiziellen Builds neben
llama-server) auf einem festen Text: deutsche und englische Wikipedia-Artikel, einmal geladen und gespeichert.

Die genaueste vorhandene Fassung des Modells (höchstes Quant im selben Ordner, KV-Cache f16) ist die Referenz: ihre
Wahrscheinlichkeiten für jedes nächste Token werden gespeichert. Jede Variante – derselbe Text mit KV-Cache q8/q4
oder ein anderes Quant – wird dagegen verglichen:
- **gleiches Top-Token**: wie oft die Variante dasselbe wahrscheinlichste nächste Token wählt (100 % = gleich),
- **KL-Divergenz**: wie weit ihre Wahrscheinlichkeiten im Mittel abweichen (0 = gleich; < 0,01 kaum spürbar),
- **Perplexität**: wie gut sie den Text vorhersagt (kleiner = besser).
Das misst die Nähe zum Original, nicht wie gut das Modell Aufgaben löst – dafür gibt es scripts/llm_bench.py."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shlex
import time
from pathlib import Path

import httpx

from . import llamacpp, state
from .lang import T
from .llamacpp_tune import Progress, Runner, _why, run_process
from .llm_memory import cache_type

log = logging.getLogger(__name__)

CHUNKS = 12            # Abschnitte zu je CHUNK Token (~6000 Token – genug für stabile Mittelwerte)
CHUNK = 512
TIMEOUT_S = 1800       # je Lauf
ARTICLES = {"de": ["Schwarzwald", "Johann Sebastian Bach", "Photosynthese", "Linux", "Rhein", "Kaffee"],
            "en": ["Black Forest", "Johann Sebastian Bach", "Photosynthesis", "Linux", "Rhine", "Coffee"]}
ARTICLE_CHARS = 12000  # je Artikel höchstens so viel Text
KV_VARIANTS = ("f16", "q8_0", "q4_0")
# Kopiert aus dem Startbefehl des Profils: wie das Modell auf die GPUs verteilt ist und rechnet
_KEEP = {"-ngl": 1, "--n-gpu-layers": 1, "--n-cpu-moe": 1, "-ncmoe": 1, "-ts": 1, "--tensor-split": 1, "-sm": 1,
         "--split-mode": 1, "-mg": 1, "--main-gpu": 1, "-fa": 1, "--flash-attn": 1, "-ub": 1, "--ubatch-size": 1}


DATASETS_API = "https://datasets-server.huggingface.co/rows"
MIN_CHARS = 20000


def user_agent() -> str:
    """Wikimedia verlangt bei automatischen Zugriffen Programm, Version und Kontakt – sonst 403 („robot policy“)."""
    from . import __version__
    return (f"Orbwise/{__version__} (https://github.com/ressjo/orbwise-linux-agent; local assistant) "
            f"httpx/{httpx.__version__}")


def corpus_dir() -> Path:
    """Der Messtext liegt im Cache (lässt sich jederzeit neu laden), nicht bei den Daten."""
    return Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "orbwise" / "quality"


def corpus_path(cfg) -> Path:
    return corpus_dir() / "text.txt"


def own_text() -> Path:
    """Eigener Messtext, falls Wikipedia und Hugging Face beide nicht gehen."""
    return corpus_dir() / "eigener-text.txt"


async def _wikipedia(client: httpx.AsyncClient) -> str:
    parts = []
    for lang, titles in ARTICLES.items():
        for title in titles:
            r = await client.get(f"https://{lang}.wikipedia.org/w/api.php", params={
                "action": "query", "prop": "extracts", "explaintext": 1, "format": "json", "redirects": 1,
                "titles": title})
            r.raise_for_status()
            pages = (r.json().get("query") or {}).get("pages") or {}
            text = next((p.get("extract") or "" for p in pages.values()), "")
            text = re.sub(r"\n={2,}[^\n]*={2,}\n", "\n\n", text)  # Überschriften raus
            if text:
                parts.append(text[:ARTICLE_CHARS])
            await asyncio.sleep(PAUSE_S)  # Wikimedia bittet um Pausen zwischen automatischen Anfragen
    return "\n\n".join(parts)


async def _wikitext(client: httpx.AsyncClient) -> str:
    """wikitext-2 (Testteil) – der übliche englische Text für Perplexitätsmessungen."""
    lines: list[str] = []
    for offset in range(0, 3000, 100):
        r = await client.get(DATASETS_API, params={"dataset": "Salesforce/wikitext", "config": "wikitext-2-raw-v1",
                                                   "split": "test", "offset": offset, "length": 100})
        r.raise_for_status()
        rows = r.json().get("rows") or []
        lines += [row["row"].get("text", "") for row in rows]
        if not rows or sum(map(len, lines)) >= ARTICLE_CHARS * 6:
            break
    return "".join(x for x in lines if not x.strip().startswith("="))  # Überschriften raus


SOURCES = (("Wikipedia de/en", _wikipedia), ("wikitext-2 (en)", _wikitext))
PAUSE_S = 0.5


async def ensure_corpus(cfg, client: httpx.AsyncClient | None = None) -> tuple[Path, str]:
    """Den Messtext einmal laden und speichern – danach immer derselbe (vergleichbare Messungen). Reihenfolge: eigener
    Text, gespeicherter Text, Wikipedia (deutsch und englisch), wikitext-2 von Hugging Face. → (Datei, Quelle)."""
    own = own_text()
    if own.exists() and own.stat().st_size >= MIN_CHARS:
        return own, T("eigener Text", "own text")
    path = corpus_path(cfg)
    meta = path.with_suffix(".json")
    if path.exists() and path.stat().st_size >= MIN_CHARS:
        try:
            return path, json.loads(meta.read_text(encoding="utf-8")).get("source", "")
        except (OSError, ValueError):
            return path, ""
    mine = client is None
    client = client or httpx.AsyncClient(timeout=30, follow_redirects=True,
                                         headers={"User-Agent": user_agent(), "Accept-Encoding": "gzip"})
    tried = []
    try:
        for source, fetch in SOURCES:
            try:
                text = await fetch(client)
            except (httpx.HTTPError, ValueError, KeyError) as e:
                tried.append(f"{source}: {str(e).splitlines()[0][:160]}")
                log.warning("Messtext von %s nicht ladbar: %s", source, e)
                continue
            if len(text) < MIN_CHARS:
                tried.append(f"{source}: " + T("zu wenig Text", "too little text"))
                continue
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
            meta.write_text(json.dumps({"source": source}), encoding="utf-8")
            return path, source
    finally:
        if mine:
            await client.aclose()
    raise RuntimeError(T(
        "Kein Messtext ladbar (" + "; ".join(tried) + f"). Eigenen Text (mindestens {MIN_CHARS} Zeichen) als {own} "
        "ablegen und erneut messen.",
        "No test text could be loaded (" + "; ".join(tried) + f"). Put your own text (at least {MIN_CHARS} "
        f"characters) at {own} and measure again."))


def quant_of(gguf: Path) -> str:
    from .models import QUANT_RE, SPLIT_RE
    m = QUANT_RE.search(SPLIT_RE.sub(".gguf", gguf.name))
    return (m.group(1) if m else "").upper()


def precision(quant: str) -> float:
    """Ungefähre Bits je Gewicht aus dem Namen (F16/BF16 16, Q8_0 8, Q6_K 6, UD-Q4_K_XL 4 …) – für die Reihenfolge."""
    q = quant.upper()
    if q in ("F32",):
        return 32
    if q in ("F16", "BF16"):
        return 16
    m = re.search(r"Q(\d)", q)
    if not m:
        return 0
    bits = float(m.group(1))
    return bits + (0.5 if "_K_XL" in q or "_K_L" in q else 0.3 if "_K_M" in q else 0.1 if "_K" in q else 0)


def sibling_quants(gguf: Path) -> list[Path]:
    """Andere Quants desselben Modells im selben Ordner (gleicher Name ohne Quant-Teil), samt dem Modell selbst."""
    from .models import QUANT_RE, SPLIT_RE, is_sidecar
    def base(p: Path) -> str:
        name = SPLIT_RE.sub(".gguf", p.name)
        return QUANT_RE.sub("", name).lower().strip("-_. ")
    me = base(gguf)
    out = []
    for f in sorted(gguf.parent.glob("*.gguf")):
        if is_sidecar(f.name) or not quant_of(f):
            continue
        if SPLIT_RE.search(f.name) and not re.search(r"-00001-of-", f.name):
            continue  # bei geteilten Dateien nur der erste Teil
        if base(f) == me:
            out.append(f)
    return out or [gguf]


def keep_flags(command: str) -> list[str]:
    """Verteilung auf GPUs, Experten im RAM, Flash-Attention, Batch aus dem Startbefehl übernehmen."""
    try:
        words = shlex.split(command)
    except ValueError:
        return []
    out, i = [], 0
    while i < len(words):
        w = words[i]
        if w in _KEEP and i + 1 < len(words):
            out += [w, words[i + 1]]
            i += 2
            continue
        i += 1
    if not any(f in out for f in ("-ngl", "--n-gpu-layers")):
        out = ["-ngl", "99", *out]
    return out


def perplexity_args(exe: Path, gguf: Path, text: Path, kv: str, flags: list[str], kld: Path, base: bool) -> list[str]:
    args = [str(exe), "-m", str(gguf), "-f", str(text), "-c", str(CHUNK), "--chunks", str(CHUNKS), *flags]
    if kv != "f16":
        args += ["-ctk", kv, "-ctv", kv]
        if not any(f in flags for f in ("-fa", "--flash-attn")):
            args += ["-fa", "on"]  # quantisierter V-Cache braucht Flash-Attention
    args += ["--kl-divergence-base", str(kld)] + ([] if base else ["--kl-divergence"])
    return args


_NUM = r"([0-9]+(?:\.[0-9]+)?)"


def parse_perplexity(text: str) -> dict | None:
    """Ausgabe von llama-perplexity → {ppl, kld, same_top} (soweit vorhanden); None ohne Ergebnis."""
    out = {}
    if (m := re.search(rf"Mean PPL\(Q\)\s*:\s*{_NUM}", text)) or (m := re.search(rf"Final estimate: PPL = {_NUM}", text)):
        out["ppl"] = float(m.group(1))
    if m := re.search(rf"Mean\s+KLD:\s*{_NUM}", text):
        out["kld"] = float(m.group(1))
    if m := re.search(rf"Same top p:\s*{_NUM}\s*±?\s*[0-9.]*\s*%", text):
        out["same_top"] = float(m.group(1))
    return out or None


def variants(gguf: Path, current_kv: str, kv_options: list[str]) -> tuple[dict, list[dict]]:
    """Referenz und zu messende Varianten: dieses Modell mit jeder KV-Stufe, andere Quants mit KV f16."""
    quants = sibling_quants(gguf)
    ref = max(quants, key=lambda f: (precision(quant_of(f)), f == gguf))
    reference = {"file": ref, "kv": "f16", "label": f"{quant_of(ref) or ref.name} · KV f16"}
    out = []
    for kv in [k for k in KV_VARIANTS if k in kv_options]:
        if ref == gguf and kv == "f16":
            continue  # das ist die Referenz selbst
        out.append({"file": gguf, "kv": kv, "kind": "kv", "label": f"{quant_of(gguf) or gguf.name} · KV {kv.replace('_0', '')}",
                    "current": kv == current_kv})
    for f in quants:
        if f not in (gguf, ref):
            out.append({"file": f, "kv": "f16", "kind": "quant", "label": f"{quant_of(f)} · KV f16", "current": False})
    if ref != gguf:  # das Referenz-Quant selbst zeigen (100 %), damit die Tabelle lesbar bleibt
        out.insert(0, {"file": ref, "kv": "f16", "kind": "ref", "label": reference["label"], "current": False})
    return reference, out


async def measure(cfg, router, name: str, progress: Progress | None = None, run: Runner = run_process,
                  corpus=None) -> dict:
    """Messen und Ergebnis in state.json["quality"][name] merken → Ergebnis."""
    from .llm_router import kv_types
    p = router.profiles[name]
    files = [f for f in llamacpp.profile_paths(p) if f.is_file()]
    if not p.server or not files:
        raise ValueError(T("Messen geht nur bei Modellen mit Orbwises llama.cpp.",
                           "Measuring only works for models running on Orbwise's llama.cpp."))
    gguf = files[0]
    say = progress or (lambda text: asyncio.sleep(0))
    folder = (llamacpp.root(cfg) / "current").resolve()
    exe = folder / "llama-perplexity"
    if not exe.exists():
        raise ValueError(T("llama-perplexity fehlt im llama.cpp-Build – bitte llama.cpp aktualisieren.",
                           "llama-perplexity is missing from the llama.cpp build – please update llama.cpp."))
    await say(T("Lade den Messtext …", "Loading the test text …"))
    text, source = await (corpus() if corpus else ensure_corpus(cfg))
    reference, todo = variants(gguf, cache_type(p.server.command, {}), kv_types(p.server.command, p.server.env or {}))
    flags = keep_flags(p.server.command)
    env = llamacpp._env(folder)
    kld = text.parent / f"base-{name}.kld"
    if router.gpu_borrowed:  # Bild-Modus hält den Grafikspeicher
        await router.gpu_borrowed(restart=False)
    was_loaded = not router.unloaded
    if was_loaded:
        await say(T("Entlade das Modell, damit der Grafikspeicher frei ist …", "Unloading the model to free video memory …"))
        await router.unload()
    results: list[dict] = []
    notes: list[str] = []
    base: dict = {}
    try:
        await say(T(f"Referenz {reference['label']} (1/{len(todo) + 1}) …", f"Reference {reference['label']} (1/{len(todo) + 1}) …"))
        code, out, err = await run(perplexity_args(exe, reference["file"], text, "f16", flags, kld, True), env, TIMEOUT_S)
        base = parse_perplexity(out + err) if code == 0 else {}
        if code != 0 or not kld.exists():
            raise RuntimeError(T("Referenz nicht gemessen: ", "Reference not measured: ") + _why(code, err))
        for i, v in enumerate(todo, 2):
            if v["kind"] == "ref":
                results.append({**_row(v), "same_top": 100.0, "kld": 0.0, "ppl": base.get("ppl")})
                continue
            await say(T(f"Messe {v['label']} ({i}/{len(todo) + 1}) …", f"Measuring {v['label']} ({i}/{len(todo) + 1}) …"))
            code, out, err = await run(perplexity_args(exe, v["file"], text, v["kv"], flags, kld, False), env, TIMEOUT_S)
            got = parse_perplexity(out + err) if code == 0 else None
            results.append({**_row(v), **got} if got and "kld" in got else
                           {**_row(v), "error": _why(code, err) if code else T("keine Messwerte", "no results")})
    finally:
        kld.unlink(missing_ok=True)  # die Referenz-Logits sind groß (GB) – nur für diese Messung
        if was_loaded:
            await say(T("Lade das Modell wieder …", "Loading the model again …"))
            try:
                await router.ensure_loaded()
            except Exception as e:  # noqa: BLE001 – Ergebnis trotzdem melden
                notes.append(T(f"Neu laden fehlgeschlagen: {e}", f"Reloading failed: {e}"))
    result = {"at": time.time(), "reference": reference["label"], "reference_ppl": base.get("ppl"), "source": source,
              "tokens": CHUNKS * CHUNK, "variants": results, "notes": notes}
    if router.state_path:
        def remember(data: dict) -> None:
            data.setdefault("quality", {})[name] = result
        state.update(router.state_path, remember)
    return result


def _row(v: dict) -> dict:
    f = v["file"]
    size = sum(p.stat().st_size for p in llamacpp.shards(f) if p.exists())
    return {"label": v["label"], "kind": v["kind"], "kv": v["kv"], "file": f.name, "current": v.get("current", False),
            "size_gb": round(size / 1e9, 1)}


def saved(state_path: Path | None, name: str) -> dict | None:
    if not state_path:
        return None
    return (state.load(state_path).get("quality") or {}).get(name)

