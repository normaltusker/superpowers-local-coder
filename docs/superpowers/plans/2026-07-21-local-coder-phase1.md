# Local-Coder Delegation (Phase 1: Aider + SDD Rewiring) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the local-coder MCP server (Aider backend only — Codex,
Gemini, OpenRouter stay `NotImplementedError` stubs) and rewire
`subagent-driven-development`'s implementer subagent to delegate
implementation to it instead of using Edit/Write directly, with that
delegation structurally enforced via a new restricted-tools subagent
definition.

**Architecture:** A Python FastMCP server at `mcp-servers/local-coder/`
exposes three tools (`delegate_implementation`, `configure`,
`list_available_models`) backed by a pluggable `BackendAdapter` interface.
Shared branch/stall-monitoring logic lives in `backends/common.py`; the
Aider adapter is the only concrete implementation this phase. The server
registers via a root-level `.mcp.json`. A new `agents/local-coder-implementer.md`
subagent definition excludes Edit/Write from its toolset entirely, and
`skills/subagent-driven-development/implementer-prompt.md` is rewritten to
dispatch that subagent and call `delegate_implementation` instead of
editing files.

**Tech Stack:** Python 3.13, FastMCP, PyYAML, pytest, subprocess-based
shelling out to `aider`/`git`/`gh`/`ollama` CLIs. No new JS/Node tooling —
this repo's existing skill-file/plugin infrastructure is untouched except
for the two files Part 2 modifies.

**Import convention (read this before Task 1):** `mcp-servers/local-coder/`
is run directly as a script (`python3 server.py`, per `.mcp.json`'s
invocation), not installed as a package — there is no `pyproject.toml` and
the directory name (`local-coder`, hyphenated) isn't a valid Python
identifier anyway, so a `from local_coder import ...`-style import would
never resolve. All internal imports inside `mcp-servers/local-coder/` are
**flat, relative to that directory**: `import config`, `import ollama`,
`from backends.base import BackendAdapter, CompletionResult`,
`from backends import common`, `from backends.aider import AiderBackend`.
`server.py` runs correctly because it's launched with
`mcp-servers/local-coder/` as its own directory (Python adds a script's own
directory to `sys.path[0]` automatically). Tests need the same resolution,
so Task 1 creates a `conftest.py` at `mcp-servers/local-coder/` (empty
except for one `sys.path` line) that makes pytest — invoked as
`cd mcp-servers/local-coder && .venv/bin/python -m pytest` — see the same
flat imports as `server.py` does. Every code sample in every task below
already reflects this; there is no ambiguity left to resolve mid-task.

## Global Constraints

