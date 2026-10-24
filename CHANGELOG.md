# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Removed
- **Piper speech output:** replaced by Pocket TTS – the Piper voice catalogue, voice uploads, `voice.tts_voice` /
  `voice.tts_length_scale` and the `piper-tts` dependency are gone. Downloaded Piper voices in
  `~/.local/share/orbwise/voices/` are deleted on the first start. Without Pocket TTS the browser speaks.
- **Strata engine:** the experimental Strata integration for Qwen3.8-Flash-Next is gone again – the dense
  Qwen3.8-27B on llama.cpp answered better, and loading froze AMD desktops. On the first start Orbwise removes Strata
  profiles from `state.json`, stops a running Strata server and deletes the model (`~/models/strata`) and the engine it
  installed (`~/.local/share/strata`).
- **Bonsai setup:** `orbwise model add bonsai`/`bonsai-kompakt`, the PrismML fork with its own launcher and the
  `BONSAI_*` settings are gone; GGUF models from Hugging Face run on Orbwise's own llama.cpp. Bonsai profiles are
  removed on the first start; `~/bonsai` stays and can be deleted by hand. The prompt-cache options
  (`--ctx-checkpoints`, `--cache-ram`, `--cache-idle-slots`) still apply to every llama-server Orbwise starts.
- **Ollama → llama.cpp conversion** ("Reload with llama.cpp" for old `hf.co/…` Ollama models): such models simply stay
  Ollama models; new Hugging Face downloads always run on llama.cpp.

### Changed
- **Brave tile shows Brave's numbers:** instead of Orbwise's own request count, the tile shows the usage Brave reports
  (used / limit / left / reset) on plans with a monthly quota; on credit plans – where Brave reports no usage and has
  no balance API – it links to the Brave dashboard. `orbwise doctor` says the same.
- **Thoughts in the chat:** in thinking mode the reasoning now streams directly into Orbwise's answer and collapses
  as soon as the model answers or calls a tool ("Thoughts · 12 s"), instead of a bubble next to the orb with zoom.
- **Tidier model page:** a *Performance* section (prompt speed, find the fastest setting) follows the context window;
  KV cache, manual batch size and speculative decoding moved under a collapsed *Advanced* section (only for
  llama.cpp models where they apply), and the explanations show one sentence with **More** for the rest.
- **Code structure:** `agent.py` is split into focused parts – `agent_compact.py` (making room, summarising),
  `agent_tools.py` (which tools go along), `agent_safety.py` (approvals, foreign content, leak guard) and
  `agent_speed.py` (prompt speed, prewarming); the model endpoints moved from `server.py` into `api_models.py`, with one helper for "restart the
  loaded model with a new option" instead of four copies; unused constants removed.
- **state.json is written safely:** all model profiles and settings go through one module that writes atomically
  (temporary file + rename) under a lock – a crash while saving can no longer leave a half-written file, and two
  changes at the same time (a download finishing while you change a setting) no longer overwrite each other.
- **Fewer full re-reads in big windows:** from 32k on all tools go along from the start (if they take ≤ 25 % of the
  budget and < 60 s to read) instead of growing mid-chat, which re-read the whole chat; the step-limit summary keeps
  the tool list as well.
- **Auto mode after foreign content:** after a web page, e-mail, PDF or image, reading tools (web search,
  `fetch_url`, reading and listing files, recall, calendar/notes/Home Assistant/Portainer lookups) no longer ask –
  only actions that change or start something do; the data-leak guard still checks what goes out.

### Added
- **Pocket TTS voices:** Orbwise now speaks with Kyutai's Pocket TTS (a ready-made program, no PyTorch, local CPU
  server) – far more natural than Piper. The installer sets it up (`orbwise voice install`), otherwise *Settings →
  Voice → Install Pocket TTS*. Its 27 voices are listed with German *Juergen* first; preview with ▶.
- **Language per voice:** each voice speaks a chosen language (German, English, French, Spanish, Italian, Portuguese,
  Dutch), and Orbwise answers in the language of the active voice – Whisper then detects the spoken language itself
  and the fixed spoken phrases follow (German/English). Stored in `state.json`.
