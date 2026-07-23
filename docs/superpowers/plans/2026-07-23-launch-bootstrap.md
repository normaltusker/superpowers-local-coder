# Launch / Bootstrap Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the `local-coder` MCP server launch on a fresh plugin install and on Windows, by provisioning its venv into the persistent `${CLAUDE_PLUGIN_DATA}` dir via a SessionStart hook and launching through the project's cross-platform hook dispatcher, with `config.py`/`server.py` made Windows-safe.

**Architecture:** A new `plugin_data_dir()` helper resolves the persistent state dir (`${CLAUDE_PLUGIN_DATA}/local-coder`, in-tree fallback for tests/dev). `config.py` and `server.py` write their runtime state (config.yaml, per-call logs) there instead of the ephemeral plugin root, and `config.py`'s lock becomes fcntl-on-POSIX / msvcrt-on-Windows. Two new extensionless bash hook scripts — dispatched by the existing `hooks/run-hook.cmd` polyglot wrapper — provision the venv on SessionStart and launch the server via the venv interpreter.

**Tech Stack:** Python 3.10+, FastMCP, pytest (Python unit tests), bash + the repo's `run-hook.cmd` dispatcher (hook/shell tests via `tests/hooks/`). Flat imports (`from backends import common`) per repo convention.

## Global Constraints

- Follow `docs/windows/polyglot-hooks.md`: hook scripts are **extensionless** (no `.sh`); dispatched via `hooks/run-hook.cmd`; **no `cygpath`**; provisioning **silently exits 0** on any failure (never breaks the session); the existing `hooks/session-start` stdout JSON contract must not be touched.
- The venv, config.yaml, and per-call log files live under `${CLAUDE_PLUGIN_DATA}/local-coder/` (persistent, survives plugin updates) — NEVER under `${CLAUDE_PLUGIN_ROOT}` (ephemeral, wiped on update).
- Provisioning failure = non-fatal (stderr note, `exit 0`, retry next session). Launch failure (missing venv/interpreter) = hard `exit 1` (surfaced by Claude Code as a failed server, not a silent hang).
- Internal Python imports are flat: `from paths import plugin_data_dir`, never `from local_coder import ...`.
- Run Python tests from `mcp-servers/local-coder/`: `.venv/bin/python -m pytest tests/ -q`. Baseline before this plan: 127 passing. Gate = zero failures, no reduction from baseline (do not assert a hard count; the suite grows as tasks add tests).
- Run hook tests as: `bash tests/hooks/<name>`.

---

## File Structure

- `mcp-servers/local-coder/paths.py` — NEW. `plugin_data_dir()` helper.
- `mcp-servers/local-coder/config.py` — MODIFY. `CONFIG_PATH` via helper + template-seeding in `load_config`; cross-platform lock.
- `mcp-servers/local-coder/server.py` — MODIFY. `OUTPUT_LOG_PATH` via helper.
- `mcp-servers/local-coder/tests/test_paths.py` — NEW. Unit tests for the helper.
- `mcp-servers/local-coder/tests/test_config.py` — MODIFY. Add seeding + lock-selection tests.
- `hooks/provision-local-coder` — NEW extensionless bash provisioner.
- `hooks/launch-local-coder` — NEW extensionless bash launcher.
- `hooks/hooks.json` — MODIFY. Add the provisioning SessionStart entry.
- `.mcp.json` — MODIFY. Launch via `run-hook.cmd launch-local-coder`.
- `tests/hooks/test-provision-local-coder` — NEW hook/shell test.
- `mcp-servers/local-coder/README.md` — MODIFY. Document auto-provisioning.

---

### Task 1: `plugin_data_dir()` helper

**Files:**
- Create: `mcp-servers/local-coder/paths.py`
- Test: `mcp-servers/local-coder/tests/test_paths.py`

**Interfaces:**
- Produces: `plugin_data_dir() -> pathlib.Path`. Returns `Path(os.environ["CLAUDE_PLUGIN_DATA"]) / "local-coder"` (creating it) when that env var is set and non-empty; otherwise returns `Path(<this file's dir>)` (the in-tree fallback). Consumed by `config.py` (Task 3) and `server.py` (Task 4).

- [ ] **Step 1: Write the failing tests**

Create `mcp-servers/local-coder/tests/test_paths.py`:

```python
import os
from pathlib import Path

import paths


def test_plugin_data_dir_uses_env_var_when_set(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path))
    result = paths.plugin_data_dir()
    assert result == tmp_path / "local-coder"
    # It must create the directory (subprocesses write into it immediately).
    assert result.is_dir()


def test_plugin_data_dir_falls_back_to_in_tree_when_env_unset(monkeypatch):
    monkeypatch.delenv("CLAUDE_PLUGIN_DATA", raising=False)
    result = paths.plugin_data_dir()
    # Fallback is the directory containing paths.py itself.
    assert result == Path(paths.__file__).parent


def test_plugin_data_dir_falls_back_when_env_empty(monkeypatch):
    monkeypatch.setenv("CLAUDE_PLUGIN_DATA", "")
    result = paths.plugin_data_dir()
    assert result == Path(paths.__file__).parent
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd mcp-servers/local-coder && .venv/bin/python -m pytest tests/test_paths.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'paths'`

