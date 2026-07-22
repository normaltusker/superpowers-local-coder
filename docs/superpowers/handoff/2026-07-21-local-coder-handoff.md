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
As of this update: Tasks 1-2 complete and reviewed clean (Task 1 needed
one fix round — see "Gotchas hit during execution" below); Task 3 dispatched.

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
