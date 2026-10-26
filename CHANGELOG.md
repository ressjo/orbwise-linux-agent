# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Changed
- **Prompts, two targeted rules from observed mistakes:** coding mode changes existing files with `edit` instead of
  rewriting them (whole rewrites caused the cut-off tool calls); in both modes a failing tool is reported with its
  own message, without inventing a cause.
- **Room for output in every mode and window:** a quarter of the context window (at most 16k tokens) stays free for
  the answer – tools and coding mode, small and large windows (8k: 2k, 49k: ~12k, 128k+: 16k). Compaction starts a
  bit earlier, long answers and whole files no longer break off mid-call.
- **Start:** Orbwise always starts with an empty chat in tools mode (an empty one is reused, earlier chats stay in
  the list); after the model has loaded only the system prompt and tools are pre-read, no old chat. The image view is
  only restored on a page reload within the same server run.
- **Sandbox in auto mode:** everything inside the sandbox runs without asking – also `sudo`/`apt` (root in the
  container only) and shell commands after reading a web page. In return, reading files outside the working copy asks
  (nothing private gets into the container); writing outside the copy and GitHub pushes still ask.
- **Fewer tools by default:** 15 thin wrappers around single commands are off by default – `top_processes`,
  `kill_process`, `service_status`/`_control`/`_logs`, `network_info`, `ping_host`, `open_ports`, `check_port`,
  `disk_usage`, `list_directory`, `search_file_contents`, `ssh_connect`, `ssh_disconnect`, `list_installed_apps`. The
  model does the same with `run_shell` (`ps`, `systemctl`, `journalctl`, `ss`, `df` … – read-only commands still run
  without asking, changing ones still ask), guided by a short hint. The basic tool set shrinks from about 6.0k to
  4.4k estimated tokens; `tools.extra: [sysadmin]` (or single names) brings them back.
- **Leaner tool descriptions:** the tools-mode tool list is about 22 % smaller (≈16.4k → ≈12.7k estimated tokens with
  all integrations): shorter descriptions, no descriptions for self-explanatory parameters, repeated phrases unified.
  Names, types, required parameters and the rules the model must follow (confirmations, untrusted mail content,
  `power` instead of the shell …) are unchanged.
- **ROCm | Vulkan switch** (CUDA | Vulkan on NVIDIA) on the llama.cpp line of the model page: moves all llama.cpp
  models to the other build of the installed version, downloading it first if needed, and restarts a loaded model.
- **Thinking levels strictly from the template:** only levels the chat template itself compares or lists (at least
  two) are offered, under the model's own names (`low`, `xhigh` …) with the template lines as evidence in the menu.
  No built-in levels or names any more (also no low/medium/high for gpt-oss); templates that merely mention
  `reasoning_effort` count as on/off.
- **llama.cpp updates on the model page:** the llama.cpp line shows when a newer build exists ("Update to b…",
  checked at most every 3 hours, offline the last known state), a loaded model restarts with the new build right
  away, and the ROCm build matching the installed ROCm version is chosen when a release has several. The installed
  ROCm version is shown next to it with a link to AMD's guide when a newer one is out – Orbwise does not update ROCm
  itself (system package with drivers). `orbwise doctor` shows ROCm and warns when the build targets another major.
- **Thinking levels come from the model:** models with their own levels (`reasoning_effort` in the chat template)
  show exactly those levels in the Think menu and settings, with the model's default marked; models with only on/off
  (or unknown) show Off / On. Orbwise's own level budgets (~500 / ~2,000 tokens with a cut-off and a second pass
  without thinking) and the "think briefly" hints are gone – the model decides how long it reasons at its level. The
  cap in small context windows (< 16k) stays.

### Removed
- **Separate vision model:** screen and image questions (`look_at_screen`, `look_at_image`) are now answered by the
  loaded model when it can see images (llama.cpp with `mmproj`, Ollama with vision); without image support the tools
  are not offered. `vision.backend`, `base_url`, `model`, `api_key` and `keep_alive` are ignored; `vision.enabled`,
  `timeout` and `screenshot_command` stay.
