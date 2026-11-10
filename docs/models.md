# Models

← [Back to the README](../README.md)

## Models & profiles

**Adding models after installation:** open **Models** in the sidebar (or **Manage models …** on the model chip at the
top) → **Add** and pick one of the presets (with the same fit marks and download progress; a running download can be
cancelled there with **Cancel**). Models added this way can be deleted on the model page via
**⋯ → Delete**, which also removes their files (models from `config.yaml` stay). Or run

```bash
orbwise model add              # interactive list of presets for your GPU
orbwise model add qwen3:14b    # or any model from ollama.com/library
orbwise model search qwen3.6 27b                  # search GGUF models on Hugging Face
orbwise model search unsloth/Qwen3.6-27B-GGUF     # its quantizations with size and fit (✔ ~ ✘)
orbwise model add https://huggingface.co/unsloth/Qwen3.6-27B-GGUF/blob/main/Qwen3.6-27B-UD-Q4_K_XL.gguf
orbwise model add hf.co/unsloth/Qwen3.6-27B-GGUF:UD-Q4_K_XL   # same as the link (runs with llama.cpp)
orbwise model add ~/models/my-model-Q4_K_M.gguf                # a GGUF file you already have
orbwise model llamacpp [--update]                               # llama.cpp build: show / update
orbwise model remove qwen3-14b # remove it from the list (optionally also delete the files)
```

**Unloading:** **Unload** on the model page (or **Unload model** on the model chip) frees the video memory – the
llama.cpp server stops, Ollama drops the model – while the model stays selected; the next question (or the
API server) loads it again, warming the prompt cache does not. Handy before a game, during a big download or before
deleting. It also survives a restart: Orbwise then starts without loading the model. Deleting the selected model
unloads it first and selects another one (loaded with the next question).

**Any model from Hugging Face – no config needed:** the tab **Add** opens with **trending GGUF models that fit your
video memory** (one entry per model, even if several people uploaded it; refreshed hourly). Results appear while you
type a search term (`qwen3.6 27b`) or paste a Hugging Face link; sort them by downloads, trend, likes or last update
and tick *only what fits my memory*. Each model shows its size, parameters, architecture, maximum context, license and
whether it understands images, supports tool calls or is gated. Pick one, then a quantization: each shows its
download size, the video memory it needs (weights + vision module + ~1.5 GB for context) as a bar, roughly how much
context fits next to it and a short quality note (*Q4_K_M – usually the best pick*, *Q8_0 – nearly lossless* …); the
largest one that fits completely is marked **Recommended**. If your llama.cpp does not know the model's architecture
yet, Orbwise says so before the download (update llama.cpp at the top of the page). Orbwise then

- downloads the GGUF file – split files (`-00001-of-00003`) and the vision module (`mmproj`, for pictures in the chat)
  included, resumable – to `~/models/<user>_<repo>/` (`llm.models_dir`),
- fetches the official **llama.cpp** build once (ROCm or CUDA when it runs on your machine, otherwise Vulkan) to
  `~/.local/share/orbwise/llama.cpp/`,
- and adds a model that starts its own `llama-server`: free port, a random API key, the largest context (8k–64k) that
  fits your video memory, prompt-cache checkpoints for hybrid models.

Pick it in the model list like any other model; the context size can be changed there too. GGUF files you already
have show up under **On disk · ~/models** – one click turns them into a model without downloading (pasting a path to a
`.gguf` into the search field works too). New model types often need a newer llama.cpp: the llama.cpp line next to the
tabs of the model page shows when a newer build exists (**Update to b…**, checked at most every 3 hours); the button
(or `orbwise model llamacpp --update`) fetches it, and a loaded model restarts with it right away. If a release has
ROCm builds for several ROCm versions, Orbwise picks the one matching your installed ROCm (`/opt/rocm/.info/version`).
The **ROCm | Vulkan** switch next to it (CUDA | Vulkan on NVIDIA) moves all llama.cpp models to the other build of
the same version – downloaded first if it is not there yet – and restarts a loaded model with it.

