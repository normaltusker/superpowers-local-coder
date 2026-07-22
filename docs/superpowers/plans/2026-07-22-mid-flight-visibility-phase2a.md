# Mid-Flight Visibility (Phase 2a) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `delegate_implementation` show real backend progress while it runs (a live pulse in the chat carrying the latest output line) and leave the full transcript in the persisted final result.

**Architecture:** Two independent additions to the existing local-coder MCP server. (1) A durable record: thread the backend's captured output tail up through `CompletionResult` into `delegate_implementation`'s returned dict, on both success and failure. (2) A live pulse: have the existing per-tick `report_progress` heartbeat carry the latest real output line (fed from the existing `on_output` chunk callback via a small shared buffer) instead of a static "still running" string, throttled to the existing tick cadence. No new MCP primitives, no new dependencies, no changes to `stdin=DEVNULL`.

**Tech Stack:** Python 3.13, FastMCP 3.4.4, pytest. Flat imports (`from backends import common`) per the repo's existing convention. Existing test mock patterns: `fake_run_backend`/`fake_run` stubs, `isolated_config`/`git_repo_no_remote` fixtures.

## Global Constraints

- All internal imports are **flat** (`import config`, `from backends.aider import AiderBackend`) — `mcp-servers/local-coder/` is hyphenated, not a valid package name. Never write `from local_coder import ...`.
- The output tail bound is the existing `_MAX_OUTPUT_CHARS = 20_000` in `backends/common.py`. Do NOT introduce a new bound or an unbounded field.
- `run_monitored_subprocess` returns its bounded output tail as `CompletedProcess.stdout` (see `backends/common.py:408-409`). That is the source of all output-tail data — do not re-capture output elsewhere.
- **Never revert `stdin=subprocess.DEVNULL`** in `run_monitored_subprocess` (commit `620cfc0`). Nothing in this plan touches subprocess stdin.
- The live pulse must be throttled to the existing tick cadence (`on_tick`), NOT emitted per raw output chunk. `report_progress` is ephemeral; per-chunk emission only adds flicker and notification traffic.
- Run tests with the venv: `mcp-servers/local-coder/.venv/bin/python -m pytest ...`, from `mcp-servers/local-coder/`.
- All tests must stay green: the suite is at 110 passing before this plan.

---

## File Structure

- `mcp-servers/local-coder/backends/base.py` — add `output_tail` field to `CompletionResult`.
- `mcp-servers/local-coder/backends/aider.py` — populate `output_tail` on the three `CompletionResult` returns that have a captured `result` (non-zero exit, no-commits, success).
- `mcp-servers/local-coder/server.py` — surface `output_tail` into the result dict (success + all-failed paths); upgrade `make_on_tick`/`make_on_output` so the tick pulse carries the latest output line.
- `mcp-servers/local-coder/tests/test_aider.py` — tests for `output_tail` population.
- `mcp-servers/local-coder/tests/test_server.py` — tests for `output_tail` in the result dict and for the live-pulse message content.

---

### Task 1: Add `output_tail` to `CompletionResult`

**Files:**
- Modify: `mcp-servers/local-coder/backends/base.py:6-11`

**Interfaces:**
- Produces: `CompletionResult(..., output_tail: str = "")` — a new defaulted dataclass field carrying the backend subprocess's bounded output tail. Consumed by Task 2 (aider populates it) and Task 3 (server reads it).

- [ ] **Step 1: Write the failing test**

Add to `mcp-servers/local-coder/tests/test_aider.py` (near the top, after the existing imports and `BASE_CONFIG`):

```python
def test_completion_result_has_output_tail_field_defaulting_empty():
    from backends.base import CompletionResult
    r = CompletionResult(success=True)
    assert r.output_tail == ""
    r2 = CompletionResult(success=True, output_tail="some output")
    assert r2.output_tail == "some output"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd mcp-servers/local-coder && .venv/bin/python -m pytest tests/test_aider.py::test_completion_result_has_output_tail_field_defaulting_empty -v`
Expected: FAIL with `TypeError: __init__() got an unexpected keyword argument 'output_tail'`

- [ ] **Step 3: Add the field**

In `backends/base.py`, change the `CompletionResult` dataclass:

```python
@dataclass
class CompletionResult:
    success: bool
    files_changed: list[str] = field(default_factory=list)
    commit_sha: str | None = None
    error: str | None = None
    output_tail: str = ""
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd mcp-servers/local-coder && .venv/bin/python -m pytest tests/test_aider.py::test_completion_result_has_output_tail_field_defaulting_empty -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add mcp-servers/local-coder/backends/base.py mcp-servers/local-coder/tests/test_aider.py
git commit -m "local-coder: add output_tail field to CompletionResult"
```

