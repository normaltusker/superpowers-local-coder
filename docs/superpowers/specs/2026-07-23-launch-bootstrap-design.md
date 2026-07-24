# Launch / Bootstrap — Cross-Platform Design Spec

**Status:** Approved (2026-07-23)
**Phase:** Phase 2, items 2 + 3 combined (fresh-install venv provisioning +
Windows support), with item 1's manual smoke test as the acceptance gate.
**Objective:** make the `local-coder` MCP server launch correctly on a
fresh plugin install and on Windows, without the current manual
venv-creation step, by provisioning its Python virtualenv into the
persistent plugin-data directory and launching through the project's own
cross-platform hook infrastructure.

## Background — the three real problems

1. **`.mcp.json` hardcodes `${CLAUDE_PLUGIN_ROOT}/mcp-servers/local-coder/.venv/bin/python`.**
   That interpreter does not exist until someone manually runs the
   README's `python -m venv` step, so a fresh plugin install cannot start
   the server at all. The path is also POSIX-only (`bin/python` vs
   Windows's `Scripts/python.exe`).
2. **The venv lives in an ephemeral directory.** Claude Code plugin docs
   state `${CLAUDE_PLUGIN_ROOT}` "changes when the plugin updates … treat
   it as ephemeral and don't write state there." A venv (or any
   runtime-written state) placed there is wiped on every plugin update.
   `${CLAUDE_PLUGIN_DATA}` is the documented persistent directory,
   explicitly intended for "Python virtual environments."
3. **`config.py` uses `fcntl.flock`** for its config-write lock — POSIX
   only. `import fcntl` alone crashes on Windows (the module doesn't
   exist), so the server can't even import there.

## Distribution model (scope anchor)

The one supported path: this fork installed as a Claude Code **plugin** on
the user's own machine (as done via the `superpowers-dev` marketplace),
running delegations locally with aider/Ollama. Python 3.10+ is assumed
present (a documented prerequisite; `fastmcp` requires it). We do NOT
design for a cold machine with no Python, no package manager, etc.

## Authoritative conventions this design follows

- **Claude Code plugin docs:** `${CLAUDE_PLUGIN_DATA}` is the persistent,
  update-surviving dir (`~/.claude/plugins/data/{id}/`) meant for venvs;
  it is exported to MCP/hook subprocesses and substitutes in `.mcp.json`
  `command`/`args`/`env`. The recommended provisioning pattern is a
  `SessionStart` hook that diffs the bundled dependency manifest against a
  stored copy and reinstalls on first-run or manifest change.
- **Superpowers' own `docs/windows/polyglot-hooks.md`** (the canonical
  in-repo convention — trumps generic guidance per this repo's CLAUDE.md):
  - Hooks run through a single canonical polyglot dispatcher,
    `hooks/run-hook.cmd` (Windows CMD block + Unix bash, in one file).
  - Hook scripts are **extensionless** (`session-start`, not
    `session-start.sh`) — deliberately, because Claude Code on Windows
    auto-prepends `bash` to any command containing `.sh`, which would
    break the dispatcher.
  - **No `cygpath`** — the repo dropped it; direct `exec` handles Windows
    paths correctly.
  - **Silent `exit 0` when bash is unavailable** on Windows — a user
    without Git Bash must not get a hard failure; the plugin degrades
    gracefully.
  - `hooks/session-start` **emits JSON on stdout** as its contract — any
    new provisioning must not pollute that stream (we solve this by using
    a SEPARATE hook script that emits no stdout).

## Architecture — provision + launch chain

```
SessionStart  (hooks/hooks.json — ADD a 2nd entry; leave existing one alone)
  └─ run-hook.cmd provision-local-coder      [existing canonical dispatcher]
       └─ hooks/provision-local-coder  (NEW, extensionless bash)
            • discover a working Python 3.10+ (probe order; graceful skip)
            • manifest-diff: requirements.txt (PLUGIN_ROOT) vs stored stamp
              (PLUGIN_DATA); on first-run OR change → create/refresh the
              venv at ${CLAUDE_PLUGIN_DATA}/local-coder/.venv and
              pip install -r requirements.txt
            • ALL output → stderr; NEVER exit nonzero (non-fatal, retries
              next session)

.mcp.json  (CHANGED)
  └─ command = run-hook.cmd,  args = ["launch-local-coder", "<server.py>"]
       └─ hooks/launch-local-coder  (NEW, extensionless bash)
            • resolve ${CLAUDE_PLUGIN_DATA}/local-coder/.venv
            • pick bin/python (POSIX) or Scripts/python.exe (Windows) by
              existence
            • exec that interpreter on server.py
            • if the venv is missing → exit NONZERO with a clear stderr
              message (a server that can't find its interpreter genuinely
              cannot run; Claude Code surfaces it as a failed server)
```

