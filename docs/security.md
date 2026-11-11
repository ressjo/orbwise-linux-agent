# Security

← [Back to the README](../README.md)

## Threat model

What Orbwise is built to withstand, how, and where it is proven. Every line links to a test you can read without
knowing the code – [`tests/test_attacks.py`](https://github.com/ressjo/orbwise-linux-agent/blob/main/tests/test_attacks.py) holds the scenarios, one function per threat.

| Threat | What Orbwise does | Proof |
|---|---|---|
| A web page, mail, PDF or image contains hidden instructions ("delete …", "send …") | While that content is in the chat, everything that changes something asks first – also what the level would otherwise allow | `test_a_web_page_cannot_make_orbwise_change_things_unasked` |
| The model tries to read your keys and passwords | The file tools never read `~/.ssh`, `~/.gnupg`, password stores, Orbwise's config … – in no level | `test_secrets_cannot_be_read_with_the_file_tools_in_any_mode` |
| Private data is smuggled out in a search or URL | The data-leak guard checks what leaves the machine: keys, IBANs, text copied from your files | `test_private_data_does_not_leave_the_machine_unnoticed` |
| The model gets root without you noticing | `sudo`, `su`, `pkexec`, AUR helpers in every spelling ask; in *Auto* only through the password dialog, never a remembered password | `test_root_never_runs_without_a_question` |
| A command destroys the system | `rm -rf /`, overwriting disks, `mkfs`, `chmod -R … /`, fork bombs are never run – not even after confirmation | `test_destruction_is_never_run_not_even_with_confirmation` |
| The model makes itself permanent or deletes your files | Start-up files, autostart, cron, SSH keys, deleting, shutting down ask up to *Read + edit files* | `test_persistence_deletion_and_shutdown_ask_below_auto` |
| A script does something its command line doesn't show | In *Read + edit files* scripts and builds run in a cage: no network, writable only in the project | `test_scripts_run_caged_without_network_in_read_plus_edit` |
| Code in the sandbox reaches your real files | Host writes into the working copy never follow a symlink out of it | `test_sandbox_code_cannot_write_outside_through_a_symlink` |
| A third-party MCP server claims its tool is harmless | Its tools ask unless you allowed them; its results count as outside content | `test_other_servers_tools_ask_and_their_results_are_outside_content` |
| Something is pushed or sent in your name | `git push` and sending mail ask in every level, also in *Auto* | `test_pushing_and_sending_mail_ask_even_in_auto` |
| Plan mode changes something after all | In plan mode nothing that changes something is run | `test_plan_mode_runs_nothing_that_changes_something` |

The rules live in code, not in the prompt: a model cannot talk its way past them.

### What runs in which level

Computed from the rules the agent applies (`python -m orbwise.security_matrix` rewrites the table; a test fails when
it is out of date). Assumes the usual case: not root, cage available, the web UI open, sudo with a password, no
outside content in the chat.

<!-- security-matrix:begin -->
|  | Off | Read only | Read + edit files | Auto |
|---|---|---|---|---|
| Reading: `ls`, `df`, `git status` | **asks** | runs | runs | runs |
| Create or change a file in your home | **asks** | **asks** | runs | runs |
| Scripts, builds, tests: `python3 x.py`, `npm test` | **asks** | **asks** | runs caged | runs |
| Deleting: `rm file` | **asks** | **asks** | **asks** | runs |
| Sending to the network: `curl -d @file …` | **asks** | **asks** | **asks** | runs |
| Changing start-up files: `~/.bashrc`, autostart | **asks** | **asks** | **asks** | runs |
| Reading credentials: `cat ~/.ssh/id_rsa` | **asks** | **asks** | **asks** | runs |
| The same file with the read tool | **never** | **never** | **never** | **never** |
| Root: `sudo …`, `su`, `pkexec`, `yay` | **asks** | **asks** | **asks** | **password dialog** |
| `git push` | **asks** | **asks** | **asks** | **asks** |
| `rm -rf /`, overwriting a disk, `chmod -R … /` | **never** | **never** | **never** | **never** |
<!-- security-matrix:end -->

### What it does not protect against

- **You approving without reading.** The card shows the exact command or diff; it protects only if you look.
- **The level *Auto*.** It lets everything but root, pushing and mail run – deleting, network, start-up files, and
  whatever a web page talks the model into. In *Auto* the model can also read credentials with a shell command
  (`cat ~/.ssh/id_rsa`), although the read tool refuses the same file.
- **Passwordless sudo (`NOPASSWD`).** Orbwise then asks for a visible `sudo` with a card, but a script that calls
  sudo itself runs as root without asking in *Auto*.
- **The `docker` group.** `docker run -v /:/host …` is root without a password; Orbwise does not see it as root.
- **A kernel exploit.** Neither the cage nor the sandbox container rules it out; rootless Podman limits the damage to
  your user.
- **Programs running as you.** They can read the access token and control Orbwise.
- **A model that is simply wrong.** It can misunderstand you and propose something harmful that looks reasonable.

`orbwise doctor --security` shows which of these apply on your machine: whether the cage works, whether sudo asks
for a password, whether Orbwise listens beyond localhost, which level is active.

## Security

Orbwise can run commands on your system, so:

- **Confirmation required:** everything that changes something (installs, updates, `rm`, writing files, service
  control, killing processes, unknown programs, command substitution `$(…)`) shows a confirmation card in the chat with
  the exact command (for file changes a diff). Confirm by click, `Enter`/`Esc` or voice ("yes"/"no").
- **Read-only commands** (`ls`, `df`, `systemctl status`, `grep`, …) run directly. While an e-mail, a fetched web page,
  an attached PDF or screen/image content is part of the chat (it could contain hidden instructions), every action
  that changes or starts something (writing files, memory, reminders, shell commands that change things, opening
  websites or apps, …) needs confirmation – except in **Auto**, which accepts that risk. If you trust that content,
  **Allow + stop asking in this chat** on the card switches back to your auto mode for this chat until new outside
  content arrives. Reading and searching keep running (reading files, web search, fetching
  pages, purely local read-only commands like `cd`, `ls`, `echo`) – what leaves the machine is still checked by the
  data-leak guard.
- **Data-leak guard:** whatever leaves the machine – web searches, fetched URLs, opened websites, `curl`/`wget`
  URLs and data – is checked first. Access keys/tokens, passwords, IBANs, card numbers, e-mail addresses, phone
  numbers, long encoded parameters and text copied from your own files, mails or documents (read in this chat) make
  Orbwise ask, showing what it found (masked) – in **Auto** only access keys, tokens and passwords. Values you typed
  yourself in your last message don't trigger it.
- **Isolation:** in *Read + edit files*, builds, tests and scripts run in a bubblewrap cage without network – writable
  only in the project folder and a private `/tmp`, without credentials or tokens. → [Auto mode](#auto-mode)
- **Blocklist:** `rm -rf /`, formatting or overwriting disks, fork bombs, `chmod -R … /` and similar are never run –
  not even after confirmation.
- **Local only:** the server listens on `127.0.0.1` and checks the `Host` header and `Origin` – other websites
  cannot send commands. The optional OpenAI-compatible API (`/v1`) is off by default, needs its own key and only
  reaches the bare model, never tools. Another address needs `allow_remote: true` (the dashboard has no login).
- **Secrets:** files with keys and passwords (`~/.ssh`, `~/.gnupg`, password stores, browser logins, cloud
  credentials, Orbwise's own config, …) are never read by the file tools or sent to the phone – symlinks included;
  shell commands that print them, environment variables or Wi-Fi passwords need confirmation.
- **Root privileges** (`privilege_cmd: dashboard`, default): after your confirmation a **password field** appears
  in the dashboard. It works through `sudo -A` with a small helper that uses a one-time token for exactly that
  command; the password goes straight to sudo and is never written to disk, logged or shown to the model. With
  "remember" ticked it is kept in memory for 15 minutes (`tools.sudo_remember_minutes`) for requests at the
  computer – not for Telegram or routines; Settings → Status shows it and can forget it. If a script, Makefile or
  other program the model runs calls `sudo` itself (`./install.sh`, `make install`), a small `sudo` wrapper in front
  of the `PATH` routes it to the same dialog, which then also shows what really gets root
  (`./install.sh → sudo pacman -S …`); a remembered password never applies there. The password is only handed to the
  helper when the request really comes from it running under a root `sudo` – a script that grabbed the one-time token
  and asks by itself gets nothing. Alternatives: `pkexec` (desktop polkit dialog) or `sudo` with a NOPASSWD rule.
- **SSH logins** (NAS, servers): user name and password are asked in the dashboard for every new connection, handed
  to `ssh` through the same one-time-token helper and never stored, logged or shown to the model; remote commands
  are classified like local ones and remote `sudo` asks again.
- **Shutdown, reboot, suspend, lock** go through systemd/logind and need no password for the active session.

See [SECURITY.md](../SECURITY.md) for details and how to report vulnerabilities.

### Auto mode

The drop-down next to *Think* decides what runs without asking. The rules live in code (no prompt context), and the
server keeps the setting for every open window, Telegram and routines.

- **Off** – every shell command asks.
- **Read only** (default) – recognised read-only commands (`ls`, `df`, `docker ps`, `git status`, `journalctl` …) run
  directly.
- **Read + edit files** – also create, write, copy and move files in your home (`write_file`, `mkdir`, `cp`, `mv`,
  `tee`, `>`); never with root, never deleting, never hidden files (shell start files, autostart, `~/.ssh`,
  `~/.config` …), launchers or credentials. In coding mode, changes inside the project folder run directly. Builds,
  tests and scripts (`make`, `npm test`, `python3 x.py` …) run **isolated without network** (see below). The safe
  middle ground.
- **Auto** – **everything runs without asking**, like in your own terminal: servers, `docker compose up`, package
  managers without root, network, deleting, editing any file. Only three things ask:
  - `sudo` (also `yay`/`paru`/`makepkg -si`, which call sudo themselves) – no extra card, just the **password
    dialog**, which shows the command; a remembered password is not used for these, and neither is a ticket sudo
    itself may still hold (Orbwise drops it before such a command and after every password entry – this matters with
    `timestamp_type=global` or `!tty_tickets` in sudoers). If sudo asks this user for **no password** (`NOPASSWD`),
    there is no dialog to be the question: `sudo` then asks with a normal card, and `orbwise doctor` warns – a
    script that calls sudo itself still runs as root without asking on such a machine;
  - pushing (`git push`, *Push to GitHub*);
  - sending e-mail in your name.

  The risk is deliberate: content from a web page or e-mail could make the model run something, and data can go out
  over the network (the data-leak guard only stops access keys, tokens and passwords here). If you don't want that,
  use **Read + edit files**.

**Isolation in "Read + edit files".** Looking at a command only shows what is written in it – what `python -c …`, a
script or `npm test` then do, it cannot see. So shell commands that this level lets through without asking run in a
cage ([bubblewrap](https://github.com/containers/bubblewrap), installed by the installer):

| In the cage | |
|---|---|
| Writable | coding: the project folder · tools mode: nothing in your home · both: a private `/tmp` per chat |
| Read-only | the rest of the system and your home |
| Invisible | `~/.ssh`, `~/.gnupg`, keyrings, browser profiles, cloud and Docker credentials, Orbwise's config, `.netrc` & co. |
| Gone | network, environment variables with tokens/passwords/keys, the session D-Bus and `systemd --user`, the Docker socket |

If a command fails because of the cage – it wanted to write elsewhere, needed root or the internet – Orbwise asks once
whether it may run **without isolation** (with root: then the password dialog). The activity panel marks caged
commands with *isolated*. Background jobs (servers) are not caged here – they ask. Without a working bubblewrap
(missing, or user namespaces blocked, e.g. by Ubuntu's AppArmor setting) these commands simply ask as before;
`orbwise doctor` and the Auto menu say why. `tools.jail_writable` adds folders that may be written without asking.

Plan mode always asks. If Orbwise runs as **root**, the upper two levels approve nothing (`orbwise doctor` warns).