---

### Task 2: Populate `output_tail` in AiderBackend

**Files:**
- Modify: `mcp-servers/local-coder/backends/aider.py:51-80`
- Test: `mcp-servers/local-coder/tests/test_aider.py`

**Interfaces:**
- Consumes: `CompletionResult(..., output_tail=...)` from Task 1; `run_monitored_subprocess` returning `CompletedProcess` whose `.stdout` is the bounded output tail.
- Produces: `AiderBackend.run_backend` now returns `output_tail=result.stdout` on the non-zero-exit, no-commits, and success paths. (The pre-subprocess "no model" path and the `StallError` path have no captured `result`, so they keep `output_tail=""` — this is intentional; there is no subprocess output to report in those cases.)

- [ ] **Step 1: Write the failing tests**

Add to `mcp-servers/local-coder/tests/test_aider.py`:

```python
def test_run_backend_success_carries_output_tail(git_repo):
    def fake_run(cmd, cwd, stall_timeout_seconds, idle_notify_interval_seconds, on_tick=None, on_output=None):
        (git_repo / "new_file.py").write_text("# new\n")
        subprocess.run(["git", "add", "new_file.py"], cwd=git_repo, check=True)
        subprocess.run(["git", "commit", "-m", "aider commit"], cwd=git_repo, check=True, capture_output=True)
        return subprocess.CompletedProcess(cmd, 0, stdout="aider did the thing", stderr="")

    backend = AiderBackend()
    with patch.object(common, "run_monitored_subprocess", side_effect=fake_run):
        result = backend.run_backend(
            task="add a file", repo_path=str(git_repo), branch="test-branch",
            config=BASE_CONFIG, model="ollama/qwen3-coder:30b",
        )

    assert result.success is True
    assert result.output_tail == "aider did the thing"


def test_run_backend_nonzero_exit_carries_output_tail(git_repo):
    def fake_run(cmd, cwd, stall_timeout_seconds, idle_notify_interval_seconds, on_tick=None, on_output=None):
        return subprocess.CompletedProcess(cmd, 1, stdout="aider error trace here", stderr="")

    backend = AiderBackend()
    with patch.object(common, "run_monitored_subprocess", side_effect=fake_run):
        result = backend.run_backend(
            task="do something", repo_path=str(git_repo), branch="test-branch",
            config=BASE_CONFIG, model="ollama/qwen3-coder:30b",
        )

    assert result.success is False
    assert result.output_tail == "aider error trace here"


def test_run_backend_no_commit_carries_output_tail(git_repo):
    def fake_run(cmd, cwd, stall_timeout_seconds, idle_notify_interval_seconds, on_tick=None, on_output=None):
        return subprocess.CompletedProcess(cmd, 0, stdout="aider ran but made no commit", stderr="")

    backend = AiderBackend()
    with patch.object(common, "run_monitored_subprocess", side_effect=fake_run):
        result = backend.run_backend(
            task="do nothing", repo_path=str(git_repo), branch="test-branch",
            config=BASE_CONFIG, model="ollama/qwen3-coder:30b",
        )

    assert result.success is False
    assert result.output_tail == "aider ran but made no commit"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd mcp-servers/local-coder && .venv/bin/python -m pytest tests/test_aider.py -k output_tail -v`
