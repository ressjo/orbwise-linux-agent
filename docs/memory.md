# Memory

← [Back to the README](../README.md)

## Memory – without context-window problems

Everything is stored as readable files in `~/.local/share/orbwise/memory/`:

| File | Content |
|---|---|
| `journal/2026-09-27.md` | Complete log of the day – every question, answer and tool call |
| `summaries/2026-09-27.md` | Daily summary, generated automatically (day change or 15 min idle) – once per day, again only if the day's content changes (e.g. a chat was deleted) |
| `facts.md` | Permanent facts ("The NAS is mounted at /mnt/nas") – editable by hand |
| `chats/<id>.json` | One chat: all messages, the summary of each compacted part, title, star (`chats/active` = current chat) |
| `projects/<id>/` | Own memory of one coding project: `facts.md`, `journal/`, `project.txt` (the folder) |
| `index.sqlite` | Search index (full text + embeddings) – just a cache, rebuild with `orbwise reindex` |

**Coding has its own memory – one per project folder.** In a coding chat `remember`, `forget` and `recall` only see
that project: its facts (also in the system prompt) and the earlier coding chats about it. Regular (tools) chats never
see them, and coding chats do not see your general facts or other projects. Coding conversations are not part of the
daily summaries. The *Memory* sheet shows the memory of the chat that is open. On the first start of this version the
search entries of earlier coding chats move to their project; facts stored so far stay in the general memory.

<details>
<summary><b>How the context stays small</b> – prompt cache, two stages by window size, what happens when it is full</summary>

**How the context stays small – and fast:** Orbwise handles the context like Claude Code. Between two compactions
the prompt **only grows at the end**: instructions, tools, earlier messages and the note in front of each of your
questions (date and time, memories, the approved plan) stay byte for byte the same once they were sent. llama.cpp
(llama-server) therefore reuses what they already processed and only read the new part – a slow graphics card does
not have to re-read the conversation for every question or tool step. The *Context* tile's tooltip shows how many tokens
came from the cache.