- [ ] **Step 3: Create the helper**

Create `mcp-servers/local-coder/paths.py`:

```python
import os
from pathlib import Path


def plugin_data_dir() -> Path:
    """Directory for the server's persistent runtime state (config.yaml,
    per-call log files, the provisioned venv).

    In an installed Claude Code plugin, ${CLAUDE_PLUGIN_DATA} is set and
    points at a directory that survives plugin updates — the documented
    home for Python virtualenvs, config, and caches. Outside a plugin
    (pytest, standalone/dev use), the env var is unset, so fall back to the
    in-tree directory next to this file, preserving existing behavior.
    """
    data = os.environ.get("CLAUDE_PLUGIN_DATA")
    if data:
        d = Path(data) / "local-coder"
        d.mkdir(parents=True, exist_ok=True)
        return d
    return Path(__file__).parent
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd mcp-servers/local-coder && .venv/bin/python -m pytest tests/test_paths.py -v`
Expected: PASS (3 tests)

- [ ] **Step 5: Commit**

```bash
git add mcp-servers/local-coder/paths.py mcp-servers/local-coder/tests/test_paths.py
git commit -m "local-coder: add plugin_data_dir() helper for persistent state"
```

---

### Task 2: Cross-platform config lock (fcntl / msvcrt)

**Files:**
- Modify: `mcp-servers/local-coder/config.py:1-49`
- Test: `mcp-servers/local-coder/tests/test_config.py`

**Interfaces:**
- Consumes: nothing new.
- Produces: `_config_lock()` unchanged signature/behavior for callers — a context manager holding an exclusive lock across the load→merge→save sequence. Internally selects `fcntl` (POSIX) or `msvcrt` (Windows) locking. Also exposes a module-level `_IS_WINDOWS: bool` (from `os.name == "nt"`) so tests can assert branch selection.

- [ ] **Step 1: Write the failing test**

Add to `mcp-servers/local-coder/tests/test_config.py`:

```python
def test_config_lock_selects_platform_lock_module():
    # The module must pick the OS-appropriate lock primitive at import:
    # fcntl on POSIX, msvcrt on Windows. This guards against the current
    # unconditional `import fcntl`, which crashes import on Windows.
    import config as config_module
    if config_module._IS_WINDOWS:
        import msvcrt  # noqa: F401 — must be importable on Windows
        assert config_module._lock_module.__name__ == "msvcrt"
    else:
        import fcntl  # noqa: F401
        assert config_module._lock_module.__name__ == "fcntl"


def test_config_lock_still_guards_the_critical_section(isolated_config):
    # The lock must still actually serialize: acquiring it, then confirming
    # the guarded save round-trips a value, proves the context manager
    # yields and releases cleanly on this platform.
    import config as config_module
    with config_module._config_lock():
        config_module.save_config({"backend": "aider", "model": "ollama/x",
                                   "fallback_models": [], "max_fallback_models": 3,
                                   "stall_timeout_seconds": 300, "target_repo_path": None,
                                   "branch_prefix": "local-coder/", "open_pr": False,
                                   "pr_base_branch": "main",
                                   "idle_notify_interval_seconds": 20,
                                   "extra_backend_args": []})
    assert config_module.load_config()["model"] == "ollama/x"
```

(Note: `test_config.py` already defines an `isolated_config` fixture that monkeypatches `config_module.CONFIG_PATH` to a temp file — reuse it.)

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd mcp-servers/local-coder && .venv/bin/python -m pytest tests/test_config.py -k "platform_lock or critical_section" -v`
Expected: FAIL — `_IS_WINDOWS` / `_lock_module` don't exist yet (`AttributeError`).

- [ ] **Step 3: Replace the import + lock with a platform branch**

In `config.py`, replace the top import block (lines 1-10) — remove the unconditional `import fcntl`, add the platform selection:

```python
import contextlib
import os
import tempfile
from pathlib import Path

import yaml

import ollama as ollama_module
from backends.common import KNOWN_BACKENDS

# Select the OS-appropriate file-locking primitive at import time. `fcntl`
# does not exist on Windows (importing it unconditionally crashes the
# server there); `msvcrt` is the Windows stdlib equivalent. Both are wrapped
# behind _config_lock() below so callers are platform-agnostic.
_IS_WINDOWS = os.name == "nt"
if _IS_WINDOWS:
    import msvcrt as _lock_module
else:
    import fcntl as _lock_module
