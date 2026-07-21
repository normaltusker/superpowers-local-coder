# Local-Coder Delegation — Design Spec

**Status:** Proposed
**Objective:** replace Claude Code's own Edit/Write/Bash-based implementation
step in `subagent-driven-development` with delegation to a local/remote model
(initially Aider + Ollama), while leaving brainstorming, planning, and review
untouched.

## Scope

Two independently shippable pieces:

1. `mcp-servers/local-coder/` — a new, source-controlled FastMCP server that
   wraps pluggable coding-agent backends (Aider now; Codex/Gemini/OpenRouter
   stubbed) behind two MCP tools.
2. A modification to `skills/subagent-driven-development/` so its per-task
   implementer subagent calls local-coder instead of editing files itself,
   with the subagent's tool access structurally restricted at the harness
   level (a new `.claude/agents/local-coder-implementer.md` definition), not
   just instructed via prompt.

Out of scope: `brainstorming`, `writing-plans`, `requesting-code-review`,
`executing-plans` (the parallel-session alternative flow), and the review
subagent — none of these change.

## Part 1 — `mcp-servers/local-coder/`

### Layout

```
mcp-servers/local-coder/
  server.py            # FastMCP app; delegate_implementation, configure
  config.py             # load/save config.yaml, partial-merge logic
  config.yaml            # default config (checked in)
  backends/
    base.py              # BackendAdapter ABC + CompletionResult dataclass
    aider.py              # real adapter
    codex.py               # stub, NotImplementedError
    gemini.py               # stub, NotImplementedError
    openrouter.py            # stub, NotImplementedError
  README.md
```

### config.yaml

```yaml
backend: "aider"
model: "ollama/qwen3-coder:30b"
target_repo_path: null     # no hardcoded default — see "Repo path resolution" below
branch_prefix: "local-coder/"
open_pr: false              # see "PR ownership" below for why this defaults false
pr_base_branch: "main"
idle_notify_interval_seconds: 20
extra_backend_args: []
```

### Backend adapter interface

```python
@dataclass
class CompletionResult:
    success: bool
    files_changed: list[str]
    commit_sha: str | None
    error: str | None = None

class BackendAdapter(ABC):
    @abstractmethod
    def run_backend(self, task: str, repo_path: str, branch: str, config: dict) -> CompletionResult: ...
```

`AiderBackend.run_backend`:
1. `git -C repo_path rev-parse --verify branch` to check if the branch
   exists; `git checkout branch` if so, else `git checkout -b branch`.
2. Record `pre_head = git rev-parse HEAD`.
3. Run `aider --model {model} --yes --message "{task}" {*extra_backend_args}`
   as a subprocess in `repo_path`, relying on aider's own auto-commit.
4. While the subprocess runs, a background thread fires every
   `idle_notify_interval_seconds`: calls FastMCP's `ctx.report_progress()`
   (best-effort — harmless no-op if the client isn't tracking a progress
   token) **and** writes a line to stderr, so there's always a visible trace
   in Claude Code's MCP logs even if progress notifications aren't surfaced.
5. On subprocess exit: `post_head = git rev-parse HEAD`. If unchanged →
   `success=False`, `error="aider made no commits"`. Otherwise
   `files_changed = git diff --name-only pre_head post_head`,
   `commit_sha = post_head`, `success=True`.
6. Non-zero aider exit code → `success=False`, `error` = captured stderr tail.

`CodexBackend`, `GeminiBackend`, `OpenRouterBackend`: same `run_backend`
signature, body raises `NotImplementedError("<name> backend not yet implemented")`.

### MCP tools

**`delegate_implementation(task: str, branch: str, target_repo_path: str | None = None) -> dict`**

