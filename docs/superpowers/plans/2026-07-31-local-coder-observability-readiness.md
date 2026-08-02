# local-coder Observability & Readiness Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. NOTE: for THIS plan the executor is the Codex CLI (via the `codex:codex-rescue` subagent), orchestrated + reviewed by the controller. Each task is dispatched to Codex with its brief; the controller reviews the returned diff before the next task.

**Goal:** Add pollable file-based status, a hybrid coarse-phase + latest-activity model, `/local-coder:status` and `/local-coder:setup` commands, and orphan cleanup to the synchronous local-coder MCP delegation path — with no async conversion.

**Architecture:** A new isolated `status.py` module owns all record read/write, phase tracking, monotonic dedupe, and the orphan sweep. `server.py` gets four additive, non-fatal-guarded hooks into the existing delegation path. `common.py` gains one optional `on_start(pid)` callback. A `status_cli.py` + two plugin commands surface the records; a SessionEnd hook plus startup sweep handle orphans.

**Tech Stack:** Python 3.10+ (stdlib only — json, os, pathlib, hashlib, time, signal), pytest, bash hooks (POSIX + Windows-aware). No new third-party dependencies (zero-dependency plugin constraint).

## Global Constraints

- No async conversion: `delegate_implementation` stays synchronous and returns the final result.
- No new third-party dependencies. Stdlib + existing deps only.
- Every status/cleanup write is NON-FATAL: on any failure it warns and continues; it must NEVER kill or fail a delegation.
- State writes that touch shared files reuse `config._config_lock()` for cross-process safety.
- The ollama readiness probe targets `http://localhost:11434` ONLY — local, unauthenticated. No remote endpoint, no credential/token handling.
- State dir: `${CLAUDE_PLUGIN_DATA}/status/<slug>-<sha256(realpath(target_repo))[:16]>`; fall back to a tmpdir root when `CLAUDE_PLUGIN_DATA` is unset.
- Phase is coarse and lifecycle-driven (`starting`/`working`/`committing`/`done`/`failed`) — NOT regex over aider's output wording. `latestActivity` is free text (the existing `latest_line`), never parsed into a phase.
- Record rewritten only when `phase` OR `latestActivity` changes (monotonic dedupe).
- Commands use `disable-model-invocation: true` + a tight `allowed-tools` allowlist + `!` command-expansion.
- Follow existing repo conventions: python tests in `mcp-servers/local-coder/tests/test_*.py`; hook tests in `tests/hooks/test-*` (shell). Never stage `.gitignore`.

---

### Task 1: `status.py` — record model, phase updater, dedupe, sweep

**Files:**
- Create: `mcp-servers/local-coder/status.py`
- Test: `mcp-servers/local-coder/tests/test_status.py`

**Interfaces:**
- Consumes: `config._config_lock` (context manager), `config.plugin_data_dir()` (returns the `${CLAUDE_PLUGIN_DATA}` Path).
- Produces:
  - `resolve_state_dir(target_repo: str) -> Path`
  - `generate_job_id() -> str` (`lc-<base36-time>-<rand>`)
  - `create_record(target_repo, branch, model, output_log, session_id=None) -> dict` (writes `<id>.json`, upserts index, runs the startup sweep, returns the record with its `id`)
  - `ProgressUpdater` class with `on_output(chunk: str) -> None` (flips `starting`→`working` on first non-empty chunk) and `on_activity(line: str) -> None` (updates `latestActivity`); both deduped.
  - `set_pid(record, pid: int) -> None`
  - `finalize(record, *, status: str, phase: str, commit_sha=None, error_message=None) -> None`
  - `list_records(target_repo, all_sessions=False, session_id=None) -> list[dict]`
  - `sweep_orphans(target_repo) -> None` (marks `running` records with a dead pid as `orphaned`)
  - `read_record(job_file: Path) -> dict`

- [ ] **Step 1: Write failing tests for record lifecycle + dedupe + sweep**

