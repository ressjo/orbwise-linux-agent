#!/usr/bin/env python3
"""LLM-Benchmark für die Orbwise-Modellprofile – eigenständig, nicht Teil der Oberfläche.

Prüft jedes Profil (Ollama oder llama-server) mit Stichproben aus den Original-Datensätzen und gibt am Ende eine
Tabelle wie bei offiziellen Modellvorstellungen aus:

    MMLU-Pro   Wissen, 10 Antwortmöglichkeiten          TIGER-Lab/MMLU-Pro
    GSM8K      Mathe-Textaufgaben                       openai/gsm8k
    HumanEval  Python-Funktionen schreiben (pass@1)     openai/openai_humaneval
    ARC-C      naturwissenschaftliches Schlussfolgern   allenai/ai2_arc (ARC-Challenge)
    IFEval     prüfbare Anweisungen befolgen            google/IFEval
    Speed      Prompt einlesen (Prefill) und Generieren in Token/s

Aufruf (im Orbwise-Ordner):
    uv run python scripts/llm_bench.py --list
    uv run python scripts/llm_bench.py --profiles qwen3-14b --limit 50
    uv run python scripts/llm_bench.py --resume            # abgebrochenen Lauf fortsetzen
    uv run python scripts/llm_bench.py --table <ordner>    # Tabelle eines Laufs erneut ausgeben

Die Datensätze werden beim ersten Lauf über die Hugging-Face-Datasets-API geladen und in ~/.cache/orbwise/bench
gespeichert (HF_TOKEN wird mitgeschickt, wenn gesetzt). Ergebnisse: ~/.local/share/orbwise/bench/<zeit>/
(results.md, results.csv, results.json und je Profil alle Einzelantworten).

Hinweis: Eine Stichprobe (Standard 200 Fragen) und 0-shot-Prompts sind nicht exakt die offiziellen Zahlen, aber mit
festem Seed zwischen deinen Modellen vergleichbar. HumanEval führt den vom Modell geschriebenen Code lokal aus
(eigener Prozess, Zeitlimit, Speicherlimit, leerer Temp-Ordner) – mit --no-exec wird es übersprungen.
"""

from __future__ import annotations

import argparse
import asyncio
import collections
import csv
import json
import os
import random
import re
import statistics
import subprocess
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable

import httpx

from orbwise.config import DATA_DIR, load_config
from orbwise.lang import T, set_lang
from orbwise.llm import LLMError, OllamaLLM, OpenAICompatLLM, strip_think

ROWS_API = os.environ.get("HF_DATASETS_SERVER", "https://datasets-server.huggingface.co").rstrip("/") + "/rows"
PAGE = 100  # mehr gibt die Datasets-API je Anfrage nicht heraus


def cache_dir() -> Path:
    return Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "orbwise" / "bench"


RUNS_DIR = DATA_DIR / "bench"


class BenchError(RuntimeError):
    pass


# ---------------------------------------------------------------------------------------------------------------
# Antworten auswerten
# ---------------------------------------------------------------------------------------------------------------

def extract_choice(text: str, labels: list[str]) -> str | None:
    """Gewählte Antwort aus „The answer is (B)“ & Co. – None, wenn keine erkennbar ist."""
    cls = "".join(re.escape(label) for label in labels)
    patterns = (
        rf"(?i:answer is)\s*:?\s*[*_\s]*\(?\s*([{cls}])\b",
        rf"(?i:answer)\s*:\s*[*_\s]*\(?\s*([{cls}])\b",
        rf"\\boxed\{{\s*\(?([{cls}])\)?\s*\}}",
    )
    for pat in patterns:
        found = re.findall(pat, text)
        if found:
            return found[-1]
    m = re.fullmatch(rf"\W*\(?([{cls}])\)?\W*", text.strip())
    return m.group(1) if m else None


_NUM = r"-?[\d,]*\.?\d+"


def _to_float(s: str) -> float | None:
    try:
        return float(s.replace(",", "").rstrip("."))
    except ValueError:
        return None


def extract_number(text: str) -> float | None:
    """Endergebnis einer Rechenaufgabe: „The answer is 42“, \\boxed{42} oder die letzte Zahl im Text."""
    for pat in (rf"(?i:answer is)\s*:?\s*[*_\s]*\$?\s*({_NUM})", rf"\\boxed\{{\s*\$?({_NUM})"):
        found = re.findall(pat, text)
        if found:
            return _to_float(found[-1])
    found = re.findall(_NUM, text)
    return _to_float(found[-1]) if found else None


def extract_code(text: str, entry_point: str) -> str:
    """Python-Code aus der Antwort: der Codeblock mit der gesuchten Funktion, sonst der erste, sonst alles."""
    blocks = re.findall(r"```[ \t]*(?:python|py|Python|python3)?[ \t]*\n(.*?)```", text, re.S)
    if not blocks:
        m = re.search(r"```[ \t]*\w*[ \t]*\n(.*)$", text, re.S)  # abgeschnittener Block ohne Ende
        blocks = [m.group(1)] if m else [text]
    for block in blocks:
        if re.search(rf"^\s*def\s+{re.escape(entry_point)}\s*\(", block, re.M):
            return block
    return blocks[0]


def humaneval_program(row: dict, answer: str) -> str:
    code = extract_code(answer, row["entry_point"])
    if re.search(rf"^def\s+{re.escape(row['entry_point'])}\s*\(", code, re.M):
        program = row["prompt"] + "\n" + code  # ganze Funktion: ersetzt den Rumpf aus der Vorgabe (Importe bleiben)
    else:
        program = row["prompt"] + code  # nur der Rumpf
    return program + "\n\n" + row["test"] + f"\n\ncheck({row['entry_point']})\n"


