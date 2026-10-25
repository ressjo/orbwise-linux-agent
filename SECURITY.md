# Security

Orbwise can run commands on your computer, so its safety model matters.

## How Orbwise protects your system

- **Local only.** The server binds to `127.0.0.1`, checks the `Host` header (DNS rebinding) and the `Origin`
  of WebSocket and write requests (CSRF), so other websites cannot send commands.
- **Confirmation.** Every tool that changes the system (shell commands that modify something, package
  installs, service control, killing processes, file writes, locks/alarms in Home Assistant, …) is shown in
  the dashboard and must be approved. Read-only commands run directly.
- **Blocklist.** Obviously destructive commands (e.g. `rm -rf /`, formatting or overwriting disks, fork bombs,
  `chmod -R` on `/`) are never executed; risky ones such as piping a download into a shell or partitioning
  tools are flagged with an extra warning in the confirmation dialog.
- **Root access** goes through `sudo -A` with a one-time token per command; the password is typed into the
  dashboard, handed straight to sudo and never written to disk, logged or shown to the language model. If you tick
  "remember" (default), Orbwise keeps it **in memory only** for `tools.sudo_remember_minutes` (default 15, like
  sudo itself) and answers sudo with it – only once sudo accepted it, only for requests given at the computer
  (never for Telegram or routines), and every root command still needs your confirmation. "Forget" in
  Settings → Status, a changed password or the timeout drops it. Python cannot wipe strings from memory, so a
  process memory dump during that window could contain it – set `sudo_remember_minutes: 0` if that matters to you.
- **Auto mode "Auto"** runs every shell command without root without asking, except deleting, sudo, shutting down,
  sending over the network, start-up files/autostart/credentials and Orbwise's own config. These checks only see the
  command itself: a script or `python -c …` can do anything your user account may do. Use it when you trust the
  task; plan mode and untrusted content (mail, screen) still ask.
- **Auto mode "read + edit files"** only lets file changes through that stay in your own home, need no root, delete
  nothing and do not touch hidden files, launchers or credentials; everything else asks as before.
- **Secrets stay put.** Files with keys and passwords (`~/.ssh`, `~/.gnupg`, password stores and keyrings,
  browser profiles, `.aws`/`.kube`/`.docker`, `*.pem`/`*.key`/`*.kdbx`, `.env`, Orbwise's own config, …) are
  never read by `read_file`, sent to the phone or uploaded – the resolved path is checked, so symlinks don't help.
  Shell commands that print such files, the environment (`printenv`, `env`, `$…TOKEN`) or Wi-Fi passwords
  (`nmcli -s`) need confirmation. This keeps a prompt injection in a mail or web page from quietly exfiltrating
  credentials.
- **Home network services** (Trilium, Paperless, Home Assistant) are contacted directly, never through a proxy.
- **Credentials live in the keyring.** Tokens, passwords and API keys entered under Settings → Skills or with
  `orbwise skills setup` are stored in the session keyring (Secret Service), or – without one – in
  `~/.config/orbwise/secrets.json` with mode 0600. Plain-text credentials found in `config.yaml` are moved there on
  start (backup `config.yaml.bak`). The web UI never receives a stored secret back, only "set". Both files are on the
  agent's protected list: the model cannot read them.

Please keep `host: 127.0.0.1`. Exposing Orbwise to a network gives anyone on that network a way to ask it to
run commands – the dashboard has no login, and the `Host` check stops browsers, not a device that sets the header
itself. Orbwise therefore refuses to start on another address unless `allow_remote: true` is set. For remote use
prefer the Telegram bot, a VPN (WireGuard, Tailscale) or an SSH tunnel (`ssh -L 8765:localhost:8765 your-pc`).

## Telegram and your home network

- **No open port.** The bot fetches messages itself (outgoing long polling to `api.telegram.org`); nothing on
  your router or PC is reachable from the internet because of it.
- **Only your chat.** Messages and button presses from any other chat ID are ignored (after setup, without even
  a reply). Confirmation buttons carry a random one-time key.
- **Your Telegram account is a remote control.** Whoever can use your Telegram account can ask Orbwise to do
  things and approve confirmations with the buttons. Protect it: enable **two-step verification** (Settings →
  Privacy and Security → Two-Step Verification), use a screen lock, and check *Devices* for sessions you don't
  know. `/stop` cancels whatever is running.
- **The bot token is a key.** Anyone holding it can read what is sent to the bot and write to you in its name.
  Orbwise keeps it out of logs and error messages; if it ever leaks, revoke it in **@BotFather** (`/revoke`) and
  enter the new token under Settings → Skills → Telegram.
- **Home network from the phone.** Requests from the phone act with the PC's access – e.g. web fetches can reach
  devices in your LAN. That is intended; keep the account secure (see above).
- Messages pass through Telegram's servers and are not end-to-end encrypted.

## Reporting a vulnerability

Please do **not** open a public issue for security problems. Use GitHub's private
[security advisory](https://github.com/ressjo/orbwise-linux-agent/security/advisories/new) form instead. You will get
an answer as soon as possible; fixes are released as a new version and mentioned in the changelog.