**ROCm** is shown next to it (installed version, and a link to AMD's install guide when a newer one is out) but not
updated by Orbwise – it is a system package with kernel drivers that needs a reboot, and a broken update leaves the
GPU unusable. Update it yourself following the guide, then press **Update** on the llama.cpp line so the build
matches; `orbwise doctor` warns when the build was made for a different ROCm major version. Deleting a model (**⋯ → Delete**) removes files Orbwise downloaded; files you put into `~/models` yourself stay.
Gated repositories (e.g. Llama, Gemma) need a Hugging Face token: accept the license on the model page and enter a
read token under **Settings → General → Hugging Face** (kept in the keyring; `HF_TOKEN` in the environment works too).
`HF_ENDPOINT` points Orbwise at a mirror instead of huggingface.co. Ollama is still used for the memory embeddings and the
Ollama presets above.

**The model page** lists your models grouped into *Local* and *API* (the loaded one first, a filter field from seven
models on) with a pill that says whether each fits your video memory. The details of a model are split into tabs:
**Overview** (context window), **Performance** (experts in RAM, GPUs, batch size, finding the fastest setting,
speculative decoding), **Quality** (measuring quality, KV cache, sampling) and **Diagnostics** (context test, server
log); the last tab you used stays selected. Options are switches with a one-line explanation of the selected one, the
longer explanation sits behind the **i** next to each heading. The *Add* tab hides presets and search results that
don't fit your memory (one click shows them), and the *On disk* column only appears when there are new GGUF files.

**Context window, KV cache and sampling per model** (Models page, for any model – changes to a model that is not
loaded apply the next time it is loaded): the context window (a slider from 4k to 256k, up to what the model was trained for, separately for Tools and Coding – applied shortly after you let go), the KV cache level (f16 / q8 ≈ half / q4 ≈ a quarter of the memory – own llama-server only; with Ollama it
is service-wide via `OLLAMA_KV_CACHE_TYPE`) and the sampling values for **thinking** and **non-thinking** answers
(temperature, top_p, top_k, min_p, presence and repeat penalty; a **Qwen** preset fills in the vendor recommendation).
Context and KV cache restart the model server; sampling applies from the next answer. Empty sampling fields keep the
model's defaults.

Orbwise can know several language models and switch between them – click the **model chip** at the top (quick
switch), **Load** on the models page, or run
`orbwise model <name>` (`orbwise model` lists all profiles). The choice is remembered.

- `backend: ollama` – models from Ollama (Qwen 3 recommended for tool use)
- `backend: openai` – any OpenAI-compatible server: **llama-server** from llama.cpp, LM Studio, vLLM, …
- With `server:` Orbwise starts the model server itself when the profile is activated, waits until it is ready and
  stops it again when switching back or quitting (log: `~/.local/state/orbwise-llm.log`). The real context size
  is read from the server.
- `unload_ollama: true` evicts Ollama models from VRAM first, `embed_on_cpu: true` runs the memory embeddings on
  the CPU – useful with 8 GB of VRAM.

A hand-written profile for your own **llama-server** (models added in the web UI get one automatically):

```yaml
llm:
  active: llama
  profiles:
    qwen:
      label: Qwen 3 8B
      model: qwen3:8b
    llama:
      label: Qwen 27B (llama.cpp)
      backend: openai
      base_url: http://127.0.0.1:8080/v1
      model: qwen-27b
      api_key: "a-long-random-password"
      unload_ollama: true
      server:
        command: llama-server -m ~/models/qwen-27b-Q4_K_M.gguf -ngl 99 -c 32768 -np 1 --jinja --port 8080 --api-key a-long-random-password
        env:
          HSA_OVERRIDE_GFX_VERSION: "10.3.0"   # RX 6600/6650/6700 (gfx103x) → report as supported gfx1030
        startup_timeout: 240
```

The `api_key` keeps websites in your browser from talking to the llama-server.

**Small context windows** (below 16k, e.g. 8k on an 8 GB card – *lean mode*): Orbwise then sends only 9 core tools plus
the groups that match the request – weather only when you ask about the weather, power when you want to shut down,
Home Assistant when you talk about lights or heating. Of big groups only the reading part comes first: "find my
invoice" loads Paperless search/ask/read/open (~800 tokens instead of ~1,700), "sort the Paperless inbox" also the
sorting tools; a short "yes, do it" loads what the model just proposed ("Shall I archive them?" → mail tools). The
selection only changes when something new is needed (the activity shows "tools added … – one-off re-read" and what was
dropped); groups used in the last two questions stay while they fit (~35 % of the budget). If no keyword matched, the
model can still get any tool: **load_tools** lists exactly the groups that are missing and loads the one it needs for
the next step (it then stays until the next compaction). From 16k the core groups are always there and added groups
stay. A new chat with every integration set up starts at ~2.3k tokens with 8k (before: ~4.8k) and ~4.8k from 16k. When the model asks
for several things at once that need no confirmation (e.g. weather and calendar), they run in parallel. You can also
switch tools or whole groups off: `tools: {disabled: [sysadmin, paperless]}`.

### Prompt cache tips

<details>
<summary>Only the new part of a chat should be read – how llama-server and Ollama are set up for that</summary>

How long the model reads before it answers depends mostly on how much of the prompt the model server can reuse. The
activity panel shows it for every step ("1,240 tokens read in 4 s (+11,980 cached)") – after the first question in
a chat only the new part should be read.

**Hybrid models** (attention plus recurrent layers, e.g. Qwen 3.5) can only reuse an earlier prompt by restoring a
saved checkpoint. Orbwise therefore starts its llama-server with `--ctx-checkpoints 32`, a prompt cache
in RAM (`--cache-ram`) and `--cache-idle-slots` – only the options your llama-server knows (checked with `--help`)
and only those you did not set yourself. The RAM cache is sized for about two full chats of the loaded model (KV
size per token from the GGUF file × context window), at most a quarter of your RAM: llama-server does not keep a
chat at all if its state is larger than the limit, so a long chat would otherwise be read again after every summary,
Telegram message or chat switch.

As soon as you start typing or speaking, Orbwise has the server read the open chat in the background if something
else is in its cache (also after a chat switch, when the UI opens, after Telegram, routines and daily summaries) –
when you send, only your question is new. It warms up with the thinking mode you have selected: chat templates
build a different prompt with thinking on or off, so toggling the thinking button can make the model read the whole
chat again – Orbwise warms up the new variant right away. The start line in `~/.local/state/orbwise-llm.log`
shows the full command; restored checkpoints appear in the same log.

- **llama-server:** start it with **one slot** (`-np 1` – Orbwise's profiles do that); with several slots requests
  can land in different caches, `orbwise doctor` points this out. Optionally `--cache-reuse 256` lets the server reuse
  matching blocks even after a change further up (e.g. when a tool group is added).
- **Ollama:** `llm.keep_alive` (default `30m`) – after that Ollama unloads the model and the next question reads the
  whole chat again. Raise it if you often take longer breaks and the VRAM is not needed elsewhere.

</details>

### Faster prompt reading (llama.cpp)

<details>
<summary>One click measures which llama.cpp build and batch size read prompts fastest on your GPU</summary>

**Find the fastest setting** on the model page (section *Performance*) measures with `llama-bench` – part of
every llama.cpp build – which build and batch size read prompts fastest on your card: the installed build (e.g. ROCm)
against the other one (Vulkan; on NVIDIA CUDA ↔ Vulkan, downloaded once for the comparison), each with `-ub`
512 / 1024 / 2048, a 2k prompt at the start of a chat and at 16k depth, plus the output speed. None of this changes
the answers. It takes about 3–6 minutes; the model is unloaded meanwhile and loaded again afterwards. The fastest batch
size is applied; if the other build is ≥ 10 % faster, the page offers to switch (all llama.cpp models, kept for
updates). For IQ quants it notes that a K quant (Q4_K_M, UD-Q4_K_XL) has at least the same quality and usually reads
faster, at ~1–2 GB more VRAM.

Expect roughly +10–40 %, not MoE speed: a dense model (e.g. 27B) computes all its parameters for every prompt token,
an MoE model only its few active billions – prompt reading is compute-bound, so that gap stays. The bigger
lever remains not reading at all (prompt cache, see above).

</details>

### Why was so much read again?

<details>
<summary>Orbwise names the reason under the answer when more than 5,000 tokens were read again</summary>

With a warm prompt cache only what is new gets read; a long wait means the server had to read most of the chat
again. Orbwise compares every request with the previous one and, when more than 5,000 tokens were read again, names
the reason under the answer and in the activity: tool list changed, old results hidden, after summarising, other chat
or model, an earlier message edited – or *the server no longer had the beginning* (restart, image mode, another
program via the API, or a hybrid model such as Qwen3.5+ without a matching checkpoint). In windows from 32k on the
tool list now stays complete and fixed as long as it takes at most a quarter of the window and under a minute to
read, so a new topic no longer forces the whole chat to be read again; the step-limit summary keeps the tool list too.

</details>

### Several GPUs

With two (or more) graphics cards Orbwise counts their memory together: the model page shows e.g. *2× RTX 3090 ·
48 GB*, model recommendations and the context size of new models use the shared memory (with a buffer per card), and
the telemetry tiles add the cards up (hover for each card). llama.cpp splits the model's layers across all cards –
so bigger models or a bigger context fit.

**Models → a llama.cpp model → GPUs:** *Automatic* (default) lets llama.cpp split by free VRAM. *Custom split* sets a
share per card (`--tensor-split`, card order as in `nvidia-smi`); 0 % leaves a card out – one card alone is fastest
when the model fits on it. The setting is kept per model and restarts its server. After adding a card, raise the
context of existing models on the model page. Two NVIDIA cards use the CUDA build; mixing AMD and NVIDIA needs the
Vulkan build. Ollama models split by themselves.

### MoE models bigger than the GPU: experts in RAM

<details>
<summary>Mixture-of-experts models stay fast when only their expert weights move to system memory</summary>

MoE models (Qwen3-30B-A3B, gpt-oss, GLM-Air …) use only a few experts per token. **Models → Experts in RAM (MoE)**
keeps the expert weights of the first N layers in system memory (llama.cpp's `--n-cpu-moe`); attention and the KV
cache stay on the GPU, so prompt processing stays nearly as fast and generation slows down only somewhat – much
better than moving whole layers. **Automatic** moves just as many layers as the context window needs and recalculates
when you change the context or KV cache; **Custom** takes a number. A newly added MoE model that would only fit with a
small context gets 32k context and the right number automatically. llama.cpp models only; the section appears for
MoE models when the installed llama.cpp knows the option.

</details>

### Measuring quality: KV cache and quants

<details>
<summary>How close are KV cache q8/q4 or a smaller quant to the original? Measured with llama-perplexity</summary>

**Models → Measure quality** compares the model with KV cache f16, q8 and q4, and other quants of the same model in the
same folder, against the most precise version available (e.g. the Q8 next to your Q4) – on a fixed German and English
text (Wikipedia articles, downloaded once; if Wikipedia can't be reached, the English *wikitext-2* test set from
Hugging Face – the result says which text was used, and your own text at `~/.cache/orbwise/quality/eigener-text.txt`
takes precedence). For each variant it shows how often it picks the same most likely next
token (above ~98 % is hard to notice), the mean KL divergence and the perplexity. It takes a few minutes per variant,
the model is unloaded meanwhile, and the reference logits need a few GB in the cache folder for a moment. This measures
closeness to the original – to compare different models on tasks, use `scripts/llm_bench.py`.

</details>

### Faster prompt processing: batch size

<details>
<summary>A bigger micro-batch reads long inputs faster, at the cost of some VRAM</summary>

**Models → Performance → Prompt processing (batch size)** sets llama-server's micro-batch (`-ub`, default 512) per model to 1024 or
2048 (`-b` grows with it). Bigger batches keep the GPU busier while reading long inputs – tool results, files, PDFs,
the first message of a chat – at the cost of a larger compute buffer in VRAM (shown next to the setting, together
with the measured prompt speed; the measurement restarts after a change). Short questions hardly change. Compare
with **Test context**, which reads a full window and reports tokens/s. llama.cpp models only.

</details>

### Faster answers: speculative decoding (MTP / n-gram)

<details>
<summary>Several tokens per step – same answer, sooner</summary>

**Models → Performance → Faster answers** lets llama.cpp models propose several tokens at once, which the server then verifies in a
single pass – the answer stays the same, it just arrives sooner. **MTP** uses the model's own multi-token-prediction
layers (GLM-4.5, DeepSeek-V3, Qwen3.5/3.6 and others – either inside the GGUF as `nextn_predict_layers` or as an
`mtp-*.gguf` file next to it, which Orbwise downloads together with the model) and costs a little VRAM; **n-gram**
repeats passages already in the context (code, tool calls) and works with any model at no memory cost. Orbwise only
offers what both the model and the llama.cpp build support (`--spec-type` in `llama-server --help`; otherwise it
suggests updating llama.cpp) and shows the hit rate from the server log – below ~30 % it is better switched off.
Not available for Ollama models.

</details>

### API server for other programs (OpenAI compatible)

Like the server mode of LM Studio: **Models → API server** turns on an OpenAI-compatible endpoint at
`http://127.0.0.1:8765/v1` (`/v1/models`, `/v1/chat/completions` with streaming, `/v1/completions`). VS Code with
Continue or Cline, Open WebUI or your own scripts then use the loaded model through Orbwise. It is **off by
default**, every request needs the API key shown there (`Authorization: Bearer ow-…`, a new key can be created at
any time), and it is only reachable on this computer.

Programs get the model **raw** – without the Orbwise system prompt, memory or tools, so they cannot do anything on
the PC. Orbwise sits in front instead of exposing llama-server directly: it starts the model server when needed,
hands the GPU back from image mode, and knows that its own prompt cache is gone after an API request (the next
Orbwise answer re-reads the chat). By default the loaded model answers whatever model a program names; with
**Programs may switch the model** Orbwise loads the named model (for your chat too). With Ollama models, Ollama may
reload the model with a different context size. For Open WebUI in Docker use `--network=host`.

### Thinking mode

By default the model answers directly (`think: false`) – fast. The **Think** button turns reasoning on per
request: the thoughts stream into the answer as an open block that collapses as soon as the model writes its answer
or calls a tool (it opens again if the model thinks on after a tool); it can be expanded any time and is never read
aloud. It costs time (often 10–60 s per step on smaller GPUs). For
llama-server, **don't** pass `--reasoning-budget 0`, which disables reasoning server-side.

The menu follows what the loaded model can do. Orbwise reads it from the model's chat template (llama-server
`/props`) or Ollama's capabilities:
- **The model's own levels:** **Off** plus exactly the levels the chat template itself handles (values it compares
  `reasoning_effort` with or lists, at least two), shown under the model's own names (e.g. `low`, `xhigh`), its
  default marked; hovering the note at the bottom of the menu shows the template lines they come from. Orbwise has
  no levels or names of its own – not even for gpt-oss: a template that names no levels counts as on/off. The level
  goes to the model as `reasoning_effort`; Orbwise adds no token budgets or "think briefly" hints.
- **Only on/off** (Qwen 3 and most others) or unknown: **Off / On** – no invented levels.
- **No thinking at all**: the button is greyed out.
Only in a small context window (< 16k) is the reasoning capped so the answer still fits; if it runs longer, the step
continues without further thinking, with the thoughts so far (the activity shows "enough thinking – acting now").

### Benchmarking your models

A standalone script (not part of the web UI) runs your model profiles one after another through the benchmarks
model announcements use and prints a score table at the end:

| Column | Dataset | Measures |
|---|---|---|
| MMLU-Pro | TIGER-Lab/MMLU-Pro | knowledge, 10 choices, 0-shot CoT |
| GSM8K | openai/gsm8k | maths word problems |
| HumanEval | openai/openai_humaneval | writing Python functions (pass@1) |
| ARC-C | allenai/ai2_arc (Challenge) | science reasoning |
| IFEval | google/IFEval | following verifiable instructions (strict, prompt-level) |
| Speed | – | prompt reading (prefill) and generation in tokens/s |

```bash
uv run python scripts/llm_bench.py --list                        # profiles and benchmarks
uv run python scripts/llm_bench.py --profiles qwen3-14b,gemma --limit 50
uv run python scripts/llm_bench.py --resume                      # continue an interrupted run
```

Profiles that start their own llama-server are started and stopped by the script (stop Orbwise first with
`orbwise stop` – both would share the GPU memory); Ollama models are unloaded after each profile. Datasets come
from the Hugging Face datasets API and are cached page by page in `~/.cache/orbwise/bench` (an interrupted download
continues where it stopped; for large sets like MMLU-Pro a sample only loads randomly chosen pages; `HF_TOKEN`
raises the rate limit); by default a fixed sample
of 200 questions per benchmark (`--seed`, `--full` for everything), greedy decoding, thinking off (`--think`).
Results land in `~/.local/share/orbwise/bench/<time>/` as `results.md`, `results.csv`, `results.json`, plus every
single answer per profile for checking. Samples with 0-shot prompts are comparable between your models, not identical
to official numbers. HumanEval runs the model's code locally in a separate process with time and memory limits –
`--no-exec` skips it.

## Own backend (experts) – e.g. Strata, vLLM, LM Studio, Forge

Orbwise does not have to run the models itself. **Models → Add → Own backend (experts)** takes any
**OpenAI-compatible server** – Strata, vLLM, LM Studio, TabbyAPI, a llama-server on another machine: name, address
(`http://host:port/v1`; *Test connection* adds `/v1` if needed and lists the server's models), optional API key and
optional context window. Orbwise then only talks to it: it starts and stops nothing there, so context window, KV
cache, GPUs and sampling defaults are up to the server – the context window you enter only tells Orbwise how much
fits. Switching to such a backend still stops Orbwise's own llama-server (only one model in memory).

The same for images, on the image model page (image mode → *Models*): **OpenAI Images API**
(`/v1/images/generations`), **Automatic1111 / Forge** (`/sdapi/v1/txt2img`, the model goes in as
`sd_model_checkpoint`) or an external **stable-diffusion.cpp sd-server** (editing with references works there).
Prompt, negative prompt, size, steps, seed and count are sent as far as the interface knows them. API keys are kept in
the keyring, never in `state.json` or `manifest.json`.

## AMD GPUs

- Check with `ollama ps` during a request – it should say `100% GPU`.
- Cards that ROCm doesn't officially support (e.g. RX 6600/6700, RX 7600) usually work with an override:
  ```bash
  sudo systemctl edit ollama
  # [Service]
  # Environment="HSA_OVERRIDE_GFX_VERSION=10.3.0"   # RDNA2 (RX 6000)
  # Environment="HSA_OVERRIDE_GFX_VERSION=11.0.0"   # RDNA3 (RX 7000)
  sudo systemctl restart ollama
  ```
- If ROCm doesn't work at all: `./scripts/install.sh --gpu vulkan` (Arch).