def _limits() -> None:  # im Kindprozess: Speicher, Dateigröße, Prozesse begrenzen
    import resource
    gb = 1024 ** 3
    for res, value in ((resource.RLIMIT_AS, 2 * gb), (resource.RLIMIT_FSIZE, 16 * 1024 ** 2),
                       (resource.RLIMIT_CPU, 20)):
        try:
            resource.setrlimit(res, (value, value))
        except (ValueError, OSError):
            pass


def run_python(program: str, timeout: float = 10.0) -> tuple[bool, str]:
    """Programm in einem leeren Temp-Ordner ausführen – (bestanden, Fehlerausgabe)."""
    with tempfile.TemporaryDirectory(prefix="orbwise-bench-") as tmp:
        path = Path(tmp) / "prog.py"
        path.write_text(program, encoding="utf-8")
        env = {"PATH": "/usr/bin:/bin", "HOME": tmp, "TMPDIR": tmp, "PYTHONDONTWRITEBYTECODE": "1"}
        try:
            proc = subprocess.run([sys.executable, "-I", str(path)], cwd=tmp, env=env, stdin=subprocess.DEVNULL,
                                  capture_output=True, timeout=timeout, preexec_fn=_limits)
        except subprocess.TimeoutExpired:
            return False, "timeout"
        return proc.returncode == 0, proc.stderr.decode(errors="replace")[-500:]


# ---------- IFEval: Prüffunktionen nach google-research/instruction_following_eval ----------

def _rel(count: int, n, relation: str | None) -> bool:
    return count < int(n) if relation == "less than" else count >= int(n)


def _number_paragraphs(r: str, num_paragraphs, **_) -> bool:
    parts = re.split(r"\s?\*\*\*\s?", r)
    count = len(parts)
    for i, p in enumerate(parts):
        if not p.strip():
            if i in (0, len(parts) - 1):
                count -= 1
            else:
                return False
    return count == int(num_paragraphs)


def _nth_paragraph_first_word(r: str, num_paragraphs, nth_paragraph, first_word, **_) -> bool:
    parts = re.split(r"\n\n", r)
    count = sum(1 for p in parts if p.strip())
    nth = int(nth_paragraph)
    if nth > count or nth < 1:
        return False
    paragraph = parts[nth - 1].strip()
    if not paragraph:
        return False
    word = paragraph.split()[0].strip().lstrip("'").lstrip('"')
    first = ""
    for ch in word:
        if ch in ".,?!'\"":
            break
        first += ch.lower()
    return count == int(num_paragraphs) and first == str(first_word).lower()


def _postscript(r: str, postscript_marker, **_) -> bool:
    marker = str(postscript_marker).strip()
    if marker == "P.P.S":
        pat = r"\s*p\.\s?p\.\s?s.*$"
    elif marker == "P.S.":
        pat = r"\s*p\.\s?s\..*$"
    else:
        pat = r"\s*" + re.escape(marker.lower()) + r".*$"
    return bool(re.findall(pat, r.lower(), flags=re.M))


def _highlighted(r: str, num_highlights, **_) -> bool:
    count = sum(1 for h in re.findall(r"\*[^\n\*]*\*", r) if h.strip("*").strip())
    count += sum(1 for h in re.findall(r"\*\*[^\n\*]*\*\*", r) if h.removeprefix("**").removesuffix("**").strip())
    return count >= int(num_highlights)


def _json_format(r: str, **_) -> bool:
    value = r.strip()
    for prefix in ("```json", "```Json", "```JSON", "```"):
        value = value.removeprefix(prefix)
    value = value.removesuffix("```").strip()
    try:
        json.loads(value)
    except ValueError:
        return False
    return True


def _two_responses(r: str, **_) -> bool:
    parts = r.split("******")
    valid = []
    for i, p in enumerate(parts):
        if not p.strip():
            if i not in (0, len(parts) - 1):
                return False
        else:
            valid.append(p)
    return len(valid) == 2 and valid[0].strip() != valid[1].strip()


def _language(r: str, language, **_) -> bool:
    from langdetect import LangDetectException, detect  # nur, wenn installiert (siehe IFEVAL_CHECKS)
    try:
        return detect(r) == language
    except LangDetectException:
        return True


def _sentences(r: str) -> int:
    return len([s for s in re.split(r"(?<=[.!?])\s+", r.strip()) if s.strip()])


