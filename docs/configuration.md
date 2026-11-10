# Configuration (experts)

← [Back to the README](../README.md)

**You don't need this page for normal use.** Models, skills (integrations and their passwords), voice, the step
limit, disk space, autostart and the orb are all set in the web UI – see the [README](../README.md#using-orbwise).
This page is for things the UI doesn't cover and for setting Orbwise up by script.

## Where settings live

| What | Where |
|---|---|
| Settings from the UI (models added there, skill values, step limit, MoE, sampling …) | `~/.local/share/orbwise/state.json` |
| Passwords, tokens, API keys | your session keyring (KWallet / GNOME keyring); without one `~/.config/orbwise/secrets.json` (0600) |
| Optional config file | `~/.config/orbwise/config.yaml` (or `$ORBWISE_CONFIG`) |
| Per project (coding mode) | `AGENTS.md` (instructions for the agent) and `.orbwise.yaml` (`check:` command, see [Coding](coding.md#coding-mode)) |

The **config file is optional**: without it the defaults apply. The installer creates one with your language and
model; settings made in the UI take precedence over it. Credentials found in it are moved to the keyring on the next
start (a backup `config.yaml.bak` is kept).

## config.yaml

**Every** option with its default and an explanation is in
[`orbwise/config.example.yaml`](../orbwise/config.example.yaml); `orbwise init-config` copies it to
`~/.config/orbwise/config.yaml`. After editing, restart Orbwise (`orbwise stop`, then start it again, or
`systemctl --user restart orbwise` when it runs as a service). The most important ones:

| Option | Meaning |
|---|---|
| `language` | `de` or `en` – assistant, speech recognition, default voice, UI and CLI |
| `llm.model` | Ollama model, e.g. `qwen3:14b`, `qwen3:8b` |
| `llm.profiles`, `llm.active` | Several models, see [Models & profiles](models.md#models--profiles) |
| `memory.context_budget_tokens` | Optional cap for the prompt size (default: automatic from the context window) |
| `memory.compact_idle` | Compact a nearly full context right after an answer instead of before the next question (default on) |
| `voice.stt_model` | Whisper size: `tiny`, `base`, `small`, `medium`, `large-v3` |
| `voice.wakeword_model` | Pre-trained openWakeWord model (the wake phrase) |
| `voice.wakeword_threshold` | Wake word sensitivity (lower = more sensitive) |
| `tools.nas_paths` | Mounted NAS folders |
| `tools.privilege_cmd` | `dashboard` (password field, default), `pkexec` or `sudo` (NOPASSWD) |
| `tools.sudo_remember_minutes` | How long the dashboard password is remembered (default 15, RAM only, never for Telegram/routines; 0 = always ask) |
| `tools.package_manager` | `auto`, `pacman` or `apt` |
| `tools.max_steps` | Max. tool rounds per request (default 25), then Orbwise summarises and offers to continue; `0` = unlimited. Also in **Settings → General → Step limit** (takes precedence) |
| `coding.max_steps` | The same in coding mode (default 60) |
| `tools.disabled` | Tools or groups to switch off, e.g. `[routine_tools, weather]` |
| `tools.extra` | Switch thin wrapper tools back on that are off by default (process list, services, logs, network, ports, disk usage, `ls`, `rg`, `ssh_connect` …) – the model does these with `run_shell` and the usual commands, which run without asking when read-only. E.g. `[sysadmin]` |
| `user_name`, `persona_extra` | How the assistant addresses you and extra personality instructions (its name: `assistant_name`) |
| `host`, `allow_remote` | Keep `127.0.0.1`; other addresses only with `allow_remote: true` |
| `mail.send_enabled` | Let Orbwise write and send mails (always through the editable confirmation card) |
| `telegram.token`, `telegram.chat_id` | Telegram bot, see [Telegram](integrations.md#telegram-phone) |
| `coding.*` | Coding mode: undo snapshots, subagent steps, background jobs, project instructions (`AGENTS.md`) – see [Coding mode](coding.md#coding-mode) |
| `sandbox.*` | Container engine (`auto`/`podman`/`docker`), image, memory, CPUs, network default, idle stop |
| `github.token`, `github.clone_dir` | GitHub token (or `gh auth login`) and where repos are cloned |

## Integrations in config.yaml

The Skills page writes the same values. For reference – passwords can be left out here and come from the keyring or
an environment variable:

**Home Assistant**

```yaml
homeassistant:
  url: http://homeassistant.local:8123
  token: "your-token"          # or $ORBWISE_HA_TOKEN
```

**Portainer**

```yaml
portainer:
  url: https://nas.local:9443
  token: "ptr_…"              # or $ORBWISE_PORTAINER_TOKEN
  verify_ssl: false           # Portainer's own certificate is self-signed
  # environment: nas          # only if Portainer manages several Docker hosts
```

**SSH**

```yaml
ssh:
  hosts:
    nas: {host: 192.168.1.10, user: admin}   # user is only pre-filled in the login dialog
    pi:  {host: raspberrypi.local, port: 2222}
  # keep_minutes: 15
```

**Paperless**

```yaml
paperless:
  url: http://nas.local:8000
  token: "your-token"          # or $ORBWISE_PAPERLESS_TOKEN
```

**E-mail**

```yaml
mail:
  username: "you@proton.me"
  password: "bridge-password"   # or $ORBWISE_MAIL_PASSWORD
  # port: 1143                  # only if the Bridge shows a different one
```

**Obsidian**

```yaml
obsidian:
  vault: ~/Obsidian/Notes
  inbox: Inbox               # folder for new notes
```

**Calendar**

```yaml
calendar:
  url: https://caldav.icloud.com   # or your Nextcloud/Radicale/… CalDAV URL
  username: you@icloud.com
  password: "xxxx-xxxx-xxxx-xxxx"  # or $ORBWISE_CALENDAR_PASSWORD
  calendars: []                    # which calendars to read (empty = all)
  default_calendar: ""             # where new events go
```

**Websites & weather**

```yaml
websites:
  nas: http://192.168.1.10:5000            # "open the NAS"
  shop: https://example.com/search?q={q}   # {q} = search term
weather:
  location: "Berlin"
```

**Telegram**

```yaml
telegram:
  token: "123456:ABC…"     # or $ORBWISE_TELEGRAM_TOKEN
  chat_id: 123456789
```

**Morning briefing**

```yaml
briefing:
  sections: [weather, calendar, reminders, mail, paperless_inbox, news, updates, storage]   # order = order in the briefing
  lookahead_days: 2          # events and reminders/deadlines for today + the next 2 days (0 = today only)
  news_topics: [Linux, Berlin]   # headlines of the last 24 h per topic (Brave API, otherwise ddgs)
  news_count: 3
  inbox_tag: ""              # Paperless inbox tag – empty = the tags marked as inbox tags in Paperless
  instructions: "Keep it short and start with my appointments."
```

## Environment variables

Passwords and tokens can also come from environment variables (`ORBWISE_MAIL_PASSWORD`, `ORBWISE_TELEGRAM_TOKEN`,
`ORBWISE_PAPERLESS_TOKEN`, `ORBWISE_HA_TOKEN`, `ORBWISE_TRILIUM_TOKEN`, `ORBWISE_PORTAINER_TOKEN`,
`ORBWISE_CALENDAR_PASSWORD`, `ORBWISE_BRAVE_API_KEY`, `ORBWISE_GITHUB_TOKEN`) – then they don't have to be written into the config file at all.
