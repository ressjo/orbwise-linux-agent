"""scripts/llm_bench.py: Antwort-Auswertung, HumanEval-Ausführung, IFEval-Prüfer, Tabelle und ein ganzer Lauf gegen
einen Test-llama-server (ohne Netz: die Datensätze liegen schon im Cache)."""

import asyncio
import importlib.util
import json
import socket
import sys
from pathlib import Path

import httpx
import pytest

HERE = Path(__file__).parent
_spec = importlib.util.spec_from_file_location("llm_bench", HERE.parent / "scripts" / "llm_bench.py")
bench = importlib.util.module_from_spec(_spec)
sys.modules["llm_bench"] = bench
_spec.loader.exec_module(bench)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# ---------------------------------------------------------------- Auswertung

@pytest.mark.parametrize("text, want", [
    ("Let me think... The answer is (C).", "C"),
    ("so the answer is **B**", "B"),
    ("Answer: (J)", "J"),
    ("The answer is A. Wait, no – the answer is (D)", "D"),
    ("\\boxed{E}", "E"),
    ("B", "B"),
    ("I am not sure.", None),
])
def test_extract_choice(text, want):
    assert bench.extract_choice(text, bench._letters(10)) == want


def test_extract_choice_numeric_labels():
    assert bench.extract_choice("The answer is (3)", ["1", "2", "3", "4"]) == "3"


@pytest.mark.parametrize("text, want", [
    ("5 + 7 = 12\nThe answer is 12", 12),
    ("The answer is $1,250.", 1250),
    ("so we get \\boxed{-3.5}", -3.5),
    ("first 3, then 4, finally 18", 18),
    ("no numbers", None),
])
def test_extract_number(text, want):
    assert bench.extract_number(text) == want


def test_mmlu_and_gsm8k_grade():
    row = {"question": "Q?", "options": ["x", "y", "z"], "answer": "B", "category": "math"}
    assert "Options:\nA. x\nB. y\nC. z" in bench.mmlu_prompt(row)
    assert bench.mmlu_grade(row, "The answer is (B)") == (True, "B")
    assert bench.mmlu_grade(row, "The answer is (C)") == (False, "C")
    gsm = {"question": "?", "answer": "steps\n#### 1,000"}
    assert bench.gsm8k_grade(gsm, "The answer is 1000")[0]
    assert not bench.gsm8k_grade(gsm, "The answer is 999")[0]


HUMANEVAL_ROW = {
    "task_id": "HumanEval/0", "entry_point": "add",
    "prompt": "from typing import List\n\n\ndef add(xs: List[int]) -> int:\n    \"\"\"Sum of xs.\"\"\"\n",
    "test": "def check(candidate):\n    assert candidate([1, 2, 3]) == 6\n    assert candidate([]) == 0\n",
}


def test_humaneval_full_function_passes():
    answer = "Here you go:\n```python\ndef add(xs: List[int]) -> int:\n    return sum(xs)\n```"
    assert bench.humaneval_grade(HUMANEVAL_ROW, answer) == (True, "")


def test_humaneval_body_only_passes():
    assert bench.humaneval_grade(HUMANEVAL_ROW, "```python\n    return sum(xs)\n```")[0]


def test_humaneval_wrong_and_endless_fail():
    ok, err = bench.humaneval_grade(HUMANEVAL_ROW, "```python\ndef add(xs):\n    return 0\n```")
    assert not ok and "AssertionError" in err
    ok, err = bench.run_python("while True:\n    pass\n", timeout=1)
    assert not ok and err == "timeout"


def test_humaneval_runs_in_empty_dir():
    ok, _ = bench.run_python("import os\nassert os.listdir('.') == ['prog.py']\n")
    assert ok


# ---------------------------------------------------------------- IFEval

def _ifeval(ids, kwargs):
    return {"prompt": "p", "instruction_id_list": ids, "kwargs": kwargs}


