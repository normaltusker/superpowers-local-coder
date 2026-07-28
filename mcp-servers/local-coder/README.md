# local-coder MCP server

Delegates implementation work to a local or remote coding-agent backend
(Aider + Ollama today; Codex and Gemini CLI designed but not yet
implemented — see `docs/superpowers/specs/2026-07-21-local-coder-delegation-design.md`)
instead of Claude Code's own Edit/Write tools.

## Prerequisites

- `aider` installed and on `PATH` (`pip install aider-chat` or `pipx install aider-chat`).
- [Ollama](https://ollama.com), running locally with the configured model
  pulled (default: `qwen3-coder:30b` — `ollama pull qwen3-coder:30b`), and
  every model listed in `fallback_models` (if any) also pulled — only
  needed if `model` (or a `fallback_models` entry) uses the `ollama/`
  prefix; not required if you're using a remote backend/model.
- `gh` CLI installed and authenticated (`gh auth status`) — only needed if
  `open_pr` is enabled.
- The server's Python dependencies are provisioned **automatically**: on
  the first session after install, a `SessionStart` hook (or the MCP
  launcher itself, whichever runs first) creates a virtualenv under
  the plugin's persistent data directory (`${CLAUDE_PLUGIN_DATA}/local-coder/.venv`)
  and installs `requirements.txt` into it, re-installing only when
  `requirements.txt` changes. You need a Python 3.10+ interpreter on `PATH`
  (`python3`, `python`, or the `py` launcher on Windows) and network access
  on first run; nothing else. On Windows, provisioning and launch run
  through Git Bash (the same requirement as all Superpowers hooks — see
  `docs/windows/polyglot-hooks.md`).

  For local development outside a plugin install, create the venv manually.
  POSIX:
  ```bash
  cd mcp-servers/local-coder
  python3 -m venv .venv
  .venv/bin/pip install -r requirements.txt
  ```
  Windows (PowerShell or cmd):
  ```
  cd mcp-servers\local-coder
  py -3 -m venv .venv
  .venv\Scripts\python.exe -m pip install -r requirements.txt
  ```

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

## Cold-load latency and the stall timeout

Once a backend has been running past its cold-load floor (below), a gap of
more than `stall_timeout_seconds` (default `300`) with no output is treated
as a stall and killed. But the FIRST call to a large local model after it has
gone idle can be slow purely from **cold-load** — the weights are read into
memory before a single token is generated, and that phase emits no output.
Counting cold-load time against the stall window would kill a model that is
loading normally, so the floor below suspends stall detection until the
cold-load window has elapsed.

`first_output_timeout_seconds` (default `600`) governs this. It is a hard
minimum-runtime **floor**: no stall is declared until the backend has run for
at least this long — the cold-load grace window. Startup banner output during
the floor does not end the grace, and silence during the floor does not kill,
so a model that prints a banner and then loads quietly for minutes is not
mistaken for a stall. Once the floor elapses, `stall_timeout_seconds` governs
inactivity as usual (a gap of more than that since the last real output). A
backend that never emits anything is killed the moment the floor passes. Both
are set from chat via `configure`, never by hand-editing `config.yaml`.

When `first_output_timeout_seconds` is omitted from a config, it falls back
to `stall_timeout_seconds`, so the floor and the inactivity window coincide
and a config predating this setting behaves exactly as before.

For large primary models, also configure `fallback_models`: if a genuine
cold-load failure does occur, the call fails over to the next model instead
of aborting outright.

## Watching a delegation run

While `delegate_implementation` runs, its progress appears live in the
Claude Code conversation — the latest line of backend output surfaces
as a progress update, and a bounded tail of the backend output is
included in the tool's final result (the `output_tail` field). That tail
is truncated to the last 20,000 characters, so for a long or chatty run
it is the recent output, not the entire backend transcript. No extra
steps are needed to see what the backend is doing.

For deep debugging, the raw backend output is also written to a per-call
log file under the plugin's persistent data directory
(`${CLAUDE_PLUGIN_DATA}/local-coder/local-coder-output-<pid>-<id>.log`).
Each call gets its own file so concurrent delegations never clobber each
other's log. The exact path is announced early (as a progress update, before
the backend starts) AND returned in the final tool result as `output_log`, so
you can start `tail -f`-ing it while the run is still in progress if you
want the raw stream. Tailing it is a power-user convenience, not the
normal way to follow a run.

These per-call logs would otherwise accumulate one-per-call forever. On each
delegation the server prunes the directory so that this call's about-to-be-written
log plus the retained older ones total no more than `log_retention_count`
(default `50`) — i.e. it keeps the `log_retention_count - 1` most recent
existing logs and lets the new one fill the last slot, so the directory stays
bounded at the configured cap rather than one over it. The prune and the new
log's creation run as one step under the same cross-process lock `configure`
uses, so the cap holds strictly even when several delegations overlap — two
concurrent calls cannot each prune-then-create and momentarily leave one log
over the cap. A log whose delegation is still running is never pruned, even
while it sits quiet during a cold load with a stale modification time, so it
is not deleted out from under a live call and then lazily recreated over the
cap. (That protection is per-process; a rare setup where several separate
Claude Code processes share one plugin-data dir can still drift transiently,
never leaking.) The whole prune-and-create step runs on a worker thread so its
blocking file lock never stalls the server's event loop. Set the cap via the
`configure` tool (never hand-edit `config.yaml`); it must be a strictly-positive
integer. If `config.yaml` is nonetheless hand-edited to a non-integer or
non-positive value, the server falls back to the default rather than failing
the delegation. Pruning is best-effort — a failure to delete an old log never
affects the delegation.

## Known issues

### Aider crashes importing scipy on macOS 26+ (repo-map)

**Symptom.** A delegation fails within seconds, and the backend output
(see the log file above) ends in a scipy traceback:

```
ImportError: dlopen(.../scipy/sparse/linalg/_propack/_spropack.cpython-312-darwin.so):
  section '__DATA/__thread_bss' has a zero-fill section type, but offset field is not zero
```

**Cause.** This is an OS/toolchain incompatibility, not a `local-coder`
bug and not a broken install — recent macOS versions' dyld rejects the
thread-local-storage section layout in scipy's precompiled `_propack`
extension. Reinstalling scipy or aider does **not** fix it; the same error
reproduces with freshly downloaded wheels and with `aider` run directly.

Only Aider's **repo-map** feature reaches this code (it ranks files with
networkx's pagerank, which pulls in `scipy.sparse`). Aider crashes while
building the repo-map, before it ever contacts the model.

**Workaround.** Disable the repo-map by passing `--map-tokens 0` through
to Aider. From chat:

```
You: set local-coder's extra_backend_args to --map-tokens 0
Claude Code: [calls configure(extra_backend_args=["--map-tokens", "0"])]
```

Delegations then run normally. The tradeoff is that Aider loses the
repo-map, so it has less automatic context about files you didn't name —
worth restoring (`extra_backend_args: []`) once your platform ships a fix.
This is left as opt-in configuration rather than a default because the
repo-map is genuinely useful on larger repositories.