IFEVAL_CHECKS: dict[str, Callable[..., bool]] = {
    "keywords:existence": lambda r, keywords, **_: all(re.search(re.escape(k), r, re.I) for k in keywords),
    "keywords:frequency": lambda r, keyword, frequency, relation, **_:
        _rel(len(re.findall(re.escape(keyword), r, re.I)), frequency, relation),
    "keywords:forbidden_words": lambda r, forbidden_words, **_:
        not any(re.search(r"\b" + re.escape(w) + r"\b", r, re.I) for w in forbidden_words),
    "keywords:letter_frequency": lambda r, letter, let_frequency, let_relation, **_:
        _rel(collections.Counter(r.lower())[str(letter).lower()], let_frequency, let_relation),
    "length_constraints:number_words": lambda r, num_words, relation, **_:
        _rel(len(re.findall(r"\w+", r)), num_words, relation),
    "length_constraints:number_sentences": lambda r, num_sentences, relation, **_:
        _rel(_sentences(r), num_sentences, relation),
    "length_constraints:number_paragraphs": _number_paragraphs,
    "length_constraints:nth_paragraph_first_word": _nth_paragraph_first_word,
    "detectable_content:number_placeholders": lambda r, num_placeholders, **_:
        len(re.findall(r"\[.*?\]", r)) >= int(num_placeholders),
    "detectable_content:postscript": _postscript,
    "detectable_format:number_bullet_lists": lambda r, num_bullets, **_:
        len(re.findall(r"^\s*\*[^\*].*$", r, re.M)) + len(re.findall(r"^\s*-.*$", r, re.M)) == int(num_bullets),
    "detectable_format:constrained_response": lambda r, **_:
        any(c in r.strip() for c in ("My answer is yes.", "My answer is no.", "My answer is maybe.")),
    "detectable_format:number_highlighted_sections": _highlighted,
    "detectable_format:multiple_sections": lambda r, section_spliter, num_sections, **_:
        len(re.split(r"\s?" + re.escape(section_spliter) + r"\s?\d+\s?", r)) - 1 >= int(num_sections),
    "detectable_format:json_format": _json_format,
    "detectable_format:title": lambda r, **_:
        any(t.lstrip("<").rstrip(">").strip() for t in re.findall(r"<<[^\n]+>>", r)),
    "combination:two_responses": _two_responses,
    "combination:repeat_prompt": lambda r, prompt_to_repeat, **_:
        r.strip().lower().startswith(prompt_to_repeat.strip().lower()),
    "startend:end_checker": lambda r, end_phrase, **_:
        r.strip().strip('"').lower().endswith(end_phrase.strip().lower()),
    "startend:quotation": lambda r, **_: len(r.strip()) > 1 and r.strip()[0] == '"' and r.strip()[-1] == '"',
    "change_case:capital_word_frequency": lambda r, capital_frequency, capital_relation, **_:
        _rel(sum(1 for w in re.findall(r"\b\w+\b", r) if w.isupper()), capital_frequency, capital_relation),
    "change_case:english_capital": lambda r, **_: r.isupper(),
    "change_case:english_lowercase": lambda r, **_: r.islower(),
    "punctuation:no_comma": lambda r, **_: "," not in r,
}
try:
    import langdetect  # noqa: F401 – optional; ohne fallen die (wenigen) Sprach-Aufgaben aus der Stichprobe
    IFEVAL_CHECKS["language:response_language"] = _language
except ImportError:
    pass


def ifeval_supported(row: dict) -> bool:
    return all(i in IFEVAL_CHECKS for i in row["instruction_id_list"])


def ifeval_grade(row: dict, answer: str) -> tuple[bool, list[str]]:
    """Strict, prompt-level: alle Anweisungen müssen erfüllt sein. Liefert auch die nicht erfüllten."""
    failed = []
    for iid, kwargs in zip(row["instruction_id_list"], row["kwargs"]):
        kw = {k: v for k, v in (kwargs or {}).items() if v is not None}
        try:
            ok = IFEVAL_CHECKS[iid](answer, **kw)
        except (TypeError, ValueError, KeyError, re.error):
            ok = False
        if not ok:
            failed.append(iid)
    return not failed, failed


# ---------------------------------------------------------------------------------------------------------------
# Benchmarks
# ---------------------------------------------------------------------------------------------------------------

def _letters(n: int) -> list[str]:
    return [chr(ord("A") + i) for i in range(n)]


def mmlu_prompt(row: dict) -> str:
    opts = "\n".join(f"{label}. {o}" for label, o in zip(_letters(len(row["options"])), row["options"]))
    return (f"The following is a multiple choice question about {row.get('category', 'general knowledge')}. "
            "Think step by step and then finish your answer with \"The answer is (X)\" where X is the correct "
            f"letter choice.\n\nQuestion: {row['question']}\nOptions:\n{opts}")


def mmlu_grade(row: dict, answer: str) -> tuple[bool, str | None]:
    got = extract_choice(answer, _letters(len(row["options"])))
    return got == row["answer"], got


def arc_prompt(row: dict) -> str:
    opts = "\n".join(f"{label}. {t}" for label, t in zip(row["choices"]["label"], row["choices"]["text"]))
    return ("Answer the following science question. Think step by step and then finish your answer with "
            f"\"The answer is (X)\" where X is the correct choice.\n\nQuestion: {row['question']}\nOptions:\n{opts}")


def arc_grade(row: dict, answer: str) -> tuple[bool, str | None]:
    got = extract_choice(answer, list(row["choices"]["label"]))
    return got == row["answerKey"], got


def gsm8k_prompt(row: dict) -> str:
    return ("Solve the following math problem step by step. At the end, write the final answer on its own line "
            f"as \"The answer is N\", where N is just the number.\n\nQuestion: {row['question']}")


def gsm8k_gold(row: dict) -> float | None:
    return _to_float(row["answer"].split("####")[-1].strip())


def gsm8k_grade(row: dict, answer: str) -> tuple[bool, float | None]:
    got, gold = extract_number(answer), gsm8k_gold(row)
    return got is not None and gold is not None and abs(got - gold) < 1e-6, got


def humaneval_prompt(row: dict) -> str:
    return ("Complete the following Python function. Reply with the complete function, including the signature "
            "and any imports it needs, in a single ```python code block – no tests, no explanations.\n\n"
            f"```python\n{row['prompt']}```")