@pytest.mark.parametrize("iid, kw, good, bad", [
    ("punctuation:no_comma", {}, "no commas here", "a, b"),
    ("change_case:english_lowercase", {}, "all lower", "Not lower"),
    ("change_case:english_capital", {}, "ALL CAPS", "Mixed Case"),
    ("length_constraints:number_words", {"num_words": 3, "relation": "less than"}, "one two", "one two three"),
    ("length_constraints:number_words", {"num_words": 3, "relation": "at least"}, "one two three", "one two"),
    ("length_constraints:number_paragraphs", {"num_paragraphs": 2}, "a\n***\nb", "a\n***\nb\n***\nc"),
    ("keywords:existence", {"keywords": ["cat", "dog"]}, "Cat and dog", "only a cat"),
    ("keywords:forbidden_words", {"forbidden_words": ["bad"]}, "badge is fine", "this is bad"),
    ("keywords:frequency", {"keyword": "ok", "frequency": 2, "relation": "at least"}, "ok ok", "ok"),
    ("keywords:letter_frequency", {"letter": "z", "let_frequency": 2, "let_relation": "less than"}, "z", "zz"),
    ("detectable_format:json_format", {}, '```json\n{"a": 1}\n```', "{a: 1}"),
    ("detectable_format:title", {}, "<<My Title>>\ntext", "My Title"),
    ("detectable_format:number_bullet_lists", {"num_bullets": 2}, "* a\n* b", "* a\n* b\n- c"),
    ("detectable_format:number_highlighted_sections", {"num_highlights": 2}, "*one* and *two*", "*one*"),
    ("detectable_format:multiple_sections", {"section_spliter": "Section", "num_sections": 2},
     "Section 1\na\nSection 2\nb", "Section 1\na"),
    ("detectable_format:constrained_response", {}, "My answer is yes.", "Yes!"),
    ("detectable_content:number_placeholders", {"num_placeholders": 2}, "[name] at [address]", "[name]"),
    ("detectable_content:postscript", {"postscript_marker": "P.S."}, "Hi\nP.S. bye", "Hi bye"),
    ("startend:end_checker", {"end_phrase": "Any other questions?"}, "Text. Any other questions?", "Text."),
    ("startend:quotation", {}, '"quoted"', "not quoted"),
    ("combination:repeat_prompt", {"prompt_to_repeat": "Say hi"}, "Say hi. Hi!", "Hi!"),
    ("combination:two_responses", {}, "one\n******\ntwo", "one\n******\none"),
    ("length_constraints:nth_paragraph_first_word",
     {"num_paragraphs": 2, "nth_paragraph": 2, "first_word": "then"}, "First.\n\nThen more.", "First.\n\nAnd more."),
    ("change_case:capital_word_frequency", {"capital_frequency": 2, "capital_relation": "at least"},
     "VERY BIG news", "VERY small news"),
])
def test_ifeval_checks(iid, kw, good, bad):
    full = {**kw, "unused": None}  # der Datensatz hat alle Schlüssel, unbenutzte sind None
    row = _ifeval([iid], [full])
    assert bench.ifeval_grade(row, good)[0], good
    ok, failed = bench.ifeval_grade(row, bad)
    assert not ok and failed == [iid], bad


def test_ifeval_unsupported_is_skipped():
    assert bench.ifeval_supported(_ifeval(["punctuation:no_comma"], [{}]))
    assert not bench.ifeval_supported(_ifeval(["gibt:es_nicht"], [{}]))


# ---------------------------------------------------------------- Daten

def test_sample_is_stable_and_respects_keep():
    b = bench.BENCHES["gsm8k"]
    rows = [{"question": str(i), "answer": "#### 1"} for i in range(500)]
    first = bench.sample(b, rows, 20, seed=1)
    assert first == bench.sample(b, rows, 20, seed=1)
    assert first != bench.sample(b, rows, 20, seed=2)
    assert len(first) == 20 and len(bench.sample(b, rows, None, seed=1)) == 500
    ifeval = bench.BENCHES["ifeval"]
    mixed = [_ifeval(["punctuation:no_comma"], [{}]), _ifeval(["gibt:es_nicht"], [{}])]
    assert [i for i, _ in bench.sample(ifeval, mixed, None, 0)] == ["0"]


