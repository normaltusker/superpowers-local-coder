# local-coder MCP server

Delegates implementation work to a local or remote coding-agent backend
(Aider + Ollama today; Codex and Gemini CLI designed but not yet
implemented — see `docs/superpowers/specs/2026-07-21-local-coder-delegation-design.md`)
instead of Claude Code's own Edit/Write tools.

## Prerequisites

- `aider` installed and on `PATH` (`pip install aider-chat` or `pipx install aider-chat`).
- [Ollama](https://ollama.com) running locally, with the configured model
  pulled (default: `qwen3-coder:30b` — `ollama pull qwen3-coder:30b`), and
  every model listed in `fallback_models` (if any) also pulled.
- `gh` CLI installed and authenticated (`gh auth status`).
- This server's own dependencies installed into its venv:
  ```bash
  cd mcp-servers/local-coder
  python3.13 -m venv .venv
  .venv/bin/pip install -r requirements.txt
  ```
  Use a Python 3.10+ interpreter explicitly (e.g. `python3.13`), not a bare `python3` — `fastmcp` requires >=3.10 and an older system `python3` will fail the install silently.

## Configuring backend/model

Never edit `config.yaml` by hand. Do it from chat with Claude Code:

```
You: what models do I have available for local-coder?
Claude Code: [calls list_available_models] "You have 3 pulled: ..."
You: use qwen2.5-coder:14b as primary, deepseek as fallback
Claude Code: [calls configure(model=..., fallback_models=[...])]
             "Updated. Current config: ..."
```

`configure` validates any `ollama/`-prefixed model against `ollama list`
before writing, and rejects the change with a clear error (naming what IS
available) if the model isn't actually pulled.

**Important:** `model`/`backend`/`fallback_models` are pre-run settings.
Once `subagent-driven-development` starts executing a plan, there's no
live per-task override — the model in effect for the whole plan is whatever
`config.yaml` held when execution started. To change it mid-plan, stop the
running skill, reconfigure, and resume.

## Long-running tasks and the MCP idle timeout

Claude Code has a default MCP stdio tool idle timeout
(`CLAUDE_CODE_MCP_TOOL_IDLE_TIMEOUT`, ~30 minutes). If you expect
`delegate_implementation` calls to run longer than that on large tasks,
raise this environment variable (or set it to `0` to disable it) before
starting Claude Code. `local-coder` sends periodic progress
notifications and stderr log lines while a backend subprocess runs, but
these are a best-effort mitigation, not a guarantee against this timeout.