- **Find the fastest llama.cpp setting:** the model page measures with `llama-bench` which build (ROCm ↔ Vulkan,
  CUDA ↔ Vulkan) and batch size (512/1024/2048) read prompts fastest – at the start of a chat and at 16k depth – and
  shows a table with prompt and output speed. The fastest batch size is applied; a faster other build can be adopted
  with one click (`POST /api/llamacpp/flavor`, kept as preferred build for updates). IQ quants get a note about
  K quants. Lossless: only speed settings change.
- **Unload the model by hand:** **Unload** on the model page and the model chip frees the video memory (stops the
  llama.cpp server, unloads Ollama) while the model stays selected; the next question or API request loads it
  again (prompt-cache warming does not), and an unloaded model stays unloaded across a restart. The selected model
  can now be deleted: Orbwise unloads it and selects another one without loading it.
- **Brave quota in the activity panel:** Brave Search requests are counted per month and the quota Brave reports
  (`X-RateLimit-*`) is kept; a tile shows used / limit / remaining / reset date (orange from 80 %, a notice below
  10 %), `orbwise doctor` prints it, and a used-up quota is not queried again until it resets (with
  `search_fallback` the result pages are used instead).
- **Why was it read again?** Every model request is compared with the previous one; when more than 5,000 tokens had
  to be read again, the answer's stats line and the activity name the reason (tool list changed, old results hidden,
  summary, other chat/model, edited message, server lost the cache – restart, image mode, API use or hybrid model
  without checkpoint).
- **Batch size for prompt processing:** per llama.cpp model 512 / 1024 / 2048 (`-ub`, with `-b` to match) on the
  models page, remembered and applied on the next server start; the page shows the measured prompt speed (reset after
  a change) and the compute buffer from the server log.
- **Speculative decoding (MTP / n-gram):** the models page detects whether a model has multi-token-prediction
  layers (`nextn_predict_layers` in the GGUF or an `mtp-*.gguf` next to it, now downloaded with Hugging Face models)
  and whether the llama.cpp build supports `--spec-type draft-mtp` / n-gram, offers Off / MTP / N-gram per model
  (remembered, server restart for the loaded one) and shows the draft hit rate from the server log. MTP sidecar files
  no longer show up as separate models.
- **PDFs in the chat:** attach PDFs like images (paperclip, drag and drop; up to 50 MB). They are read locally
  (pypdf, new dependency), shown as a card with title and page count, and their text goes to the model with the
  question – shortened to ~40 % of the context window with a note if needed; scanned PDFs without text are flagged
  when attaching. PDFs count as foreign content (actions need confirmation), the data-leak guard knows their text,
  and regenerate/edit/export keep them. The attach button now has a paperclip icon.
- **API server (OpenAI compatible):** Models → API server turns on `/v1/models`, `/v1/chat/completions` (with
  streaming) and `/v1/completions` at `http://127.0.0.1:8765/v1` for VS Code (Continue, Cline), Open WebUI or scripts.
  Off by default, with its own API key (show, copy, renew), local only. Programs get the loaded model without system
  prompt, memory or tools; Orbwise starts the model server if needed, takes the GPU back from image mode, notes that
  its prompt cache is gone and, if allowed, loads the model a program asks for. The page shows the request count and
  the last client, plus ready-made settings for Continue, Open WebUI and curl.
- **Models page (like LM Studio):** a new **Models** entry in the sidebar lists every model with engine,
  quantization, size and whether it fits in VRAM; the detail view loads a model with one click and shows its memory
  use (model + KV cache, Flash Attention), the context window (separately for Tools and Coding), KV cache, sampling,
  the context test and the log of the last llama-server start (API key masked). Changes to a model that is not loaded
  apply the next time it is loaded. The **Add** tab bundles Hugging Face search, GGUF files in `~/models` and the
  presets; downloads show above both tabs. The header shows the GPU with live VRAM use and the llama.cpp build with
  update/install. The model chip at the top is now a quick switch with a link to the page.
- **Chat actions:** copy any message, edit your own message (the chat continues from there), regenerate the last
  answer, export a chat as Markdown. The chat list is grouped by date (pinned, today, yesterday, …) with a ⋯ menu
  for rename, pin, export and delete; click the chat title to rename it.