- **Learning from experience:** lessons, the idle reflection and the thumbs up/down under answers are gone – with
  local models they rarely helped, and reflecting while idle pushed the chat out of the prompt cache (the next
  question re-read everything). Stored lessons (`lessons.json`) are deleted on the first start; old
  `memory.learning` / `memory.lessons_max` settings are ignored.
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
- **Coding permissions:** from the auto level *Read + edit files* on, files inside the project folder are changed without
  asking (also outside `$HOME` and in hidden folders like `.github`; never `.git/`, credentials or as root); files
  outside the project ask.
- **Activity panel:** steps are grouped under the question (or routine / Telegram run) that caused them, with readable
  names and an icon ("Web search · linux 6.18 release notes" instead of `web_search query=…`), a spinner while running
  and a check mark with the duration or a cross when done. Clicking a step shows the raw call and its output. Reading
  the chat into the prompt cache is now a single quiet note above the list instead of a row per question.
- **Redesign:** a warm, calm look in **light, dark or system** (Settings → General → Appearance) with a petrol
  accent; headings and answers in a serif reading font (Newsreader), controls in Inter – both bundled under the SIL
  Open Font License. The composer has the text on top and the switches below; the user's message sits in a soft bubble,
  the answer reads as plain text. The orb is now three fine rings around a dot (same states, running tools as dots, the
  context as an outer arc) instead of the glowing neural network. **Planner and Memory open as large windows** like the
  model page, with their cards side by side.
- **Confirmations in the chat:** a request for approval now appears as a card below the last message (title, command,
  reason, Cancel/Run; Enter/Esc and "yes"/"no" by voice as before; mail fields editable in the card) instead of a
  dialog over the whole page. The sidebar shows the product name **Orbwise**.
- **Think menu follows the model:** levels (Brief/Normal/Thorough) only for models with real levels (gpt-oss), plain
  on/off for the others, greyed out for models that cannot think – read from the chat template or Ollama's
  capabilities. "On" no longer invents a level or a token budget.
- **One setting for all windows:** the server keeps *Think* and the auto mode and sends them to every window (phone
  and PC no longer overwrite each other silently); Telegram and routines use the same values.
- **Calmer interface:** one scale for font sizes, corner radii and colours; normal capitalisation instead of
  ALL-CAPS buttons and tags; SVG icons instead of emoji; less glow and shorter help texts; monospace only for code,
  paths and numbers.
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
- **Leaving a sandbox chat asks first:** with changes not yet applied, a new chat, another chat, a mode switch, a
  project change, cloning or deleting the chat offers *Apply and continue*, *Continue without applying* (they stay in
  that chat) or *Cancel*; deleting a chat with a sandbox warns that the working copy goes with it.
- **Docker in the sandbox (opt-in per chat, rootless Podman only):** build and run Docker projects inside the
  sandbox – nested rootless Podman as an unprivileged user behind `docker`/`docker compose`, with only `/dev/fuse` and
  `/dev/net/tun` added (no `--privileged`, no `CAP_SYS_ADMIN`, `no-new-privileges` kept); a self-test runs on
  switching it on and falls back to a bundled seccomp profile only if needed. Published ports work with `screenshot`.
  Image `orbwise-sandbox:3`; older sandboxes switch to it when Docker is turned on.
- **Screenshots in coding mode:** new tool `screenshot` renders a page (dev server on localhost, HTML file) with
  headless Chromium – inside the sandbox container (image `orbwise-sandbox:2` now ships Chromium) or with the
  browser on the machine – and shows it under the answer, also after reloading; models that see images get a
  description back. Screenshots of foreign web pages count as untrusted content.
- **Clone from GitHub with a branch:** click a repo to pick its branch; `github_clone` takes `branch`, an existing
  folder is switched to it.
