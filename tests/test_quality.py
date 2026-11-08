"""Qualität messen mit llama-perplexity: Ausgabe lesen, Referenz und Varianten wählen, ganzer Mess-Job mit einem
nachgebauten llama-perplexity (gibt aus, was llama.cpp ausgibt)."""

import asyncio
import json
import sys

import httpx
import pytest

from orbwise import llamacpp_quality as q
from orbwise import llm_router as router_mod
from orbwise.config import LLMConfig, ProfileConfig, ServerConfig
from orbwise.llm_router import LLMRouter

BASE_OUT = """perplexity: calculating perplexity over 12 chunks, n_ctx=512, batch_size=2048, n_seq=4
[1]4.1234,[2]4.5678,[3]4.4321,[4]4.3210,
Final estimate: PPL = 4.3210 +/- 0.08765
"""

KLD_OUT = """[1]4.1500,[2]4.5900,
====== Perplexity statistics ======
Mean PPL(Q)                   :   4.352100 ±   0.088123
Mean PPL(base)                :   4.321000 ±   0.087650
Cor(ln(PPL(Q)), ln(PPL(base))):  99.81%
Mean ln(PPL(Q)/PPL(base))     :   0.007170 ±   0.001020

====== KL divergence statistics ======
Mean    KLD:   0.006543 ±   0.000210
Maximum KLD:   3.210000
99.9%   KLD:   0.912345
Median  KLD:   0.001234

====== Token probability statistics ======
Mean    Δp: -0.045 ± 0.012 %
Same top p: 98.765 ± 0.123 %
"""


def test_parse_base_and_comparison_output():
    assert q.parse_perplexity(BASE_OUT) == {"ppl": 4.321}
    assert q.parse_perplexity(KLD_OUT) == {"ppl": 4.3521, "kld": 0.006543, "same_top": 98.765}
    assert q.parse_perplexity("error: failed to load model") is None


def _models(folder, *names):
    folder.mkdir(parents=True, exist_ok=True)
    for n in names:
        (folder / n).write_bytes(b"GGUF" + b"\0" * 64)
    return [folder / n for n in names]


def test_reference_is_the_most_precise_quant_of_the_same_model(tmp_path):
    q4, q8, _other, _mm = _models(tmp_path / "m", "Model-Q4_K_M.gguf", "Model-Q8_0.gguf", "Other-Q2_K.gguf",
                                  "mmproj-F16.gguf")
    assert q.precision("Q8_0") > q.precision("UD-Q4_K_XL") > q.precision("Q4_K_M") > q.precision("Q4_0")
    ref, todo = q.variants(q4, "q8_0", ["f16", "q8_0", "q4_0"])
    assert ref["file"] == q8 and ref["kv"] == "f16"
    labels = [(v["label"], v["kind"], v["current"]) for v in todo]
    assert labels == [("Q8_0 · KV f16", "ref", False), ("Q4_K_M · KV f16", "kv", False),
                      ("Q4_K_M · KV q8", "kv", True), ("Q4_K_M · KV q4", "kv", False)]
    # nur ein Quant vorhanden: Referenz ist das Modell selbst mit f16 – gemessen werden nur die KV-Stufen
    solo, = _models(tmp_path / "s", "Solo-Q6_K.gguf")
    ref, todo = q.variants(solo, "f16", ["f16", "q8_0"])
    assert ref["file"] == solo and [v["label"] for v in todo] == ["Q6_K · KV q8"]


def test_flags_follow_the_profile():
    flags = q.keep_flags("llama-server -m x.gguf -ngl 99 -c 32768 --n-cpu-moe 6 -ts 60,40 -fa on --port 8091")
    assert flags == ["-ngl", "99", "--n-cpu-moe", "6", "-ts", "60,40", "-fa", "on"]
    assert q.keep_flags("llama-server -m x.gguf -c 8192")[:2] == ["-ngl", "99"]
    args = q.perplexity_args(q.Path("/b/llama-perplexity"), q.Path("m.gguf"), q.Path("t.txt"), "q8_0",
                             ["-ngl", "99"], q.Path("k.kld"), base=False)
    assert args[-2:] == ["k.kld", "--kl-divergence"] and "-ctk" in args and ["-fa", "on"] == args[args.index("-fa"):args.index("-fa") + 2]
    base = q.perplexity_args(q.Path("p"), q.Path("m.gguf"), q.Path("t.txt"), "f16", [], q.Path("k.kld"), base=True)
    assert "--kl-divergence" not in base and "-ctk" not in base and "--chunks" in base


FAKE = r'''#!{python}
import sys
args = sys.argv[1:]
open("{log}", "a").write(" ".join(args) + "\n")
kld = args[args.index("--kl-divergence-base") + 1]
model = args[args.index("-m") + 1]
if "Q5_K_M" in model:
    sys.stderr.write("error: failed to allocate buffer (out of memory)\n"); sys.exit(1)
if "--kl-divergence" not in args:
    open(kld, "wb").write(b"logits")
    print({base!r})
else:
    assert open(kld, "rb").read() == b"logits"
    print({kld_out!r})
'''