```python
# tests/test_status.py
import json, os, time
from pathlib import Path
import pytest
import status

@pytest.fixture
def repo(tmp_path, monkeypatch):
    data = tmp_path / "plugindata"
    data.mkdir()
    monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(data))
    r = tmp_path / "repo"; r.mkdir()
    return str(r)

def _records_dir(repo):
    return status.resolve_state_dir(repo)

def test_create_record_writes_running_record(repo):
    rec = status.create_record(repo, branch="dev", model="ollama/qwen2.5-coder:7b",
                               output_log="/tmp/x.log", session_id="sess1")
    assert rec["status"] == "running"
    assert rec["phase"] == "starting"
    assert rec["id"].startswith("lc-")
    jf = status.resolve_state_dir(repo) / f"{rec['id']}.json"
    assert jf.exists()
    assert json.loads(jf.read_text())["sessionId"] == "sess1"

def test_progress_updater_first_chunk_moves_to_working(repo):
    rec = status.create_record(repo, "dev", "m", "/tmp/x.log")
    up = status.ProgressUpdater(repo, rec["id"])
    up.on_output("some output")
    assert status.read_record(status.resolve_state_dir(repo) / f"{rec['id']}.json")["phase"] == "working"

def test_progress_updater_dedupes_unchanged_activity(repo):
    rec = status.create_record(repo, "dev", "m", "/tmp/x.log")
    up = status.ProgressUpdater(repo, rec["id"])
    up.on_activity("line A")
    jf = status.resolve_state_dir(repo) / f"{rec['id']}.json"
    mtime1 = jf.stat().st_mtime_ns
    time.sleep(0.01)
    up.on_activity("line A")  # unchanged -> no rewrite
    assert jf.stat().st_mtime_ns == mtime1
    up.on_activity("line B")  # changed -> rewrite
    assert jf.stat().st_mtime_ns != mtime1

def test_finalize_sets_terminal_state(repo):
    rec = status.create_record(repo, "dev", "m", "/tmp/x.log")
    status.finalize(rec, status="completed", phase="done", commit_sha="abc123")
    jf = json.loads((status.resolve_state_dir(repo) / f"{rec['id']}.json").read_text())
    assert jf["status"] == "completed" and jf["phase"] == "done"
    assert jf["commitSha"] == "abc123" and jf["pid"] is None
    assert jf["completedAt"]

def test_sweep_marks_dead_pid_running_as_orphaned(repo):
    rec = status.create_record(repo, "dev", "m", "/tmp/x.log")
    status.set_pid(rec, 999999)  # almost certainly not alive
    status.sweep_orphans(repo)
    jf = json.loads((status.resolve_state_dir(repo) / f"{rec['id']}.json").read_text())
    assert jf["status"] == "orphaned"

def test_state_dir_is_workspace_keyed(repo, tmp_path):
    other = tmp_path / "other_repo"; other.mkdir()
    assert status.resolve_state_dir(repo) != status.resolve_state_dir(str(other))
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd mcp-servers/local-coder && python -m pytest tests/test_status.py -v`
Expected: FAIL (module `status` not found / functions undefined)

- [ ] **Step 3: Implement `status.py`**