`SessionStart` runs before MCP servers connect, so by the time
`launch-local-coder` runs, the venv is present (unless provisioning was
skipped/failed, which the launcher reports as a hard failure).

## Components

### C1. `hooks/hooks.json` — add a second SessionStart entry

Keep the existing `session-start` entry (skill-context injection) exactly
as-is. Add a second hook in the same `SessionStart` array:

```json
{
  "type": "command",
  "command": "\"${CLAUDE_PLUGIN_ROOT}/hooks/run-hook.cmd\" provision-local-coder",
  "async": false
}
```

Quoted path (may contain spaces), same shape as the existing entry.
`async: false` so provisioning completes before the session proceeds to
connect MCP servers. Also mirror this into `hooks-cursor.json` if that
harness variant should provision too (Cursor uses `sessionStart` matcher);
in-scope only if Cursor is a target — default: add it for parity, it's
harmless where local-coder isn't used.

### C2. `hooks/provision-local-coder` — NEW extensionless bash provisioner

Responsibilities, in order:

1. Resolve `PLUGIN_ROOT` from the script's own location (as
   `session-start` does) and `DATA_DIR="${CLAUDE_PLUGIN_DATA}/local-coder"`.
   If `CLAUDE_PLUGIN_DATA` is unset (non-plugin/dev run), skip silently
   (`exit 0`) — provisioning is only meaningful in an installed plugin.
2. Discover a Python 3.10+ interpreter. Probe order, first that satisfies
   both `-c ""` (runs cleanly — skips the Windows Store stub) AND
   `sys.version_info >= (3,10)`: `python3`, `python`, `py -3`. If none →
   one stderr note, `exit 0`.
3. Manifest-diff:
   - `REQ="${PLUGIN_ROOT}/mcp-servers/local-coder/requirements.txt"`
   - `STAMP="${DATA_DIR}/requirements.installed.txt"`
   - `VENV="${DATA_DIR}/.venv"`
   - If `VENV` missing OR `STAMP` missing OR `REQ` differs from `STAMP`:
     - create `VENV` (if missing) with the discovered interpreter
     - `"$VENV"/<bindir>/python -m pip install -r "$REQ"` where `<bindir>`
       is `bin` on POSIX, `Scripts` on Windows (detect via
       `[ -d "$VENV/Scripts" ]` after creation, or OS check)
     - on success: `cp "$REQ" "$STAMP"` (stamp only AFTER a successful
       install, so a failed install re-runs next session)
     - on failure: `rm -f "$STAMP"`; stderr note; `exit 0`
4. Never emit stdout. Never `exit` nonzero. All diagnostics → stderr.

`set -uo pipefail` (NOT `-e`: we handle failures explicitly and must not
abort the session).

### C3. `.mcp.json` — CHANGED launch command

```json
{
  "mcpServers": {
    "local-coder": {
      "type": "stdio",
      "command": "${CLAUDE_PLUGIN_ROOT}/hooks/run-hook.cmd",
      "args": [
        "launch-local-coder",
        "${CLAUDE_PLUGIN_ROOT}/mcp-servers/local-coder/server.py"
      ]
    }
  }
}
```

`run-hook.cmd` forwards `%2..%9` on Windows and `"$@"` on Unix, so the
single `server.py` arg passes through cleanly.

### C4. `hooks/launch-local-coder` — NEW extensionless bash launcher

1. `DATA_DIR="${CLAUDE_PLUGIN_DATA}/local-coder"`, `VENV="$DATA_DIR/.venv"`.
2. Pick the interpreter: `PY="$VENV/bin/python"`; if that doesn't exist,
   try `PY="$VENV/Scripts/python.exe"`.
3. If neither exists → stderr message ("venv not provisioned; the
   SessionStart provisioning hook did not complete — check its stderr
   output") and `exit 1` (hard failure — correct for a launcher; a silent
   `exit 0` would masquerade as a connected-but-dead server).
4. `exec "$PY" "$1"` (server.py path passed as `$1`), so the server
   replaces the shell process and owns stdio directly (matching how the
   current direct-python launch behaves for the MCP stdio transport).

Note: unlike provisioning, the launcher does NOT silent-exit-0 on
failure. The no-bash-on-Windows case is inherent to any bash-routed
launch and is the documented Git-Bash prerequisite.

### C5. `plugin_data_dir()` helper (DRY)

Add a small shared helper (new module, e.g.
`mcp-servers/local-coder/paths.py`) used by `config.py` and `server.py`:

```python
def plugin_data_dir() -> Path:
    """Persistent per-call/config/log state directory.

    In an installed plugin, ${CLAUDE_PLUGIN_DATA} is set and survives
    plugin updates — the documented home for venvs, config, and caches.
    Outside a plugin (tests, standalone/dev use), fall back to the
    in-tree directory so existing behavior is unchanged.
    """
    data = os.environ.get("CLAUDE_PLUGIN_DATA")
    if data:
        d = Path(data) / "local-coder"
        d.mkdir(parents=True, exist_ok=True)
        return d
    return Path(__file__).parent  # in-tree fallback (dev/tests)
```

### C6. `config.py` — config path + cross-platform lock

- **Path:** `CONFIG_PATH = plugin_data_dir() / "config.yaml"`. The
  checked-in `config.yaml` becomes a default TEMPLATE: on first load, if
  the resolved `CONFIG_PATH` doesn't exist, seed it by copying the bundled
  default (`Path(__file__).parent / "config.yaml"`). Tests are unaffected:
  the `isolated_config` fixture already monkeypatches `CONFIG_PATH`, so
  changing its default value doesn't touch them.
- **Lock:** replace the module-level `import fcntl` and the `_config_lock`
  body with a platform branch behind the SAME `_config_lock()`
  context-manager interface (all callers unchanged):
  - Import selection at module load: `fcntl` on POSIX, `msvcrt` on Windows
    (`if os.name == "nt": import msvcrt` else `import fcntl`).
  - POSIX: `fcntl.flock(f, LOCK_EX)` / `LOCK_UN` (unchanged).
  - Windows: `msvcrt.locking(f.fileno(), msvcrt.LK_LOCK, 1)` /
    `msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)`.

### C7. `server.py` — log path

`OUTPUT_LOG_PATH` (item 7's per-call log base) moves from
`Path(__file__).parent / "local-coder-output.log"` to
`plugin_data_dir() / "local-coder-output.log"`, so it too lives in the
persistent dir rather than the ephemeral plugin root. The per-call unique
filename logic (`_make_output_log_path`) is unchanged — only the base
directory moves.

## Testing

### Unit (pytest — existing suite)
- `plugin_data_dir()`: returns `${CLAUDE_PLUGIN_DATA}/local-coder` when the
  env var is set (create-dir behavior verified); falls back to the in-tree
  path when unset.
- config seeding: absent data-dir copy → seeded from the bundled default;
  present copy → used as-is. Existing `isolated_config`-based config tests
  pass unchanged.
- lock module selection: with `os.name` mocked to `"nt"`, the code selects
  the `msvcrt` branch; on POSIX the real `fcntl` path still guards a
  compound load→merge→save against a concurrent writer (existing lock test
  stays).
- `server.py` log base resolves under the data dir when the env var is set.

### Hook/shell (repo has `tests/hooks/`)
- `test-provision-local-coder`: run the provisioner against a temp
  `CLAUDE_PLUGIN_DATA` with a throwaway `PLUGIN_ROOT`; assert the venv is
  created and the stamp written; assert idempotency (a second run makes no
  changes); assert graceful non-fatal behavior when Python is "absent"
  (PATH stubbed) — exits 0, writes no stamp.

### Manual acceptance — Phase 2 item 1's smoke test (the gate)
On a session with the plugin freshly (re)installed:
1. The SessionStart provisioning hook creates the venv under
   `${CLAUDE_PLUGIN_DATA}/local-coder/.venv` with no manual step.
2. `claude mcp list` shows `local-coder` **connected** (not "pending" /
   missing-interpreter).
3. A real `delegate_implementation` run works end-to-end (item 7's
   hang/visibility fixes make this observable). Documented as a manual
   checklist in the plan; this is the acceptance gate, not an automated
   test (it needs a real plugin install + a real backend).

## Error handling (consolidated)
- Provisioning: no `CLAUDE_PLUGIN_DATA` → skip (dev/non-plugin); no Python
  3.10+ → stderr note + `exit 0`; pip/venv failure → remove stamp, stderr
  note, `exit 0`, retry next session. Never fatal to the session.
- Launch: venv/interpreter missing → `exit 1` with a clear stderr reason
  (surfaced by Claude Code as a failed server, not a silent hang).
- No bash on Windows: provisioning silent-skips (`run-hook.cmd` exits 0);
  launch cannot proceed — this is the documented Git-Bash prerequisite.
- Config on Windows without `msvcrt` locking available: not expected
  (msvcrt is stdlib on Windows); if the lock call raised, it would
  propagate — acceptable, since a broken lock is a real error, unlike a
  best-effort log write.

## Out of scope (explicitly)
- A cold machine with no Python / no ability to install one.
- Bundling a Python runtime.
- The two item-7 deferrals (StallError-tail retention; log-retention
  policy) — tracked separately.
- Codex/Gemini/OpenRouter backends (their own later phase).
