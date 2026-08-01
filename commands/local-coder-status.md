---
description: Show active and recent local-coder delegations for this repository
argument-hint: '[job-id] [--all]'
disable-model-invocation: true
allowed-tools: Bash(python3:*), Bash(python:*)
---

!`python3 "${CLAUDE_PLUGIN_ROOT}/mcp-servers/local-coder/status_cli.py" status $ARGUMENTS`

If no job-id was passed: render the output above as a single compact Markdown table (columns: id, status, phase, latest activity, elapsed, model, output log). Do not add prose outside the table.

If a job-id was passed: present the full output above verbatim. Do not summarize or condense it.