1. Resolve repo path: argument if given, else `config["target_repo_path"]`,
   else raise a clear error (no silent cwd guessing — see "Repo path
   resolution").
2. Load full config, instantiate the configured backend adapter.
3. Call `run_backend(task, repo_path, branch, config)`.
4. On success: `git push -u origin branch`. If `config["open_pr"]` is true
   AND `gh pr view branch` finds no existing open PR, run
   `gh pr create --fill --head branch --base {pr_base_branch}` and capture
   its URL; otherwise `pr_url` is `null`.
5. Return `{"pr_url": ..., "branch": ..., "files_changed": [...], "summary": "..."}`
   on success. On any failure (backend error, push failure), return
   `{"success": false, "error": "..."}` with no push/PR attempted.

**`configure(backend=None, model=None, target_repo_path=None, open_pr=None, **overrides) -> dict`**

Merges only the provided keys into `config.yaml` (untouched keys keep their
current value), writes it back, returns the full resulting config so the
caller can confirm what changed.

### Repo path resolution

`target_repo_path` is **not** auto-detected inside the server — it has no
visibility into which project the calling Claude Code session is working on.
This mirrors how `subagent-driven-development` already works today: the
*controller* session (not the subagent) resolves the working directory via
`superpowers:using-git-worktrees` (`git rev-parse --show-toplevel` /
`--git-common-dir`) and fills it into the dispatch prompt's `Work from:
[directory]` line as plain text. Under this design, the controller passes
that same resolved path as the `target_repo_path` argument on every
`delegate_implementation` call. `config.yaml`'s `target_repo_path: null` stays
the documented fallback for standalone/manual use of the tool outside the
subagent-driven-development flow.

### PR ownership

`subagent-driven-development` reuses **one branch across all tasks in a
plan** (see "Branch granularity" below) and already ends the whole plan with
`superpowers:finishing-a-development-branch`, which itself offers to open the
PR. If `delegate_implementation` also opened a PR after every task, the
first task's call would create it and later calls would try to recreate or
redundantly re-touch it, racing with the final skill's own PR step. To avoid
this:

- `config.yaml` ships with `open_pr: false` by default.
- `delegate_implementation` always pushes the branch on success regardless
  of `open_pr`.
- `open_pr: true` remains fully supported for standalone/non-SDD use of the
  tool (e.g. someone invoking local-coder directly outside a Superpowers
  plan) — the guard against duplicate PRs (`gh pr view` check) covers that
  case too.
- The SDD integration in Part 2 does not change this default; it relies on
  it.

### Branch granularity

One branch is created for the whole plan (as today, via
`using-git-worktrees` / the existing SDD flow) and reused across every
task's `delegate_implementation` call — the tool checks out the existing
branch rather than creating a new one once it exists. This matches today's
SDD behavior of one feature branch with sequential task commits and a single
final PR, and avoids diverging SDD's completion story
(`finishing-a-development-branch` still governs the single, final PR).

### Idle timeout note

MCP stdio tool calls have a default idle timeout in Claude Code
(`CLAUDE_CODE_MCP_TOOL_IDLE_TIMEOUT`, default ~30 min). Long aider runs on
larger tasks can exceed this even with progress notifications firing,
because the *notification* doesn't reset a client-side timeout that isn't
listening for it. The README will tell users running long tasks to raise or
disable this env var; the periodic progress/stderr ping is a best-effort
mitigation, not a guarantee.

### README contents

- Prerequisites: `aider` on PATH, the configured backend's runtime running
  (Ollama + the configured model pulled, for the default config), `gh` CLI
  authenticated (`gh auth status`).
- How to reconfigure: use the `configure` MCP tool (example calls), not
  manual YAML edits.
- `CLAUDE_CODE_MCP_TOOL_IDLE_TIMEOUT` note as above.

## Part 2 — Rewiring `subagent-driven-development`

### New file: `.claude/agents/local-coder-implementer.md`

A Claude Code subagent definition (this fork currently has no
`.claude/agents/` directory — this is new) with YAML frontmatter:

```yaml
---
name: local-coder-implementer
description: Delegates implementation of a single SDD task to local-coder; verifies the result. Does not edit files directly.
tools: Read, Grep, Glob, Bash, mcp__local-coder__delegate_implementation
---
```

This is a structural restriction enforced by the harness: this subagent
literally does not have Edit/Write in its toolset, so it cannot implement
directly even if it wanted to. `Bash` cannot be scoped to a read-only
command allowlist purely via frontmatter in this harness, so the agent body
text will explicitly instruct read-only/inspection use only (`git log`,
`git diff`, `git status`, `git show`, `ls`, test-running commands to verify,
etc.) — this one piece remains an instruction, not a hard gate, and the
design doc calls that out rather than overclaiming enforcement.

### Modified: `skills/subagent-driven-development/implementer-prompt.md`

- Dispatch changes from `subagent_type: general-purpose` to
  `subagent_type: local-coder-implementer`.
- "Your Job" section rewritten: instead of "1. Implement exactly what the
  task specifies... 2. Write tests...", the subagent's job becomes: call
  `mcp__local-coder__delegate_implementation` with the task brief's
  description and the current working branch (both filled in by the
  controller, same as today's `[directory]`/brief-path placeholders), wait
  for the result, then use Read/Grep/Glob and read-only Bash to verify the
  changed files satisfy the brief (and that any tests the brief calls for
  pass, run via Bash — running tests is read-only even though it's not
  strictly git-inspection, so the agent's Bash instructions permit
  test-execution commands specifically alongside git-inspection ones).
- Report format is unchanged (DONE/DONE_WITH_CONCERNS/BLOCKED/NEEDS_CONTEXT,
  commits, test summary, concerns, report file path) — from the controller's
  perspective the contract is identical; only *how* the work happens changed.
- A fork-divergence header comment added at the top of the file.

### Modified: `skills/subagent-driven-development/SKILL.md`

- Wherever prose or diagram nodes currently describe the implementer as
  editing/testing/committing directly (e.g. "Implementer subagent
  implements, tests, commits, self-reviews"), update to reflect that it
  delegates via local-coder and then verifies.
- Add the fork-divergence header comment.
- No change to the reviewer flow, model-selection guidance, ledger
  mechanism, or file-handoff conventions — those are backend-agnostic.

### Fork-divergence marker

Every modified file gets, at the top:

```
<!--
FORK DIVERGENCE from upstream obra/superpowers:
This file was modified to delegate implementation to the local-coder MCP
server instead of using Edit/Write directly. See
docs/superpowers/specs/2026-07-21-local-coder-delegation-design.md.
Reconcile carefully on upstream merges.
-->
```

(For `.md` files this is an HTML comment, invisible in rendered form but
visible in source — consistent with how the rest of the skill files are
plain markdown with no other front-matter convention for this kind of note.)

## Part 3 — Smoke test (manual, after both parts ship)

Run, end-to-end, in this repo (chosen since it's the fork itself and already
has `README.md` to edit) or the confirmed-ready target repo:

1. Brainstorm a trivial task: "add a one-line comment to README.md".
2. `writing-plans` produces a one-task plan.
3. `subagent-driven-development` dispatches the (now-restricted)
   `local-coder-implementer` subagent for that task.
4. Verify: the subagent calls `delegate_implementation`, not Edit/Write; the
   MCP server invokes aider against the local Ollama model; a commit lands
   on the shared branch; the subagent verifies via Read and reports DONE.
5. Task reviewer reviews the diff as normal (no change to review skill).
6. `finishing-a-development-branch` opens the PR (local-coder itself does
   not, per the PR-ownership decision above).
7. Confirm the PR exists, contains exactly the one-line change, and the full
   chain (brainstorm → plan → delegated execution → review → PR) worked
   without any direct Edit/Write from the controller or implementer.

Environment confirmed ready for this: `gh` CLI authenticated, Ollama running
with `qwen3-coder:30b` pulled, `aider` installed on PATH.

## Testing approach

- `mcp-servers/local-coder/` gets unit tests for: config partial-merge
  (`configure`), the Aider adapter's branch-create-vs-checkout logic and
  success/failure detection (mocking the subprocess call), and the
  `delegate_implementation` PR-guard logic (`open_pr` off, `open_pr` on with
  no existing PR, `open_pr` on with an existing PR) — using Python's
  `unittest`/`pytest` with subprocess and `gh`/`git` calls mocked, not live.
- No new tests needed for the skill-file changes themselves (they're prompt
  content); Part 3's manual smoke test is the acceptance check for the
  rewired flow.

## Error handling

- Missing `target_repo_path` (neither arg nor config) → `delegate_implementation`
  returns a clear error, no subprocess run.
- Aider produces no new commit → treated as failure, no push, no PR attempt.
- `git push` failure (e.g. remote rejected) → returned as failure; the
  commit still exists locally, so the controller can inspect and retry
  manually rather than losing work.
- Codex/Gemini/OpenRouter selected via `configure` before they're
  implemented → `delegate_implementation` surfaces the adapter's
  `NotImplementedError` message as a clean `error` field, not a stack trace.