def _rows_api(total: int, calls: list, fail: dict | None = None):
    """Test-Datasets-API: fail = {offset: [status, …]} – diese Antworten kommen vor dem Erfolg."""
    fail = {k: list(v) for k, v in (fail or {}).items()}

    def handler(request: httpx.Request):
        offset = int(request.url.params["offset"])
        calls.append(offset)
        if fail.get(offset):
            return httpx.Response(fail[offset].pop(0), headers={"Retry-After": "0"}, text="slow down")
        rows = [{"row_idx": i, "row": {"question": f"q{i}", "answer": "#### 1"}, "truncated_cells": []}
                for i in range(offset, min(offset + 100, total))]
        return httpx.Response(200, json={"rows": rows, "num_rows_total": total})
    return httpx.MockTransport(handler)


def _load(transport, limit=None, seed=0, say=lambda _: None):
    async def scenario():
        async with httpx.AsyncClient(transport=transport) as c:
            return await bench.load_rows(bench.BENCHES["gsm8k"], c, limit, seed, say)
    return asyncio.run(scenario())


@pytest.fixture
def fast(monkeypatch):
    monkeypatch.setattr(bench, "BACKOFF", (0, 0, 0))
    monkeypatch.setattr(bench, "PAUSE", 0)


def test_load_rows_pages_and_caches(fast):
    calls = []
    first = _load(_rows_api(250, calls))
    assert sorted(first) == list(range(250)) and calls == [0, 100, 200]
    assert _load(_rows_api(250, calls)) == first and calls == [0, 100, 200]  # zweites Mal aus dem Cache


def test_load_rows_waits_visibly_on_rate_limit(fast):
    calls, said = [], []
    rows = _load(_rows_api(250, calls, fail={100: [429, 503]}), say=said.append)
    assert len(rows) == 250 and calls == [0, 100, 100, 100, 200]
    assert any("429" in s and "warte" in s and "2/3" in s for s in said)


def test_load_rows_resumes_after_failure(fast):
    calls = []
    with pytest.raises(bench.BenchError, match="HF_TOKEN"):
        _load(_rows_api(450, calls, fail={300: [429, 429, 429]}))
    calls.clear()
    assert len(_load(_rows_api(450, calls))) == 450
    assert calls == [300, 400]  # die Seiten davor kamen aus dem Cache


def test_big_dataset_loads_only_sampled_pages(fast):
    calls = []
    rows = _load(_rows_api(12_000, calls), limit=20, seed=7)
    pages = sorted({i - i % 100 for i in rows})
    assert len(pages) == 4 and calls == [0, *[p for p in pages if p]]  # Seite 0 liefert die Gesamtzahl
    picked = bench.sample(bench.BENCHES["gsm8k"], rows, 20, 7)
    assert len(picked) == 20 and picked == bench.sample(bench.BENCHES["gsm8k"], _load(_rows_api(12_000, []), limit=20, seed=7), 20, 7)
    assert len(_load(_rows_api(12_000, []), limit=None)) == 12_000  # --full: alles


# ---------------------------------------------------------------- Ablauf und Tabelle

class StubClient:
    """Antwortet bei GSM8K richtig, wenn die Frage gerade ist."""

    async def chat_stream(self, messages, think=None, max_tokens=None):
        q = messages[-1]["content"].rsplit("Question: ", 1)[-1]
        yield {"type": "token", "text": "<think>hm</think>The answer is " + ("4" if int(q) % 2 == 0 else "5")}
        yield {"type": "done", "message": {}, "stats": {"tokens": 5, "tps": 50.0}}


def test_run_bench_scores_and_resumes(tmp_path):
    b = bench.BENCHES["gsm8k"]
    items = [(str(i), {"question": str(i), "answer": "#### 4"}) for i in range(10)]
    path = tmp_path / "p" / "gsm8k.json"
    assert asyncio.run(bench.run_bench(StubClient(), b, items, path, False, 100, 3)) == {"n": 10, "ok": 5}
    saved = json.loads(path.read_text())["items"]
    assert saved["0"]["answer"] == "The answer is 4"  # Denkblock entfernt

    class Boom:
        async def chat_stream(self, *a, **k):
            raise AssertionError("darf nicht erneut fragen")
            yield

    assert asyncio.run(bench.run_bench(Boom(), b, items, path, False, 100, 1)) == {"n": 10, "ok": 5}