```

Then replace the `_config_lock()` body (the `with open(...)` block, lines ~44-49) with:

```python
    lock_path = _lock_path()
    lock_path.touch(exist_ok=True)
    with open(lock_path, "w") as lock_file:
        if _IS_WINDOWS:
            # Lock 1 byte at offset 0; LK_LOCK blocks until the lock is free.
            lock_file.write("\0")
            lock_file.flush()
            lock_file.seek(0)
            _lock_module.locking(lock_file.fileno(), _lock_module.LK_LOCK, 1)
            try:
                yield
            finally:
                lock_file.seek(0)
                _lock_module.locking(lock_file.fileno(), _lock_module.LK_UNLCK, 1)
        else:
            _lock_module.flock(lock_file, _lock_module.LOCK_EX)
            try:
                yield
            finally:
                _lock_module.flock(lock_file, _lock_module.LOCK_UN)
```

Also update the docstring line that says "POSIX-only (fcntl)" to note it is now cross-platform (fcntl on POSIX, msvcrt on Windows).

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd mcp-servers/local-coder && .venv/bin/python -m pytest tests/test_config.py -v`
Expected: PASS (all config tests, including the two new ones; on this POSIX machine the `msvcrt` branch is not exercised but the selection test confirms `fcntl` is chosen).

- [ ] **Step 5: Commit**

```bash
git add mcp-servers/local-coder/config.py mcp-servers/local-coder/tests/test_config.py
git commit -m "local-coder: cross-platform config lock (fcntl on POSIX, msvcrt on Windows)"
```

---

### Task 3: config.yaml in the persistent dir + template seeding

**Files:**
- Modify: `mcp-servers/local-coder/config.py` (`CONFIG_PATH` at line 12; `load_config` at lines 52-54)
- Test: `mcp-servers/local-coder/tests/test_config.py`

**Interfaces:**
- Consumes: `plugin_data_dir()` from Task 1.
- Produces: `CONFIG_PATH` now resolves to `plugin_data_dir() / "config.yaml"`. `load_config()` seeds that path from the bundled default template (`Path(__file__).parent / "config.yaml"`) on first use if it doesn't exist. `_DEFAULT_CONFIG_PATH` (the bundled template) is a new module constant. `save_config()` / `_lock_path()` are unchanged — they already derive from `CONFIG_PATH.parent`, so they follow automatically.

- [ ] **Step 1: Write the failing test**

Add to `mcp-servers/local-coder/tests/test_config.py`:

```python
def test_load_config_seeds_from_default_template_when_missing(tmp_path, monkeypatch):
    # In a fresh install, the persistent config.yaml doesn't exist yet.
    # load_config() must seed it from the bundled default template rather
    # than crashing on a missing file.
    import config as config_module
    fresh = tmp_path / "config.yaml"
    assert not fresh.exists()
    monkeypatch.setattr(config_module, "CONFIG_PATH", fresh)

    cfg = config_module.load_config()

    assert fresh.exists()  # seeded
    assert cfg["backend"] == "aider"  # matches the bundled default template
    assert cfg["model"] == "ollama/qwen3-coder:30b"


def test_load_config_uses_existing_file_when_present(tmp_path, monkeypatch):
    import config as config_module
    existing = tmp_path / "config.yaml"
    existing.write_text("backend: aider\nmodel: ollama/custom\nfallback_models: []\n")
    monkeypatch.setattr(config_module, "CONFIG_PATH", existing)

    cfg = config_module.load_config()

    assert cfg["model"] == "ollama/custom"  # existing file wins, not re-seeded
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd mcp-servers/local-coder && .venv/bin/python -m pytest tests/test_config.py -k "seeds_from_default or uses_existing_file" -v`
Expected: `test_load_config_seeds_from_default_template_when_missing` FAILs (currently `load_config` does `open(CONFIG_PATH)` on a missing file → `FileNotFoundError`). `test_load_config_uses_existing_file_when_present` passes already (it writes the file first).

- [ ] **Step 3: Change `CONFIG_PATH` and add seeding**

In `config.py`, add the import near the other imports:

```python
from paths import plugin_data_dir
```

Replace the `CONFIG_PATH` line (line 12):

```python
# The bundled default config that ships with the plugin — used as a
# read-only template to seed the real config on first use.
_DEFAULT_CONFIG_PATH = Path(__file__).parent / "config.yaml"

# The live, user-mutable config lives in the persistent plugin-data dir so
# it survives plugin updates (the plugin root is wiped on update). Falls
# back to the in-tree path outside a plugin (tests/dev) via plugin_data_dir.
CONFIG_PATH = plugin_data_dir() / "config.yaml"
```

Replace `load_config()` (lines 52-54) with:

```python
def load_config() -> dict:
    # First use in a fresh install: the persistent config doesn't exist yet.
    # Seed it from the bundled default template. (Skip when the resolved
    # path IS the template itself — the in-tree/dev fallback — to avoid a
    # pointless self-copy.)
    if not CONFIG_PATH.exists() and CONFIG_PATH != _DEFAULT_CONFIG_PATH:
        CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        CONFIG_PATH.write_text(_DEFAULT_CONFIG_PATH.read_text())
    with open(CONFIG_PATH) as f:
        return yaml.safe_load(f)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd mcp-servers/local-coder && .venv/bin/python -m pytest tests/test_config.py -v`
Expected: PASS (all, including the two new seeding tests).

- [ ] **Step 5: Run the full suite (config change touches shared state)**

Run: `cd mcp-servers/local-coder && .venv/bin/python -m pytest tests/ -q`
Expected: zero failures. (The existing `isolated_config` fixtures monkeypatch `CONFIG_PATH`, so they are unaffected by the default-value change.)

- [ ] **Step 6: Commit**

```bash
git add mcp-servers/local-coder/config.py mcp-servers/local-coder/tests/test_config.py
git commit -m "local-coder: store config.yaml in persistent dir, seed from bundled default"
```

---

### Task 4: Move per-call log files to the persistent dir

**Files:**
- Modify: `mcp-servers/local-coder/server.py` (`OUTPUT_LOG_PATH` at line 41)
- Test: `mcp-servers/local-coder/tests/test_server.py`

**Interfaces:**
- Consumes: `plugin_data_dir()` from Task 1.
- Produces: `OUTPUT_LOG_PATH = plugin_data_dir() / "local-coder-output.log"`. The `_make_output_log_path()` per-call logic is unchanged (it does `OUTPUT_LOG_PATH.with_name(...)`, so only the base directory moves).

- [ ] **Step 1: Write the failing test**

Add to `mcp-servers/local-coder/tests/test_server.py`:

```python
def test_output_log_path_lives_under_plugin_data_dir(tmp_path, monkeypatch):
    # The per-call log base must live in the persistent plugin-data dir
    # (survives plugin updates), not the ephemeral plugin root. server.py
    # computes OUTPUT_LOG_PATH at import via plugin_data_dir(); verify the
    # resolution honors CLAUDE_PLUGIN_DATA by re-importing the module fresh.
    import importlib
    monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path))
    import server as server_module
    importlib.reload(server_module)
    try:
        assert server_module.OUTPUT_LOG_PATH.parent == tmp_path / "local-coder"
        assert server_module.OUTPUT_LOG_PATH.name == "local-coder-output.log"
    finally:
        monkeypatch.delenv("CLAUDE_PLUGIN_DATA", raising=False)
        importlib.reload(server_module)  # restore in-tree default for other tests
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd mcp-servers/local-coder && .venv/bin/python -m pytest tests/test_server.py::test_output_log_path_lives_under_plugin_data_dir -v`
Expected: FAIL — `OUTPUT_LOG_PATH.parent` is currently the in-tree dir, not `tmp_path/local-coder`, even with the env var set (the current code uses `Path(__file__).parent`).

- [ ] **Step 3: Change `OUTPUT_LOG_PATH`**

In `server.py`, add the import near the other imports:

```python
from paths import plugin_data_dir
```

Replace the `OUTPUT_LOG_PATH` line (line 41):

```python
OUTPUT_LOG_PATH = plugin_data_dir() / "local-coder-output.log"
```

Update the nearby comment block for `OUTPUT_LOG_PATH` to say it lives in the persistent plugin-data dir (not next to server.py).

- [ ] **Step 4: Run test to verify it passes**

Run: `cd mcp-servers/local-coder && .venv/bin/python -m pytest tests/test_server.py::test_output_log_path_lives_under_plugin_data_dir -v`
Expected: PASS

- [ ] **Step 5: Run the full suite**

Run: `cd mcp-servers/local-coder && .venv/bin/python -m pytest tests/ -q`
Expected: zero failures. (The log tests that monkeypatch `server.OUTPUT_LOG_PATH` are unaffected by the default-value change.)

- [ ] **Step 6: Commit**

```bash
git add mcp-servers/local-coder/server.py mcp-servers/local-coder/tests/test_server.py
git commit -m "local-coder: store per-call log files in persistent dir, not plugin root"
```

---

### Task 5: The provisioning hook (`hooks/provision-local-coder`)

**Files:**
- Create: `hooks/provision-local-coder` (extensionless bash, executable)
- Create: `tests/hooks/test-provision-local-coder` (bash test, executable)

**Interfaces:**
- Consumes: `${CLAUDE_PLUGIN_ROOT}` (to find requirements.txt) and `${CLAUDE_PLUGIN_DATA}` (where the venv goes), both exported to the hook process by Claude Code.
- Produces: side effect only — a provisioned venv at `${CLAUDE_PLUGIN_DATA}/local-coder/.venv` and a stamp file `${CLAUDE_PLUGIN_DATA}/local-coder/requirements.installed.txt`. No stdout. Always exits 0.

- [ ] **Step 1: Write the failing test**