def humaneval_grade(row: dict, answer: str) -> tuple[bool, str]:
    ok, err = run_python(humaneval_program(row, answer))
    if ok:
        return True, ""
    lines = err.strip().splitlines()
    return False, lines[-1] if lines else "fail"


@dataclass(frozen=True)
class Bench:
    key: str
    column: str
    dataset: str
    config: str
    split: str
    prompt: Callable[[dict], str]
    grade: Callable[[dict, str], tuple]
    keep: Callable[[dict], bool] = lambda row: True
    executes_code: bool = False


BENCHES: dict[str, Bench] = {b.key: b for b in (
    Bench("mmlu_pro", "MMLU-Pro", "TIGER-Lab/MMLU-Pro", "default", "test", mmlu_prompt, mmlu_grade),
    Bench("gsm8k", "GSM8K", "openai/gsm8k", "main", "test", gsm8k_prompt, gsm8k_grade),
    Bench("humaneval", "HumanEval", "openai/openai_humaneval", "openai_humaneval", "test", humaneval_prompt,
          humaneval_grade, executes_code=True),
    Bench("arc", "ARC-C", "allenai/ai2_arc", "ARC-Challenge", "test", arc_prompt, arc_grade),
    Bench("ifeval", "IFEval", "google/IFEval", "default", "train", lambda row: row["prompt"], ifeval_grade,
          keep=ifeval_supported),
)}
ALL_BENCHES = [*BENCHES, "speed"]


# ---------------------------------------------------------------------------------------------------------------
# Datensätze
# ---------------------------------------------------------------------------------------------------------------

BIG = 2000  # ab hier lädt eine Stichprobe nur ausgewählte Seiten statt des ganzen Datensatzes
ROWS_PER_PAGE_PICK = 5  # so viele Fragen je ausgewählter Seite (200 Fragen → 40 Seiten, über alles verteilt)
BACKOFF = (5, 10, 20, 40, 60, 60, 60, 60)  # Wartezeiten bei Rate-Limit/Serverfehler, Sekunden
PAUSE = 0.3  # zwischen zwei Seiten – damit das Rate-Limit seltener greift
REQUEST_TIMEOUT = 30.0


def _retry_after(resp: httpx.Response, default: float) -> float:
    try:
        return max(0.0, min(float(resp.headers.get("Retry-After", "")), 300.0))
    except ValueError:
        return default


async def fetch_page(bench: Bench, client: httpx.AsyncClient, offset: int,
                     say: Callable[[str], None] = print) -> dict:
    """Eine Seite (100 Zeilen) von der Datasets-API – wartet sichtbar bei Rate-Limit, Timeout, Serverfehler."""
    params = {"dataset": bench.dataset, "config": bench.config, "split": bench.split,
              "offset": offset, "length": PAGE}
    attempts = len(BACKOFF)
    err = ""
    for attempt, wait in enumerate(BACKOFF, 1):
        try:
            resp = await client.get(ROWS_API, params=params, timeout=REQUEST_TIMEOUT)
        except httpx.TimeoutException:
            err = T(f"keine Antwort in {int(REQUEST_TIMEOUT)} s", f"no answer within {int(REQUEST_TIMEOUT)} s")
        except httpx.HTTPError as e:
            err = str(e) or type(e).__name__
        else:
            if resp.status_code == 200:
                return resp.json()
            err = f"HTTP {resp.status_code}"
            if resp.status_code not in (429, 500, 502, 503, 504):
                raise BenchError(T(f"Datensatz {bench.dataset} nicht ladbar ({err}: {resp.text[:200]})",
                                   f"Could not load dataset {bench.dataset} ({err}: {resp.text[:200]})"))
            wait = _retry_after(resp, wait)
        if attempt == attempts:
            break
        reason = T("Hugging Face bremst", "Hugging Face is throttling") if "429" in err else T("Problem", "problem")
        say(T(f"  {bench.column}: {reason} ({err}) – warte {wait:g} s … (Versuch {attempt + 1}/{attempts})",
              f"  {bench.column}: {reason} ({err}) – waiting {wait:g} s … (attempt {attempt + 1}/{attempts})"))
        await asyncio.sleep(wait)
    raise BenchError(T(
        f"Datensatz {bench.dataset} nicht ladbar ({err}). Mit HF_TOKEN=… (Hugging-Face-Token) ist das Limit höher; "
        "ein Neustart macht bei der fehlenden Seite weiter.",
        f"Could not load dataset {bench.dataset} ({err}). With HF_TOKEN=… (Hugging Face token) the limit is higher; "
        "a restart continues at the missing page."))


