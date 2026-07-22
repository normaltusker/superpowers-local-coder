# Local-Coder Delegation — Session Handoff

**Purpose of this file:** if this session ends mid-work, read this first —
it should make resumption fast without re-deriving context from the whole
conversation. **Update this file whenever a task completes**, not just at
session end — a stale handoff is worse than none.

## Where things stand

**Phase:** Design + plan are done and merged. **Currently executing the
implementation plan via `superpowers:subagent-driven-development`,
task-by-task.** Check the progress ledger (see below) for exactly which
tasks are done — trust it and `git log` over this prose if they conflict.
**ALL 9 PLAN TASKS + THE TASK 6.5 ADDENDUM ARE COMPLETE AND REVIEWED
CLEAN.** Task 9 (verification-only, no code) also done — 43/43 full
suite passing, no stray `general-purpose` references, fork-divergence
markers confirmed in both rewired skill files. **Currently: the final
whole-branch code review is dispatched (on the most capable model), per
`subagent-driven-development`'s process — this is the last gate before
`finishing-a-development-branch`.** If resuming and this review hasn't
completed yet, check for its result before doing anything else; if it
already completed with findings, those need a fix round + re-review
before merge. If it came back clean, the next and final step is inviting
`superpowers:finishing-a-development-branch` to decide how this branch
gets proposed for merge (a new PR from `local-coder-impl` into `dev`, most
likely — no PR exists for this branch yet).

**Plan file note:** `docs/superpowers/plans/2026-07-21-local-coder-phase1.md`
now has a "Task 6.5" section inserted between Task 6 and Task 7 — this
was NOT in the original plan, it's an addendum added mid-execution after
Task 7's review caught the gap. If resuming, read Task 6.5's section in
the plan file before assuming the original 9-task numbering still holds
end to end.

## Gotchas hit during execution (read before dispatching later tasks)

- **Task 1's venv was initially built with the wrong Python.** The plan
  said `python3 -m venv .venv`, and on this machine bare `python3` on PATH
  resolves to 3.9.6 — too old for `fastmcp` (needs >=3.10). This silently
  made `fastmcp` uninstallable without erroring loudly at venv-creation
  time (pip just reported the package "not found," which the implementer
  misread as a PyPI availability problem rather than a Python-version
  problem). **Fixed**: venv recreated with `/usr/local/bin/python3.13`,
  and the plan file itself was corrected at both `python3 -m venv`
  occurrences to say `python3.13 -m venv` explicitly. If you're resuming
  and a task's venv step seems to silently fail to install something,
  check `.venv/bin/python --version` first — this exact failure mode can
  recur if a task ever recreates the venv from scratch again.