Create `tests/hooks/test-provision-local-coder`:

```bash
#!/usr/bin/env bash
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
HOOK="$REPO_ROOT/hooks/provision-local-coder"

FAILURES=0
TEST_ROOT="$(mktemp -d)"
trap 'rm -rf "$TEST_ROOT"' EXIT

pass() { echo "  [PASS] $1"; }
fail() { echo "  [FAIL] $1"; FAILURES=$((FAILURES + 1)); }

# --- Test 1: creates the venv + stamp on first run ---
DATA1="$TEST_ROOT/data1"
mkdir -p "$DATA1"
CLAUDE_PLUGIN_ROOT="$REPO_ROOT" CLAUDE_PLUGIN_DATA="$DATA1" bash "$HOOK" >/dev/null 2>&1
if [ -x "$DATA1/local-coder/.venv/bin/python" ] || [ -x "$DATA1/local-coder/.venv/Scripts/python.exe" ]; then
    pass "first run creates the venv"
else
    fail "first run creates the venv"
fi
if [ -f "$DATA1/local-coder/requirements.installed.txt" ]; then
    pass "first run writes the stamp"
else
    fail "first run writes the stamp"
fi

# --- Test 2: idempotent (second run leaves venv mtime unchanged-ish) ---
STAMP="$DATA1/local-coder/requirements.installed.txt"
BEFORE="$(cat "$STAMP" 2>/dev/null || true)"
CLAUDE_PLUGIN_ROOT="$REPO_ROOT" CLAUDE_PLUGIN_DATA="$DATA1" bash "$HOOK" >/dev/null 2>&1
AFTER="$(cat "$STAMP" 2>/dev/null || true)"
if [ "$BEFORE" = "$AFTER" ]; then
    pass "second run is idempotent (stamp unchanged)"
else
    fail "second run is idempotent (stamp unchanged)"
fi

# --- Test 3: no CLAUDE_PLUGIN_DATA → skips silently, exits 0 ---
if env -u CLAUDE_PLUGIN_DATA CLAUDE_PLUGIN_ROOT="$REPO_ROOT" bash "$HOOK" >/dev/null 2>&1; then
    pass "exits 0 when CLAUDE_PLUGIN_DATA is unset"
else
    fail "exits 0 when CLAUDE_PLUGIN_DATA is unset"
fi

# --- Test 4: no usable python → exits 0, writes no venv ---
DATA2="$TEST_ROOT/data2"
mkdir -p "$DATA2"
# Empty PATH so no python3/python/py is found.
if env -i PATH="" CLAUDE_PLUGIN_ROOT="$REPO_ROOT" CLAUDE_PLUGIN_DATA="$DATA2" bash "$HOOK" >/dev/null 2>&1; then
    pass "exits 0 when no python is available"
else
    fail "exits 0 when no python is available"
fi
if [ ! -e "$DATA2/local-coder/.venv" ]; then
    pass "creates no venv when no python is available"
else
    fail "creates no venv when no python is available"
fi

if [ "$FAILURES" -gt 0 ]; then echo "STATUS: FAILED ($FAILURES)"; exit 1; fi
echo "STATUS: PASSED"
```

Make it executable: `chmod +x tests/hooks/test-provision-local-coder`

- [ ] **Step 2: Run the test to verify it fails**

Run: `bash tests/hooks/test-provision-local-coder`
Expected: FAIL — the hook doesn't exist yet (`bash: .../hooks/provision-local-coder: No such file or directory`), so the venv/stamp assertions fail.

- [ ] **Step 3: Create the provisioning hook**

Create `hooks/provision-local-coder`:

