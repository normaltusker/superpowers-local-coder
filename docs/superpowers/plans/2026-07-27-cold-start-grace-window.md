# Cold-Start Grace Window Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stop the stall detector from killing a large local model while it cold-loads (produces no output during load), by adding a separate first-output timeout that governs the pre-first-byte phase.

**Architecture:** `run_monitored_subprocess` splits its single output-inactivity timer into two phases keyed on whether the subprocess has emitted its first byte. Before first output: elapsed-since-start is measured against a new `first_output_timeout_seconds`. After first output: the existing `stall_timeout_seconds` inactivity check applies unchanged. A new config field carries the value; when absent it falls back to `stall_timeout_seconds` so existing configs are unaffected.

**Tech Stack:** Python 3.10+, pytest, FastMCP (unchanged), aider backend. Flat imports (`import config`, `from backends.aider import AiderBackend`).

## Global Constraints

- Design spec: `docs/superpowers/specs/2026-07-27-cold-start-grace-window-design.md` — binding source of truth.
- New config field name (exact): `first_output_timeout_seconds`.
- Shipped `config.yaml` default (exact): `600`.
- Absent-key fallback (exact): the effective `stall_timeout_seconds` value — NOT a hardcoded 600. Existing configs without the key must show ZERO behavior change.
- `run_monitored_subprocess`'s new parameter defaults to `None`; `None` means "use `stall_timeout_seconds` for the phase-1 budget."
- Validation: strictly positive, identical to `stall_timeout_seconds` / `idle_notify_interval_seconds`. NO cross-field constraint.
- The one-way phase transition flips on the first NON-EMPTY decoded chunk (same point `last_activity` is currently first updated).
- Grace window is self-bounding — do NOT add a separate hard cap.
- Do NOT touch: `stdin=DEVNULL`, `output_tail` logic, `idle_notify_interval_seconds`, the tick/progress path, or `stall_timeout_seconds`'s default (300).
- Flat imports only. Do NOT create `tests/__init__.py`.
- Tests use sub-second fake timeouts — never sleep for real minutes.

---

### Task 1: Two-phase timer in `run_monitored_subprocess`

**Files:**
- Modify: `mcp-servers/local-coder/backends/common.py` (`StallError` class ~22-25; `run_monitored_subprocess` signature ~293-300; timer init ~330-331; stall check ~393-396)
- Test: `mcp-servers/local-coder/tests/test_backends_common.py`

**Interfaces:**
- Consumes: nothing new.
- Produces:
  - `StallError(timeout_seconds: float, phase: str = "stall")` — `phase` is `"first-output"` or `"stall"`; attribute `stall_timeout_seconds` preserved (= `timeout_seconds`) for existing callers/tests.
  - `run_monitored_subprocess(cmd, cwd, stall_timeout_seconds, idle_notify_interval_seconds, on_tick=None, on_output=None, first_output_timeout_seconds=None)` — when `first_output_timeout_seconds is None`, phase-1 budget = `stall_timeout_seconds`.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_backends_common.py`. Reuse the existing helper pattern for a fake command that emits on a schedule (grep the file for how current stall tests build `cmd` — typically a `python -c` that prints then sleeps). If no such helper exists, use these inline `python -c` scripts.

```python
import pytest
from backends.common import run_monitored_subprocess, StallError


def _py(script):
    import sys
    return [sys.executable, "-c", script]


def test_cold_load_survives_past_stall_but_within_first_output():
    # No output until 0.3s (> stall_timeout 0.1s, < first_output 1.0s), then prints.
    # Must NOT raise: pre-first-byte phase is governed by first_output_timeout.
    cmd = _py("import time; time.sleep(0.3); print('loaded', flush=True)")
    result = run_monitored_subprocess(
        cmd, cwd=".",
        stall_timeout_seconds=0.1,
        idle_notify_interval_seconds=0.05,
        first_output_timeout_seconds=1.0,
    )
    assert result.returncode == 0
    assert "loaded" in result.stdout