- **MCP registration:** `.mcp.json` at the repo/plugin root (verified
  convention — see spec's "MCP server registration" section), not an
  `mcpServers` key in `.claude-plugin/plugin.json`.
- **Subagent definition location:** `agents/local-coder-implementer.md` at
  the plugin root (verified convention), not `.claude/agents/`.
- **Python packaging:** `requirements.txt` + venv, no `pyproject.toml`, no
  package installation — flat imports as described above.
  `requirements.txt` pins `fastmcp`, `PyYAML`, `pytest`.
- **`delegate_implementation` has no `model` parameter.** Model selection
  is `config.yaml`-only, set via `configure` before a plan starts. Do not
  add a per-call model override — this was explicitly decided against
  during design (SDD dispatch is autonomous; there's no point in the loop
  to inject a per-call value).
- **`self_commits=False` change detection uses `git status --porcelain`
  diffed against the pre-run snapshot**, never `git diff --name-only`
  alone (misses untracked/new files). This applies to the Codex/Gemini
  *design* even though they're not implemented this phase — if a task
  in a future phase implements them, it must follow this contract exactly
  as specified, not re-derive it.
- **`open_pr` defaults to `false`** in `config.yaml`. Do not change this
  default — `finishing-a-development-branch` owns PR creation for the SDD
  flow; `delegate_implementation` only pushes.
- **No remote handling:** `delegate_implementation` must check for an
  `origin` remote before attempting `git push`. No remote = success with
  `pr_url: null`, not a failure.
- **`fallback_models` capped at `max_fallback_models`** (config field,
  default 3). `configure` must reject a longer list, not silently truncate.
- **Ollama validation:** `configure` validates `ollama/`-prefixed `model`/
  `fallback_models` entries against `ollama list` before writing. Non-
  `ollama/`-prefixed strings are accepted unchecked.
- Full spec: `docs/superpowers/specs/2026-07-21-local-coder-delegation-design.md`
  — every task below implements a specific section of it; consult it for
  narrative rationale this plan doesn't repeat.

---

## File Structure

```
mcp-servers/local-coder/
  requirements.txt
  config.yaml
  conftest.py
  config.py
  ollama.py
  server.py
  backends/
    __init__.py
    base.py
    common.py
    aider.py
    codex.py          # stub only
    gemini.py         # stub only
    openrouter.py      # stub only
  README.md
  tests/
    test_config.py
    test_ollama.py
    test_backends_common.py
    test_aider.py
    test_server.py
.mcp.json
agents/
  local-coder-implementer.md
skills/subagent-driven-development/
  SKILL.md                 (modified)
  implementer-prompt.md    (modified)
```

Note: `tests/` has no `__init__.py` — pytest's default rootdir-relative
collection doesn't need one here, and adding one would risk turning
`tests` into a package that shadows the flat-import resolution described
above. `backends/` DOES get an `__init__.py` since `from backends.aider
import AiderBackend`-style imports need `backends` to be an importable
subpackage; `mcp-servers/local-coder/` itself never gets an `__init__.py`.

---

### Task 1: Python project scaffold + config load/save/merge

**Files:**
- Create: `mcp-servers/local-coder/requirements.txt`
- Create: `mcp-servers/local-coder/config.yaml`
- Create: `mcp-servers/local-coder/conftest.py`
- Create: `mcp-servers/local-coder/config.py`
- Create: `mcp-servers/local-coder/backends/__init__.py`
- Test: `mcp-servers/local-coder/tests/test_config.py`

**Interfaces:**
- Produces: `config.py::CONFIG_PATH` (module-level `Path`, resolves to the
  `config.yaml` next to this file, i.e.
  `Path(__file__).parent / "config.yaml"`), `config.py::load_config() -> dict`,
  `config.py::save_config(config: dict) -> None`, `config.py::merge_config(overrides: dict) -> dict`
  (loads current config, applies only the keys present in `overrides`,
  writes it back, returns the full resulting dict — does not perform
  Ollama/cap validation, that's layered on top in Task 5's `configure` tool).

- [ ] **Step 1: Create the venv, requirements file, and conftest.py**

Run:
```bash
cd mcp-servers/local-coder
python3.13 -m venv .venv
```
Expected: `.venv/` directory created. Use a Python 3.10+ interpreter explicitly (e.g. `python3.13`) — a bare `python3` can resolve to an older system Python (observed: 3.9.6), which silently fails to install `fastmcp` (requires >=3.10).

Check this repo's root `.gitignore` for an existing `.venv` entry:
```bash
grep -n "\.venv" ../../.gitignore
```
If no match, add one (from the repo root):
```bash
echo "mcp-servers/local-coder/.venv/" >> ../../.gitignore
```

Create `mcp-servers/local-coder/requirements.txt`:
```
fastmcp>=2.0.0
PyYAML>=6.0
pytest>=8.0.0
```

Run:
```bash
.venv/bin/pip install -r requirements.txt
```
Expected: fastmcp, PyYAML, pytest install without error.

Create `mcp-servers/local-coder/conftest.py` (this is what makes the flat
`import config`, `from backends.aider import AiderBackend`-style imports
resolve when pytest runs from this directory — see "Import convention"
in the plan header):
```python
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
```

- [ ] **Step 2: Write the default config.yaml**

Create `mcp-servers/local-coder/config.yaml`:
```yaml
backend: "aider"
model: "ollama/qwen3-coder:30b"
fallback_models: []
max_fallback_models: 3
stall_timeout_seconds: 300
target_repo_path: null
branch_prefix: "local-coder/"
open_pr: false
pr_base_branch: "main"
idle_notify_interval_seconds: 20
extra_backend_args: []
```

- [ ] **Step 3: Write the failing test for load/save/merge**

Create `mcp-servers/local-coder/tests/test_config.py`:
```python
import shutil
from pathlib import Path

import pytest

import config as config_module


@pytest.fixture
def isolated_config(tmp_path, monkeypatch):
    """Copy the real default config.yaml into a temp dir and point
    CONFIG_PATH at the copy, so tests never mutate the checked-in file."""
    real_config = Path(__file__).parent.parent / "config.yaml"
    temp_config = tmp_path / "config.yaml"
    shutil.copy(real_config, temp_config)
    monkeypatch.setattr(config_module, "CONFIG_PATH", temp_config)
    return temp_config


def test_load_config_returns_defaults(isolated_config):
    cfg = config_module.load_config()
    assert cfg["backend"] == "aider"
    assert cfg["model"] == "ollama/qwen3-coder:30b"
    assert cfg["fallback_models"] == []
    assert cfg["max_fallback_models"] == 3
    assert cfg["open_pr"] is False


def test_save_config_persists_changes(isolated_config):
    cfg = config_module.load_config()
    cfg["model"] = "ollama/qwen2.5-coder:14b"
    config_module.save_config(cfg)

    reloaded = config_module.load_config()
    assert reloaded["model"] == "ollama/qwen2.5-coder:14b"


def test_merge_config_updates_only_provided_keys(isolated_config):
    result = config_module.merge_config({"model": "ollama/deepseek-coder-v2:16b"})

    assert result["model"] == "ollama/deepseek-coder-v2:16b"
    assert result["backend"] == "aider"  # untouched key retains its value
    assert result["open_pr"] is False    # untouched key retains its value

    # confirm it was actually written, not just returned in-memory
    reloaded = config_module.load_config()
    assert reloaded["model"] == "ollama/deepseek-coder-v2:16b"


def test_merge_config_with_empty_overrides_is_a_noop(isolated_config):
    before = config_module.load_config()
    result = config_module.merge_config({})
    assert result == before
```

- [ ] **Step 4: Run test to verify it fails**

Run: `cd mcp-servers/local-coder && .venv/bin/python -m pytest tests/test_config.py -v`
Expected: FAIL — `config.py` does not exist yet (`ModuleNotFoundError: No
module named 'config'`).

- [ ] **Step 5: Write config.py**

Create `mcp-servers/local-coder/config.py`:
```python
from pathlib import Path

import yaml

CONFIG_PATH = Path(__file__).parent / "config.yaml"


def load_config() -> dict:
    with open(CONFIG_PATH) as f:
        return yaml.safe_load(f)


def save_config(config: dict) -> None:
    with open(CONFIG_PATH, "w") as f:
        yaml.safe_dump(config, f, default_flow_style=False, sort_keys=False)


def merge_config(overrides: dict) -> dict:
    current = load_config()
    for key, value in overrides.items():
        if value is not None:
            current[key] = value
    save_config(current)
    return current
```

Create `mcp-servers/local-coder/backends/__init__.py` (empty file — needed
now so `backends` is an importable subpackage from Task 3 onward).

- [ ] **Step 6: Run test to verify it passes**

Run: `cd mcp-servers/local-coder && .venv/bin/python -m pytest tests/test_config.py -v`
Expected: 4 passed.

- [ ] **Step 7: Commit**

```bash
git add mcp-servers/local-coder/requirements.txt \
        mcp-servers/local-coder/config.yaml \
        mcp-servers/local-coder/conftest.py \
        mcp-servers/local-coder/config.py \
        mcp-servers/local-coder/backends/__init__.py \
        mcp-servers/local-coder/tests/test_config.py \
        .gitignore
git commit -m "local-coder: add config load/save/merge with tests"
```

---

### Task 2: Ollama list wrapper (`ollama.py`)

**Files:**
- Create: `mcp-servers/local-coder/ollama.py`
- Test: `mcp-servers/local-coder/tests/test_ollama.py`

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces: `ollama.py::list_ollama_models() -> list[str]` (returns bare
  model names as reported by `ollama list`, e.g. `["qwen3-coder:30b",
  "qwen2.5-coder:14b"]` — NOT yet prefixed with `"ollama/"`; prefixing is
  the caller's job, done in Task 5's `list_available_models` tool, so this
  function stays a thin, testable wrapper around the CLI call).
  `ollama.py::OllamaUnavailableError(Exception)` (raised when `ollama` is
  not on PATH or the command fails — callers catch this to distinguish
  "no models" from "can't reach Ollama").

- [ ] **Step 1: Write the failing tests**

Create `mcp-servers/local-coder/tests/test_ollama.py`:
```python
from unittest.mock import patch, MagicMock

import pytest

import ollama


SAMPLE_OLLAMA_LIST_OUTPUT = """NAME                        ID              SIZE      MODIFIED
qwen3-coder:30b             06c1097efce0    18 GB     3 months ago
qwen2.5-coder:14b           dae161e27b0e    4.7 GB    6 weeks ago
"""


def test_list_ollama_models_parses_output():
    mock_result = MagicMock(returncode=0, stdout=SAMPLE_OLLAMA_LIST_OUTPUT)
    with patch("subprocess.run", return_value=mock_result) as mock_run:
        models = ollama.list_ollama_models()

    assert models == ["qwen3-coder:30b", "qwen2.5-coder:14b"]
    mock_run.assert_called_once()
    assert mock_run.call_args[0][0] == ["ollama", "list"]


def test_list_ollama_models_empty_when_none_pulled():
    header_only = "NAME                        ID              SIZE      MODIFIED\n"
    mock_result = MagicMock(returncode=0, stdout=header_only)
    with patch("subprocess.run", return_value=mock_result):
        models = ollama.list_ollama_models()

    assert models == []


def test_list_ollama_models_raises_when_ollama_not_on_path():
    with patch("subprocess.run", side_effect=FileNotFoundError()):
        with pytest.raises(ollama.OllamaUnavailableError):
            ollama.list_ollama_models()


def test_list_ollama_models_raises_when_command_fails():
    mock_result = MagicMock(returncode=1, stdout="", stderr="connection refused")
    with patch("subprocess.run", return_value=mock_result):
        with pytest.raises(ollama.OllamaUnavailableError):
            ollama.list_ollama_models()
```

Note: `patch("subprocess.run", ...)` patches the `subprocess` module's
`run` globally for the duration of the `with` block — this works here
because `ollama.py` does `import subprocess; subprocess.run(...)` (module
attribute access, not `from subprocess import run`), so the patch target
matches how the code under test actually calls it. Task 3 and 4's tests
patch `subprocess.run` the same way for the same reason; where a test
needs the REAL `subprocess.run` to still work for its own setup (e.g. a
`git_repo` fixture creating a real repo before the code under test uses
a mocked-out subprocess), that fixture code runs before the `patch(...)`
context manager opens, not inside it.

- [ ] **Step 2: Run test to verify it fails**

Run: `cd mcp-servers/local-coder && .venv/bin/python -m pytest tests/test_ollama.py -v`
Expected: FAIL — `ollama.py` does not exist.

- [ ] **Step 3: Write ollama.py**

Create `mcp-servers/local-coder/ollama.py`:
```python
import subprocess


class OllamaUnavailableError(Exception):
    pass


def list_ollama_models() -> list[str]:
    try:
        result = subprocess.run(
            ["ollama", "list"], capture_output=True, text=True
        )
    except FileNotFoundError as e:
        raise OllamaUnavailableError("ollama is not on PATH") from e

    if result.returncode != 0:
        raise OllamaUnavailableError(
            f"ollama list failed: {result.stderr.strip()}"
        )

    lines = result.stdout.strip().splitlines()
    if len(lines) <= 1:
        return []

    models = []
    for line in lines[1:]:  # skip header row
        parts = line.split()
        if parts:
            models.append(parts[0])
    return models
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd mcp-servers/local-coder && .venv/bin/python -m pytest tests/test_ollama.py -v`
Expected: 4 passed.

- [ ] **Step 5: Commit**

```bash
git add mcp-servers/local-coder/ollama.py mcp-servers/local-coder/tests/test_ollama.py
git commit -m "local-coder: add ollama list wrapper with tests"
```

---

### Task 3: Backend adapter interface + shared branch/stall logic

**Files:**
- Create: `mcp-servers/local-coder/backends/base.py`
- Create: `mcp-servers/local-coder/backends/common.py`
- Test: `mcp-servers/local-coder/tests/test_backends_common.py`

**Interfaces:**
- Consumes: nothing from earlier tasks (this is pure git/subprocess logic).
- Produces:
  - `backends/base.py::CompletionResult` (dataclass: `success: bool`,
    `files_changed: list[str]`, `commit_sha: str | None`,
    `error: str | None = None`).
  - `backends/base.py::BackendAdapter` (ABC with class attribute
    `self_commits: bool` and abstract method
    `run_backend(self, task: str, repo_path: str, branch: str, config: dict, model: str | None = None) -> CompletionResult`).
  - `backends/common.py::StallError(Exception)` (raised internally when a
    subprocess is killed for stalling; carries the timeout value in its
    message).
  - `backends/common.py::ensure_branch(repo_path: str, branch: str) -> None`
    (checks out `branch`, creating it if it doesn't exist).
  - `backends/common.py::snapshot_working_tree(repo_path: str) -> tuple[str, set[str]]`
    (returns `(pre_head_sha, porcelain_lines)` — `porcelain_lines` is the
    parsed `git status --porcelain` output as a set of strings, used later
    by `self_commits=False` backends to diff against).
  - `backends/common.py::run_monitored_subprocess(cmd: list[str], cwd: str, stall_timeout_seconds: float, idle_notify_interval_seconds: float, on_tick: Callable[[], None] | None = None) -> subprocess.CompletedProcess`
    (runs `cmd`, polls output activity every `idle_notify_interval_seconds`,
    calls `on_tick()` on each poll if provided — this is the hook Task 6's
    server wires to `ctx.report_progress()` plus a stderr log line, kept
    generic here since `common.py` has no FastMCP dependency — raises
    `StallError` if no new output for `stall_timeout_seconds`, otherwise
    returns normally with the completed process once the subprocess exits).

- [ ] **Step 1: Write the failing tests**

Create `mcp-servers/local-coder/tests/test_backends_common.py`:
```python
import subprocess
import time

import pytest

from backends import common


@pytest.fixture
def git_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
    (repo / "README.md").write_text("hello\n")
    subprocess.run(["git", "add", "README.md"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=repo, check=True, capture_output=True)
    return repo


def test_ensure_branch_creates_new_branch(git_repo):
    common.ensure_branch(str(git_repo), "feature/test-branch")
    result = subprocess.run(
        ["git", "branch", "--show-current"], cwd=git_repo,
        capture_output=True, text=True, check=True,
    )
    assert result.stdout.strip() == "feature/test-branch"


def test_ensure_branch_checks_out_existing_branch(git_repo):
    subprocess.run(["git", "checkout", "-b", "existing-branch"], cwd=git_repo, check=True, capture_output=True)
    subprocess.run(["git", "checkout", "main"], cwd=git_repo, check=True, capture_output=True)

    common.ensure_branch(str(git_repo), "existing-branch")

    result = subprocess.run(
        ["git", "branch", "--show-current"], cwd=git_repo,
        capture_output=True, text=True, check=True,
    )
    assert result.stdout.strip() == "existing-branch"


def test_snapshot_working_tree_returns_head_and_clean_status(git_repo):
    pre_head, porcelain = common.snapshot_working_tree(str(git_repo))

    expected_head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=git_repo,
        capture_output=True, text=True, check=True,
    ).stdout.strip()

    assert pre_head == expected_head
    assert porcelain == set()  # clean working tree


def test_snapshot_working_tree_detects_dirty_state(git_repo):
    (git_repo / "untracked.txt").write_text("new file\n")
    _, porcelain = common.snapshot_working_tree(str(git_repo))
    assert any("untracked.txt" in line for line in porcelain)


def test_run_monitored_subprocess_returns_completed_process_on_success():
    result = common.run_monitored_subprocess(
        ["echo", "hello"], cwd=".",
        stall_timeout_seconds=5, idle_notify_interval_seconds=1,
    )
    assert result.returncode == 0


def test_run_monitored_subprocess_calls_on_tick():
    ticks = []
    common.run_monitored_subprocess(
        ["sleep", "0.3"], cwd=".",
        stall_timeout_seconds=5, idle_notify_interval_seconds=0.1,
        on_tick=lambda: ticks.append(time.time()),
    )
    assert len(ticks) >= 1


def test_run_monitored_subprocess_raises_stall_error_when_no_output():
    with pytest.raises(common.StallError):
        common.run_monitored_subprocess(
            ["sleep", "2"], cwd=".",
            stall_timeout_seconds=0.2, idle_notify_interval_seconds=0.05,
        )
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd mcp-servers/local-coder && .venv/bin/python -m pytest tests/test_backends_common.py -v`
Expected: FAIL — `backends/common.py` does not exist.

- [ ] **Step 3: Write base.py**

Create `mcp-servers/local-coder/backends/base.py`:
```python
from abc import ABC, abstractmethod
from dataclasses import dataclass, field


@dataclass
class CompletionResult:
    success: bool
    files_changed: list[str] = field(default_factory=list)
    commit_sha: str | None = None
    error: str | None = None


class BackendAdapter(ABC):
    self_commits: bool

    @abstractmethod
    def run_backend(
        self,
        task: str,
        repo_path: str,
        branch: str,
        config: dict,
        model: str | None = None,
    ) -> CompletionResult:
        ...
```

- [ ] **Step 4: Write common.py**

Create `mcp-servers/local-coder/backends/common.py`:
```python
import subprocess
import time
from typing import Callable


class StallError(Exception):
    def __init__(self, stall_timeout_seconds: float):
        self.stall_timeout_seconds = stall_timeout_seconds
        super().__init__(f"stalled: no output for {stall_timeout_seconds}s")


def ensure_branch(repo_path: str, branch: str) -> None:
    verify = subprocess.run(
        ["git", "-C", repo_path, "rev-parse", "--verify", branch],
        capture_output=True,
    )
    if verify.returncode == 0:
        subprocess.run(
            ["git", "-C", repo_path, "checkout", branch],
            check=True, capture_output=True,
        )
    else:
        subprocess.run(
            ["git", "-C", repo_path, "checkout", "-b", branch],
            check=True, capture_output=True,
        )


def snapshot_working_tree(repo_path: str) -> tuple[str, set[str]]:
    pre_head = subprocess.run(
        ["git", "-C", repo_path, "rev-parse", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()

    status = subprocess.run(
        ["git", "-C", repo_path, "status", "--porcelain"],
        capture_output=True, text=True, check=True,
    ).stdout

    porcelain = {line for line in status.splitlines() if line.strip()}
    return pre_head, porcelain


def run_monitored_subprocess(
    cmd: list[str],
    cwd: str,
    stall_timeout_seconds: float,
    idle_notify_interval_seconds: float,
    on_tick: Callable[[], None] | None = None,
) -> subprocess.CompletedProcess:
    process = subprocess.Popen(
        cmd, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )

    output_lines: list[str] = []
    last_activity = time.monotonic()
    last_tick = time.monotonic()

    while True:
        line = process.stdout.readline() if process.stdout else ""
        if line:
            output_lines.append(line)
            last_activity = time.monotonic()

        if process.poll() is not None and not line:
            break

        now = time.monotonic()
        if now - last_tick >= idle_notify_interval_seconds:
            if on_tick is not None:
                on_tick()
            last_tick = now

        if now - last_activity > stall_timeout_seconds:
            process.kill()
            process.wait()
            raise StallError(stall_timeout_seconds)

        if not line:
            time.sleep(min(idle_notify_interval_seconds, 0.5))

    returncode = process.wait()
    return subprocess.CompletedProcess(
        cmd, returncode, stdout="".join(output_lines), stderr=""
    )
```

- [ ] **Step 5: Run test to verify it passes**

Run: `cd mcp-servers/local-coder && .venv/bin/python -m pytest tests/test_backends_common.py -v`
Expected: 7 passed.

If `test_run_monitored_subprocess_calls_on_tick` or the stall test are
flaky due to timing, that's a real risk with wall-clock-based polling —
if either fails intermittently, increase the test's margins (e.g. widen
`idle_notify_interval_seconds` and the `sleep` duration proportionally)
rather than deleting the test; do not skip stall-timeout coverage.

- [ ] **Step 6: Commit**

```bash
git add mcp-servers/local-coder/backends/base.py mcp-servers/local-coder/backends/common.py mcp-servers/local-coder/tests/test_backends_common.py
git commit -m "local-coder: add BackendAdapter interface and shared branch/stall logic"
```

---

### Task 4: Aider backend adapter

**Files:**
- Create: `mcp-servers/local-coder/backends/aider.py`
- Create: `mcp-servers/local-coder/backends/codex.py` (stub)
- Create: `mcp-servers/local-coder/backends/gemini.py` (stub)
- Create: `mcp-servers/local-coder/backends/openrouter.py` (stub)
- Test: `mcp-servers/local-coder/tests/test_aider.py`

**Interfaces:**
- Consumes: `backends.base.BackendAdapter`, `backends.base.CompletionResult`,
  `backends.common.ensure_branch`, `backends.common.snapshot_working_tree`,
  `backends.common.run_monitored_subprocess`, `backends.common.StallError`
  (all from Task 3).
- Produces: `backends/aider.py::AiderBackend` (concrete `BackendAdapter`,
  `self_commits = True`). `backends/codex.py::CodexBackend`,
  `backends/gemini.py::GeminiBackend`,
  `backends/openrouter.py::OpenRouterBackend` — each a `BackendAdapter`
  subclass whose `run_backend` immediately raises
  `NotImplementedError("<Name> backend not yet implemented")`; these are
  consumed by Task 6's backend-selection logic in `server.py` so that
  `configure`-ing to `backend: "codex"` etc. is possible without crashing
  the server, it just fails cleanly at call time.

- [ ] **Step 1: Write the failing tests**

Create `mcp-servers/local-coder/tests/test_aider.py`:
```python
import subprocess
from unittest.mock import patch

import pytest

from backends.aider import AiderBackend
from backends import common


@pytest.fixture
def git_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
    (repo / "README.md").write_text("hello\n")
    subprocess.run(["git", "add", "README.md"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=repo, check=True, capture_output=True)
    return repo


BASE_CONFIG = {
    "idle_notify_interval_seconds": 20,
    "stall_timeout_seconds": 300,
    "extra_backend_args": [],
}


def test_self_commits_is_true():
    assert AiderBackend.self_commits is True


def test_run_backend_success_when_aider_commits(git_repo):
    def fake_run(cmd, cwd, stall_timeout_seconds, idle_notify_interval_seconds, on_tick=None):
        # simulate aider making a commit
        (git_repo / "new_file.py").write_text("# new\n")
        subprocess.run(["git", "add", "new_file.py"], cwd=git_repo, check=True)
        subprocess.run(["git", "commit", "-m", "aider commit"], cwd=git_repo, check=True, capture_output=True)
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    backend = AiderBackend()
    with patch.object(common, "run_monitored_subprocess", side_effect=fake_run):
        result = backend.run_backend(
            task="add a file", repo_path=str(git_repo), branch="test-branch",
            config=BASE_CONFIG, model="ollama/qwen3-coder:30b",
        )

    assert result.success is True
    assert result.commit_sha is not None
    assert "new_file.py" in result.files_changed


def test_run_backend_failure_when_no_commit_made(git_repo):
    def fake_run(cmd, cwd, stall_timeout_seconds, idle_notify_interval_seconds, on_tick=None):
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    backend = AiderBackend()
    with patch.object(common, "run_monitored_subprocess", side_effect=fake_run):
        result = backend.run_backend(
            task="do nothing", repo_path=str(git_repo), branch="test-branch",
            config=BASE_CONFIG, model="ollama/qwen3-coder:30b",
        )

    assert result.success is False
    assert "no commits" in result.error


def test_run_backend_failure_on_nonzero_exit(git_repo):
    def fake_run(cmd, cwd, stall_timeout_seconds, idle_notify_interval_seconds, on_tick=None):
        return subprocess.CompletedProcess(cmd, 1, stdout="aider crashed", stderr="")

    backend = AiderBackend()
    with patch.object(common, "run_monitored_subprocess", side_effect=fake_run):
        result = backend.run_backend(
            task="do something", repo_path=str(git_repo), branch="test-branch",
            config=BASE_CONFIG, model="ollama/qwen3-coder:30b",
        )

    assert result.success is False
    assert "aider crashed" in result.error


def test_run_backend_failure_on_stall(git_repo):
    backend = AiderBackend()
    with patch.object(common, "run_monitored_subprocess", side_effect=common.StallError(300)):
        result = backend.run_backend(
            task="hang forever", repo_path=str(git_repo), branch="test-branch",
            config=BASE_CONFIG, model="ollama/qwen3-coder:30b",
        )

    assert result.success is False
    assert "stalled" in result.error


def test_run_backend_creates_branch_if_missing(git_repo):
    def fake_run(cmd, cwd, stall_timeout_seconds, idle_notify_interval_seconds, on_tick=None):
        subprocess.run(["git", "commit", "--allow-empty", "-m", "aider commit"], cwd=git_repo, check=True, capture_output=True)
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    backend = AiderBackend()
    with patch.object(common, "run_monitored_subprocess", side_effect=fake_run):
        backend.run_backend(
            task="task", repo_path=str(git_repo), branch="brand-new-branch",
            config=BASE_CONFIG, model="ollama/qwen3-coder:30b",
        )

    current_branch = subprocess.run(
        ["git", "branch", "--show-current"], cwd=git_repo,
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert current_branch == "brand-new-branch"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd mcp-servers/local-coder && .venv/bin/python -m pytest tests/test_aider.py -v`
Expected: FAIL — `backends/aider.py` does not exist.

- [ ] **Step 3: Write aider.py**

Create `mcp-servers/local-coder/backends/aider.py`:
```python
import subprocess

from backends.base import BackendAdapter, CompletionResult
from backends import common


class AiderBackend(BackendAdapter):
    self_commits = True

    def run_backend(
        self,
        task: str,
        repo_path: str,
        branch: str,
        config: dict,
        model: str | None = None,
    ) -> CompletionResult:
        common.ensure_branch(repo_path, branch)
        pre_head, _ = common.snapshot_working_tree(repo_path)

        cmd = [
            "aider", "--model", model, "--yes", "--message", task,
            *config.get("extra_backend_args", []),
        ]

        try:
            result = common.run_monitored_subprocess(
                cmd,
                cwd=repo_path,
                stall_timeout_seconds=config["stall_timeout_seconds"],
                idle_notify_interval_seconds=config["idle_notify_interval_seconds"],
            )
        except common.StallError as e:
            return CompletionResult(success=False, error=str(e))

        if result.returncode != 0:
            return CompletionResult(
                success=False,
                error=result.stdout.strip()[-2000:] or "aider exited non-zero",
            )

        post_head = subprocess.run(
            ["git", "-C", repo_path, "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()

        if post_head == pre_head:
            return CompletionResult(success=False, error="aider made no commits")

        files_changed = subprocess.run(
            ["git", "-C", repo_path, "diff", "--name-only", pre_head, post_head],
            capture_output=True, text=True, check=True,
        ).stdout.splitlines()

        return CompletionResult(
            success=True,
            files_changed=files_changed,
            commit_sha=post_head,
        )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd mcp-servers/local-coder && .venv/bin/python -m pytest tests/test_aider.py -v`
Expected: 6 passed.

- [ ] **Step 5: Write the three stub backends**

Create `mcp-servers/local-coder/backends/codex.py`:
```python
from backends.base import BackendAdapter, CompletionResult


class CodexBackend(BackendAdapter):
    self_commits = False

    def run_backend(
        self,
        task: str,
        repo_path: str,
        branch: str,
        config: dict,
        model: str | None = None,
    ) -> CompletionResult:
        raise NotImplementedError("Codex backend not yet implemented")
```

Create `mcp-servers/local-coder/backends/gemini.py`:
```python
from backends.base import BackendAdapter, CompletionResult


class GeminiBackend(BackendAdapter):
    self_commits = False

    def run_backend(
        self,
        task: str,
        repo_path: str,
        branch: str,
        config: dict,
        model: str | None = None,
    ) -> CompletionResult:
        raise NotImplementedError("Gemini backend not yet implemented")
```

Create `mcp-servers/local-coder/backends/openrouter.py`:
```python
from backends.base import BackendAdapter, CompletionResult


class OpenRouterBackend(BackendAdapter):
    self_commits = False  # not yet determined — OpenRouter backend is unresearched

    def run_backend(
        self,
        task: str,
        repo_path: str,
        branch: str,
        config: dict,
        model: str | None = None,
    ) -> CompletionResult:
        raise NotImplementedError("OpenRouter backend not yet implemented")
```

No tests needed for the three stubs beyond what Task 6's `test_server.py`
covers (selecting a stub backend and confirming `NotImplementedError`
surfaces as a clean error, not a crash) — testing `raise NotImplementedError`
directly here would be testing Python's own `raise` statement.

- [ ] **Step 6: Commit**

```bash
git add mcp-servers/local-coder/backends/aider.py \
        mcp-servers/local-coder/backends/codex.py \
        mcp-servers/local-coder/backends/gemini.py \
        mcp-servers/local-coder/backends/openrouter.py \
        mcp-servers/local-coder/tests/test_aider.py
git commit -m "local-coder: add Aider backend adapter, stub Codex/Gemini/OpenRouter"
```

---

### Task 5: `configure` and `list_available_models` validation logic (pre-FastMCP)

**Files:**
- Modify: `mcp-servers/local-coder/config.py`
- Test: `mcp-servers/local-coder/tests/test_config.py` (extend)

**Interfaces:**
- Consumes: `config.py::load_config`, `config.py::save_config`,
  `config.py::merge_config` (Task 1), `ollama.py::list_ollama_models`,
  `ollama.py::OllamaUnavailableError` (Task 2).
- Produces: `config.py::ConfigValidationError(Exception)` (raised by the
  functions below; the FastMCP tool wrapper in Task 6 catches this and
  turns it into a clean MCP error response). `config.py::configure_with_validation(overrides: dict) -> dict`
  (the actual logic behind the `configure` MCP tool — validates
  `ollama/`-prefixed `model`/`fallback_models` entries against
  `list_ollama_models()`, validates `fallback_models` length against
  `max_fallback_models` (current value, or the incoming override if
  `max_fallback_models` is itself being changed in the same call), rejects
  `backend == "gemini"` combined with any `ollama/`-prefixed model/fallback,
  then calls `merge_config` only if all validation passes; raises
  `ConfigValidationError` with a clear message otherwise).
  `config.py::list_available_models_with_prefix() -> list[str]` (calls
  `list_ollama_models()` and returns each entry prefixed with `"ollama/"`).

This task is written as plain functions in `config.py`, separate from the
FastMCP tool declarations in Task 6's `server.py`, so the validation logic
is unit-testable without needing a running MCP server or FastMCP's test
harness.

- [ ] **Step 1: Write the failing tests**

Append to `mcp-servers/local-coder/tests/test_config.py` (add this import
near the top of the file, alongside the existing `import config as
config_module`):
```python
import ollama as ollama_module
```

Then append these test functions to the end of the file:
```python
def test_configure_with_validation_rejects_unpulled_ollama_model(isolated_config):
    with patch.object(ollama_module, "list_ollama_models", return_value=["qwen3-coder:30b"]):
        with pytest.raises(config_module.ConfigValidationError, match="not pulled|not found|unavailable"):
            config_module.configure_with_validation({"model": "ollama/does-not-exist:1b"})


def test_configure_with_validation_accepts_pulled_ollama_model(isolated_config):
    with patch.object(ollama_module, "list_ollama_models", return_value=["qwen2.5-coder:14b"]):
        result = config_module.configure_with_validation({"model": "ollama/qwen2.5-coder:14b"})
    assert result["model"] == "ollama/qwen2.5-coder:14b"


def test_configure_with_validation_skips_check_for_non_ollama_prefix(isolated_config):
    # no mock needed — should never call list_ollama_models for a non-ollama/ model
    with patch.object(ollama_module, "list_ollama_models") as mock_list:
        result = config_module.configure_with_validation({"model": "openrouter/some-model"})
        mock_list.assert_not_called()
    assert result["model"] == "openrouter/some-model"


def test_configure_with_validation_rejects_fallback_list_over_cap(isolated_config):
    with patch.object(ollama_module, "list_ollama_models", return_value=["a:1b", "b:1b", "c:1b", "d:1b"]):
        with pytest.raises(config_module.ConfigValidationError, match="max_fallback_models|limit"):
            config_module.configure_with_validation({
                "fallback_models": ["ollama/a:1b", "ollama/b:1b", "ollama/c:1b", "ollama/d:1b"]
            })


def test_configure_with_validation_accepts_fallback_list_at_cap(isolated_config):
    with patch.object(ollama_module, "list_ollama_models", return_value=["a:1b", "b:1b", "c:1b"]):
        result = config_module.configure_with_validation({
            "fallback_models": ["ollama/a:1b", "ollama/b:1b", "ollama/c:1b"]
        })
    assert len(result["fallback_models"]) == 3


def test_configure_with_validation_rejects_gemini_with_ollama_model(isolated_config):
    with pytest.raises(config_module.ConfigValidationError, match="[Gg]emini"):
        config_module.configure_with_validation({
            "backend": "gemini", "model": "ollama/qwen3-coder:30b"
        })


def test_list_available_models_with_prefix_prefixes_correctly(isolated_config):
    with patch.object(ollama_module, "list_ollama_models", return_value=["qwen3-coder:30b", "qwen2.5-coder:14b"]):
        models = config_module.list_available_models_with_prefix()
    assert models == ["ollama/qwen3-coder:30b", "ollama/qwen2.5-coder:14b"]


def test_list_available_models_with_prefix_propagates_unavailable_error(isolated_config):
    with patch.object(ollama_module, "list_ollama_models", side_effect=ollama_module.OllamaUnavailableError("no ollama")):
        with pytest.raises(ollama_module.OllamaUnavailableError):
            config_module.list_available_models_with_prefix()
```

Also add `from unittest.mock import patch` to the top of the file if it
isn't already imported (it wasn't needed by Task 1's original tests).

- [ ] **Step 2: Run test to verify it fails**

Run: `cd mcp-servers/local-coder && .venv/bin/python -m pytest tests/test_config.py -v -k "configure_with_validation or list_available_models_with_prefix"`
Expected: FAIL — `ConfigValidationError`, `configure_with_validation`,
`list_available_models_with_prefix` don't exist yet.

- [ ] **Step 3: Extend config.py**

Add to the top of `mcp-servers/local-coder/config.py`, alongside the
existing `from pathlib import Path` / `import yaml` imports:
```python
import ollama as ollama_module
```

Then append below the existing `merge_config` function:
```python
class ConfigValidationError(Exception):
    pass


def _validate_ollama_model(model: str) -> None:
    if not model.startswith("ollama/"):
        return
    bare_name = model[len("ollama/"):]
    available = ollama_module.list_ollama_models()
    if bare_name not in available:
        raise ConfigValidationError(
            f"Model '{model}' is not pulled. Available: "
            f"{', '.join('ollama/' + m for m in available) or '(none)'}"
        )


def configure_with_validation(overrides: dict) -> dict:
    current = load_config()

    backend = overrides.get("backend", current.get("backend"))
    model = overrides.get("model", current.get("model"))
    fallback_models = overrides.get("fallback_models", current.get("fallback_models", []))
    max_fallback = overrides.get("max_fallback_models", current.get("max_fallback_models", 3))

    if model:
        _validate_ollama_model(model)
    for fb in fallback_models:
        _validate_ollama_model(fb)

    if len(fallback_models) > max_fallback:
        raise ConfigValidationError(
            f"fallback_models has {len(fallback_models)} entries, exceeding "
            f"max_fallback_models={max_fallback}. Raise the cap first with "
            f"configure(max_fallback_models=...) if you want more."
        )

    if backend == "gemini":
        gemini_models = [m for m in [model, *fallback_models] if m and m.startswith("ollama/")]
        if gemini_models:
            raise ConfigValidationError(
                "Gemini CLI has no local-model support — "
                f"cannot use backend='gemini' with {gemini_models}"
            )

    return merge_config(overrides)


def list_available_models_with_prefix() -> list[str]:
    return [f"ollama/{m}" for m in ollama_module.list_ollama_models()]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd mcp-servers/local-coder && .venv/bin/python -m pytest tests/test_config.py -v`
Expected: all tests pass (original 4 from Task 1 + 8 new = 12 total).

- [ ] **Step 5: Commit**

```bash
git add mcp-servers/local-coder/config.py mcp-servers/local-coder/tests/test_config.py
git commit -m "local-coder: add configure validation (ollama check, fallback cap, gemini guard)"
```

---

### Task 6: FastMCP server — `delegate_implementation`, `configure`, `list_available_models`

**Files:**
- Create: `mcp-servers/local-coder/server.py`
- Test: `mcp-servers/local-coder/tests/test_server.py`

**Interfaces:**
- Consumes: everything from Tasks 1-5 — `config.load_config`,
  `config.configure_with_validation`, `config.list_available_models_with_prefix`,
  `config.ConfigValidationError`, `ollama.OllamaUnavailableError`,
  `backends.aider.AiderBackend`, `backends.codex.CodexBackend`,
  `backends.gemini.GeminiBackend`, `backends.openrouter.OpenRouterBackend`,
  `backends.base.CompletionResult`.
- Produces: the three MCP tools as FastMCP-decorated functions. This is
  the last task before server.py is callable end-to-end — Task 7 (README +
  `.mcp.json`) and Task 8 (SDD rewiring) depend on this existing and
  working.

**Correction, discovered during Task 6 dispatch (binding — the code below
already reflects it):** the installed `fastmcp` (3.4.4) raises
`ValueError: Functions with **kwargs are not supported as tools` at
`@mcp.tool()` decoration time — i.e. at module import, before any test can
run. An earlier draft of this plan gave `configure`/`_configure_impl` a
`**overrides` catch-all parameter (mirroring the design spec's
`configure(backend=None, model=None, ..., **overrides)` signature), which
is incompatible with this FastMCP version's tool-schema validation.
Fixed by enumerating every `config.yaml` key as an explicit named
parameter instead of a catch-all — the set is fixed and known (11 keys),
so nothing is actually lost by naming them; MCP clients get a properly
typed schema for each field instead of an opaque passthrough. This only
affects the two `@mcp.tool()`-facing functions (`configure` and
`_configure_impl`, both shown with the corrected signature below) —
`config.py`'s internal `configure_with_validation(overrides: dict)` from
Task 5 is unaffected, since it's never decorated and can keep taking a
plain dict.

**Note on testing FastMCP tools:** FastMCP's `@mcp.tool()` decorator
returns a `FunctionTool` wrapper, not the plain function — calling
`server.delegate_implementation(...)` directly from a test would call the
wrapper, not exercise the same code path a real MCP client invocation
does, and FastMCP wrappers don't always support being called like a plain
Python function with positional/keyword args. To keep this testable
without spinning up a real MCP transport, this task separates concerns:
the actual logic for each tool lives in a plain, undecorated function
(`_delegate_implementation_impl`, `_configure_impl`,
`_list_available_models_impl`), and the `@mcp.tool()`-decorated function
is a one-line wrapper that just calls the plain function. Tests call the
plain functions directly; `.mcp.json` still launches `server.py`, which
registers the decorated wrappers with FastMCP as usual.

- [ ] **Step 1: Write the failing tests**

Create `mcp-servers/local-coder/tests/test_server.py`:
```python
import subprocess
import shutil
from pathlib import Path
from unittest.mock import patch

import pytest

import server
import config as config_module
from backends.base import CompletionResult


@pytest.fixture
def isolated_config(tmp_path, monkeypatch):
    real_config = Path(__file__).parent.parent / "config.yaml"
    temp_config = tmp_path / "config.yaml"
    shutil.copy(real_config, temp_config)
    monkeypatch.setattr(config_module, "CONFIG_PATH", temp_config)
    return temp_config


@pytest.fixture
def git_repo_with_remote(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
    (repo / "README.md").write_text("hello\n")
    subprocess.run(["git", "add", "README.md"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=repo, check=True, capture_output=True)
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)
    subprocess.run(["git", "remote", "add", "origin", str(remote)], cwd=repo, check=True)
    subprocess.run(["git", "push", "-u", "origin", "main"], cwd=repo, check=True, capture_output=True)
    return repo


@pytest.fixture
def git_repo_no_remote(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
    (repo / "README.md").write_text("hello\n")
    subprocess.run(["git", "add", "README.md"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=repo, check=True, capture_output=True)
    return repo


def test_delegate_implementation_missing_repo_path_returns_error(isolated_config):
    result = server._delegate_implementation_impl(task="do something", branch="test-branch", target_repo_path=None)
    assert result["success"] is False
    assert "target_repo_path" in result["error"]


def test_delegate_implementation_success_no_remote(isolated_config, git_repo_no_remote):
    fake_result = CompletionResult(success=True, files_changed=["a.py"], commit_sha="abc123")
    with patch("backends.aider.AiderBackend.run_backend", return_value=fake_result):
        result = server._delegate_implementation_impl(
            task="add a.py", branch="feature-branch",
            target_repo_path=str(git_repo_no_remote),
        )

    assert result["success"] is True
    assert result["pr_url"] is None
    assert "note" in result
    assert result["model_used"] == "ollama/qwen3-coder:30b"


def test_delegate_implementation_pushes_when_remote_exists(isolated_config, git_repo_with_remote):
    fake_result = CompletionResult(success=True, files_changed=["a.py"], commit_sha="abc123")
    with patch("backends.aider.AiderBackend.run_backend", return_value=fake_result):
        with patch("subprocess.run", wraps=subprocess.run) as spy:
            result = server._delegate_implementation_impl(
                task="add a.py", branch="feature-branch",
                target_repo_path=str(git_repo_with_remote),
            )

    assert result["success"] is True
    push_calls = [c for c in spy.call_args_list if "push" in c.args[0]]
    assert len(push_calls) >= 1


def test_delegate_implementation_failover_to_second_model(isolated_config, git_repo_no_remote):
    config_module.merge_config({"fallback_models": ["ollama/qwen2.5-coder:14b"]})
    fail_result = CompletionResult(success=False, error="stalled: no output for 300s")
    success_result = CompletionResult(success=True, files_changed=["a.py"], commit_sha="def456")

    with patch(
        "backends.aider.AiderBackend.run_backend",
        side_effect=[fail_result, success_result],
    ):
        result = server._delegate_implementation_impl(
            task="add a.py", branch="feature-branch",
            target_repo_path=str(git_repo_no_remote),
        )

    assert result["success"] is True
    assert result["model_used"] == "ollama/qwen2.5-coder:14b"


def test_delegate_implementation_all_models_fail(isolated_config, git_repo_no_remote):
    config_module.merge_config({"fallback_models": ["ollama/qwen2.5-coder:14b"]})
    fail_result = CompletionResult(success=False, error="aider made no commits")

    with patch("backends.aider.AiderBackend.run_backend", return_value=fail_result):
        result = server._delegate_implementation_impl(
            task="add a.py", branch="feature-branch",
            target_repo_path=str(git_repo_no_remote),
        )

    assert result["success"] is False
    assert "ollama/qwen3-coder:30b" in result["error"]
    assert "ollama/qwen2.5-coder:14b" in result["error"]


def test_delegate_implementation_unimplemented_backend_returns_clean_error(isolated_config, git_repo_no_remote):
    config_module.merge_config({"backend": "codex"})
    result = server._delegate_implementation_impl(
        task="add a.py", branch="feature-branch",
        target_repo_path=str(git_repo_no_remote),
    )
    assert result["success"] is False
    assert "not yet implemented" in result["error"]


def test_configure_returns_full_config(isolated_config):
    with patch("ollama.list_ollama_models", return_value=["qwen2.5-coder:14b"]):
        result = server._configure_impl(model="ollama/qwen2.5-coder:14b")
    assert result["model"] == "ollama/qwen2.5-coder:14b"
    assert result["backend"] == "aider"


def test_configure_rejects_invalid_model_with_clean_error(isolated_config):
    with patch("ollama.list_ollama_models", return_value=[]):
        result = server._configure_impl(model="ollama/nonexistent:1b")
    assert result["success"] is False
    assert "error" in result


def test_list_available_models_returns_prefixed_list(isolated_config):
    with patch("ollama.list_ollama_models", return_value=["qwen3-coder:30b"]):
        result = server._list_available_models_impl()
    assert result["models"] == ["ollama/qwen3-coder:30b"]


def test_list_available_models_returns_clean_error_when_unreachable(isolated_config):
    from ollama import OllamaUnavailableError
    with patch("ollama.list_ollama_models", side_effect=OllamaUnavailableError("no ollama")):
        result = server._list_available_models_impl()
    assert "error" in result
    assert "models" not in result or result.get("models") is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd mcp-servers/local-coder && .venv/bin/python -m pytest tests/test_server.py -v`
Expected: FAIL — `server.py` does not exist.

- [ ] **Step 3: Write server.py**

Create `mcp-servers/local-coder/server.py`:
```python
import subprocess

from fastmcp import FastMCP

import config as config_module
from backends.aider import AiderBackend
from backends.codex import CodexBackend
from backends.gemini import GeminiBackend
from backends.openrouter import OpenRouterBackend

mcp = FastMCP("local-coder")

BACKENDS = {
    "aider": AiderBackend,
    "codex": CodexBackend,
    "gemini": GeminiBackend,
    "openrouter": OpenRouterBackend,
}


def _has_origin_remote(repo_path: str) -> bool:
    result = subprocess.run(
        ["git", "-C", repo_path, "remote"],
        capture_output=True, text=True,
    )
    return "origin" in result.stdout.split()


def _has_open_pr(repo_path: str, branch: str) -> bool:
    result = subprocess.run(
        ["gh", "pr", "view", branch],
        cwd=repo_path, capture_output=True, text=True,
    )
    return result.returncode == 0


def _delegate_implementation_impl(
    task: str, branch: str, target_repo_path: str | None = None
) -> dict:
    cfg = config_module.load_config()
    repo_path = target_repo_path or cfg.get("target_repo_path")
    if not repo_path:
        return {"success": False, "error": "target_repo_path not provided and not set in config"}

    backend_name = cfg["backend"]
    backend_cls = BACKENDS.get(backend_name)
    if backend_cls is None:
        return {"success": False, "error": f"unknown backend: {backend_name}"}
    backend = backend_cls()

    attempt_models = [cfg["model"], *cfg.get("fallback_models", [])]
    attempt_errors = []

    for model in attempt_models:
        try:
            result = backend.run_backend(task, repo_path, branch, cfg, model=model)
        except NotImplementedError as e:
            return {"success": False, "error": str(e)}

        if result.success:
            note = None
            pr_url = None
            if _has_origin_remote(repo_path):
                push = subprocess.run(
                    ["git", "-C", repo_path, "push", "-u", "origin", branch],
                    capture_output=True, text=True,
                )
                if push.returncode != 0:
                    return {"success": False, "error": f"git push failed: {push.stderr.strip()}"}

                if cfg.get("open_pr") and not _has_open_pr(repo_path, branch):
                    pr = subprocess.run(
                        ["gh", "pr", "create", "--fill", "--head", branch,
                         "--base", cfg["pr_base_branch"]],
                        cwd=repo_path, capture_output=True, text=True,
                    )
                    if pr.returncode == 0:
                        pr_url = pr.stdout.strip()
            else:
                note = "no origin remote configured; commit created locally, nothing pushed"

            return {
                "success": True,
                "pr_url": pr_url,
                "branch": branch,
                "files_changed": result.files_changed,
                "model_used": model,
                "summary": f"Implemented via {backend_name} ({model})",
                **({"note": note} if note else {}),
            }

        attempt_errors.append(f"{model}: {result.error}")

    return {
        "success": False,
        "error": "all models failed — " + "; ".join(attempt_errors),
    }


def _configure_impl(
    backend: str | None = None,
    model: str | None = None,
    fallback_models: list[str] | None = None,
    max_fallback_models: int | None = None,
    stall_timeout_seconds: float | None = None,
    target_repo_path: str | None = None,
    branch_prefix: str | None = None,
    open_pr: bool | None = None,
    pr_base_branch: str | None = None,
    idle_notify_interval_seconds: float | None = None,
    extra_backend_args: list[str] | None = None,
) -> dict:
    all_overrides = {
        "backend": backend,
        "model": model,
        "fallback_models": fallback_models,
        "max_fallback_models": max_fallback_models,
        "stall_timeout_seconds": stall_timeout_seconds,
        "target_repo_path": target_repo_path,
        "branch_prefix": branch_prefix,
        "open_pr": open_pr,
        "pr_base_branch": pr_base_branch,
        "idle_notify_interval_seconds": idle_notify_interval_seconds,
        "extra_backend_args": extra_backend_args,
    }
    all_overrides = {k: v for k, v in all_overrides.items() if v is not None}

    try:
        return config_module.configure_with_validation(all_overrides)
    except config_module.ConfigValidationError as e:
        return {"success": False, "error": str(e)}


def _list_available_models_impl() -> dict:
    try:
        models = config_module.list_available_models_with_prefix()
    except Exception as e:
        return {"error": str(e)}
    return {"models": models}


@mcp.tool()
def delegate_implementation(
    task: str, branch: str, target_repo_path: str | None = None
) -> dict:
    return _delegate_implementation_impl(task, branch, target_repo_path)


@mcp.tool()
def configure(
    backend: str | None = None,
    model: str | None = None,
    fallback_models: list[str] | None = None,
    max_fallback_models: int | None = None,
    stall_timeout_seconds: float | None = None,
    target_repo_path: str | None = None,
    branch_prefix: str | None = None,
    open_pr: bool | None = None,
    pr_base_branch: str | None = None,
    idle_notify_interval_seconds: float | None = None,
    extra_backend_args: list[str] | None = None,
) -> dict:
    return _configure_impl(
        backend, model, fallback_models, max_fallback_models,
        stall_timeout_seconds, target_repo_path, branch_prefix,
        open_pr, pr_base_branch, idle_notify_interval_seconds,
        extra_backend_args,
    )


@mcp.tool()
def list_available_models() -> dict:
    return _list_available_models_impl()


if __name__ == "__main__":
    mcp.run()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd mcp-servers/local-coder && .venv/bin/python -m pytest tests/test_server.py -v`
Expected: all tests pass. If `test_delegate_implementation_pushes_when_remote_exists`
fails because the spy assertion doesn't match how `subprocess.run` args are
captured, adjust the assertion to check `spy.call_args_list` for any call
whose first positional arg is a list containing `"push"` — the point is
confirming push was attempted, not the exact call signature.

- [ ] **Step 5: Run the full test suite**

Run: `cd mcp-servers/local-coder && .venv/bin/python -m pytest -v`
Expected: all tests across all files pass (Tasks 1-6 combined — roughly
12 config + 4 ollama + 7 backends_common + 6 aider + 11 server = ~40 tests).

- [ ] **Step 6: Commit**

```bash
git add mcp-servers/local-coder/server.py mcp-servers/local-coder/tests/test_server.py
git commit -m "local-coder: add FastMCP server with delegate_implementation, configure, list_available_models"
```

---

### Task 6.5: Wire on_tick to real progress notifications (async conversion)

**Discovered during Task 7's review, not part of the original plan.** The
design spec requires `delegate_implementation` to emit an MCP progress
notification and a stderr log line roughly every
`idle_notify_interval_seconds` while a backend subprocess runs. Task 3
built `run_monitored_subprocess`'s `on_tick` callback hook correctly, but
no task ever actually constructed and passed a real `on_tick` — Task 4's
`AiderBackend.run_backend` calls `run_monitored_subprocess` without it,
and Task 6's `_delegate_implementation_impl` never receives or threads
through a FastMCP `Context`. The notification mechanism has been silently
inert this whole time. This task fixes that end-to-end.

**Files:**
- Modify: `mcp-servers/local-coder/backends/base.py`
- Modify: `mcp-servers/local-coder/backends/aider.py`
- Modify: `mcp-servers/local-coder/backends/codex.py`,
  `backends/gemini.py`, `backends/openrouter.py` (signature only, still stubs)
- Modify: `mcp-servers/local-coder/server.py`
- Modify: `mcp-servers/local-coder/tests/test_aider.py`,
  `tests/test_server.py`

**The design (confirmed against the actually-installed FastMCP 3.4.4 in
this venv before writing this task — do not assume, verify
`Context.report_progress` exists and is `async` via
`inspect.iscoroutinefunction`):**

`Context.report_progress(progress, total=None, message=None)` is `async`.
`run_monitored_subprocess`'s polling loop is synchronous by design (a
tight `selectors.select()` loop) — it must NOT become async itself, since
awaiting it directly from an async caller would block the event loop for
the entire subprocess duration. The correct pattern: run the
still-synchronous backend/monitoring call in a worker thread via
`anyio.to_thread.run_sync` (from the async `_delegate_implementation_impl`),
and have `on_tick` — invoked synchronously from within that worker thread
— hand off to the async `Context.report_progress()` via
`anyio.from_thread.run`. `anyio` is already a FastMCP dependency, no new
requirement needed.

