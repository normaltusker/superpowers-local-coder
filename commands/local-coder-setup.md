---
description: Check whether local-coder is ready (venv, aider, ollama+model, config)
argument-hint: ''
disable-model-invocation: true
allowed-tools: Bash(python3:*), Bash(python:*), Bash(sh:*)
---

!`sh -c 'PY=""; for c in "${CLAUDE_PLUGIN_DATA:-}/local-coder/.venv/bin/python" "${CLAUDE_PLUGIN_DATA:-}/local-coder/.venv/Scripts/python.exe" "$(command -v python3)" "$(command -v python)"; do [ -n "$c" ] && [ -x "$c" ] && PY="$c" && break; done; [ -z "$PY" ] && { echo "no python interpreter found"; exit 0; }; "$PY" "${CLAUDE_PLUGIN_ROOT}/mcp-servers/local-coder/status_cli.py" setup'`

Present each check's PASS/FAIL line and any `->` next-step guidance above verbatim. Do not summarize away or omit a failing check — the next-steps are the actionable part.