def test_measure_job_with_fake_binary(tmp_path, cfg, monkeypatch):
    monkeypatch.setattr(router_mod, "_help_text", lambda command, env: "")
    folder = q.llamacpp.root(cfg) / "current"
    folder.mkdir(parents=True)
    log = tmp_path / "calls.log"
    exe = folder / "llama-perplexity"
    exe.write_text(FAKE.format(python=sys.executable, log=log, base=BASE_OUT, kld_out=KLD_OUT))
    exe.chmod(0o755)
    q4, _q8, _q5 = _models(tmp_path / "models", "Model-Q4_K_M.gguf", "Model-Q8_0.gguf", "Model-Q5_K_M.gguf")
    state = cfg.memory.dir.parent / "state.json"
    state.parent.mkdir(parents=True, exist_ok=True)
    llm = LLMConfig(base_url="http://127.0.0.1:9", profiles={"m": ProfileConfig(
        backend="openai", base_url="http://127.0.0.1:8099/v1", model="m", num_ctx=8192,
        server=ServerConfig(command=f"{q.llamacpp.binary(cfg)} -m {q4} -ngl 99 -c 8192 --n-cpu-moe 2"))}, active="m")
    router = LLMRouter(llm, state_path=state)
    router.unloaded = True  # nichts zu entladen/neu zu laden
    text = tmp_path / "text.txt"
    text.write_text("Ein fester Text. " * 100)
    said = []

    async def say(t):
        said.append(t)

    async def corpus():
        return text

    result = asyncio.run(q.measure(cfg, router, "m", say, corpus=corpus))
    assert result["reference"] == "Q8_0 · KV f16" and result["reference_ppl"] == 4.321
    rows = {r["label"]: r for r in result["variants"]}
    assert rows["Q8_0 · KV f16"]["same_top"] == 100.0 and rows["Q8_0 · KV f16"]["kind"] == "ref"
    assert rows["Q4_K_M · KV f16"]["same_top"] == 98.765 and rows["Q4_K_M · KV f16"]["current"]
    assert rows["Q4_K_M · KV q8"]["kld"] == 0.006543
    assert "zu wenig Grafikspeicher" in rows["Q5_K_M · KV f16"]["error"]
    calls = log.read_text().splitlines()
    assert all("--n-cpu-moe 2" in c and "--chunks 12" in c for c in calls)  # Einstellungen des Profils
    assert "--kl-divergence" not in calls[0].split() and all(c.endswith("--kl-divergence") for c in calls[1:])
    assert not list(text.parent.glob("*.kld"))  # große Referenzdatei wieder weg
    assert json.loads(state.read_text())["quality"]["m"]["reference"] == "Q8_0 · KV f16"
    assert q.saved(state, "m")["tokens"] == 12 * 512 and any("Referenz" in s for s in said)


def test_missing_binary_and_no_llamacpp(tmp_path, cfg):
    llm = LLMConfig(base_url="http://127.0.0.1:9", profiles={"o": ProfileConfig(backend="ollama", model="x")}, active="o")
    router = LLMRouter(llm, state_path=tmp_path / "s.json")
    with pytest.raises(ValueError, match="llama.cpp"):
        asyncio.run(q.measure(cfg, router, "o"))


def test_corpus_is_loaded_once(tmp_path, cfg, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    calls = []

    def handler(request):
        calls.append(request.url.params["titles"])
        return httpx.Response(200, json={"query": {"pages": {"1": {"extract": "Absatz.\n== Abschnitt ==\nText " * 600}}}})

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
            first = await q.ensure_corpus(cfg, c)
            again = await q.ensure_corpus(cfg, c)
        return first, again

    first, again = asyncio.run(run())
    assert first == again and len(calls) == sum(len(v) for v in q.ARTICLES.values())
    body = first.read_text()
    assert "== Abschnitt ==" not in body and len(body) > 20000


def test_quality_routes(cfg, monkeypatch):
    from fastapi.testclient import TestClient

    from orbwise.server import create_app
    monkeypatch.setenv("ORBWISE_SKIP_WARMUP", "1")
    monkeypatch.delenv("ORBWISE_FAKE_LLM", raising=False)
    cfg.llm.base_url = "http://127.0.0.1:9"
    cfg.llm.profiles = {"o": ProfileConfig(backend="ollama", model="qwen3")}
    with TestClient(create_app(cfg), base_url="http://localhost:8765") as client:
        assert client.post("/api/models/o/quality").status_code == 400  # Ollama: kein llama-perplexity
        assert client.get("/api/models/o/quality").json() == {"result": None}
        assert client.post("/api/models/fehlt/quality").status_code == 404
