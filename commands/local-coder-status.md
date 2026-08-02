---
description: Show active and recent local-coder delegations for this repository
argument-hint: '[job-id] [--all]'
disable-model-invocation: true
allowed-tools: Bash(python3:*), Bash(python:*), Bash(sh:*)
---

!`sh -c 'PY=""; for c in "${CLAUDE_PLUGIN_DATA:-}/local-coder/.venv/bin/python" "${CLAUDE_PLUGIN_DATA:-}/local-coder/.venv/Scripts/python.exe" "$(command -v python3)" "$(command -v python)"; do [ -n "$c" ] && [ -x "$c" ] && PY="$c" && break; done; [ -z "$PY" ] && { echo "no python interpreter found"; exit 0; }; "$PY" "${CLAUDE_PLUGIN_ROOT}/mcp-servers/local-coder/status_cli.py" status '"$ARGUMENTS"`

If no job-id was passed: render the output above as a single compact Markdown table (columns: id, status, phase, latest activity, elapsed, model, output log). Do not add prose outside the table.

If a job-id was passed: present the full output above verbatim. Do not summarize or condense it.