def test_steady_state_stall_still_fires_after_first_output():
    # Prints immediately (flips to phase 2), then goes silent past stall_timeout.
    # Must raise StallError with phase "stall".
    cmd = _py("import time; print('go', flush=True); time.sleep(2)")
    with pytest.raises(StallError) as ei:
        run_monitored_subprocess(
            cmd, cwd=".",
            stall_timeout_seconds=0.2,
            idle_notify_interval_seconds=0.05,
            first_output_timeout_seconds=5.0,
        )
    assert ei.value.phase == "stall"


def test_wedged_cold_load_fires_on_first_output_budget():
    # Never prints, runs past first_output_timeout. Must raise with phase "first-output".
    cmd = _py("import time; time.sleep(5)")
    with pytest.raises(StallError) as ei:
        run_monitored_subprocess(
            cmd, cwd=".",
            stall_timeout_seconds=5.0,
            idle_notify_interval_seconds=0.05,
            first_output_timeout_seconds=0.2,
        )
    assert ei.value.phase == "first-output"


def test_omitted_first_output_param_matches_old_behavior():
    # first_output_timeout_seconds=None → phase-1 budget = stall_timeout.
    # A no-output process dies at stall_timeout (0.2s), exactly as before.
    cmd = _py("import time; time.sleep(5)")
    with pytest.raises(StallError) as ei:
        run_monitored_subprocess(
            cmd, cwd=".",
            stall_timeout_seconds=0.2,
            idle_notify_interval_seconds=0.05,
        )
    assert ei.value.stall_timeout_seconds == 0.2
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd mcp-servers/local-coder && .venv/bin/python -m pytest tests/test_backends_common.py -k "cold_load or steady_state_stall or wedged or omitted_first_output" -v`
Expected: `test_cold_load_survives...` FAILs (killed at stall_timeout under current code); the `phase`-asserting tests FAIL with `AttributeError: 'StallError' object has no attribute 'phase'` or `TypeError` on the unknown `first_output_timeout_seconds` kwarg.

- [ ] **Step 3: Update `StallError` to carry the phase**

Replace the `StallError` class:

```python
class StallError(Exception):
    def __init__(self, timeout_seconds: float, phase: str = "stall"):
        # phase is "first-output" (killed before the first byte of output,
        # i.e. a cold-load that never produced anything within its grace
        # window) or "stall" (went silent after producing output).
        self.stall_timeout_seconds = timeout_seconds
        self.phase = phase
        if phase == "first-output":
            msg = f"stalled: no first output within {timeout_seconds}s (cold-load grace)"
        else:
            msg = f"stalled: no output for {timeout_seconds}s"
        super().__init__(msg)
```

- [ ] **Step 4: Add the parameter and two-phase logic**

Add `first_output_timeout_seconds: float | None = None` as the last parameter of `run_monitored_subprocess` (after `on_output`).

Immediately after `last_activity = time.monotonic()` / `last_tick = time.monotonic()` (~330-331), add:

```python
    start_time = last_activity
    seen_output = False
    first_budget = (
        first_output_timeout_seconds
        if first_output_timeout_seconds is not None
        else stall_timeout_seconds
    )
```

In the read block, where a non-empty `decoded` currently sets `last_activity = time.monotonic()` (~361), also set the flag (only needs to flip once; setting it every chunk is harmless):

```python
                    last_activity = time.monotonic()
                    seen_output = True
```

Replace the existing stall check (~393-396):

```python
            if not seen_output:
                if now - start_time > first_budget:
                    process.kill()
                    process.wait()
                    raise StallError(first_budget, phase="first-output")
            else:
                if now - last_activity > stall_timeout_seconds:
                    process.kill()
                    process.wait()
                    raise StallError(stall_timeout_seconds, phase="stall")