- **Learning from experience:** Orbwise notes failed-then-solved tool runs, your corrections and 👎 (new buttons under
  each answer) and, while idle, turns them into short lessons. Only lessons matching the current question go into
  its context note, under a small fixed budget (≈2 % of the window below 16k, ≈3 % above) – the prompt does not grow
  with the number of lessons and the system prompt stays cached. Similar lessons are merged, useless ones dropped,
  everything is visible and editable under Memory → Experience (`memory.learning`, `memory.lessons_max`).
- **Image mode: several references and resolution:** up to 3 reference images (numbered image 1–3, e.g. to bring
  people from different photos into one picture), resolution as megapixels (0.5–2) per aspect ratio, a custom
  width × height or the aspect of image 1; the resulting size is shown before generating.
- **Images in the chat:** attach up to 4 images to a message (image button next to the microphone, Ctrl+V or drag
  and drop). If the active model can see images (llama-server with a vision module – `/props` modalities – or an
  Ollama model with the `vision` capability), it gets them directly; otherwise the message names the file and the
  model uses `look_at_image` with the separate vision model (the tool is loaded automatically, also in lean mode).
  Images are stored under `~/.local/share/orbwise/attachments`, shown in the chat history and count towards the
  context display.
- **Image mode with Qwen-Image-2.1:** a third tab next to Tools and Coding generates and edits images locally via
  stable-diffusion.cpp (`orbwise model add qwen-image` downloads the prebuilt Vulkan build and the model files
  matching the GPU). Format, steps, count, seed, negative prompt, progress per step, gallery with variation, reuse,
  edit, send to phone and delete. On cards below 16 GB the language model steps aside while painting and returns
  automatically when it is needed again, when the image view is left or after 10 idle minutes. Also available as
  `generate_image` in the tools mode and `/bild …` via Telegram.
- **All tools only when they are quick to read:** with a large window Orbwise used to send every tool description
  (~16k tokens with all integrations) – on a slow card (a 27B on 8 GB, ~140 tokens/s) that is over 2 minutes of
  reading after every start, model switch and summary. Now all tools only go along if reading them takes ≤ 10 s;
  otherwise the selection (core tools + matching groups, the rest via `load_tools`) applies – the start prompt drops
  from ~18k to ~5k tokens there. The measured reading speed is remembered per model (`state.json`), and the
  background pre-read now shows how much came from the cache.
- **Docker via Portainer** (`portainer:` in the config): list containers and stacks, check for newer images without
  downloading (registry digest vs. running image), update a container or stack to the newest image, start/stop/
  restart/remove containers and stacks, read logs and install new apps from a docker-compose file. Changes need a
  confirmation; `orbwise doctor` checks the token and lists the environments.
- **SSH to other machines** (`ssh_connect`, `ssh_run`, `ssh_disconnect`; optional short names under `ssh.hosts`):
  Linux commands on a NAS or server like on the PC. User name and password are asked in a dashboard dialog for every
  new connection and go straight to `ssh` – never to the model, the config or the disk. The connection stays open
  for 15 minutes after the last command. Commands are classified like local ones (read-only runs directly, the rest
  after confirmation), `sudo` on the remote machine asks for its password in the dashboard too.
- **Context self-test** (`orbwise context-test [--quick]`, Settings → Context window → *Test context*): checks with
  the active model whether the server runs with the configured window (and no `context_budget_tokens` caps it), the
  KV cache sits in VRAM, how far the token estimate is off, whether the prompt cache works for the same prompt and a
  follow-up (hybrid models: checkpoints), whether a prompt up to the compaction point fits and the model still sees its
  start (code word), and whether an oversized prompt is rejected instead of silently cut. Uses its own messages –
  the open chat stays untouched.

### Fixed
- **Cache numbers for one-token requests:** llama-server sometimes omits the generation speed when only one token is
  generated (warm-up, self-test); the prompt counts then came from `usage`, which includes cached tokens, so a
  cache hit looked like a full re-read. The prompt counts now always come from the timings, and the self-test
  also judges the cache by time.