**Two stages – below 16k and from 16k:** how much room each part gets depends on the model's context window
(`orbwise/context_plan.py`; the *Context* tile's tooltip shows the stage).

| | below 16k (e.g. 8k on an 8 GB card): **lean mode** | from 16k |
|---|---|---|
| Tools up front | 9 core tools (~1.5k tokens): shell, read/find/list files, web search, recall/remember, date_info, load_tools – everything else by keyword or `load_tools` | core groups (~4k tokens) plus matching groups; from 32k all tools if they fit (see [Models & profiles](models.md#models--profiles)) |
| Big groups (Paperless, mail, calendar, system, notes, packages) | reading part first; the changing part (sort, apply, send, add events …) only when the request sounds like it | whole group |
| One tool result | ~10 % of the window (8k: ~2,500 characters, the rest in a file / by `offset`) | ~15 %, up to the configured `max_output_chars` / `max_chars` |
| Memories / facts | ~6 % / ~5 % of the window | up to 1,500 / 1,200 tokens |
| Room for the answer | 1,024 tokens; thinking capped at 20 % of the window (also "thorough") | 1,500; with thinking up to 3,000 |
| Summary | short (4 sections, ≤ ~900 tokens) | structured (9 sections, ~10 % of the window) |

Details on lean mode: [Small context windows](models.md#models--profiles).

- **Memories** (hybrid search: BM25 full text + bge-m3 embeddings, slight preference for recent entries): the first
  question of a chat gets up to `memory.retrieval_max_tokens` (lean mode: less, see above), later questions a third of
  that and only what was not shown yet. Coding mode adds none by itself – there the model calls `recall` when it needs
  something.
- Only memories that really match the question are shown (`memory.retrieval_min_similarity`, default 0.5; without
  embeddings at least two shared words); facts are not repeated there because they are in the instructions anyway.
- **Facts** stay consistent: if `remember` gets a newer version of a known fact ("the NAS is at /media/nas" after
  "/mnt/nas"), the old line in `facts.md` is replaced instead of keeping both.
- **Facts** are a snapshot: `remember` saves right away and the model knows the new fact from the tool result, but it
  moves into the instructions only with the next compaction (otherwise the whole prompt would be re-read).
- **Tools:** if all tool descriptions fit comfortably, all of them are always sent (from 32k: ≤ 25 % of the budget and
  under a minute to read). Otherwise see [Models & profiles](models.md#models--profiles). Instructions for a tool are only in
  the prompt while the tool is loaded.
- **Long tool output is limited at the source** (see the table). `read_file` reads big files page by
  page (`offset`/`limit`: "lines 1–300 of 1,240 – continue with offset=301") instead of silently cutting the middle;
  Paperless, mail, Obsidian and Trilium read long documents section by section; long shell, log and web output keeps
  its start and end, the full text is saved in `~/.cache/orbwise/outputs/` (only readable by you, the last 100 are
  kept) and the model can read the rest from there. Anything still too long is cut and saved the same way.

**What happens when the context is full?** Nothing is lost, and it happens rarely – like *microcompact* and
*auto-compact* in Claude Code:

1. Only when a request would come close to the model's window (8k: from ~7.2k tokens, 88 %; 16k: ~14.9k, 91 %; with
   thinking earlier, because the reasoning needs room; 32k: ~30.4k) – the next step still needs room for its answer –
   Orbwise first **hides old tool results**: each becomes a short placeholder with its start and the file that holds
   it completely ("older result hidden … complete in ~/.cache/orbwise/outputs/… (read it with read_file if needed)");
   long arguments of old calls (e.g. a whole file for `write_file` / `write`) are shortened too. The newest results stay (8k:
   ~12 % of the window, at least the last step; from 16k ~25 %). This needs no model call – the model server only
   re-reads from the first hidden result. The activity panel shows *made room · 6 old tool results hidden*. In a
   simulated task with 24 big files this replaced most summaries (8k: 4 instead of 23, 16k: 1 instead of 6) and cut
   the re-read tokens to a fifth (8k) or two thirds (16k).
2. Only if that does not free enough (e.g. a long conversation without tool results), Orbwise lets the model write a
   **summary**: from 16k structured (the request, important facts and values, files and commands, errors and fixes,
   decisions, all your messages, what is still open, the current work and the next step), in lean mode short (request,
   key values word for word, done, open and next step). The request for it is the current prompt plus one
   instruction, so the model server reads almost nothing new. Orbwise itself appends your recent messages, the files
   that were touched and the open task list, so they do not depend on the model; in Coding mode from 16k also the
   current content of the files worked on last (fresh from disk, so they need not be read again).
3. The chat goes on with a fresh context: your current question carries the summary in its note, followed by the last
   step word for word. **Instructions, facts and tools stay exactly the same** (from 16k the tool selection is kept),
   so the model server keeps them in its cache and only reads the summary and the last step. In the middle of a task
   Orbwise simply carries on.
4. If an answer is finished and the prompt is so close to that point that even a short next question would cross it,
   Orbwise makes room right away while you read (`memory.compact_idle`, on by default) and warms the cache up again –
   the next question starts immediately.
5. Nothing is deleted: the chat keeps every message (a divider *Context summarised* with the summary to expand marks
   the spot), journal and search index keep everything, and the search brings details from the compacted part back
   when they are relevant.
6. **By hand:** type `/compact` (or `/komprimieren`), optionally with a focus – `/compact keep the error messages` – or
   click the *Context* tile. This always writes a summary.
7. If the model server still refuses a prompt as too large, Orbwise hides old results (if that covers the overflow) or
   compacts and continues; if even that does not fit,
   the finished steps stay in the chat and "say *continue*" picks up there. With thinking on, more room is kept free
   for the reasoning.

**Pre-reading the chat:** Telegram, routines and daily summaries use the same model server and overwrite its cache.
Afterwards – and after switching chat, mode or model – Orbwise reads the current chat in the background as soon as
nothing else runs, and again when you start typing or recording if needed. A quiet note above the activity list
shows it (*chat pre-read · 4,716 tokens*); the next question then only reads its new part. See also the
[prompt cache tips](models.md#prompt-cache-tips).

</details>

### Chat history

The **sidebar** lists all chats (Pinned, Today, Yesterday, …) with search across titles and content.
**New chat** starts a new one; the old one is kept. Click a chat to open and continue it – Orbwise still remembers
everything from the other chats.

- The **⋯ menu** of a chat: *Rename*, *Pin*, *Export as Markdown*, *Delete*. Clicking the title at the top renames
  it too (otherwise it is taken from the first question). Nothing is deleted automatically.
- **In the chat:** copy any message, edit your own message and send it again, or regenerate the last answer.
- **Delete** removes a chat **completely**: from the list, the journal and the search index; affected daily
  summaries are rebuilt from the rest. Learned facts are kept (delete them under **Memory** or say "forget …").

### Private chat

The ghost button next to **New chat** starts a private chat (Tools mode). It lives in RAM only: no chat file, no
journal entry, nothing in the search index or the day summaries, and `remember` refuses to store facts. Orbwise can
still *read* its memory there. The chat is gone as soon as you leave it – another chat, **New chat**, the ghost
button again, a mode switch or quitting Orbwise; images and PDFs attached only to it and cached tool outputs are
deleted with it. It is not in the chat list and cannot be exported, renamed or restored.

What it does not hide: what a tool did on your system (files written, shell commands and their history, web
requests), the log of the model server, and RAM that the OS may swap to disk.

## Telemetry

The activity panel shows (every 2 s, with a sparkline): **Token/s** (generation speed), **Context** (prompt
size vs. the model's context window, with a breakdown and the compaction point in the tooltip – click it to compact),
**GPU** load and temperature, **VRAM**, **RAM**/CPU, **Power** draw and – with a Brave key – the **Brave search**
quota as reported by the API. NVIDIA is read via `nvidia-smi`, AMD directly
from the `amdgpu` driver.

**Context in memory** shows how big the model's KV cache is and where it lives – green in VRAM, orange in RAM (slow) –
plus a recommendation: "up to ~32k possible" when VRAM is free, or "better 12k" when parts already spill into RAM.
For a llama-server started by Orbwise the numbers come straight from its log; otherwise they are estimated
from the model architecture. The context window itself is set per model on the **Models** page (see
[Models & profiles](models.md#models--profiles)).