```bash
#!/usr/bin/env bash
# SessionStart hook: provision the local-coder MCP server's Python venv into
# the persistent plugin-data dir. Best-effort — NEVER fails the session.
#
# Extensionless on purpose (see docs/windows/polyglot-hooks.md): Claude Code
# on Windows prepends `bash` to any command containing `.sh`. Dispatched by
# hooks/run-hook.cmd. Emits NO stdout (unlike hooks/session-start, which has
# a JSON stdout contract) — all diagnostics go to stderr.
set -uo pipefail

# Only meaningful in an installed plugin, where CLAUDE_PLUGIN_DATA is set.
if [ -z "${CLAUDE_PLUGIN_DATA:-}" ]; then
    exit 0
fi

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PLUGIN_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
REQ="${PLUGIN_ROOT}/mcp-servers/local-coder/requirements.txt"
DATA_DIR="${CLAUDE_PLUGIN_DATA}/local-coder"
VENV="${DATA_DIR}/.venv"
STAMP="${DATA_DIR}/requirements.installed.txt"

mkdir -p "$DATA_DIR"

# Find a Python >=3.10 that actually runs (skips the Windows Store stub,
# which exits nonzero on `-c ""`). No cygpath (per repo convention).
PYTHON=""
for candidate in "python3" "python" "py -3"; do
    if $candidate -c "import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)" >/dev/null 2>&1; then
        PYTHON="$candidate"
        break
    fi
done
if [ -z "$PYTHON" ]; then
    echo "[local-coder] provisioning skipped: no Python >=3.10 found on PATH" >&2
    exit 0
fi

# Up to date? (venv present AND stamp matches current requirements.txt)
if [ -e "$VENV" ] && [ -f "$STAMP" ] && cmp -s "$REQ" "$STAMP"; then
    exit 0
fi

echo "[local-coder] provisioning venv (first run or requirements changed)..." >&2

# Create the venv if missing.
if [ ! -e "$VENV" ]; then
    if ! $PYTHON -m venv "$VENV" >&2; then
        echo "[local-coder] provisioning failed: could not create venv" >&2
        rm -f "$STAMP"
        exit 0
    fi
fi

# Locate the venv's python (POSIX vs Windows layout).
if [ -x "$VENV/bin/python" ]; then
    VENV_PY="$VENV/bin/python"
elif [ -x "$VENV/Scripts/python.exe" ]; then
    VENV_PY="$VENV/Scripts/python.exe"
else
    echo "[local-coder] provisioning failed: venv python not found" >&2
    rm -f "$STAMP"
    exit 0
fi

# Install deps. Stamp only on success (so a failure retries next session).
if "$VENV_PY" -m pip install -r "$REQ" >&2; then
    cp "$REQ" "$STAMP"
    echo "[local-coder] provisioning complete" >&2
else
    echo "[local-coder] provisioning failed: pip install error" >&2
    rm -f "$STAMP"
fi

exit 0
```

Make it executable: `chmod +x hooks/provision-local-coder`

- [ ] **Step 4: Run the test to verify it passes**

Run: `bash tests/hooks/test-provision-local-coder`
Expected: `STATUS: PASSED` (all 4 checks — note Test 1 actually creates a real venv + pip installs, so it needs network and takes some seconds).

- [ ] **Step 5: Commit**

```bash
git add hooks/provision-local-coder tests/hooks/test-provision-local-coder
git commit -m "local-coder: SessionStart hook to provision the venv into plugin-data dir"
```

---

### Task 6: The launch hook (`hooks/launch-local-coder`)

**Files:**
- Create: `hooks/launch-local-coder` (extensionless bash, executable)
- Test: `tests/hooks/test-launch-local-coder` (bash test, executable)

**Interfaces:**
- Consumes: `${CLAUDE_PLUGIN_DATA}` (to find the venv), and `$1` = the absolute path to `server.py`.
- Produces: execs the venv interpreter on `server.py` (replacing the shell). On missing venv/interpreter, exits 1 with a stderr message. Unlike provisioning, this does NOT silent-exit-0.

- [ ] **Step 1: Write the failing test**

Create `tests/hooks/test-launch-local-coder`:

```bash
#!/usr/bin/env bash
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
HOOK="$REPO_ROOT/hooks/launch-local-coder"

FAILURES=0
TEST_ROOT="$(mktemp -d)"
trap 'rm -rf "$TEST_ROOT"' EXIT
pass() { echo "  [PASS] $1"; }
fail() { echo "  [FAIL] $1"; FAILURES=$((FAILURES + 1)); }

# --- Test 1: missing venv → exit 1 with a clear message ---
DATA1="$TEST_ROOT/data1"; mkdir -p "$DATA1"
OUT="$(CLAUDE_PLUGIN_DATA="$DATA1" bash "$HOOK" /nonexistent/server.py 2>&1)"; RC=$?
if [ "$RC" -eq 1 ]; then pass "missing venv exits 1"; else fail "missing venv exits 1 (got $RC)"; fi
if printf '%s' "$OUT" | grep -q "venv"; then pass "missing venv prints a clear reason"; else fail "missing venv prints a clear reason"; fi

# --- Test 2: with a fake venv python, it execs that python on the arg ---
DATA2="$TEST_ROOT/data2"
mkdir -p "$DATA2/local-coder/.venv/bin"
# Fake python that just echoes a marker + its arg, so we can prove exec.
cat > "$DATA2/local-coder/.venv/bin/python" <<'FAKE'
#!/usr/bin/env bash
echo "FAKE_PY_RAN arg=$1"
FAKE
chmod +x "$DATA2/local-coder/.venv/bin/python"
OUT="$(CLAUDE_PLUGIN_DATA="$DATA2" bash "$HOOK" /some/server.py 2>&1)"; RC=$?
if [ "$RC" -eq 0 ] && printf '%s' "$OUT" | grep -q "FAKE_PY_RAN arg=/some/server.py"; then
    pass "execs the venv python on the server.py arg"
else
    fail "execs the venv python on the server.py arg (rc=$RC out=$OUT)"
fi

if [ "$FAILURES" -gt 0 ]; then echo "STATUS: FAILED ($FAILURES)"; exit 1; fi
echo "STATUS: PASSED"
```

