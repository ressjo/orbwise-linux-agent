# Coding

← [Back to the README](../README.md)

## Coding mode

Switch to **Coding** top left and pick a **project folder** (chip at the top). The coding agent works like
[OpenCode 2](https://github.com/anomalyco/opencode): it reads before it claims, changes files with small exact edits,
verifies with the project's tests/linters/build and answers briefly with `path:line` references. Web search runs over
the **Brave Search API** when a key is set (see [Web search](integrations.md#web-search)).

<details>
<summary>The coding tools</summary>

| Tool | What it does |
|---|---|
| `read` | File with line numbers (`12: …`), in sections with `offset`/`limit`; folders (one entry per line, `/` for folders); PDFs. An `AGENTS.md` in a sub-folder comes along once |
| `edit` | Replaces exact text (`oldString` → `newString`, `replaceAll` for renames). Forgives curly quotes/dashes and wrong indentation as long as the spot is unique, strips line numbers copied from `read`, keeps CRLF; refuses when the file changed on disk since it was read, and an existing file that was never read in this chat. Several spots in one file in one call with `edits: [{oldString, newString}, …]` (at most 50) – applied in order, all or nothing. After three failed edits on the same file it tells the model to re-read the section or rewrite it with `write` instead of guessing. Result: a short diff |
| `write` | Creates or overwrites a file (folders are created); an existing file must have been read in this chat before it is replaced |
| `map` | Repo map: classes, functions and methods per file with line and signature – the whole project (large ones as folders with counts), a folder (`path`) or the definitions of a name (`query`). Uses universal-ctags if installed, otherwise Python's `ast` and patterns for JS/TS, Go, Rust, Java/Kotlin/C#, C/C++, Ruby, PHP; cached per project |
| `grep` / `glob` | ripgrep with regex or literal text, matching lines with numbers – with `context: N` also the N lines before and after each match / file paths by pattern, newest first – both respect `.gitignore` |
| `shell` | Bash in the project (or `workdir`); `background: true` for dev servers and long builds – the agent is told when the job ends (no polling), the activity panel shows its live output with **Stop** |
| `websearch` / `webfetch` | Brave API (then SearXNG/DuckDuckGo as configured) / page as Markdown |
| `subagent` | `explore`: searches the code read-only in its **own context** and returns only the result – saves context on small windows. `general`: a self-contained sub-task, same confirmations. Its steps appear indented in the activity panel; when its context fills up, older results are shortened and it summarises instead of failing |
| `question` | Asks you with answer buttons (or free text) when something is genuinely unclear |
| `job_output` | Status and last output of a background job, or stop it |
| `github_clone` / `github_push` | Clone a repo as the project / push the current branch with a link to create the pull request – both always ask (only with a GitHub token) |

Also: `todo_write` (task list in the answer), `remember`/`recall`/`forget` (the project's own memory).

</details>

- **Automatic checks before "done":** when an answer changed files, Orbwise runs the project's checks before the
  answer ends – the command from `check:` in the project's `.orbwise.yaml` (one command, a list, or `name: command`
  pairs), otherwise `coding.verify_command`, otherwise detected: `ruff` on changed Python files, `pytest` when there are
  tests (both from the project's `.venv`/`venv` if there is one) – first the test files that belong to the changed files, the whole suite only when those are green; Poetry projects run through `poetry run` – `npm run lint/typecheck/test` (with `node_modules`; vitest and jest run the tests related to the changed files first), `go vet`/`go test` (changed packages first), `cargo check`, `make check`/`test`.
  They run like any shell command (sandbox, cage without network, confirmation per auto level). Failures go back to
  the model with the output ("fix the cause, don't weaken tests"), up to `coding.verify_rounds` (3) times; the answer
  then ends with a line from Orbwise itself – **✓ Checked: ruff, pytest** or **⚠ Checks still failing: pytest (2
  failed)** – or **○ Not checked – not installed here: pytest** when the tools are missing. Declining the check skips it for that answer; `coding.verify: off` turns it off.
- **Syntax check after every change:** `edit`/`write` parse Python, JSON, YAML, TOML, shell (`bash -n`), JS
  (`node --check`) and TypeScript (the machine's `tsc`, otherwise Node's type stripping – no type check) right away – nothing is executed – and add "⚠ SyntaxError line 42 …" to the result.
- **AGENTS.md:** the project's `AGENTS.md` (or `CLAUDE.md`, see `coding.instructions`) goes into the system prompt,
  together with the project folder, whether it is a git repository and the shell.
- **Diffs:** confirmations for `edit`/`write` show the change as a coloured diff instead of JSON.
- **Undo:** before and after every answer Orbwise takes a snapshot of the project in its own git directory
  (`~/.local/share/orbwise/snapshots/` – your repository is not touched, `.gitignore` applies). Under an answer that
  changed files: "3 files changed +12 −4" with the file list and **Undo** / **Restore**. The agent is told about it.
  Off for folders with more than 20,000 files or without git (`coding.snapshots: false` switches it off).
- **Project chip:** shows the folder – or `owner/repo` and the current branch when `origin` points to GitHub (in the
  sandbox the `orbwise/…` branch of the working copy); click it to pick a folder (folder browser) or to clone from
  GitHub.
- **Permissions:** from the auto level **Read + edit files** on, `edit`/`write` inside the project folder run without asking
  (also hidden folders like `.github`, also outside `$HOME`) – never `.git/`, credentials, or as root. Anything
  outside the project folder asks. Deleting, root and network still ask as everywhere.

### Sandbox (container, like Claude on the web)

Click **Sandbox** next to the project chip. Orbwise then makes a **working copy** of the project
(`~/.local/share/orbwise/workspaces/…`, a local `git clone` plus your uncommitted changes, on a new branch
`orbwise/<topic>-<id>`) and runs the agent's shell commands in a **container** – Podman preferred (rootless: you are
root in the container, `apt` works, files stay yours), otherwise Docker. The agent may build, test and install
freely there: everything inside the sandbox runs **without asking** (except with auto mode *Off*) – shell commands
including `sudo`/`apt` (root in the container is not root on your machine), edits in the copy, web search, also after
reading a web page. Still asked: reading files **outside** the copy (so nothing private ends up in the container),
writing outside the copy, and pushing to GitHub; the leak guard keeps watching what goes out.

- Your project folder stays untouched until you choose **Apply to project** (copies the changed files; files you
  changed in the project yourself meanwhile are reported as conflicts and only overwritten if you confirm).
- **View changes** shows every changed file with its diff; **Network: on/off** per chat (`--network none`);
  **End sandbox** deletes container and copy.
- No credentials go into the container (no token, no `~/.ssh`, no Docker socket); memory, CPU and processes are
  limited; the container stops after 30 minutes without commands and starts again with the next one.
- **Screenshots:** the agent can take a screenshot of the app it is building (`screenshot`, e.g. of the dev
  server on `localhost` or an HTML file) with headless Chromium – in the sandbox inside the container, otherwise with
  the Chromium/Chrome on your machine. The image appears under the answer (see the
  [screenshot above](../README.md#what-makes-it-different)); if the loaded model sees images, it also gets a description to check
  its work.
- The default image `orbwise-sandbox:4` (Debian with git, Python, Node, build tools, ripgrep, Chromium for screenshots, Podman, and for the code check ruff, bandit, semgrep, gitleaks, shellcheck) is built on first use;
  `sandbox.image` takes any other image (e.g. `python:3.12`). Install Podman with `sudo apt install podman`.
  If Podman is installed but not usable, the error says why (e.g. missing `subuid` ranges, with the fix) instead of
  Docker's “cannot connect to the Docker daemon”. On btrfs Podman may need `fuse-overlayfs` (`mount_program` in
  `~/.config/containers/storage.conf`) – that only changes how image layers are stored, not the isolation.

<details>
<summary><b>How tight is the sandbox?</b> · Docker inside the sandbox</summary>

Capabilities dropped, `no-new-privileges`, only the working copy is mounted. Orbwise never runs
git on the host inside the copy's `.git` (hooks or `core.fsmonitor` written by the agent would run outside the
container): commits happen in the container, a push fetches the branch into a fresh repository and pushes it to the
remote remembered when the copy was made. Host writes into the copy never follow a symlink out of it. With network
on, the container reaches the internet and your LAN (not services on your machine's `127.0.0.1`, such as Orbwise
itself – `orbwise doctor` warns if Orbwise listens on another address). Prefer rootless Podman over Docker: a
kernel exploit – which no container fully rules out – then only gets your own user.

**Docker in the container** (sandbox menu, per chat, off by default – rootless Podman only): the agent may run
`docker build`, `docker run -p 8080:80 …` (Podman behind a `docker` command) and `docker compose` (podman-compose)
inside the sandbox – as an unprivileged user with its own user namespace, no `--privileged`, no `CAP_SYS_ADMIN`,
`no-new-privileges` stays. The sandbox only gets `/dev/fuse` (image storage) and `/dev/net/tun` (network of the
inner containers); a short self-test runs when you switch it on, and only if the default seccomp profile blocks
nested containers, a bundled profile (default allow, dangerous calls such as module loading, kexec, bpf, keyctl
blocked) is used. Published ports are reachable inside the sandbox, so `screenshot http://localhost:8080` works.
Trade-off: a slightly larger attack surface towards the kernel – still limited to your user.

</details>

### GitHub

Sign in once with the GitHub CLI (`gh auth login`) – Orbwise uses its token – or put a
[fine-grained token](https://github.com/settings/personal-access-tokens) with *Contents: read & write* into
`github.token` (or `$ORBWISE_GITHUB_TOKEN`). Then:

- **Project chip → Clone from GitHub …**: search your repos, one click clones into `~/Projects` (`github.clone_dir`)
  and makes it the project of the chat. Click a repo to pick the **branch** (default branch preselected); if the
  folder exists already it is fetched and switched to that branch (only without uncommitted changes).
- **Push:** from the sandbox menu, the *Changes* window (*Push to GitHub*) or by the agent (`github_push`, always asks – also in auto mode *Auto*); afterwards
  a link to **create the pull request** on GitHub. The token is handed to git only for that one call (via
  `GIT_ASKPASS`) and never written into `.git/config`.
- `orbwise doctor` shows the container engine and the GitHub account.

## Code check (🛡 Check)

**Check** next to the sandbox chip (or `/review`, `/review all` for the whole project) reviews the project – as well as
a local model can review its own code:

1. **Hard facts first** – in the sandbox (turned on if needed): the project's tests, linter and type check (pytest,
   `npm test`/`lint`/`typecheck`, `tsc`, `make test`, `cargo test`, `go test`) plus scanners from the sandbox image:
   ruff, bandit, semgrep, gitleaks (secrets in code), shellcheck. What they find is real.
2. **Fresh eyes** – one sub-agent per topic (bugs and logic · security · structure), each with an empty context: it
   sees the code and the changes, not the reasoning from the chat, so it doesn't just nod its own work through.
3. **Sceptical verification** – a second pass has to prove every finding in the code (read it, or show it with a small
   command or test); unproven findings are dropped. This filters out invented findings, the biggest weakness of a
   self-review.
4. **Report and fix** – the report (severity, `file:line`, why, fix) lands in the chat; confirmed findings and failing
   checks are then fixed in the sandbox and the checks run again, up to `coding.review_rounds` (2) rounds. Your
   project folder stays untouched until you apply the changes.

By default it checks what changed in the sandbox – or, if nothing did, what is open since the last commit or the last
commit itself; *Check the whole project* reviews all source files (much slower with a local model). Older sandboxes
(image `:3`) skip the scanners – end the sandbox and turn it on again to get image `:4`.

## Goal mode (/goal)

Type `/goal <what should be done>` (or `/ziel …`) and Orbwise works on it **without stopping for "continue"**: the
usual step limit of an answer doesn't apply, the context is compacted in between as needed, and an answer only
counts as finished when the model ends it with `[GOAL REACHED]`. If it stops early ("shall I continue?"), it is
nudged to carry on; if it needs a decision from you it asks and waits (`[QUESTION]`) – your answer continues the
goal. A chip above the input shows the goal and the step count; ✕ or `/goal stop` ends it, STOP cancels as usual.
Limits: `coding.goal_max_steps` (300 tool steps) and `coding.goal_max_minutes` (120) – then it pauses and
`/goal continue` carries on. Confirmations still ask as before; in the sandbox with auto mode practically everything
runs on its own.

## Plan mode

For bigger jobs switch on **Plan** in the input bar. Orbwise then may only
look things up with read-only tools (system info, file and package search, status …) and answers with a numbered
plan: what it will do, with which tool or exact command, and where it will ask for confirmation. Anything that would
change something is not run – it becomes a step of the plan. After **Run** plan mode switches itself off. While the plan runs it stays pinned to the current request, so it is never cut from the context even when long tool output fills it up. Plan mode uses thinking only when **Think** is
switched on too – leave it off for fast plans, switch it on for more thorough ones (also for Telegram `/plan`).

Under the plan: **Run** carries it out (confirmations for changes still appear as usual), **Change** lets you
write what should be different ("keep the journal logs") and Orbwise presents a revised plan, **Discard** drops it.
By voice, a short "yes" or "no" decides; the plan itself is not read out, Orbwise only says how many steps it has.
From the phone: `/plan clean up my disk` – the plan arrives with ✅ Run / ✏️ Change / ❌ Discard buttons.