Implement per the Interfaces block. Key details:
- `resolve_state_dir`: slug = sanitized `os.path.basename(target_repo)` (`[^A-Za-z0-9._-]+` → `-`, strip leading/trailing `-`, default `workspace`); hash = `hashlib.sha256(os.path.realpath(target_repo).encode()).hexdigest()[:16]`; root = `plugin_data_dir()/"status"` or, if `CLAUDE_PLUGIN_DATA` unset, `Path(tempfile.gettempdir())/"local-coder-status"`. `mkdir(parents=True, exist_ok=True)`.
- Record fields exactly as the spec's JSON shape (camelCase keys: `id, sessionId, status, phase, latestActivity, model, targetRepo, branch, pid, outputLog, createdAt, updatedAt, completedAt, commitSha, errorMessage`). ISO-8601 UTC timestamps.
- Index `state.json` in the state dir: `{ "version": 1, "jobs": [ {id,status,phase,updatedAt,...small summary...} ] }`, pruned to newest 50 by `updatedAt`, written under `config._config_lock()`.
- `ProgressUpdater` holds `last_phase`, `last_activity`; only writes `<id>.json` + upserts index when a tracked field changes.
- `sweep_orphans`: for each `running` record, `pid` present and not `_pid_alive(pid)` → set `status="orphaned"`, `phase="failed"`, `completedAt=now`.
- `_pid_alive(pid)`: `os.kill(pid, 0)` in try/except (ESRCH → dead, EPERM → alive).
- Every public write function wraps its body so an unexpected exception is swallowed with a `sys.stderr` warning (non-fatal contract) — EXCEPT the pure readers (`list_records`, `read_record`) which may raise.

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd mcp-servers/local-coder && python -m pytest tests/test_status.py -v`
Expected: PASS (all)

- [ ] **Step 5: Commit**

```bash
git add mcp-servers/local-coder/status.py mcp-servers/local-coder/tests/test_status.py
git commit -m "local-coder: add status record module (record, phase updater, dedupe, sweep)"
```

---

### Task 2: `common.py` — optional `on_start(pid)` callback

**Files:**
- Modify: `mcp-servers/local-coder/backends/common.py` (`run_monitored_subprocess`)
- Test: `mcp-servers/local-coder/tests/test_backends_common.py` (additions)

**Interfaces:**
- Consumes: nothing new.
- Produces: `run_monitored_subprocess(..., on_start: Callable[[int], None] | None = None)` — invoked exactly once, right after `Popen`, with the child pid. Absent callback = unchanged behavior.

- [ ] **Step 1: Write failing tests**

```python
def test_run_monitored_subprocess_invokes_on_start_with_pid():
    seen = []
    common.run_monitored_subprocess(
        ["sh", "-c", "echo hi"], cwd=".",
        stall_timeout_seconds=30, idle_notify_interval_seconds=5,
        on_start=lambda pid: seen.append(pid),
    )
    assert len(seen) == 1 and isinstance(seen[0], int) and seen[0] > 0

def test_run_monitored_subprocess_without_on_start_unchanged():
    r = common.run_monitored_subprocess(
        ["sh", "-c", "echo hi"], cwd=".",
        stall_timeout_seconds=30, idle_notify_interval_seconds=5,
    )
    assert r.returncode == 0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd mcp-servers/local-coder && python -m pytest tests/test_backends_common.py -k on_start -v`
Expected: FAIL (unexpected kwarg `on_start`)

- [ ] **Step 3: Implement**

Add `on_start: Callable[[int], None] | None = None` to the signature. Right after the `subprocess.Popen(...)` call, add: `if on_start is not None:` then call `on_start(process.pid)` inside a try/except that warns to stderr and continues (non-fatal — the pid callback must not break the run).

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd mcp-servers/local-coder && python -m pytest tests/test_backends_common.py -v`
Expected: PASS (new + all existing)

- [ ] **Step 5: Commit**

```bash
git add mcp-servers/local-coder/backends/common.py mcp-servers/local-coder/tests/test_backends_common.py
git commit -m "local-coder: add optional on_start(pid) callback to run_monitored_subprocess"
```

---

### Task 3: `server.py` — four additive status hooks

