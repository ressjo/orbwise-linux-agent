# Contributing

Thanks for helping to make Orbwise better! Bug reports, new tools, translations and documentation are all welcome.

## Development setup

```bash
git clone https://github.com/ressjo/orbwise-linux-agent.git && cd orbwise-linux-agent
uv sync --extra dev --extra voice
uv run pytest -q -n auto         # all tests run offline, no GPU or Ollama needed (-n auto: in parallel)
uv run ruff check orbwise tests
ORBWISE_FAKE_LLM=1 uv run orbwise serve --open   # UI demo with a fake model
```

## Project layout

| Path | What lives there |
|---|---|
| `orbwise/agent.py` | The agent loop: prompt building, streaming, tool calls, confirmations, step limit |
| `orbwise/prompts.py` | System prompt and fixed texts (German + English) |
| `orbwise/llm.py`, `orbwise/llm_router.py` | Ollama and OpenAI-compatible clients, model profiles, managed llama-server |
| `orbwise/memory/` | Journal, summaries, facts, chats, search index, context budget |
| `orbwise/tools/` | All tools – one module per group |
| `orbwise/tools/safety/` | Which shell commands run without asking: parsing (`shellwords`), read-only rules per program (`readonly.RULES`), credentials, the auto modes (`auto.AUTO_RULES`), `sudo` rewriting |
| `orbwise/server.py` | `create_app`: builds the FastAPI app from the parts below |
| `orbwise/hub.py` | WebSocket hub: clients, requests to the agent, confirmations, questions |
| `orbwise/webapp/` | Shared app state (`context.AppContext`), start/stop (`lifecycle`), Telegram, background loops |
| `orbwise/webapp/routes/` | HTTP and WebSocket endpoints, one module per area, each with `register(app, ctx)` |
| `orbwise/web/` | The web UI (vanilla JS, no build step): `app.js` loads the ES modules in `web/js/` and calls their `init()` in order |
| `scripts/` | Installer, launcher, desktop files |

## Writing a new tool

Tools are plain async functions registered with the `@tool` decorator; the JSON schema for the model is built
from the type annotations:

```python
from typing import Annotated
from .registry import CONFIRM, ToolContext, tool

@tool("Restarts the Docker container with the given name.",
      risk=lambda ctx, args: (CONFIRM, f"restart container {args.get('name')}"))
async def restart_container(ctx: ToolContext, name: Annotated[str, "container name"]) -> str:
    ...
    return "Done."
```

- Put it in a module under `orbwise/tools/` and add the module to `load_all_tools()` in `registry.py`.
- Anything that changes the system must use `CONFIRM` (or a risk function); never run user input through a
  shell without validation – prefer argument lists with `proc.run(ctx, [...])`.
- Integrations that need configuration use `enabled=` so they only appear when configured.
- Add tests in `tests/` (mock external services with `httpx.MockTransport`, see `tests/fake_*.py`).
- If the tool belongs to a large optional group, add keywords for it in `orbwise/toolselect.py`.

### Where new code goes

- **New endpoint:** in the matching module under `orbwise/webapp/routes/` (or a new one, registered in
  `server.create_app` – the order there is the order FastAPI matches paths in). Shared objects come from `ctx`;
  a helper other parts need is put on `ctx` at the end of `register`.
- **New UI code:** in the matching module under `orbwise/web/js/`; import what you use, export what others use.
  Code that has to run on page load goes into the module's `init()`. A new module must be imported somewhere
  (a test checks that every module is reachable from `app.js`).
- **Shell command rules:** a program that only reads with certain arguments gets a function in
  `tools/safety/readonly.RULES`; what Auto mode should still ask for goes into `tools/safety/auto.AUTO_RULES`.

## Pull requests

- Keep changes focused, add tests, make sure `pytest` and `ruff` pass.
- User-visible texts exist in German and English (`prompts.py`, `web_i18n.py`, `L("…", "…")` in `web/js/`,
  `T("…", "…")` in the CLI).

## Releases

1. Set the version in `orbwise/__init__.py` (`__version__`) – the only place; `pyproject.toml` reads it from there.
2. Rename `## [Unreleased]` in `CHANGELOG.md` to `## [x.y.z] – YYYY-MM-DD` and start a new empty `[Unreleased]`.
   CI fails when the version has no changelog section.
3. Merge into the main branch, then push the tag `vx.y.z` on that commit (or create it in GitHub's release form).
   The *Release* workflow checks tag, version and changelog, runs the tests, builds the wheel and publishes the
   release with the changelog section as notes.