- **Task 1's implementer also created `tests/__init__.py`**, which the
  plan explicitly forbids (see plan's File Structure section) because it
  would risk breaking the flat-import resolution. Caught by task review,
  fixed with `git rm`. Worth explicitly telling every later task's
  implementer NOT to create this file, since it's an easy default
  reflex ("tests dirs usually get an __init__.py") that contradicts this
  specific plan's convention.
- **Task 3's implementer found and fixed a real bug in the plan's own
  reference code** for `run_monitored_subprocess` (in `backends/common.py`):
  the plan's exact code used a blocking `process.stdout.readline()` as the
  loop's first statement, which blocks for the entire subprocess lifetime
  when the subprocess produces zero stdout output (e.g. `sleep 0.3`,
  which the plan's own tests use) — meaning the tick/stall-detection
  checks later in the loop never actually ran. Fixed with a
  `selectors`-based non-blocking poll; the reviewer independently
  reproduced the original bug in isolation before approving the fix. The
  public interface (signature, `StallError`, tick cadence, return type)
  is unchanged, so this doesn't affect how Task 4+ call the function.
- **Task 5's implementer weakened `configure_with_validation`'s
  validation** (silently, not maliciously — it read as a plausible
  "avoid re-validating unchanged config" optimization) by gating
  `_validate_ollama_model` calls on whether `model`/`fallback_models`
  were present in the `overrides` dict passed to a given `configure()`
  call, rather than validating the merged/effective values unconditionally
  as the plan specifies. Practical effect: a stale/removed Ollama model
  already sitting in a persisted config could survive later, unrelated
  `configure()` calls without being re-checked. Caught by task review
  (the reviewer noted this gap was invisible to the original test suite,
  since every original test happened to pass `model`/`fallback_models`
  via `overrides` whenever it mattered) — fixed to match the plan's exact
  unconditional form, plus a new regression test specifically covering
  the previously-uncovered case. Approved on re-review.
- **Task 6's original `configure`/`_configure_impl` signature used
  `**overrides` to reach config fields not in the named parameter list
  (mirroring the design spec's own documented signature) — but the
  installed `fastmcp` (3.4.4) raises `ValueError: Functions with **kwargs
  are not supported as tools` at `@mcp.tool()` DECORATION time (module
  import), before any test or call can even happen.** This isn't a bug in
  implementer code — it's a real incompatibility between the design
  spec/plan's specified signature and the actual FastMCP version
  installed. The first Task 6 dispatch correctly identified this, refused
  to unilaterally patch a public API signature, and reported BLOCKED with
  three resolution options. **Decision made: enumerate all 11 config.yaml
  keys as explicit named parameters on `configure`/`_configure_impl`,
  dropping `**overrides` entirely** — the key set is fixed/closed, so
  nothing is actually lost, and MCP clients get a properly typed schema
  per field. **This is now the plan's binding, committed signature for
  `configure`** (see `docs/superpowers/plans/2026-07-21-local-coder-phase1.md`,
  Task 6 section, and commit `7c2af76`) — if you're implementing Codex/
  Gemini/OpenRouter in a later phase and see any reference elsewhere
  (design spec, memory) to `configure(..., **overrides)`, that's now
  stale; the actual `configure` tool takes named parameters only.
  `config.py`'s internal `configure_with_validation(overrides: dict)`
  from Task 5 is UNAFFECTED — it's never `@mcp.tool()`-decorated and can
  keep taking a plain dict; only the two MCP-facing functions changed.
- **Task 7's reviewer found that `on_tick` — the progress-notification
  callback hook Task 3 built into `run_monitored_subprocess` — was never
  actually wired up anywhere.** Task 4's `AiderBackend.run_backend` calls
  `run_monitored_subprocess` without passing `on_tick` at all, and Task
  6's `_delegate_implementation_impl` never received or threaded through
  a FastMCP `Context`. This is a real gap in THE PLAN itself (my own
  reference code for Task 4 never passed `on_tick` either), not an
  implementer deviation — the design spec explicitly requires this
  mechanism (stderr log + MCP progress ping every
  `idle_notify_interval_seconds`) and it was silently dead code across
  three already-approved tasks. Decision made (user chose the full option
  over "defer, just fix the docs"): convert `_delegate_implementation_impl`
  to `async def` (FastMCP's `Context.report_progress` is async),
  bridging the still-synchronous `run_monitored_subprocess` call via
  `anyio.to_thread.run_sync` / `anyio.from_thread.run` (anyio is already
  a FastMCP dependency — verified in this venv before committing to this
  design, not assumed). Documented as a new **Task 6.5** addendum in the
  plan (inserted between Task 6 and Task 7, see commit `fe83fe7`) rather
  than rewriting Tasks 3/4/6's already-reviewed history. Dispatched;
  result pending as of this handoff update — **if resuming, check
  `.superpowers/sdd/progress.md` for whether Task 6.5 completed, and if
  Task 6.5 isn't done, Task 7 cannot be finalized either** (its README's
  progress-notification claim depends on Task 6.5's fix actually landing).
- **A checked-in file (`config.yaml`) got silently mutated on disk**
  during Task 5's fix work — quoted YAML strings (`"aider"`) became
  unquoted (`aider`), same values, no functional change, but real
  uncommitted drift on a tracked file. Cause not fully diagnosed (likely
  some test path writing through the real `CONFIG_PATH` instead of the
  isolated temp-file fixture at some point), but it was caught via
  `git status`/`git diff` before the fix was committed and reverted with
  `git checkout -- mcp-servers/local-coder/config.yaml`. **Worth watching
  for in later tasks**: run `git status` before every commit review, not
  just `git diff --stat BASE..HEAD`, since an uncommitted working-tree
  mutation wouldn't show up in a commit-range diff at all.

**Workspace:** isolated git worktree at
`.worktrees/local-coder-impl/` (relative to the main checkout at
`/Users/niravthakker/Downloads/Nirav/Personal/Coding/superpowers-local-coder`),
on branch `local-coder-impl`, pushed to `origin`, tracking `origin/dev`.
**No PR opened yet for this branch** — that happens after all 9 tasks +
final review are complete, per `finishing-a-development-branch`.

If resuming in a fresh session: `cd .worktrees/local-coder-impl` (or if
that worktree doesn't exist in your checkout, `git worktree list` from the
main repo root to find it, or `git checkout local-coder-impl` if working
without worktrees).

**Design spec:** `docs/superpowers/specs/2026-07-21-local-coder-delegation-design.md`
(merged to `dev` via PR #1, reviewed by Gemini Code Assist, 2 findings
fixed). Read this for full rationale — this handoff doesn't repeat it.

**Implementation plan being executed:**
`docs/superpowers/plans/2026-07-21-local-coder-phase1.md` — 9 tasks,
TDD throughout. Read this before dispatching or resuming any task; it is
the single source of truth for what each task must do.

**Progress ledger:** `.superpowers/sdd/progress.md` (git-ignored, local
only — if it's missing/deleted, reconstruct from `git log --oneline` on
this branch: each task's commits are self-describing, e.g. "local-coder:
add config load/save/merge with tests" = Task 1).

## Branch/PR state (as of this handoff)

- `main` — untouched, matches `origin/main`.
- `dev` — has the merged design spec + plan + this handoff doc (via PR #1,
  merged at commit `0aa1e39`). This is the base every implementation branch
  tracks.
- `local-coder-design` — the now-merged design-only branch. Its PR #1 is
  closed/merged. Leave it alone; don't add implementation commits to it.
- `local-coder-impl` — **the active implementation branch**, worktree at
  `.worktrees/local-coder-impl/`, branched from `origin/dev` after PR #1
  merged, then fast-forward-merged with 3 stray commits from
  `local-coder-design` that hadn't made it into PR #1 (the plan file, the
  handoff doc, and a path-correction fix) — those are now on `dev` via the
  merge commit on this branch, so future rebases/merges from `dev` will be
  clean.

## What Phase 1 actually builds (condensed — full detail in the plan)

- `mcp-servers/local-coder/` — Python FastMCP server, 3 tools
  (`delegate_implementation`, `configure`, `list_available_models`), Aider
  backend fully implemented, Codex/Gemini/OpenRouter as
  `NotImplementedError` stubs (real implementation is a later phase, not
  this plan).
- `agents/local-coder-implementer.md` — new subagent definition,
  `tools: Read, Grep, Glob, Bash, mcp__local-coder__delegate_implementation`
  (Edit/Write structurally excluded).
- `skills/subagent-driven-development/implementer-prompt.md` and
  `SKILL.md` — rewired to dispatch that subagent and call
  `delegate_implementation` instead of editing files directly. Both get a
  `FORK DIVERGENCE` HTML-comment marker at the top.

**Import convention decided while writing the plan** (a real gotcha,
don't reintroduce it): `mcp-servers/local-coder/` is hyphenated, not a
valid Python package name, so every internal import is **flat**
(`import config`, `from backends.aider import AiderBackend`), not
`from local_coder import ...`. A `conftest.py` at
`mcp-servers/local-coder/` makes this resolve identically under pytest and
under `server.py`'s direct script launch. If a future task's code doesn't
match this, that's a bug — the plan's every code sample already does.

## Task list (see the plan file for full detail on each)

1. Python scaffold + `config.py` (load/save/merge) + tests
2. `ollama.py` (list wrapper) + tests
3. `backends/base.py` (interface) + `backends/common.py` (shared branch/stall
   logic) + tests
4. `backends/aider.py` (real) + `backends/{codex,gemini,openrouter}.py`
   (stubs) + tests
5. `configure`/`list_available_models` validation logic in `config.py`
   (ollama-list check, fallback cap, gemini+local-model guard) + tests
6. `server.py` — FastMCP tool wrappers around plain `_*_impl` functions
   (kept separate so tests call plain functions, not FastMCP-wrapped ones)
   + tests
7. `.mcp.json` registration + `README.md`
8. `agents/local-coder-implementer.md` + rewire the two SDD skill files
9. Full test suite run + verification checkpoint (no code changes)

## Key decisions from design (don't re-litigate — see spec for why)

- `delegate_implementation` has **no `model` parameter** — model is a
  pre-run `config.yaml` setting only, changed via `configure` before a
  plan starts.
- `open_pr` defaults `false` for the SDD flow — `delegate_implementation`
  only pushes; `finishing-a-development-branch` owns the actual PR.
- `self_commits=False` backends (Codex/Gemini, not built this phase) must
  diff `git status --porcelain` against a pre-run snapshot, never
  `git diff --name-only` alone (misses untracked files) — this was a real
  bug caught by Gemini Code Assist's review of PR #1's design doc.
- No `origin` remote on `target_repo_path` → success with `pr_url: null`,
  not a failure (also a Gemini Code Assist finding on PR #1).
- MCP servers register via root `.mcp.json`, subagents live in `agents/`
  at plugin root — both verified against real installed plugins
  (`figma`, `feature-dev`), NOT `.claude/agents/` or a `plugin.json`
  `mcpServers` key (an earlier spec draft assumed the latter incorrectly).

## Explored and ruled out (don't revisit unless asked)

- `markelz0r/superpowers-codex` fork — unrelated (ports Superpowers to run
  natively inside Codex CLI as its own harness; not delegation-from-Claude-
  Code-to-Codex-backend, which is what our design does).

## Environment facts confirmed by the user

- `gh` CLI authenticated.
- Ollama running locally with `qwen3-coder:30b` pulled (plus several other
  models — `ollama list` showed `qwen2.5-coder:7b`,
  `qwen2.5-coder:1.5b-base`, etc., useful if a fallback-model test needs a
  second real model name).
- `aider` 0.86.2 installed on PATH.
- Python 3.13.7, pytest 8.2.2 available system-wide (each task still
  creates/uses `mcp-servers/local-coder/.venv/` per the plan, not the
  system Python, once Task 1 creates it).
- `fastmcp` is NOT installed system-wide — Task 1's venv setup step
  installs it from `requirements.txt`.

## Execution mode

`superpowers:subagent-driven-development` — fresh implementer subagent per
task, task-scoped reviewer after each, final whole-branch review after
Task 9, then `superpowers:finishing-a-development-branch` (which will
open the actual PR for this branch against `dev`).