def test_render_table_marks_best():
    results = {
        "a": {"label": "Modell A", "model": "a", "scores": {"gsm8k": (90.0, 10), "arc": (80.0, 10)},
              "speed": {"prefill_tps": 1850.0, "gen_tps": 42.7}},
        "b": {"label": "Modell B", "model": "b", "scores": {"gsm8k": (70.0, 10), "arc": (85.0, 10)},
              "speed": {"prefill_tps": 900.0, "gen_tps": 60.1}},
    }
    table = bench.render_table(results, {"limit": 10, "seed": 42, "greedy": True})
    lines = table.splitlines()
    assert lines[0] == "| Modell | GSM8K | ARC-C | Ø | Prefill tok/s | Gen tok/s |"
    assert lines[2] == "| Modell A | **90,0** | 80,0 | **85,0** | **1850** | 42,7 |"
    assert lines[3] == "| Modell B | 70,0 | **85,0** | 77,5 | 900 | **60,1** |"
    assert "Seed 42" in table


def test_whole_run_against_test_server(tmp_path, monkeypatch, capsys):
    """Ein Profil mit eigenem (Test-)llama-server: starten, alle Benchmarks, stoppen, Berichte schreiben."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    cache = bench.cache_dir()
    cache.mkdir(parents=True)
    data = {
        "mmlu_pro": [{"question": "q", "options": ["a", "b"], "answer": "A", "category": "x"}] * 3,
        "gsm8k": [{"question": "q", "answer": "#### 1"}] * 3,
        "humaneval": [HUMANEVAL_ROW],
        "arc": [{"question": "q", "choices": {"label": ["A", "B"], "text": ["a", "b"]}, "answerKey": "A"}] * 3,
        "ifeval": [_ifeval(["punctuation:no_comma"], [{}])] * 3,
    }
    for key, rows in data.items():
        (cache / f"{key}.json").write_text(json.dumps(rows))
    port = free_port()
    config = tmp_path / "config.yaml"
    config.write_text(json.dumps({
        "port": free_port(),
        "memory": {"dir": str(tmp_path / "data" / "memory")},
        "llm": {"base_url": f"http://127.0.0.1:{free_port()}",  # kein Ollama
                "profiles": {"fake": {"backend": "openai", "label": "Fake Llama",
                                      "base_url": f"http://127.0.0.1:{port}/v1",
                                      "server": {"command": f"{sys.executable} {HERE / 'fake_llama_server.py'} {port}",
                                                 "startup_timeout": 20}}}},
    }))
    monkeypatch.setenv("ORBWISE_CONFIG", str(config))
    out = tmp_path / "run"
    assert bench.main(["--limit", "2", "--out", str(out)]) == 0
    printed = capsys.readouterr().out
    assert "| Fake Llama |" in printed
    results = json.loads((out / "results.json").read_text())["results"]["fake"]
    assert results["scores"]["gsm8k"] == {"percent": 0.0, "n": 2}  # der Test-Server antwortet nur „Llama meldet …“
    assert results["scores"]["ifeval"]["percent"] == 100.0  # … ohne Komma
    assert results["speed"]["gen_tps"] == 19.2 and results["speed"]["prefill_tps"] == 310.5
    assert (out / "results.csv").read_text().startswith("profile,model,MMLU-Pro")
    assert (out / "results.md").exists()
    with pytest.raises(httpx.HTTPError):  # Server wieder beendet
        httpx.get(f"http://127.0.0.1:{port}/health", timeout=2)
    # Tabelle erneut ausgeben, ohne etwas zu rechnen
    assert bench.main(["--table", str(out)]) == 0
    assert "| Fake Llama |" in capsys.readouterr().out