Expected: the three new `run_backend_*_carries_output_tail` tests FAIL with `AssertionError` (output_tail is `""`, the default, because aider.py doesn't set it yet). The Task 1 field test still passes.

- [ ] **Step 3: Populate `output_tail` on the three result-bearing returns**

In `backends/aider.py`, update the non-zero-exit return (lines ~53-56):

```python
        if result.returncode != 0:
            common.restore_working_tree(repo_path, pre_head, pre_porcelain)
            return CompletionResult(
                success=False,
                error=result.stdout.strip()[-2000:] or "aider exited non-zero",
                output_tail=result.stdout,
            )
```

Update the no-commits return (lines ~63-69):

```python
        if post_head == pre_head:
            # aider exited cleanly (returncode 0) without committing — this
            # can still happen with dirty, uncommitted writes on disk (e.g.
            # aider wrote files but a lint/commit step declined or failed
            # silently), so clean up here too.
            common.restore_working_tree(repo_path, pre_head, pre_porcelain)
            return CompletionResult(
                success=False,
                error="aider made no commits",
                output_tail=result.stdout,
            )
```

Update the success return (lines ~76-80):

```python
        return CompletionResult(
            success=True,
            files_changed=files_changed,
            commit_sha=post_head,
            output_tail=result.stdout,
        )
```

Leave the "no model specified" return and the `StallError` return unchanged (they have no `result`).

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd mcp-servers/local-coder && .venv/bin/python -m pytest tests/test_aider.py -v`
Expected: all pass (the three new ones plus every existing aider test).

- [ ] **Step 5: Commit**

```bash
git add mcp-servers/local-coder/backends/aider.py mcp-servers/local-coder/tests/test_aider.py
git commit -m "local-coder: populate output_tail from aider subprocess output"
```

---

### Task 3: Surface `output_tail` in the `delegate_implementation` result dict

**Files:**
- Modify: `mcp-servers/local-coder/server.py:243-258` (success return and all-failed return)
- Test: `mcp-servers/local-coder/tests/test_server.py`

**Interfaces:**
- Consumes: `CompletionResult.output_tail` from Task 2.
- Produces: `_delegate_implementation_impl`'s returned dict includes `"output_tail"` on the success path (from the succeeding attempt's `result.output_tail`) and on the all-models-failed path (from the LAST attempt's `result.output_tail`, so the user sees what the final model was doing when it failed).

- [ ] **Step 1: Write the failing tests**

Add to `mcp-servers/local-coder/tests/test_server.py`:

```python
async def test_delegate_implementation_success_includes_output_tail(isolated_config, git_repo_no_remote):
    def fake_run_backend(task, repo_path, branch, config, model=None, on_tick=None, on_output=None):
        return CompletionResult(
            success=True, files_changed=["a.py"], commit_sha="abc123",
            output_tail="full aider transcript here",
        )

    with patch("backends.aider.AiderBackend.run_backend", side_effect=fake_run_backend):
        result = await server._delegate_implementation_impl(
            task="add a.py", branch="feature-branch",
            target_repo_path=str(git_repo_no_remote), ctx=None,
        )

    assert result["success"] is True
    assert result["output_tail"] == "full aider transcript here"


async def test_delegate_implementation_all_failed_includes_last_output_tail(isolated_config, git_repo_no_remote):
    def fake_run_backend(task, repo_path, branch, config, model=None, on_tick=None, on_output=None):
        return CompletionResult(
            success=False, error=f"{model} failed",
            output_tail=f"transcript from {model}",
        )

    # config has one primary model, no fallbacks (isolated_config default)
    with patch("backends.aider.AiderBackend.run_backend", side_effect=fake_run_backend):
        result = await server._delegate_implementation_impl(
            task="add a.py", branch="feature-branch",
            target_repo_path=str(git_repo_no_remote), ctx=None,
        )

    assert result["success"] is False
    assert "output_tail" in result
    assert result["output_tail"].startswith("transcript from ")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd mcp-servers/local-coder && .venv/bin/python -m pytest tests/test_server.py -k output_tail -v`
Expected: both FAIL — `KeyError: 'output_tail'` (the result dict has no such key yet).

- [ ] **Step 3: Track the last result and surface its output_tail**

In `server.py`, the failover loop currently discards `result` when an attempt fails (it only appends to `attempt_errors`). Capture the last attempt's `output_tail` so the all-failed return can include it. Change the loop and both returns.

First, initialize a holder before the loop (near `attempt_errors = []`, around line 117):

```python
    attempt_errors = []
    last_output_tail = ""
```

In the failover loop, after a failed attempt appends its error (the `attempt_errors.append(f"{model}: {result.error}")` line near line 253), also record its output tail. Change:

```python
        attempt_errors.append(f"{model}: {result.error}")
```

to:

```python
        attempt_errors.append(f"{model}: {result.error}")
        last_output_tail = result.output_tail
```

Add `output_tail` to the success return (lines ~243-251):

```python
            return {
                "success": True,
                "pr_url": pr_url,
                "branch": branch,
                "files_changed": result.files_changed,
                "model_used": model,
                "summary": f"Implemented via {backend_name} ({model})",
                "output_tail": result.output_tail,
                **({"note": note} if note else {}),
            }
```

Add `output_tail` to the all-failed return (lines ~255-258):

```python
    return {
        "success": False,
        "error": "all models failed — " + "; ".join(attempt_errors),
        "output_tail": last_output_tail,
    }
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd mcp-servers/local-coder && .venv/bin/python -m pytest tests/test_server.py -v`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add mcp-servers/local-coder/server.py mcp-servers/local-coder/tests/test_server.py
git commit -m "local-coder: include output_tail in delegate_implementation result"
```

---

### Task 4: Make the live pulse carry the latest output line

**Files:**
- Modify: `mcp-servers/local-coder/server.py:119-144` (the `make_on_tick` / `make_on_output` closures and the shared buffer)
- Test: `mcp-servers/local-coder/tests/test_server.py`

**Interfaces:**
- Consumes: nothing new (uses the existing `on_tick`/`on_output` wiring into `run_monitored_subprocess`).
- Produces: the per-tick `report_progress` message now reflects the most recent non-empty output line (e.g. `"{model}: Running tests..."`) instead of the static `"Running {model}..."`. Cadence unchanged — still only on tick, not per chunk.

Background: `make_on_tick` and `make_on_output` are separate closures defined inside `_delegate_implementation_impl`. To let the tick read what the latest output was, both close over one shared mutable holder. `on_output` writes the latest line into it; `on_tick` reads it when it fires.

- [ ] **Step 1: Write the failing test**

Add to `mcp-servers/local-coder/tests/test_server.py`:

```python
async def test_on_tick_pulse_reflects_latest_output_line(isolated_config, git_repo_no_remote):
    captured = {}

    def fake_run_backend(task, repo_path, branch, config, model=None, on_tick=None, on_output=None):
        captured["on_tick"] = on_tick
        captured["on_output"] = on_output
        return CompletionResult(success=True, files_changed=["a.py"], commit_sha="abc123")

    mock_ctx = MagicMock()
    mock_ctx.report_progress = MagicMock()

    with patch("backends.aider.AiderBackend.run_backend", side_effect=fake_run_backend):
        with patch("anyio.from_thread.run") as mock_from_thread_run:
            await server._delegate_implementation_impl(
                task="add a.py", branch="feature-branch",
                target_repo_path=str(git_repo_no_remote), ctx=mock_ctx,
            )

            on_tick = captured["on_tick"]
            on_output = captured["on_output"]

            # Simulate the backend emitting output, then a tick firing.
            on_output("Applying edit...\n")
            on_output("Running tests...\n")
            on_tick()

            # The progress message bridged to report_progress should carry
            # the latest output line, not a static "still running".
            assert mock_from_thread_run.called
            progress_args = mock_from_thread_run.call_args.args
            # args: (ctx.report_progress, progress, total, message)
            message = progress_args[3]
            assert "Running tests..." in message
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd mcp-servers/local-coder && .venv/bin/python -m pytest tests/test_server.py::test_on_tick_pulse_reflects_latest_output_line -v`
Expected: FAIL — the message is currently `f"Running {model}..."`, which does not contain "Running tests...".

- [ ] **Step 3: Add the shared buffer and use it in the tick pulse**

In `server.py`, inside `_delegate_implementation_impl`, replace the existing `make_on_tick` and `make_on_output` definitions (lines ~119-144) with versions that share a latest-line holder. Define the holder just before them:

```python
    # Shared holder: on_output writes the most recent non-empty output line
    # here; on_tick reads it so the periodic progress pulse carries real
    # backend output instead of a static heartbeat. A one-element list is a
    # simple mutable cell both closures can see.
    latest_line = [""]

    def make_on_tick(model_name: str):
        def on_tick():
            line = latest_line[0]
            pulse = f"{model_name}: {line}" if line else f"Running {model_name}..."
            print(f"[local-coder] {pulse}", file=sys.stderr, flush=True)
            if ctx is not None:
                anyio.from_thread.run(
                    ctx.report_progress, 0, None, pulse
                )
        return on_tick

    def make_on_output(model_name: str):
        # Streams the backend subprocess's actual output as it arrives, to
        # BOTH stderr and OUTPUT_LOG_PATH (see that constant's comment), and
        # records the latest non-empty line so the on_tick pulse above can
        # surface it in-chat. Kept throttled to the tick cadence — the tick
        # is what emits to report_progress; this callback only records.
        def on_output(chunk: str):
            line = f"[local-coder:{model_name}] {chunk}"
            print(line, end="", file=sys.stderr, flush=True)
            with open(OUTPUT_LOG_PATH, "a") as f:
                f.write(chunk)
            stripped = chunk.strip()
            if stripped:
                # Keep only the last line of a multi-line chunk.
                latest_line[0] = stripped.splitlines()[-1]
        return on_output
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd mcp-servers/local-coder && .venv/bin/python -m pytest tests/test_server.py -v`
Expected: all pass — the new pulse test plus the existing `test_delegate_implementation_on_tick_reports_progress_via_ctx` (which asserts `args[0] == mock_ctx.report_progress`, still true) and `test_delegate_implementation_on_tick_logs_to_stderr_without_ctx` (which asserts `"still running"` — SEE NOTE BELOW).

**NOTE for the implementer:** the existing test `test_delegate_implementation_on_tick_logs_to_stderr_without_ctx` asserts `"still running" in captured.err`. This plan's new pulse changes the no-output stderr line from `"still running (model)..."` to `"Running {model}..."` — so that assertion will now FAIL. This is an intended behavior change, not a regression. Update that existing test's assertion from:

```python
    assert "still running" in captured.err
```

to:

```python
    assert "Running ollama/qwen3-coder:30b" in captured.err
```

(the model in `isolated_config`'s default config). Include this change in this task's commit. If the model name in the default config differs, match it — read `mcp-servers/local-coder/config.yaml`'s `model:` value.

- [ ] **Step 5: Run the FULL suite to confirm nothing else regressed**

Run: `cd mcp-servers/local-coder && .venv/bin/python -m pytest tests/ -q`
Expected: all pass (114 = prior 110 + Task 1's 1 + Task 2's 3 + Task 3's 2 + Task 4's 1, minus none — recount if the number differs, but zero failures is the gate).

- [ ] **Step 6: Commit**

```bash
git add mcp-servers/local-coder/server.py mcp-servers/local-coder/tests/test_server.py
git commit -m "local-coder: live pulse carries latest backend output line"
```

---

### Task 5: Demote the log file to a debugging aside in the README

**Files:**
- Modify: `mcp-servers/local-coder/README.md`

**Interfaces:** none (docs only).

Per spec section 2a.3: the in-chat pulse + result transcript is now the normal visibility experience; the log file is a power-user/debugging aside, not "the way to watch a delegation."

- [ ] **Step 1: Read the current README to find where visibility/output is described**

Run: `cd mcp-servers/local-coder && grep -n "local-coder-output.log\|tail\|progress\|visib" README.md`
Expected: locate any existing mention of the log file or watching output. (If Phase 1's README never documented the log file, there is nothing to demote — instead ADD a short "Watching a delegation" subsection per Step 2.)

- [ ] **Step 2: Add/adjust a "Watching a delegation run" subsection**

Ensure the README contains a short subsection with this substance (adapt wording to the README's existing voice/structure; do not paste verbatim if it clashes):

```markdown
### Watching a delegation run

While `delegate_implementation` runs, its progress appears live in the
Claude Code conversation — the latest line of backend output is surfaced
as a progress update, and the full output transcript is included in the
tool's final result (the `output_tail` field). No extra steps are needed
to see what the backend is doing.

For deep debugging, the raw backend output is also written to
`mcp-servers/local-coder/.venv/../local-coder-output.log` (truncated at
the start of each call). Tailing that file is a power-user convenience,
not the normal way to follow a run.
```

Fix the log path in that snippet to match the real one: it is `OUTPUT_LOG_PATH` in `server.py` = `Path(__file__).parent / "local-coder-output.log"`, i.e. `mcp-servers/local-coder/local-coder-output.log`. Write that exact path.

- [ ] **Step 3: Verify the README still reads coherently**

Run: `cd mcp-servers/local-coder && grep -n "local-coder-output.log" README.md`
Expected: the log file is mentioned only as a debugging aside, with the in-chat experience described as primary.

- [ ] **Step 4: Commit**

```bash
git add mcp-servers/local-coder/README.md
git commit -m "docs: describe in-chat delegation visibility, demote log file to debugging aside"
```

---

## Self-Review

**Spec coverage:**
- 2a.1 live pulse → Task 4. ✓
- 2a.2 durable record (`output_tail`, bounded to `_MAX_OUTPUT_CHARS`, on success + failure) → Tasks 1-3. ✓
- 2a.3 log file kept but demoted → Task 5. ✓
- 2b (interactive) → intentionally NOT in this plan (gated in the spec). ✓
- Non-goal: no resource-based streaming, no stdin change → nothing in this plan touches either. ✓

**Placeholder scan:** all steps contain concrete code, exact paths, exact commands, expected output. Task 5's README wording is adapt-to-voice by necessity (docs), but the required substance and exact log path are specified. No "TBD"/"handle edge cases"/"add tests" placeholders.

**Type consistency:** `output_tail: str` is defined in Task 1, populated in Task 2 (`result.output_tail`), read in Task 3 (`result.output_tail`, `last_output_tail`). `latest_line` (Task 4) is a `list[str]` mutable cell used only within `_delegate_implementation_impl`. `make_on_tick`/`make_on_output` signatures unchanged (both still take `model_name: str` and return a zero/one-arg callable matching what `run_monitored_subprocess` calls). Consistent.

**Known behavior change flagged:** Task 4 Step 4's NOTE calls out that the existing `..._logs_to_stderr_without_ctx` test assertion must change (`"still running"` → `"Running {model}"`), so the implementer won't mistake it for a regression.