Make it executable: `chmod +x tests/hooks/test-launch-local-coder`

- [ ] **Step 2: Run the test to verify it fails**

Run: `bash tests/hooks/test-launch-local-coder`
Expected: FAIL — the hook doesn't exist yet.

- [ ] **Step 3: Create the launch hook**

Create `hooks/launch-local-coder`:

```bash
#!/usr/bin/env bash
# MCP launch hook: exec the local-coder server using the venv interpreter
# provisioned into the persistent plugin-data dir by hooks/provision-local-coder.
#
# Extensionless (see docs/windows/polyglot-hooks.md); dispatched by
# hooks/run-hook.cmd. Arg $1 is the absolute path to server.py.
#
# Unlike the provisioning hook, this does NOT silent-exit-0 on failure: an
# MCP server that can't find its interpreter genuinely cannot run, so we
# exit nonzero and let Claude Code surface it as a failed server rather than
# a connected-but-dead one.
set -uo pipefail

SERVER_PY="${1:-}"
if [ -z "$SERVER_PY" ]; then
    echo "[local-coder] launch failed: no server.py path argument" >&2
    exit 1
fi

VENV="${CLAUDE_PLUGIN_DATA:-}/local-coder/.venv"
if [ -x "$VENV/bin/python" ]; then
    exec "$VENV/bin/python" "$SERVER_PY"
elif [ -x "$VENV/Scripts/python.exe" ]; then
    exec "$VENV/Scripts/python.exe" "$SERVER_PY"
fi

echo "[local-coder] launch failed: venv interpreter not found at $VENV — the" \
     "SessionStart provisioning hook did not complete (check its stderr)." >&2
exit 1
```

Make it executable: `chmod +x hooks/launch-local-coder`

- [ ] **Step 4: Run the test to verify it passes**

Run: `bash tests/hooks/test-launch-local-coder`
Expected: `STATUS: PASSED` (both checks).

- [ ] **Step 5: Commit**

```bash
git add hooks/launch-local-coder tests/hooks/test-launch-local-coder
git commit -m "local-coder: cross-platform launch hook (execs venv interpreter on server.py)"
```

---

### Task 7: Wire the hooks into `hooks.json` and `.mcp.json`

**Files:**
- Modify: `hooks/hooks.json`
- Modify: `.mcp.json`

**Interfaces:**
- Consumes: `hooks/provision-local-coder` (Task 5), `hooks/launch-local-coder` (Task 6), the existing `hooks/run-hook.cmd`.
- Produces: the wired configuration. No new code symbols.

- [ ] **Step 1: Add the provisioning hook to `hooks.json`**

`hooks/hooks.json` currently has one SessionStart entry (the `session-start` context injector). Add a second entry to the same `SessionStart` array (do NOT modify the existing one). The file becomes:

```json
{
  "hooks": {
    "SessionStart": [
      {
        "matcher": "startup|clear|compact",
        "hooks": [
          {
            "type": "command",
            "command": "\"${CLAUDE_PLUGIN_ROOT}/hooks/run-hook.cmd\" session-start",
            "async": false
          }
        ]
      },
      {
        "matcher": "startup|clear|compact",
        "hooks": [
          {
            "type": "command",
            "command": "\"${CLAUDE_PLUGIN_ROOT}/hooks/run-hook.cmd\" provision-local-coder",
            "async": false
          }
        ]
      }
    ]
  }
}
```

- [ ] **Step 2: Verify `hooks.json` is valid JSON**

Run: `python3 -c "import json; json.load(open('hooks/hooks.json')); print('valid')"`
Expected: `valid`

- [ ] **Step 3: Change `.mcp.json` to launch via the hook**

Replace `.mcp.json` entirely with:

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

- [ ] **Step 4: Verify `.mcp.json` is valid JSON**

Run: `python3 -c "import json; json.load(open('.mcp.json')); print('valid')"`
Expected: `valid`

- [ ] **Step 5: Run both hook test suites end-to-end via the real dispatcher**

