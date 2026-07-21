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
fallback_models: []         # tried in order on stall/error before giving up — see "Failover" below
stall_timeout_seconds: 300  # no subprocess output for this long = stalled, kill and try next model
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
    def run_backend(self, task: str, repo_path: str, branch: str, config: dict, model: str | None = None) -> CompletionResult: ...
```

`model` defaults to `config["model"]` when not passed (see "Model selection"
below) — every backend, including the stubs, shares this signature.

`AiderBackend.run_backend`:
1. `git -C repo_path rev-parse --verify branch` to check if the branch
   exists; `git checkout branch` if so, else `git checkout -b branch`.
2. Record `pre_head = git rev-parse HEAD`.
3. Run `aider --model {model} --yes --message "{task}" {*extra_backend_args}`
   as a subprocess in `repo_path`, relying on aider's own auto-commit.
4. While the subprocess runs, a background thread does two independent
   things on every `idle_notify_interval_seconds` tick: (a) calls FastMCP's
   `ctx.report_progress()` (best-effort — harmless no-op if the client isn't
   tracking a progress token) and writes a line to stderr, so there's always
   a visible trace in Claude Code's MCP logs even if progress notifications
   aren't surfaced; (b) checks whether the subprocess has produced any new
   stdout/stderr output since the last tick — if not, increments a
   no-activity counter, and once that counter's elapsed time exceeds
   `stall_timeout_seconds`, kills the subprocess and raises a stall
   condition (see "Failover" below) rather than continuing to wait.
5. On subprocess exit: `post_head = git rev-parse HEAD`. If unchanged →
   `success=False`, `error="aider made no commits"`. Otherwise
   `files_changed = git diff --name-only pre_head post_head`,
   `commit_sha = post_head`, `success=True`.
6. Non-zero aider exit code → `success=False`, `error` = captured stderr tail.
7. Killed for stalling → `success=False`, `error="stalled: no output for
   {stall_timeout_seconds}s"`.

`CodexBackend`, `GeminiBackend`, `OpenRouterBackend`: same `run_backend`
signature, body raises `NotImplementedError("<name> backend not yet implemented")`.

### Model selection and failover

**Per-call override:** `delegate_implementation` accepts an optional `model`
argument. When provided, it is a plain string you (via Claude Code) supply
directly in the tool call — there is no discovery or auto-detection, exactly
like `target_repo_path` today. It overrides `config["model"]` for that call
only; `config.yaml` is not modified. Omit it and the call uses whatever
`configure` last set as the persistent default.

**Failover shortlist:** `config.yaml`'s `fallback_models` is an explicitly
curated, ordered list (empty by default — failover is opt-in), maintained via
`configure(fallback_models=[...])`, e.g.:

```yaml
fallback_models:
  - "ollama/qwen2.5-coder:14b"
  - "ollama/deepseek-coder-v2:16b"
```

This is a fixed list you control — not auto-discovered via `ollama list` —
so an unattended run can never pick up a model you haven't vetted for this
purpose (a huge/slow model auto-selected as a "fallback" could otherwise
hang for a very long time).

**Failover sequence in `delegate_implementation`:** build the attempt order
as `[explicit model-arg or config["model"]] + config["fallback_models"]`.
Try each in order via `run_backend`. An attempt fails over to the next
model when `run_backend` returns `success=False` for **either** reason:
a stall (killed after `stall_timeout_seconds` of no output, per step 4/7
above) or a hard backend error (non-zero exit, subprocess launch failure).
Before each retry, the branch/repo state is left exactly as `run_backend`
left it (no commit on a failed/stalled attempt, since aider only commits on
its own success) — so each retry starts from the same `pre_head`, no
special rollback needed. If every model in the attempt order fails,
`delegate_implementation` returns `success=False` with an `error` that lists
each model tried and why it failed (stalled vs. error), so the caller isn't
left guessing which of several models was the problem. The returned `dict`
also includes `model_used` on success, so the caller knows which model in
the list actually completed the task.

### MCP tools

**`delegate_implementation(task: str, branch: str, target_repo_path: str | None = None, model: str | None = None) -> dict`**

1. Resolve repo path: argument if given, else `config["target_repo_path"]`,
   else raise a clear error (no silent cwd guessing — see "Repo path
   resolution").
2. Load full config, instantiate the configured backend adapter.
3. Build the model attempt order (`model` arg or `config["model"]`, then
   `config["fallback_models"]` in order) and try each via `run_backend`
   until one succeeds or all fail (see "Model selection and failover").
4. On success: `git push -u origin branch`. If `config["open_pr"]` is true
   AND `gh pr view branch` finds no existing open PR, run
   `gh pr create --fill --head branch --base {pr_base_branch}` and capture
   its URL; otherwise `pr_url` is `null`.
5. Return `{"pr_url": ..., "branch": ..., "files_changed": [...], "model_used": ..., "summary": "..."}`
   on success. On total failure (every model in the attempt order failed),
   return `{"success": false, "error": "..."}` (error lists each model tried
   and why) with no push/PR attempted.

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
  (Ollama + the configured model, and every model listed in
  `fallback_models`, pulled — for the default config), `gh` CLI
  authenticated (`gh auth status`).
- How to reconfigure: use the `configure` MCP tool (example calls), not
  manual YAML edits. Includes an example of setting `fallback_models`.
- How to override the model for a single call without touching config: pass
  `model=` directly to `delegate_implementation`.
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

This smoke test exercises the single-model path with `fallback_models`
empty (the default) — it does not exercise failover. Failover is covered by
the unit tests in "Testing approach"; a manual failover drill (e.g.
temporarily configuring an unpulled model as primary to force a fallback) is
optional and left to you to run ad hoc if you want to see it live.

## Testing approach

- `mcp-servers/local-coder/` gets unit tests for: config partial-merge
  (`configure`, including `fallback_models`), the Aider adapter's
  branch-create-vs-checkout logic and success/failure detection (mocking the
  subprocess call), the stall-detection timer (mocking elapsed time and
  subprocess output activity, not real sleeps), the failover sequence in
  `delegate_implementation` (first model stalls → second model succeeds;
  all models fail → aggregated error naming each), and the
  `delegate_implementation` PR-guard logic (`open_pr` off, `open_pr` on with
  no existing PR, `open_pr` on with an existing PR) — using Python's
  `unittest`/`pytest` with subprocess, time, and `gh`/`git` calls mocked,
  not live.
- No new tests needed for the skill-file changes themselves (they're prompt
  content); Part 3's manual smoke test is the acceptance check for the
  rewired flow.

## Error handling

- Missing `target_repo_path` (neither arg nor config) → `delegate_implementation`
  returns a clear error, no subprocess run.
- Aider produces no new commit → treated as failure, no push, no PR attempt,
  and (per the failover sequence) the next model in the attempt order is
  tried before the call gives up.
- A model stalls (no subprocess output for `stall_timeout_seconds`) → the
  subprocess is killed, treated as a failed attempt, next model tried.
- `fallback_models` is empty (the default) → no failover occurs; a single
  failed/stalled attempt on the primary model fails the call immediately,
  same as today's single-model behavior.
- `git push` failure (e.g. remote rejected) → returned as failure; the
  commit still exists locally, so the controller can inspect and retry
  manually rather than losing work.
- Codex/Gemini/OpenRouter selected via `configure` before they're
  implemented → `delegate_implementation` surfaces the adapter's
  `NotImplementedError` message as a clean `error` field, not a stack trace.