def page_offsets(bench: Bench, total: int, limit: int | None, seed: int) -> list[int]:
    """Welche Seiten gebraucht werden: alle – oder bei großen Datensätzen und Stichprobe zufällige (fester Seed)."""
    pages = max(1, -(-total // PAGE))
    if limit is None or total <= BIG:
        return [i * PAGE for i in range(pages)]
    count = min(pages, max(1, -(-limit // ROWS_PER_PAGE_PICK)))
    return sorted(i * PAGE for i in random.Random(f"{seed}:{bench.key}:pages").sample(range(pages), count))


async def load_rows(bench: Bench, client: httpx.AsyncClient, limit: int | None, seed: int,
                    say: Callable[[str], None] = print) -> dict[int, dict]:
    """Benötigte Zeilen {Zeilennummer: Zeile}. Jede Seite wird sofort gecacht – ein Abbruch verliert nichts."""
    legacy = cache_dir() / f"{bench.key}.json"  # ganzer Datensatz in einer Datei (ältere Läufe, Tests)
    if legacy.exists():
        rows = json.loads(legacy.read_text(encoding="utf-8"))
        wanted = set(page_offsets(bench, len(rows), limit, seed))
        return {i: row for i, row in enumerate(rows) if i - i % PAGE in wanted}
    folder = cache_dir() / bench.key
    meta = _read_json(folder / "meta.json", {})
    total = meta.get("total")
    if total is None:
        data = await fetch_page(bench, client, 0, say)
        total = data.get("num_rows_total") or 0
        _store_page(bench, folder, 0, data)
        _write_json(folder / "meta.json", {"total": total, "dataset": bench.dataset})
    offsets = page_offsets(bench, total, limit, seed)
    missing = [o for o in offsets if not (folder / f"{o}.json").exists()]
    for n, offset in enumerate(missing, 1):
        _store_page(bench, folder, offset, await fetch_page(bench, client, offset, say))
        say(T(f"  {bench.column}: Seite {len(offsets) - len(missing) + n}/{len(offsets)} geladen",
              f"  {bench.column}: page {len(offsets) - len(missing) + n}/{len(offsets)} loaded"))
        await asyncio.sleep(PAUSE)
    out: dict[int, dict] = {}
    for offset in offsets:
        for i, row in enumerate(_read_json(folder / f"{offset}.json", [])):
            out[offset + i] = row
    return out


def _store_page(bench: Bench, folder: Path, offset: int, data: dict) -> None:
    rows = []
    for item in data.get("rows") or []:
        if item.get("truncated_cells"):
            raise BenchError(f"{bench.dataset}: Zeile {item.get('row_idx')} abgeschnitten (truncated_cells)")
        rows.append(item["row"])
    _write_json(folder / f"{offset}.json", rows)


def sample(bench: Bench, rows: dict[int, dict] | list[dict], limit: int | None, seed: int) -> list[tuple[str, dict]]:
    """Feste Stichprobe (gleicher Seed → gleiche Fragen); IDs = Zeilennummer im Datensatz."""
    pairs = sorted(rows.items()) if isinstance(rows, dict) else list(enumerate(rows))
    usable = [(str(i), row) for i, row in pairs if bench.keep(row)]
    if limit is None or limit >= len(usable):
        return usable
    picked = sorted(random.Random(f"{seed}:{bench.key}").sample(range(len(usable)), limit))
    return [usable[i] for i in picked]


# ---------------------------------------------------------------------------------------------------------------
# Modelle
# ---------------------------------------------------------------------------------------------------------------

def make_client(p, llm_cfg, think: bool, greedy: bool, timeout: float):
    """Eigener Client je Profil (unabhängig vom laufenden Orbwise – die Modellwahl dort bleibt unverändert)."""
    sampling = {} if greedy else p.sampling
    temperature = 0.0 if greedy else p.temperature
    if p.backend == "openai":
        prof = p.model_copy(update={"sampling": sampling, "temperature": temperature, "think": think})
        return OpenAICompatLLM(prof, timeout=timeout)
    client = OllamaLLM(llm_cfg.model_copy(update={
        "base_url": p.base_url, "model": p.model, "temperature": temperature, "num_ctx": p.num_ctx,
        "think": think, "request_timeout": timeout}))
    client.sampling = sampling
    return client


async def ask(client, prompt: str, think: bool, max_tokens: int) -> tuple[str, dict]:
    content: list[str] = []
    stats: dict = {}
    async for ev in client.chat_stream([{"role": "user", "content": prompt}], think=think, max_tokens=max_tokens):
        if ev["type"] == "token":
            content.append(ev["text"])
        elif ev["type"] == "done":
            stats = ev.get("stats") or {}
    return strip_think("".join(content)), stats


def speed_prompt() -> str:
    from orbwise.context_test import filler
    # Zufälliger Anfang: nichts darf aus dem Prompt-Cache kommen
    return (f"[{uuid.uuid4()}]\n" + filler(1800)
            + "\n\nIgnore the text above. Write a long, detailed story about a lighthouse keeper.")


async def run_speed(client, runs: int = 3) -> dict:
    prefill, gen, prompt_tokens = [], [], []
    for _ in range(runs):
        _, stats = await ask(client, speed_prompt(), think=False, max_tokens=256)
        if stats.get("prompt_tps"):
            prefill.append(stats["prompt_tps"])
        if stats.get("tps"):
            gen.append(stats["tps"])
        if stats.get("prompt_tokens"):
            prompt_tokens.append(stats["prompt_tokens"])
    return {"prefill_tps": statistics.median(prefill) if prefill else None,
            "gen_tps": statistics.median(gen) if gen else None,
            "prompt_tokens": statistics.median(prompt_tokens) if prompt_tokens else None, "runs": runs}


# ---------------------------------------------------------------------------------------------------------------
# Ergebnisse
# ---------------------------------------------------------------------------------------------------------------

def _write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".part")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(path)


def _read_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


class Progress:
    def __init__(self, label: str, total: int):
        self.label, self.total, self.done, self.ok = label, total, 0, 0
        self.tty = sys.stdout.isatty()

    def update(self, done: int, ok: int, final: bool = False) -> None:
        self.done, self.ok = done, ok
        pct = fmt(100 * ok / done) + " %" if done else "–"
        line = f"  [{self.label}] {done}/{self.total} · {pct}"
        if self.tty:
            print("\r" + line + " " * 8, end="\n" if final else "", flush=True)
        elif final or done % 25 == 0:
            print(line, flush=True)


async def run_bench(client, bench: Bench, items: list[tuple[str, dict]], path: Path, think: bool,
                    max_tokens: int, concurrency: int) -> dict:
    data = _read_json(path, {"bench": bench.key, "items": {}})
    done = data["items"]
    todo = [(i, row) for i, row in items if i not in done]
    wanted = {i for i, _ in items}
    prog = Progress(bench.column, len(items))

    def counts() -> tuple[int, int]:
        mine = [v for k, v in done.items() if k in wanted]
        return len(mine), sum(1 for v in mine if v.get("ok"))

    prog.update(*counts())
    sem = asyncio.Semaphore(max(1, concurrency))
    lock = asyncio.Lock()
    errors_in_row = 0

    async def one(item_id: str, row: dict) -> None:
        nonlocal errors_in_row
        async with sem:
            if errors_in_row >= 5:
                return
            started = time.monotonic()
            entry: dict = {}
            try:
                answer, stats = await ask(client, bench.prompt(row), think, max_tokens)
                errors_in_row = 0
                ok, extracted = await asyncio.to_thread(bench.grade, row, answer)
                entry = {"ok": bool(ok), "extracted": extracted, "answer": answer,
                         "tokens": stats.get("tokens"), "seconds": round(time.monotonic() - started, 1)}
            except LLMError as e:
                errors_in_row += 1
                entry = {"ok": False, "error": str(e)[:500], "seconds": round(time.monotonic() - started, 1)}
            async with lock:
                done[item_id] = entry
                n, ok_n = counts()
                prog.update(n, ok_n)
                if n % 10 == 0:
                    _write_json(path, data)

    await asyncio.gather(*(one(i, row) for i, row in todo))
    _write_json(path, data)
    n, ok_n = counts()
    prog.update(n, ok_n, final=True)
    if errors_in_row >= 5:
        raise LLMError(T("5 Fehler in Folge – Modell-Server antwortet nicht mehr.",
                         "5 errors in a row – the model server stopped answering."))
    return {"n": n, "ok": ok_n}


def fmt(x: float | None, digits: int = 1) -> str:
    if x is None:
        return "–"
    text = f"{x:.{digits}f}"
    return T(text.replace(".", ","), text)


def collect(run_dir: Path) -> dict:
    """Alle Ergebnisse eines Laufs: {profil: {"label", "scores": {bench: (prozent, n)}, "speed": {...}}}."""
    meta = _read_json(run_dir / "meta.json", {})
    out = {}
    for pdir in sorted(p for p in run_dir.iterdir() if p.is_dir()):
        info = (meta.get("profiles") or {}).get(pdir.name, {})
        scores = {}
        for key in BENCHES:
            data = _read_json(pdir / f"{key}.json", None)
            if not data or not data.get("items"):
                continue
            items = data["items"].values()
            scores[key] = (100 * sum(1 for v in items if v.get("ok")) / len(items), len(data["items"]))
        speed = _read_json(pdir / "speed.json", None)
        if scores or speed:
            out[pdir.name] = {"label": info.get("label") or pdir.name, "model": info.get("model", ""),
                              "scores": scores, "speed": speed}
    return out


def render_table(results: dict, meta: dict) -> str:
    benches = [k for k in BENCHES if any(k in r["scores"] for r in results.values())]
    has_speed = any(r["speed"] for r in results.values())
    header = [T("Modell", "Model"), *(BENCHES[k].column for k in benches)]
    if len(benches) > 1:
        header.append("Ø")
    if has_speed:
        header += ["Prefill tok/s", "Gen tok/s"]
    rows = []
    for r in results.values():
        vals = [r["scores"][k][0] if k in r["scores"] else None for k in benches]
        if len(benches) > 1:
            present = [v for v in vals if v is not None]
            vals.append(sum(present) / len(present) if len(present) == len(benches) else None)
        if has_speed:
            s = r["speed"] or {}
            vals += [s.get("prefill_tps"), s.get("gen_tps")]
        rows.append((r["label"], vals))
    cols = len(header) - 1
    best = [max((v[i] for _, v in rows if v[i] is not None), default=None) for i in range(cols)]
    speed_from = cols - 2 if has_speed else cols

    def cell(i: int, v):
        if v is None:
            return "–"
        text = fmt(v, 0 if i >= speed_from and v >= 100 else 1)
        return f"**{text}**" if len(rows) > 1 and v == best[i] else text

    lines = ["| " + " | ".join(header) + " |", "|" + "|".join([":---"] + ["---:"] * cols) + "|"]
    for label, vals in rows:
        lines.append("| " + " | ".join([label] + [cell(i, v) for i, v in enumerate(vals)]) + " |")
    sizes = sorted({n for r in results.values() for _, n in r["scores"].values()})
    limit = meta.get("limit")
    note = [T("Stichprobe", "Sample") + ": " + (T(f"bis {limit} je Benchmark", f"up to {limit} per benchmark")
                                               if limit else T("vollständig", "full"))
            + (f" (n = {', '.join(map(str, sizes))})" if sizes else ""),
            f"Seed {meta.get('seed', '?')}",
            T("Denkmodus an", "thinking on") if meta.get("think") else T("Denkmodus aus", "thinking off"),
            T("Sampling: greedy (Temperatur 0)", "sampling: greedy (temperature 0)") if meta.get("greedy")
            else T("Sampling: Profil", "sampling: profile")]
    return ("\n".join(lines) + "\n\n" + " · ".join(note) + "\n"
            + T("Angaben in % richtig (HumanEval: pass@1, IFEval: strict prompt-level); 0-shot – Stichproben sind "
                "untereinander vergleichbar, aber nicht identisch mit den offiziellen Zahlen.",
                "Scores in % correct (HumanEval: pass@1, IFEval: strict prompt-level); 0-shot – samples are "
                "comparable with each other, not identical to official numbers.") + "\n")


def write_reports(run_dir: Path) -> str:
    meta = _read_json(run_dir / "meta.json", {})
    results = collect(run_dir)
    if not results:
        return T("Noch keine Ergebnisse.", "No results yet.")
    table = render_table(results, meta)
    started = meta.get("started", "")
    (run_dir / "results.md").write_text(f"# LLM-Benchmark {started}\n\n" + table, encoding="utf-8")
    _write_json(run_dir / "results.json", {"meta": meta, "results": {
        name: {"label": r["label"], "model": r["model"],
               "scores": {k: {"percent": round(v[0], 2), "n": v[1]} for k, v in r["scores"].items()},
               "speed": r["speed"]} for name, r in results.items()}})
    with (run_dir / "results.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["profile", "model", *(BENCHES[k].column for k in BENCHES), "prefill_tps", "gen_tps"])
        for name, r in results.items():
            s = r["speed"] or {}
            w.writerow([name, r["model"], *(round(r["scores"][k][0], 2) if k in r["scores"] else "" for k in BENCHES),
                        s.get("prefill_tps") or "", s.get("gen_tps") or ""])
    return table


# ---------------------------------------------------------------------------------------------------------------
# Ablauf
# ---------------------------------------------------------------------------------------------------------------

def load_profiles(cfg) -> dict:
    """Profile genau wie in Orbwise (inkl. heruntergeladener Modelle und in der Oberfläche gesetzter Werte)."""
    from orbwise.llm_router import LLMRouter
    return LLMRouter(cfg.llm, state_path=cfg.memory.dir.parent / "state.json").profiles


def orbwise_running(port: int) -> bool:
    from orbwise.llm_router import listening_pids
    return bool(listening_pids(port))


async def unload_ollama(llm_cfg) -> None:
    client = OllamaLLM(llm_cfg)
    try:
        await client.unload_all()
    except (LLMError, httpx.HTTPError, OSError):
        pass
    finally:
        await client.close()


async def run_profile(name: str, p, llm_cfg, items: dict, run_dir: Path, args) -> None:
    from orbwise.llm_router import ManagedServer

    async def say(msg: str) -> None:
        print("  " + msg, flush=True)

    print(f"\n▶ {p.label} ({p.backend} · {p.model})", flush=True)
    await unload_ollama(llm_cfg)  # Grafikspeicher für dieses Modell freimachen
    server = ManagedServer(p.server, p.base_url) if p.server else None
    timeout = max(llm_cfg.request_timeout, 1800.0 if args.think else 600.0)
    client = make_client(p, llm_cfg, args.think, args.greedy, timeout)
    try:
        if server:
            await server.stop_foreign()
            await server.ensure_running(say)
        elif p.backend == "ollama":
            await client.preload()
        for key in args.bench:
            if key == "speed":
                print("  [Speed] …", flush=True)
                speed = await run_speed(client)
                _write_json(run_dir / name / "speed.json", speed)
                print(f"  [Speed] Prefill {fmt(speed['prefill_tps'], 0)} tok/s · "
                      f"Gen {fmt(speed['gen_tps'])} tok/s", flush=True)
                continue
            await run_bench(client, BENCHES[key], items[key], run_dir / name / f"{key}.json", args.think,
                            args.max_tokens, args.concurrency)
    finally:
        await client.close()
        if server:
            await server.stop()
        if p.backend == "ollama":
            await unload_ollama(llm_cfg)


def latest_run() -> Path | None:
    runs = sorted(p for p in RUNS_DIR.glob("*") if (p / "meta.json").exists()) if RUNS_DIR.exists() else []
    return runs[-1] if runs else None


async def main_async(args) -> int:
    cfg = load_config(skills=False)
    set_lang(cfg.language)
    profiles = load_profiles(cfg)

    if args.list:
        print(T("Profile:", "Profiles:"))
        for name, p in profiles.items():
            extra = T(" · startet llama-server", " · starts llama-server") if p.server else ""
            print(f"  {name:<20} {p.backend:<7} {p.model}{extra}")
        print(T("\nBenchmarks:", "\nBenchmarks:"))
        for b in BENCHES.values():
            print(f"  {b.key:<10} {b.column:<10} {b.dataset}")
        print("  speed      Speed      Prefill/Gen tok/s")
        return 0

    if args.table:
        print(write_reports(Path(args.table).expanduser()))
        return 0

    if args.resume:
        run_dir = latest_run() if args.resume is True else Path(args.resume).expanduser()
        if not run_dir or not (run_dir / "meta.json").exists():
            print(T("Kein Lauf zum Fortsetzen gefunden.", "No run to resume."), file=sys.stderr)
            return 1
        meta = _read_json(run_dir / "meta.json", {})
        for key in ("limit", "seed", "think", "greedy", "max_tokens", "bench"):  # gleiche Einstellungen wie damals
            if key in meta:
                setattr(args, key, meta[key])
        if not args.profiles:
            args.profiles = list(meta.get("profiles") or [])
    else:
        run_dir = Path(args.out).expanduser() if args.out else RUNS_DIR / datetime.now().strftime("%Y-%m-%d_%H%M%S")
        meta = {}

    names = args.profiles or list(profiles)
    unknown = [n for n in names if n not in profiles]
    if unknown:
        print(T("Unbekannte Profile: ", "Unknown profiles: ") + ", ".join(unknown)
              + T(" – vorhanden: ", " – available: ") + ", ".join(profiles), file=sys.stderr)
        return 1
    if args.no_exec and "humaneval" in args.bench:
        args.bench = [b for b in args.bench if b != "humaneval"]

    if orbwise_running(cfg.port):
        print(T("⚠ Orbwise läuft gerade. Ein vom Benchmark gestarteter llama-server beendet dessen Modell-Server, und "
                "beide teilen sich den Grafikspeicher – am besten vorher `orbwise stop`.",
                "⚠ Orbwise is running. A llama-server started by the benchmark stops Orbwise's model server, and both "
                "share the GPU memory – best run `orbwise stop` first."), flush=True)

    print(T("Datensätze …", "Datasets …"), flush=True)
    items: dict[str, list] = {}
    headers = {"Authorization": f"Bearer {os.environ['HF_TOKEN']}"} if os.environ.get("HF_TOKEN") else {}
    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT, headers=headers, follow_redirects=True) as http:
        for key in args.bench:
            if key == "speed":
                continue
            bench = BENCHES[key]
            items[key] = sample(bench, await load_rows(bench, http, args.limit, args.seed), args.limit, args.seed)
            print(f"  {bench.column}: {len(items[key])} " + T("Fragen", "questions"), flush=True)

    meta.update({"started": meta.get("started") or datetime.now().isoformat(timespec="seconds"),
                 "limit": args.limit, "seed": args.seed, "think": args.think, "greedy": args.greedy,
                 "max_tokens": args.max_tokens, "bench": args.bench})
    meta.setdefault("profiles", {})
    for n in names:
        meta["profiles"][n] = {"label": profiles[n].label, "model": profiles[n].model,
                               "backend": profiles[n].backend}
    _write_json(run_dir / "meta.json", meta)
    print(T("Ergebnisse: ", "Results: ") + str(run_dir), flush=True)

    failed = []
    for n in names:
        try:
            await run_profile(n, profiles[n], cfg.llm, items, run_dir, args)
        except LLMError as e:
            failed.append(n)
            print(T(f"  ✗ {n} abgebrochen: {e}", f"  ✗ {n} aborted: {e}"), flush=True)

    print("\n" + write_reports(run_dir))
    print(T("Gespeichert: ", "Saved: ") + str(run_dir / "results.md"))
    if failed:
        print(T("Fortsetzen: ", "Resume: ") + f"uv run python scripts/llm_bench.py --resume {run_dir}")
    return 1 if failed else 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="LLM-Benchmark für Orbwise-Modellprofile (MMLU-Pro, GSM8K, HumanEval, "
                                             "ARC-C, IFEval, Geschwindigkeit)")
    ap.add_argument("--list", action="store_true", help="Profile und Benchmarks anzeigen")
    ap.add_argument("--profiles", type=lambda s: [x.strip() for x in s.split(",") if x.strip()],
                    help="Profile, durch Komma getrennt (Standard: alle)")
    ap.add_argument("--bench", type=lambda s: [x.strip() for x in s.split(",") if x.strip()], default=ALL_BENCHES,
                    help=f"Benchmarks (Standard: {','.join(ALL_BENCHES)})")
    ap.add_argument("--limit", type=int, default=200, help="Fragen je Benchmark (Standard 200)")
    ap.add_argument("--full", action="store_true", help="kompletter Datensatz statt Stichprobe")
    ap.add_argument("--seed", type=int, default=42, help="Seed der Stichprobe (Standard 42)")
    ap.add_argument("--think", action="store_true", help="Denkmodus einschalten (Standard: aus)")
    ap.add_argument("--sampling", choices=("auto", "greedy", "profile"), default="auto",
                    help="greedy = Temperatur 0; profile = Sampling-Werte des Profils; auto = greedy ohne, "
                         "Profil mit Denkmodus")
    ap.add_argument("--max-tokens", type=int, default=None,
                    help="Antwortlänge (Standard 2048, mit --think 16384)")
    ap.add_argument("--concurrency", type=int, default=1,
                    help="parallele Anfragen (llama-server braucht dafür mehrere Slots, -np)")
    ap.add_argument("--out", help="Ergebnisordner (Standard ~/.local/share/orbwise/bench/<zeit>)")
    ap.add_argument("--resume", nargs="?", const=True, default=None, metavar="ORDNER",
                    help="Lauf fortsetzen (ohne Ordner: der letzte)")
    ap.add_argument("--table", metavar="ORDNER", help="nur die Tabelle eines Laufs ausgeben")
    ap.add_argument("--no-exec", action="store_true", help="HumanEval überspringen (führt sonst Modellcode aus)")
    args = ap.parse_args(argv)
    bad = [b for b in args.bench if b not in ALL_BENCHES]
    if bad:
        ap.error(f"unbekannte Benchmarks: {', '.join(bad)} (vorhanden: {', '.join(ALL_BENCHES)})")
    if args.full:
        args.limit = None
    args.greedy = args.sampling == "greedy" or (args.sampling == "auto" and not args.think)
    if args.max_tokens is None:
        args.max_tokens = 16384 if args.think else 2048
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        return asyncio.run(main_async(args))
    except BenchError as e:
        print(f"✗ {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print(T("\nAbgebrochen – fortsetzen mit --resume", "\nInterrupted – continue with --resume"), file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
