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
**ALL 9 PLAN TASKS + THE TASK 6.5 ADDENDUM ARE COMPLETE.** The final
whole-branch review + one fix round + a confirmatory re-review all
returned "Ready to merge: Yes" (43/43 tests). **PR #2 was opened**
(`local-coder-impl` → `dev`):
https://github.com/normaltusker/superpowers-local-coder/pull/2

**After the PR opened, CodeRabbit's automated review found 17 more
findings** (real ones — branch-name argument injection into git/gh
subprocess calls, a subprocess resource leak on unexpected exception, a
generic exception crashing the whole `delegate_implementation` call
instead of failing over, no timeout on `ollama list`, blocking git/gh
calls left on the async event loop after Task 6.5's conversion, plus doc
drift). **16 fixed** across 5 commits (`289c693`, `f18150b`, `c1dfb03`,
`6b2635b`, `3ceac27`); **1 deliberately skipped** (unbounded
`requirements.txt` version ranges / a transitive `idna` CVE — needs a
proper lockfile tool, out of scope for a review-fix round, documented
in the reply on that thread). All 17 threads replied-to and resolved on
GitHub. A dedicated review of just this fix batch (most capable model,
7 specific correctness checks with file:line evidence) came back clean.
**55/55 tests passing now** (12 new TDD tests from this round).

**After that, cubic-dev-ai reviewed the PR and found 36 more findings.**
Per explicit direction from the repo owner: 5 acknowledged as scope/process
trade-offs (Phase 1's dev-env-only scope, no Windows support, TDD becoming
brief-dependent under delegation, SDD's hard dependency on the local-coder
MCP tool being available, missing formal skill-eval evidence for the
SKILL.md/implementer-prompt.md changes) — **left open on GitHub
deliberately**, not fixed, not resolved. 2 skipped with documented reason
(delegating branch validation to `git check-ref-format`; splitting
requirements.txt into prod/dev files). **29 fixed** across 13 commits,
including a genuinely investigated correctness bug (aider's
`apply_updates()`/`auto_commit()` are non-atomic — a stall-kill mid-run
could leave partial uncommitted writes for the next failover model to run
on top of; fixed with scoped working-tree restoration) and a real behavior
change (`branch_prefix` config is now actually applied, was previously
dead). A dedicated review of the largest fix batch (most capable model)
empirically verified the three highest-risk changes — not just read the
code, actually ran tests confirming multi-byte UTF-8 correctly reassembles
across a subprocess read boundary, and that pre-existing dirty working-tree
state survives the new cleanup logic untouched. **Two findings were missed
in the original triage and one was incorrectly marked fixed** (caught
during a self-audit of my own reply batch, not by an external reviewer) —
corrected in a follow-up round: `stall_timeout_seconds`/
`idle_notify_interval_seconds` now validated as strictly positive,
unbounded subprocess-output memory accumulation now bounded to a tail
buffer, and the implementer-prompt.md's "what to do when verification
finds a gap" instruction gap fixed. **92/92 tests passing** at that point
(up from 43 at merge-ready, 55 after CodeRabbit, 87 after the first cubic
batch).

**A follow-up cubic-dev-ai pass then found 2 more real bugs in
`restore_working_tree`** (the working-tree-cleanup function added during
the first cubic round): (P1) `git checkout -- <tracked> <untracked>`
fails ENTIRELY when any untracked path is included — the whole command
aborts before touching tracked paths, so a failed attempt's tracked-file
corruption silently survived cleanup whenever the attempt also created a
new file (the normal case). (P2) default `git status --porcelain`
quotes filenames with spaces/special chars, so cleanup silently failed
to match such files. **Both verified via manual repro before fixing, not
just trusted from the finding text.** Fixed by switching to
`-z`-delimited status parsing and splitting checkout/clean into two
independent commands. **While verifying this fix, a dedicated reviewer
found a THIRD, pre-existing bug in the same function** (not from any
review-bot finding): staged-but-uncommitted edits weren't reverted either
(`checkout --` restores from the index, and the old `reset --mixed` was
conditional on HEAD having moved) — this directly undermines the exact
scenario the function's own docstring names (a stall-kill mid-attempt).
Fixed with an unconditional `reset --mixed` before cleanup, verified with
its own repro (reproduced the exact corruption pre-fix, confirmed closed
post-fix). **95/95 tests passing now.**