- **Prompt cache for hybrid models:** they can only reuse an earlier prompt through checkpoints; Orbwise now
  starts its llama-server with `--ctx-checkpoints 32`, a RAM prompt cache (`--cache-ram`, 15 % of RAM, ≤ 4 GB) and
  `--cache-idle-slots` – only what the installed llama-server knows. Much less re-reading after hidden results, a
  summary, Telegram or a chat switch.
- **Leaner Paperless packages:** with many correspondents/types/tags only the matching ones are listed (Paperless'
  suggestions, the current ones, names found in the text – up to 15) instead of up to 3×150, so a package fits in one
  result even at 8k.
- **No detour via curl:** `run_shell` refuses commands aimed at a configured Paperless, Trilium, Home Assistant or
  calendar address and names the right tools instead.
- **Correct KV size for hybrid models:** only attention layers count (`full_attention_interval` or per-layer
  `head_count_kv` from the GGUF) – a hybrid 27B at 64k is ~1.2 GB, not 4.5 GB, and no longer flagged as too big.
- **Paperless: no more duplicate names** – if a similar correspondent, document type or tag exists ("Möbelhaus
  Mustermann GmbH" vs. "Möbelhaus Mustermann", another spelling, singular/plural), that one is used instead of
  creating a new one; the confirmation already shows "Möbelhaus Mustermann (existing, instead of …)". Tags are not
  matched by their beginning ("Steuer 2025" stays its own tag), and when several existing names fit equally, nothing
  is chosen automatically.
- **Context in two stages – below 16k and from 16k** (`orbwise/context_plan.py`), as close to Claude Code as a local
  model allows:
  - **Lean mode below 16k** (e.g. 8k on an 8 GB card): only 9 core tools up front, everything else by keyword or
    `load_tools` (which now lists exactly what is missing); big groups (Paperless, mail, calendar, system, notes,
    packages) bring their changing part only when the request sounds like it; "yes, do it" loads what the model just
    proposed. The selection only changes when something new is needed and is then repacked within ~35 % of the
    budget. Tool results, memories, facts, the answer reserve, thinking and the summary scale with the window. A new
    chat with every integration starts at ~2.3k instead of ~4.8k tokens.
  - **Old tool results are hidden before anything is summarised** (both stages, like Claude Code's microcompact):
    each becomes a short placeholder with its start and the file that holds it completely; long arguments of old calls
    are shortened. No model call is needed. In a simulated task with 24 big files this cut the summaries from 23 to 4
    (8k) and from 6 to 1 (16k).
  - **The summary no longer sits in the instructions** but in the note of the first message after it, and from 16k the
    tool selection is kept – instructions, facts and tools stay in the model server's cache across a compaction.
    Lean mode writes a short summary (4 sections); Coding mode from 16k attaches the current content of the files
    worked on last.
  - Paperless, mail, Obsidian and Trilium respect the window too (section by section), and any result that is still
    too long is cut with the full text saved in `~/.cache/orbwise/outputs/` (now the last 100).
  - The CONTEXT tile shows the stage and how many results are hidden, the activity panel shows "made room" and which
    tools were added or dropped, and Settings → Models marks windows below 16k as lean mode.
- **Context in memory and context window in the UI:** a telemetry tile shows the KV cache size and how much of it
  is in VRAM or RAM (exact from the llama-server log, estimated for Ollama) with a recommendation to raise or lower
  the window; Settings → Models → Context window changes it per model (server restart or Ollama reload, remembered
  in state.json, llama-servers via `-c`).
- **Dates worked out instead of guessed:** `date_info` answers weekday, "next Tuesday", "in 3 weeks", calendar week,
  days between two dates and German public holidays (`holiday_region`, e.g. `BW`); reminders, calendar, `recall` and
  routines understand phrases like "next Tuesday 9:00" or "last Friday" directly, and a reminder's confirmation
  names the weekday.
- **Paperless: sorting many documents package by package** – `paperless_review_next` hands out 3 documents of the
  inbox (or of the documents without correspondent/type) at a time, with progress saved in a file; after applying
  Jarvis stops and asks whether to continue, and only one package per message is possible, so it can no longer run
  on endlessly or forget documents. `paperless_review_skip` skips unclear ones.
- **edit_file and a task list:** files are changed by replacing an exact snippet (with clear errors and the changed
  lines as result) instead of rewriting them; multi-step work keeps a task list (`todo_write`) that shows as a
  checklist in the answer and survives every compaction.
- **Tools on demand:** with a small window `load_tools` lists every configured tool group and loads a missing one
  for the next step, so no tool is out of reach when no keyword matched.
- **Parallel tool calls:** calls of one step that need no confirmation run at the same time (not with mail or
  screen content in the same step).
- **Smarter memory:** memories are only shown when they really match the question
  (`memory.retrieval_min_similarity`), and `remember` replaces an outdated version of a fact instead of keeping
  both.
- **Thinking levels** in the *Think* menu: off, brief (~500 tokens of reasoning), normal (~2,000) and thorough.
  gpt-oss gets real `low`/`medium`/`high`; other models (Qwen 3 …) get a reasoning budget – a short hint, and
  if the model still thinks too long the step continues without thinking, with its thoughts so far.
- **Context handling like Claude Code** – fast on slow graphics cards, without forgetting:
  - Between two compactions the prompt only grows at the end: date/time, memories and the approved plan are frozen
    into the note in front of each question, facts and tool groups are a snapshot. llama-server and Ollama therefore
    only read what is new – no more re-reading the whole chat after an answer, `remember`, a new topic or a Telegram
    message. Later questions get fewer and only new memories; Coding mode adds none by itself (`recall` on demand).
  - Only when the window is nearly full (16k: ~91 %, with thinking earlier), the model writes one structured summary
    (request, facts, files and commands, errors and fixes, decisions, all user messages, open points, current work,
    next step) from the cached prompt; the chat continues with the summary, the current question and the last step,
    and a running task carries on by itself. Right after an answer this happens while you read
    (`memory.compact_idle`). Everything stays in the chat (a divider with the summary), the journal and the search
    index, which can bring compacted details back. `/compact [focus]` or a click on the CONTEXT tile compacts by hand.
  - Orbwise pre-reads the chat in the background after Telegram, routines, daily summaries and chat, mode or model
    switches, and when you start typing or recording.
  - Big tool output is limited at the source: `read_file` reads page by page (`offset`/`limit`), long shell, log and
    web output keeps start and end and saves the full text in `~/.cache/orbwise/outputs/` for the model to read on.
  - `orbwise doctor` points out a llama-server with several slots (`-np 1` keeps one cache).
  - Leaner start: instructions for Paperless, mail, calendar, notes, smart home and system tools are only in the
    prompt together with their tools, tool descriptions lost internal details and "Optional:" prefixes, the NAS
    search only exists with a configured NAS, the Telegram tool loads on demand. A new chat with every integration
    starts with ~30 % less context (~3.8k instead of ~5.5k tokens with a small window; ~4.7k with the new
    edit_file, task list and load_tools).
- **Tools and Coding mode** (switch top left): Coding loads only files, shell, web and memory tools (≈ 2k instead of
  ≈ 10k tokens of tool descriptions), uses a developer prompt, has no voice (no read-aloud, push-to-talk or wake word)
  and a wider chat. Separate chat histories per mode, a project folder per coding chat (working directory for shell
  and relative paths), and optionally its own model per mode (remembered when you pick one in that mode).
- **Auto mode as a drop-down** next to *Think*: *Off*, *Read only* (default), **Read + edit files** and **Auto**
  (everything without root – `python`, `make`, `git commit`, `pip --user` … – runs without asking; deleting, sudo,
  shutting down, sending over the network, start-up files and credentials still ask). *Read + edit files* – also
  creates, writes, copies and moves files in your own home without asking (no root, no deleting, no hidden files,
  launchers or credentials; plan mode and untrusted content still ask). Also selectable in Settings → General.
- **Remember the sudo password** for 15 minutes (`tools.sudo_remember_minutes`, checkbox in the password dialog):
  in memory only, only after sudo accepted it, only for requests at the computer – never for Telegram or routines.
  Settings → Status shows how long it is kept and has a *Forget* button.
- **The answer shows what the model is doing** instead of a blinking cursor: a live line with a running timer –
  reading the prompt (how many new tokens, how much is cached, estimated time or real progress with llama-server),
  thinking, writing a tool call (e.g. `write_file` with its size), answering, reloading after image analysis,
  retrying with a trimmed context, summarising the chat or adding tools. Afterwards a small line under the answer
  sums up where the time went (total time, tokens read and cached, tokens written, tok/s – expand it for each step).
  Background work (pre-reading the chat, Telegram, routines) stays in the activity panel. Under the orb, changing
  Jarvis-style lines match what it is doing ("Consulting the command line …", "Leafing through your documents …").
- **Auto button for read-only commands** (next to *Think*, on by default): many more harmless commands are recognised
  as read-only and run without a confirmation – containers (`docker/podman ps|images|logs`, `kubectl get`), packages
  (`dpkg -l`, `rpm -q`, `apt list`, `flatpak list`, `pip list`, `ollama list`), network (`nmcli … show/status`,
  `resolvectl status`, `tailscale status`), git (`branch`, `remote -v`, `stash list` …), archives (list only), desktop
  (`wmctrl -l`, `swaymsg -t get_*`, `gsettings get`) and more. Subcommands and options that change something keep
  asking. Switched off, every shell command asks. The rules live in code and add nothing to the prompt.
- **Start screen shows what is loading:** connection, memory, language model (server start and preloading the Ollama
  model into memory, with progress), speech recognition, voice, wake word, Telegram and the last chat – live, with a
  progress bar; Orbwise starts by itself once everything is loaded (or start early, the rest keeps loading and the orb
  shows "loading model"; it switches to "ready" as soon as the model is loaded). The first question no longer waits for Ollama to load the model.
- **New, calmer design:** graphite instead of neon, system font, icons and chat bubbles; collapsible sidebar with
  chats, Planner/Memory sheets and a settings page (models, voices, general, status) instead of the top pills; big orb
  with greeting and suggestions on an empty chat that shrinks to a live strip during a conversation; activity panel
  with telemetry opens by itself when a tool runs; send turns into stop while Jarvis works; works on phones.
- **Plan mode** (PLAN button, Telegram `/plan`): Jarvis (thinking only if *Think* is on) only uses read-only tools and
  presents a step-by-step plan to run, change (revised by the AI from your feedback) or discard; a short spoken
  "yes"/"no" decides too; after running a plan, plan mode switches off. The approved plan stays pinned to the request while it runs, so it never drops out of a tight context. Chat Markdown now renders headings and numbered lists.
- **Understand the screen and images:** "What's that error message?" – Orbwise takes a screenshot (spectacle,
  gnome-screenshot, grim, maim, … or `vision.screenshot_command`), asks a local vision model (`vision.model`,
  default `qwen2.5vl:7b`, Ollama or OpenAI-compatible) and answers; also for image files and photos from Telegram.
  Screenshots are deleted afterwards, screen content counts as untrusted (like e-mails), an eye symbol shows at the
  orb, `orbwise doctor` checks the model and the screenshot program.

### Fixed
- The chat list in the sidebar stayed empty after starting Orbwise until *New chat* was clicked.
- The context tile showed a moving limit (e.g. "9k/8k", later "11k/9k"): it divided an estimate by a budget that
  changed with the thinking reserve and the learned token estimate. It now shows real tokens (from the model server
  when known) against the fixed context window of the model; reserve and the compaction point are in the tooltip.
- **Long tasks no longer die at a full context window** (e.g. "16k of 16k – increase the window" and the work was
  gone): Orbwise compacts the context and carries on (see *Context handling* above). If the server still refuses,
  finished steps are kept and "continue" picks up there. Thinking keeps more room free; clearer error message.
- A message with many tool calls showed a wall of `run_shell` chips; now only the current call is shown, with a
  "+N earlier" link that opens the activity panel.
- Installer, `bootstrap.sh` and all links point to the renamed repository `ressjo/orbwise-linux-agent` (they pointed to
  a non-existent `ressjo/orbwise`, so fresh installs failed at `git clone`).

## [0.1.0] – 2026-09-28

First public release.

> The project was developed as **Jarvis** and renamed to **Orbwise** for this release. The assistant's default
> persona is still "Jarvis" ("Hey Jarvis"). Earlier installations are migrated automatically; the old `jarvis`
> command and `JARVIS_*` environment variables keep working as aliases and will be removed in 0.2.

### Assistant
- Local LLM via **Ollama** (Qwen 3 recommended) or any **OpenAI-compatible server** (llama.cpp `llama-server`,
  e.g. Bonsai 27B); switchable **model profiles**, Orbwise can start/stop the model server itself.
- Native tool calling with a **confirmation step** for anything that changes the system, a blocklist for
  destructive commands and root access through a **password field in the dashboard** (`sudo -A`).
- **Thinking mode** button: the model's reasoning is shown live inside the orb.
- Step limit with loop detection and a "continue" summary instead of a hard stop.
- German and English (`language: de|en`) – prompt, voice, speech recognition, UI and CLI.

### Voice
- Wake word "Hey Jarvis" (openWakeWord), push-to-talk, faster-whisper speech recognition.
- Piper text-to-speech with installable German and English voices and an optional "Jarvis" voice effect.
- Voices are managed in the menu of the **VOICE** pill (like the models): select, preview, delete, add from the
  catalogue, plus the Jarvis effect – the separate VOICE tab is gone. "+ ADD VOICE" offers every official Piper
  voice of the language (catalogue cached for a day, search field, grouped by region, download size); own Piper
  voices (e.g. from Hugging Face) can be uploaded there (.onnx + .onnx.json, file dialog or drag & drop). The space bar no longer starts push-to-talk
  while typing in any input field (e.g. the planner).
- Commands are not read aloud: a confirmation only asks "Do you want to run the following command?" (the command is
  shown in the dialog), and command-like inline code in answers is skipped when speaking.

### Memory
- Persistent memory as readable Markdown files (daily journal, daily summaries, facts) plus a hybrid
  full-text/embedding index – no context-window overflow.
- **Chat history**: several chats, continue old ones, star them, delete them completely (including memory).
- Context budget follows the model's real context window; long tool results are trimmed instead of failing.
- The search index repairs itself when damaged ("database disk image is malformed"): the full-text index is rebuilt,
  a broken file is set aside and refilled from the Markdown memory files in the background; deleting chats never
  fails because of it, `orbwise reindex` works on a broken file and `orbwise doctor` checks the index.
- Cache-friendly prompts: time and retrieved memories are placed in front of the new message instead of the system
  prompt, so llama.cpp/Ollama reuse their prompt cache; old long tool results are aged to excerpts, condensing starts
  at ~85 % of the budget, and the token estimate calibrates itself from the server's real counts.

### Tools
- System: updates, install/remove/search packages (**pacman/AUR and apt**), system info, shell commands.
- System & network: processes, systemd services and logs, network info, ping, open ports, port check,
  disk usage and clean-up, power (shutdown, reboot, suspend, lock).
- Files & apps: find and open files (also on a NAS), read/write text files, launch applications, websites.
- Web search (Brave Search API, your own SearXNG or ddgs) and page fetching, weather (Open-Meteo), reminders
  and timers, a morning briefing with selectable items and order (weather, events and deadlines of the next
  days, Paperless inbox, news, updates, storage) – configurable in the config or the dashboard's PLANNER tab.
- Integrations: **Home Assistant**, **Paperless-ngx** (search, ask, open documents; suggest and apply
  correspondent, type, tags, title and date – also for many documents at once, after one confirmation),
  **Obsidian** vaults, **Trilium** notes and a **CalDAV/iCloud** calendar.
- **Routines**: tasks run automatically at set times (daily, weekdays or once), e.g. a web search every morning –
  created by voice or in the new **PLANNER** tab (which also holds reminders and the briefing settings); each
  routine writes into its own chat, confirmations are asked in the dashboard or declined when nobody is there.
- **Telegram bot** (optional): chat with Orbwise from the phone (text or voice messages, own "📱 Telegram" chat),
  every reminder is also sent to the phone, actions that need confirmation get ✅/❌ buttons; only your own chat ID
  is served, no open port needed (long polling). Files in both directions: PDFs, documents and photos sent to the
  bot are saved (and can go straight into Paperless), and Orbwise can send local files or Paperless documents to the
  phone (never keys or password stores). `/stop` (or "stop") from the phone cancels what is running – there and on
  the PC. The bot token is kept out of logs, error messages and the dashboard; once `telegram.chat_id` is set,
  messages from other chats are ignored silently.
- **Secret files are protected**: `read_file`, sending to the phone and uploading to Paperless refuse SSH/GPG keys,
  password stores, browser logins, cloud credentials and Orbwise's own config (also through symlinks); shell
  commands that read such files, `printenv`/`env`, token variables or Wi-Fi passwords (`nmcli -s`) need confirmation.
- Orbwise refuses to listen on a non-local address unless `allow_remote: true` is set (the dashboard has no login).
- Confirmation reasons are shown in English when `language: en`.
- README: notice that Orbwise was built with AI help, extended disclaimer, complete feature list, new screenshots
  (`scripts/screenshots.mjs` regenerates them); `config.example.yaml` now lists every option (secrets left empty).
- **E-mail** via IMAP – **Proton Mail through the Proton Mail Bridge** or any other mailbox: list unread mails,
  search, read (without marking as read), ask about a mail, archive/move/label/trash and PDF attachments to
  Paperless after confirmation, unread mails in the briefing. Mail content is passed to the model as untrusted
  data; after reading a mail, all further actions in that request need confirmation. Optional **sending**
  (`mail.send_enabled`, SMTP – Proton Bridge defaults): Orbwise drafts mails and replies, a dialog shows To, Cc,
  Subject and Text as editable fields and only a click on SEND sends; replies keep the thread.
- Small context windows get only the tool groups that match the request; `tools.disabled` switches tools off.

### Web UI
- Animated neural-network orb, live telemetry (tokens/s, context, GPU, VRAM, RAM, power), activity log,
  memory browser, voice settings – optimised for smooth rendering in Firefox.
- Orb: start-up sequence (rings assemble, network ignites), shock-wave impulses on state changes (wake word, answer,
  tool, error), the voice shapes the main ring while speaking/listening, particles flow in/out, and satellites on
  the ring show which tool or routine is running (larger, visible for at least 1.5 s; a tool call no longer ends the
  thinking zoom); the side panel is narrower, the orb wider.
  Satellites live on their own layer that does not zoom, so in thinking mode they circle the thought view at full
  brightness; a light beam shoots from the core to each tool and the result flows back (green ok, red error).
  While thinking, impulses run from the core outwards through the network – faster with every token, calmer when
  the stream stalls. A thin context ring shows how full the context is (orange from 85 %, flashes when condensed).
  Actions get symbols: a terminal for shell/packages/system tools, a cloud for web search, an envelope for mail and a
  document with a scan bar for Paperless, with light dots flowing
  from the core into the symbol; memory actions make the neurons in the core glow gold instead. HUD details: message glide-in, panel corners light up on
  activity and a copy button on code blocks – all within the same frame budget.
- The status returns to "ready" as soon as the answer is complete (condensing runs silently afterwards), after the
  model has loaded or failed to load, and after reconnecting – no more stuck "thinking". The dashboard switches to
  "ready" by itself when the answer is complete, a cancelled transcription no longer blocks the microphone, and the
  browser always revalidates the UI files. The page loads its scripts with a content version, so after an update the
  browser can no longer mix new and cached old files (which hid tools in chat, activity and orb).

### Installation
- Installer for Arch-based and Debian/Ubuntu-based systems that asks for the language, the GPU (NVIDIA/AMD/CPU,
  pre-selected from detection, ROCm override for RX 6600/6700/7600) and the model from a VRAM-aware preset list.
- Add models later with `orbwise model add` or **+ ADD MODEL** in the web UI (download progress, fit marks); running
  downloads can be cancelled in the model menu, and added models (including Bonsai) can be deleted there with 🗑 –
  the model files are removed and the freed size is shown.
- **Bonsai 2 27B** is set up automatically when chosen in the installer or with `orbwise model add bonsai`
  (Bonsai-demo checkout, llama.cpp binaries, model, GPU-specific server profile). On NVIDIA the CUDA runtime
  libraries are fetched automatically when the system has none.
- Web search via the official **Brave Search API** (key asked for by the installer) – no scraping of result
  pages and no bot blocking; SearXNG and ddgs remain as alternatives.
- `orbwise doctor`, `orbwise update`.

### Project
- MIT license, security policy, disclaimer and an overview of third-party components and their licenses.