- **Skills:** every integration and built-in ability can be set up and switched without editing `config.yaml` –
  in the installer (pick by number, enter details, connection tested right away), under **Settings → Skills** in the
  web UI (state, on/off switch, form with help per field, *Test connection*, changes apply at once; Telegram finds
  its chat ID after `/start`) and with `orbwise skills [setup|enable|disable]`. The installer's Brave question is
  now part of the skill selection.
- **Credentials in the keyring:** tokens, passwords and API keys go to the session keyring (Secret Service via the
  new `keyring` dependency), without one to `~/.config/orbwise/secrets.json` (0600). Plain-text credentials in
  `config.yaml` are moved there on start (backup `config.yaml.bak`, comments kept). `orbwise doctor` shows where they
  live and warns about leftovers. There is no `.env`; `$ORBWISE_…` variables still work as a fallback.
- **Coding sandbox (like Claude on the web):** a working copy of the project on its own branch, the agent's
  commands run in a Podman/Docker container (no credentials inside, limits, network on/off per chat, stops when
  idle). Shell and file changes in the copy run without asking; **View changes** with diffs, **Apply to project**
  with conflict check, **End sandbox**. Default image `orbwise-sandbox:1` is built on first use (`sandbox.*`).
- **Tables in answers:** Markdown tables (with column alignment) are shown as real tables; also *italic*,
  ~~strikethrough~~, quotes and horizontal rules.
- **No more browser pop-ups:** all questions (delete, confirm, feedback text, project folder) appear as Orbwise's
  own dialog; the project folder dialog lets you click through the folders.
- **Project chip shows repo and branch:** `owner/repo` and the current branch (in the sandbox the `orbwise/…` branch
  of the working copy), updated after every answer.
- **GitHub:** sign in via `gh` or a token; clone your repos from the project chip, push the branch (sandbox menu or
  `github_push`, always asks) with a link to create the pull request. The token never lands in `.git/config`.
- **Coding mode like OpenCode 2:** the coding agent gets its own tools – `read` (line numbers, folders, PDFs, nested
  `AGENTS.md`), `edit` (exact replace that forgives curly quotes, dashes and wrong indentation, strips copied line
  numbers, keeps CRLF, refuses stale files, returns a diff), `write`, `grep` (ripgrep with matching lines), `glob`,
  `shell` (`workdir`, `background` jobs with a note when they finish and a Stop button), `job_output`, `websearch`
  (Brave API first) and `webfetch` (Markdown) – plus **subagents** (`explore` read-only in its own context, `general`
  for sub-tasks; steps shown indented in the activity panel) and **`question`** (answer buttons in the chat). The
  prompt follows OpenCode's way of working; the project's `AGENTS.md`/`CLAUDE.md`, the folder, git and shell go into
  it. The tools mode keeps its tools.