**Interfaces:**
- `BackendAdapter.run_backend`'s abstract signature (base.py) gains an
  `on_tick: Callable[[], None] | None = None` parameter, forwarded by
  `AiderBackend` into its call to `common.run_monitored_subprocess`. The
  three stub backends' signatures are updated for consistency (they still
  just raise `NotImplementedError`, unaffected otherwise).
- `_delegate_implementation_impl` becomes `async def`, gains an optional
  `ctx: Context | None = None` parameter (FastMCP's `Context` type,
  imported from `fastmcp`).
- The `@mcp.tool()`-decorated `delegate_implementation` wrapper becomes
  `async def` and gains a `ctx: Context` parameter — FastMCP
  auto-injects a live `Context` into any tool function that declares this
  parameter type-annotated; it is not something the caller passes
  explicitly. Verify this injection behavior against the installed
  FastMCP version's docs/source before relying on it — if FastMCP 3.4.4's
  injection mechanism differs from what's assumed here, adapt accordingly
  and document the actual mechanism used.

- [ ] **Step 1: Write the failing tests**

Extend `tests/test_aider.py`: add a test confirming `AiderBackend.run_backend`
forwards an `on_tick` callable through to
`common.run_monitored_subprocess` (mock `run_monitored_subprocess` and
assert the `on_tick` kwarg it was called with is the same callable passed
into `run_backend`).