Run:
```bash
bash tests/hooks/test-provision-local-coder
bash tests/hooks/test-launch-local-coder
bash tests/hooks/test-session-start.sh
```
Expected: all three print `STATUS: PASSED` (the third confirms we didn't break the existing session-start hook).

- [ ] **Step 6: Commit**

```bash
git add hooks/hooks.json .mcp.json
git commit -m "local-coder: wire provisioning + launch hooks into hooks.json and .mcp.json"
```

---

### Task 8: Update the README for auto-provisioning

**Files:**
- Modify: `mcp-servers/local-coder/README.md` (Prerequisites section, lines ~8-25)

**Interfaces:** none (docs only).

- [ ] **Step 1: Replace the manual-venv prerequisite with the auto-provisioning description**

In `README.md`, the Prerequisites section currently ends with a "This server's own dependencies installed into its venv" block containing a manual `python3.13 -m venv .venv` + `pip install` step. Replace that block with:

```markdown
- The server's Python dependencies are provisioned **automatically**: when
  the plugin is installed, a `SessionStart` hook creates a virtualenv under
  the plugin's persistent data directory (`${CLAUDE_PLUGIN_DATA}/local-coder/.venv`)
  and installs `requirements.txt` into it, re-installing only when
  `requirements.txt` changes. You need a Python 3.10+ interpreter on `PATH`
  (`python3`, `python`, or the `py` launcher on Windows) and network access
  on first run; nothing else. On Windows, provisioning and launch run
  through Git Bash (the same requirement as all Superpowers hooks — see
  `docs/windows/polyglot-hooks.md`).

  For local development outside a plugin install, create the venv manually:
  ```bash
  cd mcp-servers/local-coder
  python3.13 -m venv .venv
  .venv/bin/pip install -r requirements.txt
  ```
```

Also add a one-line note that config and per-call logs now live under
`${CLAUDE_PLUGIN_DATA}/local-coder/` (so they survive plugin updates) rather
than next to the server, wherever the README references the config/log
location.

- [ ] **Step 2: Verify the README reads coherently**

Run: `grep -n "CLAUDE_PLUGIN_DATA\|SessionStart\|provision" mcp-servers/local-coder/README.md`
Expected: the auto-provisioning description is present and the old "you must manually create the venv to use the server" framing is gone (manual steps remain only as the dev-outside-plugin fallback).

- [ ] **Step 3: Commit**

```bash
git add mcp-servers/local-coder/README.md
git commit -m "docs: describe automatic venv provisioning and persistent-dir paths"
```

---

### Task 9: Full-suite verification checkpoint (no code changes)

**Files:** none (verification only).

- [ ] **Step 1: Run the full Python suite**

Run: `cd mcp-servers/local-coder && .venv/bin/python -m pytest tests/ -q`
Expected: zero failures, count >= 127 (baseline) + the new tests from Tasks 1-4.

- [ ] **Step 2: Run all hook tests**

Run:
```bash
bash tests/hooks/test-session-start.sh
bash tests/hooks/test-provision-local-coder
bash tests/hooks/test-launch-local-coder
```
Expected: all `STATUS: PASSED`.

- [ ] **Step 3: Confirm no runtime state is written under the plugin root**

Run: `git status --short mcp-servers/local-coder/` (after the suite) and confirm no `config.yaml`-mutation or stray `local-coder-output*.log` appears as a change (they should now be written to temp/data dirs, not the tree). If any appear, that's a path-resolution bug — investigate before proceeding.

- [ ] **Step 4: (Manual, out-of-band) Item 1 smoke test — the acceptance gate**

Not an automated step. In a fresh session with the plugin (re)installed:
1. Confirm the SessionStart provisioning hook created `${CLAUDE_PLUGIN_DATA}/local-coder/.venv` with no manual step (check the plugin-data dir; watch the hook's stderr).
2. `claude mcp list` shows `local-coder` **connected**.
3. Run a real `delegate_implementation` and confirm it works end-to-end (item 7's fixes make it observable).
Record the outcome in the handoff doc.

---

## Self-Review

**Spec coverage:**
- C1 (hooks.json provisioning entry) → Task 7. ✓
- C2 (provision-local-coder) → Task 5. ✓
- C3 (.mcp.json launch) → Task 7. ✓
- C4 (launch-local-coder) → Task 6. ✓
- C5 (plugin_data_dir helper) → Task 1. ✓
- C6 (config path + cross-platform lock) → Tasks 2 (lock) + 3 (path/seeding). ✓
- C7 (server.py log path) → Task 4. ✓
- Testing (unit + hook + manual acceptance) → Tasks 1-6 inline + Task 9. ✓
- Cursor `hooks-cursor.json` parity: the spec left this as "default: add it, harmless." NOTE: it is NOT covered by a task above — deliberately deferred as a soft/optional item (the spec flagged it as the one soft spot). If parity is wanted, add the same provisioning entry to `hooks-cursor.json`; not doing so only means Cursor sessions don't auto-provision, which is harmless where local-coder isn't used. Left out of the core plan to keep scope tight; call it out to the reviewer.

**Placeholder scan:** every step has concrete code, exact paths, exact commands with expected output. No TBD/"handle errors"/"add tests" placeholders.

**Type consistency:** `plugin_data_dir()` (Task 1) is consumed identically in Tasks 3 and 4 (`plugin_data_dir() / "..."`). `CONFIG_PATH`, `_DEFAULT_CONFIG_PATH`, `_IS_WINDOWS`, `_lock_module` names are defined in Tasks 2-3 and referenced consistently in their tests. Hook names (`provision-local-coder`, `launch-local-coder`) match between the create tasks (5, 6), the wiring task (7), and the tests.