- **Undo per answer** in coding mode: snapshots of the project in a separate git directory before/after each answer;
  "N files changed +a −d" under the answer with **Undo**/**Restore** (`coding.snapshots`).
- **Diffs in confirmations** for `edit`/`write` instead of raw JSON.
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
- **Brave quota in the activity panel:** the quota Brave reports with every answer
  (`X-RateLimit-*`) is kept – Orbwise does not count itself; a tile shows used / limit / remaining / reset date (orange from 80 %, a notice below
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
- **Sandbox asked for every file in Next.js-style projects:** paths with `[ ]`/`{ }` (`app/forms/[id]/page.tsx`)
  counted as glob patterns and therefore as “maybe outside”; file tools now check them literally (also in the auto
  modes without sandbox). A file really outside the working copy says so in the prompt.
- **A cut-off tool call broke the chat for good:** the truncated arguments were stored as a raw string and sent back
  with every request, so llama-server answered 500 until a new chat was started. Truncated arguments never enter the
  history any more – a cut-off `write` is saved up to its last complete line and the model continues with
  `append=true`, other calls are retried; arguments that are not valid JSON (older chats) are sent as `{}`, so
  affected chats work again. Coding mode keeps a quarter of the window (up to 16k tokens) free for output, so a
  whole file fits more often.
- Cut-off tool calls: errors llama-server sends as `error:` lines in the stream are recognised too, and the retry
  always makes room first.
- A tool call cut off mid-way (e.g. a huge file in one `write`, until the output/context limit) ended the answer with
  llama-server's 500 “Failed to parse tool call arguments”: Orbwise now retries the step once with a hint – only for
  that request, not stored – to write large files in parts; `write` got `append=true` for that, and the coding
  prompt suggests it for files over ~250 lines. A second failure shows a clear message instead of the raw error.
- Chat did not always follow while generating (e.g. when the thinking block opened): every change in the chat now
  keeps it at the bottom unless you scroll up yourself (scrolling back down follows again); following scrolls
  instantly instead of the CSS smooth animation, which lagged behind and broke off while streaming.
- Building the sandbox image failed when the host had no usable `/dev/net/tun` (pasta: “No such device”): the build
  now uses the host network (only apt-get for the own Containerfile), and errors naming `/dev/net/tun` explain the
  usual cause (kernel update without reboot → reboot or `sudo modprobe tun`; meanwhile “Network: off” works).
- `screenshot` reported only “no image”, so the model guessed a reason (e.g. TUN from an earlier Docker attempt):
  it now checks first whether a server answers at the URL (otherwise: start it with `background=true`, or pass the
  HTML file path), passes on Chromium's own error, and avoids the common renderer crash in containers
  (`--disable-dev-shm-usage`); a refused connection no longer yields a screenshot of Chrome's error page.
- Sandbox with Podman installed showed Docker's “failed to connect to the docker API”: when Podman is not usable its
  reason now comes first, with a fix for common causes (missing subuid ranges, timeout on first start, missing
  `XDG_RUNTIME_DIR` when run as a service – now set automatically); `podman info` may take up to 60 s, failures are
  only cached for 15 s.
- **Sandbox escape via git:** Orbwise ran git on the host inside the working copy's `.git`, which the container can
  write – a hook or `core.fsmonitor` placed by the agent would have run on the host (with the GitHub token in the
  environment during a push), and a rewritten `origin` would have received the token. Commits now run in the
  container, pushes go through a fresh repository to the remote remembered when the copy was made, branch and repo
  are read without git. Host writes into the copy no longer follow a symlink swapped in by the container.
- Auto mode no longer asks for `ps -o user` / `ps -C name` (option values were taken for the BSD `e` modifier) and
  for reading `/etc/passwd` (world-readable, no passwords).
- **Read-only shell commands with awk asked for confirmation:** an awk program like `'{print $2}'` was checked as
  if it were a file name (and flagged as "reads credentials"), and awk had no read-only rule. Programs and search
  patterns of awk, grep, rg and sed are no longer treated as files, and awk without `system()`, pipes, output
  redirection, `getline` or program files counts as read-only.
- **Prewarming while typing was wasted with thinking on:** it always warmed up the prompt *without* thinking, but chat
  templates render a different prompt start with thinking enabled – the question afterwards read everything again
  (a new chat: ~14k tokens of system prompt and tools). Prewarming now uses the thinking mode (and level) of the next
  question; toggling the thinking button warms up the new variant, and the activity names "thinking mode changed"
  as the reason instead of "server".
- **Long chats fell out of the model server's cache:** the RAM prompt cache (`--cache-ram`) was capped at 4 GB, and
  llama-server does not store a chat state larger than that – after a daily summary, a Telegram message or a chat
  switch a 40–60k-token chat on a 27B model was read in full again. The cache is now sized for about two full chats
  of the loaded model (at most 25 % of RAM, never less than before). After daily summaries the open chat is read in
  again even if a summary failed or came out empty.
- **Daily summaries no longer repeat:** whether a day needs a (new) summary now depends on the journal's content,
  not its file time. A journal with a file time in the future (backup, sync) made Orbwise summarise the same day
  every 5 minutes, each time pushing the chat out of the prompt cache.
- **Auto mode as root:** when Orbwise runs as root, *Read + edit files* and *Auto* no longer approve system changes as
  "without root" – they ask, and `orbwise doctor` warns.
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