**A further cubic-dev-ai pass then found 3 more real bugs, all caused by
or adjacent to the P1/P2 fix above** (the same "fourth fix to the same
function" pattern — each fix round to `restore_working_tree` has
surfaced a new edge case): (P0) the previous round's two-command split
(`git checkout --` / `git clean -fd --`) passed filenames as literal
git arguments without disabling pathspec magic — a file named
`:(glob)victim*` caused `git clean -fd --` to delete an unrelated
`victim.txt` instead of the malicious file itself. (P1) the unconditional
`git reset --mixed` added to fix the staged-edits bug in the prior round
unstages **every** staged path in the index, not just the failed
attempt's — pre-existing user work staged before delegation started got
silently unstaged by "restoration." (P2) a directory already untracked
before the attempt began collapses to one `?? dirname/` porcelain line in
both the pre- and post-attempt snapshots under default `git status`,
hiding new files the attempt added inside it. **All three verified via
manual repro in scratch directories before writing any fix**, not just
trusted from the finding text. Fixing these required a genuine redesign,
not another patch: `git reset --mixed` (blanket) is now
`git reset --soft` (moves HEAD only, doesn't touch the index) followed by
**per-path** `git restore --source=pre_head --staged --worktree --
<paths>` for only the paths this attempt actually touched;
`GIT_LITERAL_PATHSPECS=1` is now set on every path-taking git subprocess
call; both status calls now pass `--untracked-files=all`. **While
implementing this, found and fixed 2 more bugs of my own**, both caught
before commit: (a) restoring only the new path of a git rename left the
old path still marked deleted in the index (fixed by having the `-z`
porcelain parser surface both halves of a rename record — the old path
under a synthetic `"D "` code); (b) a test regressed because the HEAD-reset
step had been dropped entirely while focused on the index-preservation
fix — added back as `git reset --soft pre_head` for the case where the
attempt actually committed before failing. Committed as `f4aaadb`. Given
this was the fourth consecutive fix to this one function, dispatched a
maximally adversarial review (most capable model) specifically told to
hunt for a fifth bug, including checking a gap I'd personally flagged
(an attempt further modifying a file that was *already* dirty before the
attempt started). Verdict: no fifth bug, no regression; that one gap is
real but **pre-existing across every version of this function**, and the
redesign is strictly no worse (better on index-preservation than the
blanket-reset version it replaced) — recommended documenting it as a
known limitation rather than chasing a fifth fix. Docstring updated
accordingly, no further code/test changes. Committed as `5a58bdb`.
**99/99 tests passing now** (4 new TDD tests this round, 3 of 4 verified
genuinely RED-before/GREEN-after; the rename-bug test only ever failed
within an uncommitted intermediate edit, never against pushed history).
All 3 threads from this round (comment IDs `3629411569` P0,
`3629411575` P1, `3629411577` P2) replied to with fix explanations and
came back already resolved — turned out to be a one-off (see next
round's note below, `resolveReviewThread` was needed there), not a
repo-wide auto-resolve behavior to rely on.

**A 5th cubic-dev-ai pass then found 3 more findings in the same
function, plus 2 unrelated ones.** At this point the repo owner flagged
(rightly) that this was starting to feel endless, so this round was
explicitly **triaged before any fix work**, verifying each claim with a
manual repro against the real current code rather than trusting the
finding text — same discipline as every prior round, but confirmed
deliberately before writing code this time:
- **Real, fixed:** `git status` omits `!!` (ignored) entries by default
  even with `--untracked-files=all` — a failed attempt's newly generated
  ignored files (build output, `.pyc`, etc.) were invisible to the
  pre/post snapshot diff and survived cleanup, leaking into whatever
  fallback model ran next. Fixed by adding `--ignored` to both status
  calls and routing `!!` paths through `git clean -fdx` (`-x` is what
  makes clean remove ignored, not just untracked, paths).
- **Real, fixed, more serious than its P2 label:** `restore_working_tree`
  combines `-C repo_path` (argv, resolved against the process's cwd) with
  `cwd=repo_path` (which changes that cwd first) on the same string — a
  relative `repo_path` got resolved twice, collapsing to
  `<repo_path>/<repo_path>`, which doesn't exist. Those git calls don't
  use `check=True`, so the failure was silent and **cleanup became a
  total no-op for any relative `target_repo_path`** — same severity class
  as the P0/P1 bugs from two rounds ago, just newly surfaced. Fixed by
  normalizing `repo_path` to absolute at the top of both
  `snapshot_working_tree` and `restore_working_tree`.
- **Investigated, deliberately skipped:** a theoretical `E2BIG` if a
  single attempt changes enough paths to exceed argv limits. Measured
  this machine's `ARG_MAX` (~1MB) against realistic path lengths — needs
  ~17,000+ changed paths in one attempt to threaten, which isn't a
  realistic failure mode for this tool's actual usage (one plan task per
  delegation call). Documented as a known limit, not fixed, replied with
  the reasoning.
Fixed the two real ones with TDD (both new tests verified genuinely
RED-before/GREEN-after), committed as `ed67846`. **101/101 tests
passing.** Replied to all 3 threads; this time they did NOT auto-resolve
on reply, so `resolveReviewThread` was called explicitly on each
(`comment_ids` `3629611562`, `3629611566`, `3629611587`) — don't assume
either behavior going forward, always verify resolution state after
replying rather than assuming it happened.

**The repo owner then explicitly asked to close out the 5 remaining
open threads as won't-fix**, rather than leave them open indefinitely —
the review-bot cycle had gone on long enough that "leave it open forever
as an unresolved design question" stopped being useful. Posted a
won't-fix reply to each with the actual reasoning (not just "acknowledged,
skipping"): venv bootstrap/packaging is out of Phase 1's dev-env-only
scope; TDD-under-delegation is an inherent MCP-boundary trade-off already
documented in implementer-prompt.md's Report Format section; the SDD
rewiring is intentionally fork-specific, making it conditional is a
bigger architectural change than this PR; Windows support was never in
scope for this phase (consistent with `config.py`'s POSIX-only `fcntl`
usage elsewhere in the same PR); the missing skill-eval evidence is a
real gap but belongs in its own dedicated eval-harness pass, not bundled
into a review-fix cycle. All 5 resolved via `resolveReviewThread`.
**62/62 review threads now resolved. Zero open threads on PR #2.**

**Current state: PR #2 is open, all review threads resolved, 101/101
tests passing.** `restore_working_tree` went through 5 review-driven fix
rounds this session. **If any NEW finding shows up against that same
function in a future session, stop and raise it with the repo owner
before fixing** — don't keep patching indefinitely; either the function
needs a more fundamental rethink, or review-bot findings on it should
stop being auto-actioned without a cost/benefit check first. If resuming:
check `gh pr view 2 --repo normaltusker/superpowers-local-coder` for any
NEW review activity since this handoff was written before assuming
there's nothing left to do — CI/review bots may post more later. If
truly nothing new, this work is done; merging the PR is the human's
call, not something to do unprompted.

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
  than rewriting Tasks 3/4/6's already-reviewed history. **Task 6.5 is now
  complete** — `_delegate_implementation_impl` is `async def`, the
  synchronous backend call is bridged via `anyio.to_thread.run_sync`, and
  `on_tick` bridges back to `Context.report_progress` via
  `anyio.from_thread.run`. Task 7 was finalized after this landed, since
  its README's progress-notification claim depended on it.
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
`.worktrees/local-coder-impl/` (relative to the main repo checkout root),
on branch `local-coder-impl`, pushed to `origin`, tracking `origin/dev`.
**PR #2 is open** (`local-coder-impl` → `dev`) — see line 16 above for
the link and current status. The worktree stays alive for iterating on
review feedback; `finishing-a-development-branch`'s Option 2 ("push and
create PR") is what opened it, and that step is already done.

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

`superpowers:subagent-driven-development` was used to build all 9 tasks +
the Task 6.5 addendum — fresh implementer subagent per task, task-scoped
reviewer after each, final whole-branch review, then
`superpowers:finishing-a-development-branch`, which opened PR #2. **All of
this is done.** Any further work on this branch (e.g. responding to new
review comments) happens as direct fix-and-reply cycles against the open
PR, not another full SDD pass.
