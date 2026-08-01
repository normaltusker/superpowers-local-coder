---
description: Check whether local-coder is ready (venv, aider, ollama+model, config)
argument-hint: ''
disable-model-invocation: true
allowed-tools: Bash(python3:*), Bash(python:*)
---

!`python3 "${CLAUDE_PLUGIN_ROOT}/mcp-servers/local-coder/status_cli.py" setup`

Present each check's PASS/FAIL line and any `->` next-step guidance above verbatim. Do not summarize away or omit a failing check — the next-steps are the actionable part.