**Files:**
- Modify: `mcp-servers/local-coder/server.py` (delegation path: start, `make_on_output`, `make_on_tick`, the `try/finally`)
- Modify: `mcp-servers/local-coder/backends/aider.py` (thread `on_start` from the backend call — only if the backend, not server, owns the Popen; wire `on_start` through `run_backend`'s call to `run_monitored_subprocess`)
- Test: `mcp-servers/local-coder/tests/test_server.py` (additions)

**Interfaces:**
- Consumes: `status.create_record`, `status.ProgressUpdater`, `status.set_pid`, `status.finalize` (Task 1); `run_monitored_subprocess(..., on_start=...)` (Task 2).
- Produces: no new public surface; internal wiring only.

**Wiring (four points), each wrapped in a non-fatal guard identical in spirit to the existing announce/tick guards:**
1. After `output_log_path` is created, before backend run: `record = status.create_record(target_repo=..., branch=..., model=..., output_log=str(output_log_path), session_id=os.environ.get("CLAUDE_SESSION_ID"))`. Build a module-level `ProgressUpdater(record's repo, record["id"])`.
2. Inside `make_on_output`'s callback, after the existing log append: `updater.on_output(chunk)`.
3. Inside `make_on_tick`'s callback, after the existing `latest_line` capture: `updater.on_activity(latest_line)`.
4. In the existing `try/finally`, at finalize: on success `status.finalize(record, status="completed", phase="committing"→"done", commit_sha=result.commit_sha)`; when the commit succeeded locally but push failed or timed out, `status.finalize(record, status="completed", phase="committing", commit_sha=result.commit_sha, error_message=push_error)`; on failure/exception `status.finalize(record, status="failed", phase="failed", error_message=...)`. Place beside the existing `_ACTIVE_OUTPUT_LOGS.discard(...)`.
   - PID: pass an `on_start=lambda pid: status.set_pid(record, pid)` down to `run_monitored_subprocess` (through the backend's `run_backend`).

- [ ] **Step 1: Write failing tests**

```python
# tests/test_server.py additions
def test_delegation_creates_and_finalizes_status_record(tmp_repo_env):
    # run a delegation with a stubbed backend that returns success + commit_sha
    ...
    recs = status.list_records(tmp_repo_env.repo, all_sessions=True)
    assert recs and recs[0]["status"] == "completed"
    assert recs[0]["phase"] == "done"

def test_status_write_failure_is_nonfatal(tmp_repo_env, monkeypatch):
    # make status.create_record raise; delegation must still succeed
    monkeypatch.setattr(status, "create_record", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    result = run_delegation(...)  # stubbed backend success
    assert result["success"] is True
```

(Follow the file's existing harness/fixtures for driving a delegation with a stubbed backend — reuse the established pattern in `test_server.py`; do not invent a new harness.)

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd mcp-servers/local-coder && python -m pytest tests/test_server.py -k status -v`
Expected: FAIL (no status wiring yet)

- [ ] **Step 3: Implement the four hooks + pid plumbing**

Wire exactly the four points above. Each status call sits inside a `try/except Exception as e:` that warns to stderr (`f"[local-coder] warning: status update failed: {e}"`) and continues — mirroring the existing announce/tick guards. Do not alter any existing delegation logic, log-retention, config-lock, or working-tree-restore code.

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd mcp-servers/local-coder && python -m pytest tests/test_server.py -v`
Expected: PASS (new + all existing)

- [ ] **Step 5: Commit**

```bash
git add mcp-servers/local-coder/server.py mcp-servers/local-coder/backends/aider.py mcp-servers/local-coder/tests/test_server.py
git commit -m "local-coder: wire additive status hooks into delegation path (guarded, non-fatal)"
```

---

### Task 4: `status_cli.py` — status render + setup checks

**Files:**
- Create: `mcp-servers/local-coder/status_cli.py`
- Test: `mcp-servers/local-coder/tests/test_status_cli.py`

**Interfaces:**
- Consumes: `status.list_records`, `status.read_record`, `status.resolve_state_dir` (Task 1); `config.load_config`, `config._validate_ollama_model` (existing); venv path resolution (mirror `ensure-local-coder-venv`: `${DATA_DIR}/.venv/bin/python` or `Scripts/python.exe`).
- Produces: CLI `python status_cli.py status [job-id] [--all] [--json]` and `python status_cli.py setup [--json]`.

**setup checks (each returns pass/fail + actionable next-step):**
1. venv provisioned — venv python exists + stamp `requirements.installed.txt` present under `${CLAUDE_PLUGIN_DATA}/.venv`.
2. aider importable — run `<venv-python> -c "import aider"`; nonzero → fail with the scipy/dyld hint.
3. ollama reachable + model — `urllib.request` GET `http://localhost:11434/api/tags` (2s timeout); parse model names; assert configured model (bare name after `ollama/`) present. Fail → "start ollama / `ollama pull <model>`". MUST NOT hit any other host.
4. config valid — `config.load_config()` parses; `model` non-empty and `stall_timeout_seconds` positive.

- [ ] **Step 1: Write failing tests**

```python
# tests/test_status_cli.py
import json, subprocess, sys
import status_cli

def test_status_table_renders_records(monkeypatch, repo_with_records):
    out = status_cli.render_status(repo_with_records.repo, job_id=None, all_sessions=True, as_json=False)
    assert "phase" in out.lower()  # header present
    assert repo_with_records.job_id in out

def test_setup_ollama_check_targets_localhost_only(monkeypatch):
    calls = []
    def fake_urlopen(req, *a, **k):
        calls.append(req.full_url if hasattr(req, "full_url") else req)
        raise OSError("refused")  # simulate daemon down
    monkeypatch.setattr(status_cli.urllib.request, "urlopen", fake_urlopen)
    report = status_cli.check_ollama(model="ollama/qwen2.5-coder:7b")
    assert report["ok"] is False
    assert all("localhost:11434" in str(c) for c in calls)

def test_setup_config_check_flags_bad_model(monkeypatch):
    monkeypatch.setattr(status_cli.config, "load_config", lambda: {"model": "", "stall_timeout_seconds": 300})
    report = status_cli.check_config()
    assert report["ok"] is False and "model" in report["nextStep"].lower()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd mcp-servers/local-coder && python -m pytest tests/test_status_cli.py -v`
Expected: FAIL

- [ ] **Step 3: Implement `status_cli.py`**

`argparse` with `status` and `setup` subcommands. `render_status` builds a compact Markdown table (columns: id, status, phase, latest activity, elapsed, model, output log). `setup` runs the four checks, prints a per-check pass/fail line + next-step; `--json` emits the structured report. Determine target_repo via `git rev-parse --show-toplevel` on cwd (fall back to cwd). All output to stdout.

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd mcp-servers/local-coder && python -m pytest tests/test_status_cli.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add mcp-servers/local-coder/status_cli.py mcp-servers/local-coder/tests/test_status_cli.py
git commit -m "local-coder: add status_cli (status table + setup readiness checks)"
```

---

### Task 5: Plugin commands `/local-coder:status` and `/local-coder:setup`

**Files:**
- Create: `commands/local-coder-status.md`
- Create: `commands/local-coder-setup.md`

(Plugin `commands/*.md` auto-load; namespacing follows the plugin name → `/local-coder:status`. Confirm the existing command-file naming convention in this repo's `commands/` dir before finalizing the filenames; match it.)

**Interfaces:**
- Consumes: `status_cli.py` (Task 4). Invoke via the venv python if present, else `python3`.

- [ ] **Step 1: Write `commands/local-coder-status.md`**

```markdown
---
description: Show active and recent local-coder delegations for this repository
argument-hint: '[job-id] [--all]'
disable-model-invocation: true
allowed-tools: Bash(python3:*), Bash(python:*)
---

!`python3 "${CLAUDE_PLUGIN_ROOT}/mcp-servers/local-coder/status_cli.py" status $ARGUMENTS`

If no job-id was passed: render the output as one compact Markdown table (id, status, phase, latest activity, elapsed, model, output log). No extra prose.
If a job-id was passed: present the full output verbatim.
```

- [ ] **Step 2: Write `commands/local-coder-setup.md`**

```markdown
---
description: Check whether local-coder is ready (venv, aider, ollama+model, config)
argument-hint: ''
disable-model-invocation: true
allowed-tools: Bash(python3:*), Bash(python:*)
---

!`python3 "${CLAUDE_PLUGIN_ROOT}/mcp-servers/local-coder/status_cli.py" setup`

Present each check's pass/fail line and any next-step guidance verbatim. Do not summarize away a failing check.
```

- [ ] **Step 3: Manual verification**

Run: `python3 mcp-servers/local-coder/status_cli.py setup` from the repo root.
Expected: four check lines, each pass/fail with next-step on failure; exit without traceback.

- [ ] **Step 4: Commit**

```bash
git add commands/local-coder-status.md commands/local-coder-setup.md
git commit -m "local-coder: add /local-coder:status and /local-coder:setup commands"
```

---

### Task 6: SessionEnd cleanup hook + startup sweep wiring

**Files:**
- Create: `hooks/cleanup-local-coder-status`
- Modify: `hooks/hooks.json` (add SessionEnd entry)
- Test: `tests/hooks/test-cleanup-local-coder-status`

**Interfaces:**
- Consumes: the status records written by Task 1 (`<id>.json` under the workspace-keyed state dir); the startup sweep is already invoked from `status.create_record` (Task 1), so this task covers the SessionEnd *kill* path plus its test.

- [ ] **Step 1: Write failing hook test**

```bash
# tests/hooks/test-cleanup-local-coder-status  (bash, mirror test-session-start.sh style)
# - Seed a state dir with a "running" record whose pid is a real, live sleep process.
# - Invoke the hook with a SessionEnd JSON payload on stdin (sessionId matching).
# - Assert: the sleep process is terminated AND the record is marked "orphaned".
# - Second case: a "running" record with an already-dead pid -> marked orphaned, no error.
# - Third case: no running records -> hook is a clean no-op, exit 0.
```

- [ ] **Step 2: Run to verify it fails**

Run: `tests/hooks/test-cleanup-local-coder-status`
Expected: FAIL (hook missing)

- [ ] **Step 3: Implement `hooks/cleanup-local-coder-status`**

A hook (bash calling a small python one-liner, or node — match the repo's hook style; `session-start` is bash) that: reads the SessionEnd JSON payload from stdin, extracts the session id, resolves the workspace state dir, and for each `running` record with a matching `sessionId` and a live `pid`, process-tree-terminates it (SIGTERM then SIGKILL after a short grace) and marks the record `orphaned`. Bounded by an internal timeout so SessionEnd never hangs. Non-fatal: any error warns and exits 0. Make it executable (`chmod +x`).

**Known limitation:** Orphan cleanup at SessionEnd is scoped to the current workspace's state dir: both `create_record`'s startup sweep and the SessionEnd hook resolve a single repository's dir. A session that delegated across multiple repositories leaves records in the other workspaces unswept until a later session in those repositories runs. This is accepted behavior, not a bug.

- [ ] **Step 4: Add the SessionEnd entry to `hooks/hooks.json`**

Add a `SessionEnd` hook entry pointing at `${CLAUDE_PLUGIN_ROOT}/hooks/cleanup-local-coder-status` with a bounded `timeout`, mirroring the structure of the existing hook entries in that file.

- [ ] **Step 5: Run to verify it passes**

Run: `tests/hooks/test-cleanup-local-coder-status`
Expected: PASS (all three cases)

- [ ] **Step 6: Commit**

```bash
git add hooks/cleanup-local-coder-status hooks/hooks.json tests/hooks/test-cleanup-local-coder-status
git commit -m "local-coder: add SessionEnd orphan-cleanup hook (kill live orphans, mark orphaned)"
```

---

### Task 7: Full-suite verification

**Files:** none (verification only).

- [ ] **Step 1: Run the whole python suite**

Run: `cd mcp-servers/local-coder && python -m pytest -q`
Expected: all pass (existing + new).

- [ ] **Step 2: Run the hook suites**

Run: each `tests/hooks/test-*` script.
Expected: all STATUS: PASSED.

- [ ] **Step 3: Smoke the commands manually**

Run: `python3 mcp-servers/local-coder/status_cli.py setup` and `... status --all`.
Expected: no tracebacks; sensible output.

---

## Self-Review (controller checklist — run before dispatch)

- **Spec coverage:** status record (T1), phase model (T1+T3), latest-activity (T1+T3), dedupe (T1), workspace-keyed dir (T1), `on_start` pid (T2), four server hooks + non-fatal guard (T3), status_cli + 4 setup checks + localhost-only (T4), two commands (T5), SessionEnd kill + startup sweep (T1 sweep + T6 kill) — all mapped.
- **Placeholder scan:** test bodies for T3/T6 defer to existing harness/hook style rather than inline full fixtures — intentional, because those fixtures already exist in-repo and must be matched, not reinvented; the behavioral assertions are concrete.
- **Type consistency:** record keys camelCase throughout; `ProgressUpdater.on_output/on_activity`, `create_record/finalize/set_pid/list_records/sweep_orphans` names consistent across T1/T3/T4/T6.