```

- [ ] **Step 5: Run the new tests to verify they pass**

Run: `cd mcp-servers/local-coder && .venv/bin/python -m pytest tests/test_backends_common.py -k "cold_load or steady_state_stall or wedged or omitted_first_output" -v`
Expected: 4 PASS.

- [ ] **Step 6: Run the full common-backend suite for regressions**

Run: `cd mcp-servers/local-coder && .venv/bin/python -m pytest tests/test_backends_common.py -v`
Expected: all PASS (the omitted-param default preserves every existing test's behavior).

- [ ] **Step 7: Commit**

```bash
git add mcp-servers/local-coder/backends/common.py mcp-servers/local-coder/tests/test_backends_common.py
git commit -m "local-coder: add cold-start grace window to stall detection"
```

---

### Task 2: Thread `first_output_timeout_seconds` through the aider backend

**Files:**
- Modify: `mcp-servers/local-coder/backends/aider.py` (the `run_monitored_subprocess` call ~34-41)
- Test: `mcp-servers/local-coder/tests/test_aider.py`

**Interfaces:**
- Consumes: `run_monitored_subprocess(..., first_output_timeout_seconds=...)` from Task 1.
- Produces: `AiderBackend.run_backend` reads `config.get("first_output_timeout_seconds")` and passes it through. When the key is absent, passes `None` (Task 1 then falls back to `stall_timeout_seconds`).

Note: `base.py`'s `run_backend` signature and the other backend stubs do NOT change — the value travels inside the existing `config: dict`, not as a new parameter.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_aider.py`. Grep the file for how existing tests stub `common.run_monitored_subprocess` (monkeypatch capturing kwargs). Follow that exact pattern; the skeleton:

```python
def test_run_backend_passes_first_output_timeout_from_config(monkeypatch):
    captured = {}

    def fake_run(*args, **kwargs):
        captured.update(kwargs)
        import subprocess
        return subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")

    monkeypatch.setattr("backends.common.run_monitored_subprocess", fake_run)
    # plus the same stubs existing aider tests use for ensure_branch,
    # snapshot_working_tree, and the post-run commit/verify path.

    cfg = {
        "model": "ollama/x",
        "stall_timeout_seconds": 300,
        "idle_notify_interval_seconds": 20,
        "first_output_timeout_seconds": 600,
        "extra_backend_args": [],
    }
    AiderBackend().run_backend(
        task="t", repo_path=".", branch="b", config=cfg,
    )
    assert captured["first_output_timeout_seconds"] == 600


def test_run_backend_passes_none_when_first_output_absent(monkeypatch):
    captured = {}

    def fake_run(*args, **kwargs):
        captured.update(kwargs)
        import subprocess
        return subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")

    monkeypatch.setattr("backends.common.run_monitored_subprocess", fake_run)
    # same supporting stubs as above

    cfg = {
        "model": "ollama/x",
        "stall_timeout_seconds": 300,
        "idle_notify_interval_seconds": 20,
        "extra_backend_args": [],
    }
    AiderBackend().run_backend(
        task="t", repo_path=".", branch="b", config=cfg,
    )
    assert captured["first_output_timeout_seconds"] is None
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd mcp-servers/local-coder && .venv/bin/python -m pytest tests/test_aider.py -k first_output -v`
Expected: FAIL — `captured["first_output_timeout_seconds"]` KeyError (the call doesn't pass it yet).

- [ ] **Step 3: Pass the value in the `run_monitored_subprocess` call**

In `aider.py`, add one line inside the `common.run_monitored_subprocess(...)` call (after the `idle_notify_interval_seconds=` line):

```python
                idle_notify_interval_seconds=config["idle_notify_interval_seconds"],
                first_output_timeout_seconds=config.get("first_output_timeout_seconds"),
                on_tick=on_tick,
```

`.get()` (not `[...]`) so an absent key yields `None`, not a KeyError — the fallback contract.

- [ ] **Step 4: Run to verify it passes**

Run: `cd mcp-servers/local-coder && .venv/bin/python -m pytest tests/test_aider.py -k first_output -v`
Expected: 2 PASS.

- [ ] **Step 5: Run the full aider suite**

Run: `cd mcp-servers/local-coder && .venv/bin/python -m pytest tests/test_aider.py -v`
Expected: all PASS.

- [ ] **Step 6: Commit**

```bash
git add mcp-servers/local-coder/backends/aider.py mcp-servers/local-coder/tests/test_aider.py
git commit -m "local-coder: pass first_output_timeout_seconds through aider backend"
```

---

### Task 3: Config default + validation

**Files:**
- Modify: `mcp-servers/local-coder/config.yaml` (add the key)
- Modify: `mcp-servers/local-coder/config.py` (`_configure_with_validation_locked` value-resolution block ~194-199 and validation block ~206-213)
- Test: `mcp-servers/local-coder/tests/test_config.py`

**Interfaces:**
- Consumes: nothing new. `merge_config` (~155-163) is pass-through, so a `first_output_timeout_seconds` present in `config.yaml` already survives into the merged dict without change — only defaulting and validation need touching.
- Produces: `configure_with_validation` rejects a non-positive `first_output_timeout_seconds`; the shipped default is 600.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_config.py`. Grep for how existing tests exercise `configure_with_validation` / the isolated `CONFIG_PATH` temp fixture and the ollama-list stub; reuse that fixture. Skeleton:

```python
def test_shipped_config_has_first_output_timeout_default():
    import yaml
    from pathlib import Path
    import config as cfgmod
    data = yaml.safe_load(Path(cfgmod._DEFAULT_CONFIG_PATH).read_text())
    assert data["first_output_timeout_seconds"] == 600


def test_configure_rejects_non_positive_first_output_timeout(...):
    # use the same fixture/stubs existing non-positive-timeout tests use
    with pytest.raises(config.ConfigValidationError, match="first_output_timeout_seconds"):
        config.configure_with_validation({"first_output_timeout_seconds": 0})


def test_configure_accepts_positive_first_output_timeout(...):
    result = config.configure_with_validation({"first_output_timeout_seconds": 900})
    assert result["first_output_timeout_seconds"] == 900


def test_absent_first_output_timeout_leaves_config_unchanged(...):
    # A configure() call that doesn't mention the key must not inject it.
    # (Fallback to stall value happens at read time in aider.py, not here.)
    before = config.load_config()
    result = config.configure_with_validation({"idle_notify_interval_seconds": 20})
    assert result.get("first_output_timeout_seconds") == before.get("first_output_timeout_seconds")
```

Match the exact stub signature the existing non-positive `stall_timeout_seconds` test uses (same ollama-availability monkeypatch) — copy its decorator/fixture list into the `...` placeholders.

- [ ] **Step 2: Run to verify they fail**

Run: `cd mcp-servers/local-coder && .venv/bin/python -m pytest tests/test_config.py -k first_output -v`
Expected: `test_shipped_config...` FAILs (KeyError — key not in yaml yet); the reject test FAILs (no validation → no error raised).

- [ ] **Step 3: Add the key to `config.yaml`**

Add after the `stall_timeout_seconds: 300` line:

```yaml
stall_timeout_seconds: 300
first_output_timeout_seconds: 600
```

- [ ] **Step 4: Add resolution + validation in `config.py`**

In `_configure_with_validation_locked`, after the `idle_notify_interval_seconds = overrides.get(...)` block (~197-199), add:

```python
    first_output_timeout_seconds = overrides.get(
        "first_output_timeout_seconds",
        current.get("first_output_timeout_seconds"),
    )
```

After the `idle_notify_interval_seconds` non-positive check (~210-213), add:

```python
    # first_output_timeout_seconds governs the pre-first-output (cold-load)
    # grace window; a zero or negative value would kill every backend
    # attempt before it could emit anything. Same strictly-positive rule as
    # the sibling timeouts; no cross-field constraint (absent → falls back
    # to stall_timeout_seconds at read time in the backend).
    if first_output_timeout_seconds is not None and first_output_timeout_seconds <= 0:
        raise ConfigValidationError(
            "first_output_timeout_seconds must be strictly positive, got "
            f"{first_output_timeout_seconds!r}"
        )
```

- [ ] **Step 5: Run to verify they pass**

Run: `cd mcp-servers/local-coder && .venv/bin/python -m pytest tests/test_config.py -k first_output -v`
Expected: all PASS.

- [ ] **Step 6: Run the full config suite**

Run: `cd mcp-servers/local-coder && .venv/bin/python -m pytest tests/test_config.py -v`
Expected: all PASS.

- [ ] **Step 7: Commit**

```bash
git add mcp-servers/local-coder/config.yaml mcp-servers/local-coder/config.py mcp-servers/local-coder/tests/test_config.py
git commit -m "local-coder: add first_output_timeout_seconds config field and validation"
```

---

### Task 4: README guidance + full-suite verification

**Files:**
- Modify: `mcp-servers/local-coder/README.md` (config-reference / operational-guidance section)
- No test file (docs + verification checkpoint)

**Interfaces:**
- Consumes: everything from Tasks 1-3.
- Produces: nothing (documentation + green-suite gate).

- [ ] **Step 1: Document the new field in README**

Find the config-reference table/section (grep README.md for `stall_timeout_seconds`). Add an entry for `first_output_timeout_seconds` adjacent to `stall_timeout_seconds`, stating exactly:
- What it governs: the maximum time a backend may run producing NO output before being killed — the cold-load grace window for a large local model loading into memory.
- Shipped default: `600` (seconds).
- Fallback: when omitted from a config, it falls back to `stall_timeout_seconds`, so pre-existing configs behave exactly as before.
- After the first byte of output, `stall_timeout_seconds` governs inactivity as usual.

Then add a short operational note (near the cold-load / models guidance if one exists, else a new "Cold-load latency" paragraph): the FIRST call to a large local model after idle can be slow purely from cold-load (no output while weights load); recommend configuring `fallback_models` for large primary models so a genuine cold-load failure fails over rather than aborting the call.

Do NOT invent a config-table format — match whatever the README already uses for the sibling timeout entries.

- [ ] **Step 2: Run the ENTIRE test suite**

Run: `cd mcp-servers/local-coder && .venv/bin/python -m pytest -v`
Expected: all PASS, zero failures. Record the count.

- [ ] **Step 3: Commit**

```bash
git add mcp-servers/local-coder/README.md
git commit -m "docs(local-coder): document first_output_timeout_seconds cold-load grace window"
```

---

## Self-Review

**1. Spec coverage:**
- Two-phase timer (before/after first byte) → Task 1. ✅
- Self-bounding grace, no separate cap → Task 1 (first-output budget itself fires). ✅
- `StallError` reports which budget fired → Task 1 (`phase`). ✅
- New config field, shipped 600 → Task 3. ✅
- Absent-key fallback = stall value, in ONE place → Task 1 (`None` → `stall_timeout_seconds`), reached via aider's `.get()` in Task 2. ✅
- Strictly-positive validation, no cross-field → Task 3. ✅
- Threading config.yaml → config.py → server → aider → run_monitored_subprocess → Tasks 2+3 (server.py needs no change: it already forwards the whole `cfg` dict into `run_backend`, and the value rides inside it). ✅
- README guidance + fallback_models recommendation → Task 4. ✅
- All 7 spec test cases covered: cold-load survives (T1), steady-state stall (T1), wedged cold-load (T1), omitted=old behavior (T1), absent→fallback (T3 + the aider None-pass test T2), present honored (T3), non-positive rejected (T3). ✅

**2. Placeholder scan:** The `...` markers in Tasks 2-3 test skeletons point at "copy the existing test's exact fixture/stub list" — a concrete instruction, not a TODO, because the surrounding tests already establish the monkeypatch pattern and inventing a parallel one would diverge from the suite. Every code step that changes production code shows the exact code. No "add error handling"/"handle edge cases" left vague.

**3. Type consistency:** `first_output_timeout_seconds: float | None` used identically in Task 1 (signature), Task 2 (`config.get(...)` → passed as that kwarg), Task 3 (resolution/validation). `StallError(timeout_seconds, phase=...)` with `.phase` and `.stall_timeout_seconds` attributes consistent between Task 1's definition and its test assertions. Config key string `"first_output_timeout_seconds"` identical across yaml, config.py, aider.py, and every test.

## Note on PR scope

This plan produces ONE coherent change (cold-load grace window + its config knob + its docs) — one problem, one PR, per repo rule. It is independent of the other two `local-coder/phase2-next` backlog items (config-persistence check, skill evals); those get their own plans/PRs. At PR time, split this task-set off `local-coder/phase2-next` as its own PR against `dev`.