Extend `tests/test_server.py`: since `_delegate_implementation_impl` is
now `async def`, every existing test calling it must become `async def`
too and use `pytest.mark.asyncio` (add `pytest-asyncio` to
`requirements.txt` if not already present — check
`.venv/bin/pip show pytest-asyncio` first) or `anyio`'s pytest plugin
(check whether `pytest-anyio`/`anyio.pytest_plugin` is already available
via the existing `anyio` dependency before adding a new one — prefer
reusing what's already installed over adding another test dependency).
Add a new test confirming that when `_delegate_implementation_impl` is
called with a mock `ctx` object, the underlying backend call receives an
`on_tick` that, when invoked, calls `ctx.report_progress` (verify via the
mock) — this test should NOT require FastMCP's actual thread-bridging
machinery to work for real (mock `anyio.from_thread.run` or structure the
test to isolate what's being verified), it should verify the wiring, not
re-test anyio itself.

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd mcp-servers/local-coder && .venv/bin/python -m pytest tests/test_aider.py tests/test_server.py -v`
Expected: FAIL — `on_tick` parameter doesn't exist yet on `run_backend`,
`_delegate_implementation_impl` isn't async yet.

- [ ] **Step 3: Implement the fix**

In `backends/base.py`: add `on_tick: Callable[[], None] | None = None` to
the abstract `run_backend` signature (import `Callable` from `typing` if
not already imported).

In `backends/aider.py`: accept `on_tick` in `run_backend`'s signature,
forward it to the `common.run_monitored_subprocess(...)` call.

In `backends/codex.py`, `backends/gemini.py`, `backends/openrouter.py`:
update each stub's `run_backend` signature to match (still raises
`NotImplementedError` immediately, signature consistency only).

In `server.py`:
1. Import `Context` from `fastmcp`.
2. Convert `_delegate_implementation_impl` to `async def`, add
   `ctx: Context | None = None` parameter.
3. Inside the per-model attempt loop, before calling
   `backend.run_backend(...)`, construct the tick callback:
   ```python
   def make_on_tick(model_name: str):
       def on_tick():
           print(f"[local-coder] still running ({model_name})...", file=sys.stderr, flush=True)
           if ctx is not None:
               anyio.from_thread.run(ctx.report_progress, 0, None, f"Running {model_name}...")
       return on_tick
   ```
   (adjust the exact `report_progress` arguments to whatever's
   semantically sensible — this is a long-running indeterminate task, so
   `progress`/`total` may not have meaningful numeric values; a `message`-only
   progress ping is reasonable, but confirm this against
   `Context.report_progress`'s actual parameter meanings, don't guess
   blindly).
4. Wrap the call to `backend.run_backend(...)` (which is still a
   synchronous method) in `anyio.to_thread.run_sync`, passing the
   `on_tick` callback through: something like
   `result = await anyio.to_thread.run_sync(lambda: backend.run_backend(task, repo_path, branch, cfg, model=model, on_tick=make_on_tick(model)))`.
   Confirm `anyio.to_thread.run_sync` correctly propagates exceptions
   raised inside the thread (it should, verify with a quick manual check
   or by reading anyio's docs/source) — the existing `NotImplementedError`-catching
   logic and the failover loop's error handling must keep working
   identically to before this change.
5. Convert the `@mcp.tool()`-decorated `delegate_implementation` wrapper
   to `async def`, add the `ctx: Context` parameter, `await` the call to
   `_delegate_implementation_impl`.
6. `import sys` at the top of `server.py` if not already imported (needed
   for the stderr print in `on_tick`).

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd mcp-servers/local-coder && .venv/bin/python -m pytest tests/test_aider.py tests/test_server.py -v`
Expected: all pass.

- [ ] **Step 5: Run the full suite**

Run: `cd mcp-servers/local-coder && .venv/bin/python -m pytest -v`
Expected: every test across every file still passes — confirm the total
count and that nothing in `test_backends_common.py`, `test_config.py`,
`test_ollama.py` regressed (they shouldn't be touched by this change at
all, since `run_monitored_subprocess` itself is unchanged — only its
callers now pass a real `on_tick`).

- [ ] **Step 6: Manually verify the server still starts cleanly**

Run the same server-start smoke check Task 7 used (background + kill
after a couple seconds, confirm the FastMCP startup banner appears with
no traceback) — the `async def` conversion must not break FastMCP's
ability to register/serve the tool.

- [ ] **Step 7: Commit**

```bash
git add mcp-servers/local-coder/backends/base.py \
        mcp-servers/local-coder/backends/aider.py \
        mcp-servers/local-coder/backends/codex.py \
        mcp-servers/local-coder/backends/gemini.py \
        mcp-servers/local-coder/backends/openrouter.py \
        mcp-servers/local-coder/server.py \
        mcp-servers/local-coder/tests/test_aider.py \
        mcp-servers/local-coder/tests/test_server.py \
        mcp-servers/local-coder/requirements.txt
git commit -m "local-coder: wire on_tick to real MCP progress notifications + stderr logging"
```

(Only include `requirements.txt` in the commit if a new test-only
dependency was actually needed — omit it from the `git add` list
otherwise.)

---

### Task 7: MCP registration, README, plugin wiring

**Files:**
- Create: `.mcp.json`
- Create: `mcp-servers/local-coder/README.md`

**Interfaces:** none — this task wires up discovery/documentation, no new
code interfaces.

- [ ] **Step 1: Create `.mcp.json`**

Create `.mcp.json` at the repo root:
```json
{
  "mcpServers": {
    "local-coder": {
      "type": "stdio",
      "command": "${CLAUDE_PLUGIN_ROOT}/mcp-servers/local-coder/.venv/bin/python",
      "args": ["${CLAUDE_PLUGIN_ROOT}/mcp-servers/local-coder/server.py"]
    }
  }
}
```

Note: pointing `command` directly at the venv's own `python` binary (not a
bare `python3`) avoids the ambiguity flagged in the spec about which
interpreter resolves on `PATH` — this makes the registration
self-contained and correct regardless of the user's shell environment.
`server.py` gets its own directory added to `sys.path[0]` automatically by
Python when launched this way, so the flat imports work identically to how
they work under pytest via `conftest.py`.

- [ ] **Step 2: Write README.md**

Create `mcp-servers/local-coder/README.md`:
```markdown
# local-coder MCP server

Delegates implementation work to a local or remote coding-agent backend
(Aider + Ollama today; Codex and Gemini CLI designed but not yet
implemented — see `docs/superpowers/specs/2026-07-21-local-coder-delegation-design.md`)
instead of Claude Code's own Edit/Write tools.

## Prerequisites

- `aider` installed and on `PATH` (`pip install aider-chat` or `pipx install aider-chat`).
- [Ollama](https://ollama.com) running locally, with the configured model
  pulled (default: `qwen3-coder:30b` — `ollama pull qwen3-coder:30b`), and
  every model listed in `fallback_models` (if any) also pulled.
- `gh` CLI installed and authenticated (`gh auth status`).
- This server's own dependencies installed into its venv:
  ```bash
  cd mcp-servers/local-coder
  python3.13 -m venv .venv
  .venv/bin/pip install -r requirements.txt
  ```
  Use a Python 3.10+ interpreter explicitly (e.g. `python3.13`), not a bare `python3` — `fastmcp` requires >=3.10 and an older system `python3` will fail the install silently.

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
```

- [ ] **Step 3: Manually verify the server starts**

Run:
```bash
cd mcp-servers/local-coder && timeout 3 .venv/bin/python server.py; echo "exit code: $?"
```
Expected: the process runs until the 3-second timeout kills it (FastMCP
servers block on stdio waiting for a client) — an exit code of `124`
(timeout's own "I killed it" code) means the server started cleanly and
was still running when killed, which is success. An immediate crash with a
Python traceback and a different exit code means something is broken —
investigate before proceeding (most likely: `fastmcp` not installed in
this venv, or an import error from a typo in one of the flat imports
above).

- [ ] **Step 4: Commit**

```bash
git add .mcp.json mcp-servers/local-coder/README.md
git commit -m "local-coder: add MCP registration, README, verify server starts"
```

---

### Task 8: `agents/local-coder-implementer.md` + rewire `subagent-driven-development`

**Files:**
- Create: `agents/local-coder-implementer.md`
- Modify: `skills/subagent-driven-development/implementer-prompt.md`
- Modify: `skills/subagent-driven-development/SKILL.md`

**Interfaces:** none new — this task changes prompt/skill content, not code.

- [ ] **Step 1: Create the restricted subagent definition**

Create `agents/local-coder-implementer.md`:
```markdown
---
name: local-coder-implementer
description: Delegates implementation of a single subagent-driven-development task to the local-coder MCP server, then verifies the result. Does not edit files directly — Edit and Write are excluded from its toolset.
tools: Read, Grep, Glob, Bash, mcp__local-coder__delegate_implementation
---

You implement one task from a Superpowers implementation plan by
delegating the actual code-writing to the local-coder MCP server, then
verifying the result — you do not write or edit code yourself.

You do not have Edit or Write tools. This is intentional: your job is to
call `mcp__local-coder__delegate_implementation` with the task description
and branch you're given, wait for it to complete, then use Read, Grep,
Glob, and Bash to confirm the change satisfies the task brief.

**Bash usage:** restrict yourself to read-only and inspection commands —
`git log`, `git diff`, `git show`, `git status`, `ls`, `cat`-equivalents
via Read instead, and commands that run a project's existing test suite to
verify the delegated change (e.g. `pytest`, `npm test`) if the task brief
calls for tests to pass. Do not use Bash to edit files, write new files, or
perform any git operation that changes repository state (no `git commit`,
`git add`, `git push`, `git checkout -b`, etc.) — local-coder already
performed those as part of `delegate_implementation`.

If `delegate_implementation` returns `success: false`, do not attempt to
fix the problem yourself by editing files — you cannot, and it isn't your
job. Report back with status BLOCKED, including the full error from
local-coder's response, so the controller can decide whether to retry with
different context, a different model (via reconfiguring local-coder), or
escalate.
```

- [ ] **Step 2: Add fork-divergence marker and rewrite implementer-prompt.md**

Read the current file first: `skills/subagent-driven-development/implementer-prompt.md`
(139 lines as of this plan being written). Add this HTML comment as the
very first line of the file, before the existing `# Implementer Subagent
Prompt Template` heading:

```html
<!--
FORK DIVERGENCE from upstream obra/superpowers:
This file was modified to delegate implementation to the local-coder MCP
server instead of using Edit/Write directly. See
docs/superpowers/specs/2026-07-21-local-coder-delegation-design.md.
Reconcile carefully on upstream merges.
-->
```

Then make these two targeted edits to the existing template body (do not
rewrite the whole file — the report format, self-review checklist, and
escalation guidance in the rest of the template are backend-agnostic and
stay as-is):

**Edit 2a** — change the dispatch header. Find:
```
Subagent (general-purpose):
  description: "Implement Task N: [task name]"
```
Replace with:
```
Subagent (local-coder-implementer):
  description: "Implement Task N: [task name]"
```

**Edit 2b** — replace the "Your Job" section. Find:
```
## Your Job

Once you're clear on requirements:
1. Implement exactly what the task specifies
2. Write tests (following TDD if task says to)
3. Verify implementation works
4. Commit your work
5. Self-review (see below)
6. Report back

Work from: [directory]

**While you work:** If you encounter something unexpected or unclear, **ask questions**.
It's always OK to pause and clarify. Don't guess or make assumptions.

While iterating, run the focused test for what you're changing; run the
full suite once before committing, not after every edit.
```

Replace with:
```
## Your Job

Once you're clear on requirements:
1. Call `mcp__local-coder__delegate_implementation` with:
   - `task`: the task brief's requirements, written as a clear
     implementation instruction (not just pasted verbatim — synthesize the
     brief's acceptance criteria into a task description local-coder's
     backend can act on, including any TDD requirement from the brief)
   - `branch`: [current working branch — filled in by the controller,
     same as the directory below]
   - `target_repo_path`: [directory]
2. Wait for the result. If `success: false`, do not attempt to fix it
   yourself — report BLOCKED with the full error (see "When You're in
   Over Your Head" below).
3. If `success: true`, use Read/Grep/Glob and read-only Bash (see your own
   agent definition for what's permitted) to verify the changed files
   (`files_changed` in the response) actually satisfy the task brief. If
   the brief calls for tests, run them via Bash to confirm they pass —
   you do not write new tests yourself, but you must confirm existing
   or delegated-in tests actually run and pass.
4. Report back (see Report Format below).

Work from: [directory]

**While you work:** If you encounter something unexpected or unclear before
calling `delegate_implementation`, **ask questions**. It's always OK to
pause and clarify. Don't guess or make assumptions about what the task
means before delegating it.
```

- [ ] **Step 3: Verify the edits**

Run: `grep -n "general-purpose\|Implement exactly what the task specifies" skills/subagent-driven-development/implementer-prompt.md`
Expected: no matches (both replaced). Then:
Run: `grep -n "local-coder-implementer\|delegate_implementation" skills/subagent-driven-development/implementer-prompt.md`
Expected: multiple matches confirming the new content landed.

- [ ] **Step 4: Add fork-divergence marker and update SKILL.md prose**

Read the current file: `skills/subagent-driven-development/SKILL.md`
(418 lines as of this plan). Add the same fork-divergence HTML comment as
the first line, before the existing `# Subagent-Driven Development`
heading (identical comment block to Step 2 above).

Then make these targeted edits — do not rewrite the whole file:

**Edit 4a** — the process diagram. First run:
```bash
grep -n "Implementer subagent implements, tests, commits, self-reviews" skills/subagent-driven-development/SKILL.md
```
to confirm the exact line numbers before editing (expected: 3 occurrences
— one node definition, two edge-definition references by name). Replace
every occurrence of the exact string:
```
"Implementer subagent implements, tests, commits, self-reviews"
```
with:
```
"Implementer subagent delegates to local-coder, verifies result"
```
All 3 occurrences must use the identical replacement text so the dot graph
stays internally consistent — a dangling reference to the old node name
(if only some occurrences were replaced) would break the diagram.

**Edit 4b** — the "Advantages" section currently says (under "vs. Manual
execution"): `"Subagents follow TDD naturally"`. This claim assumes the
subagent itself writes tests, which is no longer true — TDD now happens
inside local-coder's backend (e.g. if the task brief asks aider to follow
TDD, that instruction is inside the `task` string passed to
`delegate_implementation`, and enforcement is the backend's, not this
subagent's). Find this line with:
```bash
grep -n "Subagents follow TDD naturally" skills/subagent-driven-development/SKILL.md
```
Replace it with:
```
Task delegated to local-coder; TDD requirements pass through in the task description
```

Leave everything else in `SKILL.md` unchanged — the reviewer flow,
model-selection guidance (Task complexity signals still apply to which
model `local-coder` is configured with, even though the mechanism of
"choosing a model" moved to `configure` rather than the dispatch prompt's
`model:` field), ledger mechanism, and file-handoff conventions are all
backend-agnostic and correct as written.

- [ ] **Step 5: Verify no broken diagram references remain**

Run: `grep -n "Implementer subagent implements, tests, commits, self-reviews" skills/subagent-driven-development/SKILL.md`
Expected: no matches (all 3 replaced consistently).

- [ ] **Step 6: Commit**

```bash
git add agents/local-coder-implementer.md \
        skills/subagent-driven-development/implementer-prompt.md \
        skills/subagent-driven-development/SKILL.md
git commit -m "subagent-driven-development: delegate implementation to local-coder MCP server"
```

---

### Task 9: Full test suite run + plan self-review checkpoint

**Files:** none created/modified — verification only.

- [ ] **Step 1: Run the complete local-coder test suite**

Run: `cd mcp-servers/local-coder && .venv/bin/python -m pytest -v`
Expected: all tests from Tasks 1–6 pass (~40 tests total across config,
ollama, backends/common, aider, server).

- [ ] **Step 2: Confirm no stray references to the old dispatch pattern remain**

Run: `grep -n "subagent_type.*general-purpose\|Subagent (general-purpose)" skills/subagent-driven-development/implementer-prompt.md`
Expected: no matches (the task-reviewer prompt and other files in this
directory are untouched by this plan and may still legitimately reference
`general-purpose` for the *reviewer* dispatch, which this plan does not
change — only the *implementer* dispatch changes; this check is scoped to
`implementer-prompt.md` specifically for that reason).

- [ ] **Step 3: Confirm the fork-divergence markers landed**

Run: `head -6 skills/subagent-driven-development/implementer-prompt.md skills/subagent-driven-development/SKILL.md`
Expected: both files start with the `FORK DIVERGENCE` HTML comment block.

- [ ] **Step 4: No commit needed — this task is verification only**

---

## What This Plan Does NOT Cover (by design — see spec's phasing)

- Implementing `CodexBackend`, `GeminiBackend`, or `OpenRouterBackend` for
  real — they remain `NotImplementedError` stubs after this plan.
- Part 3 of the spec (the manual smoke test: brainstorm → plan → delegated
  execution → review → PR, end-to-end against a real Ollama-backed aider
  run) — this happens *after* this plan's tasks are complete and reviewed,
  as a separate manual verification step, not as a plan task, since it
  requires a live multi-skill Claude Code session rather than something a
  single subagent can execute in isolation.
