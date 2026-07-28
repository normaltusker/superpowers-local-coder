# Local-Coder Delegation — Session Handoff

**Purpose:** fast resumption if a session ends mid-work. Update whenever a
task completes, not just at session end. A stale handoff is worse than none.

## Where things stand NOW

**MVP loop is closed and verified end-to-end.**
`subagent-driven-development` → `local-coder-implementer` subagent →
`delegate_implementation` → aider → Ollama → real commit works, verified
by reading the repo (not trusting the tool's report).

Merged to `dev`:
- **PR #2** (`70f8ba3`) — Phase 1: the whole `mcp-servers/local-coder/`
  FastMCP server + SDD rewiring.
- **PR #3** (`ea3288c`) — made `delegate_implementation` observable
  (live output pulse, `output_tail`) and non-hanging (`stdin=DEVNULL`).
- **PR #4** (`90199a0`) — launch/bootstrap: `SessionStart` +
  at-launch venv auto-provisioning into `${CLAUDE_PLUGIN_DATA}`.
- **PR #5** (`fe5aa27`) — fixed the subagent tool-name gap (see root cause
  below).
- **PR #6** (`a175e1f`) — cold-load grace window
  (`first_output_timeout_seconds`, minimum-runtime floor). See Item 6.
- **PR #7** (`5c8720c`) — delegate_implementation error-handling
  robustness (three server.py bugs). See Item 7.

**Open now:**
- Nothing in flight. Next up: Item 4 (skill-eval evidence).

`main` is untouched — everything lands on `dev`; `dev`→`main` is later and
the human's call.

## The tool-name fix (PR #5 core — was the whole MVP blocker)

The old 17s no-op: the agent declared
`mcp__local-coder__delegate_implementation`, but Claude Code namespaces a
**plugin-bundled** MCP server's tools as `mcp__plugin_<plugin>_<server>__<tool>`.
Under a plugin install that bare name matched nothing → the subagent had no
delegation tool and silently did nothing (no tool call → no server log,
which looked like a connection failure). Fixed in `c0d7092`: declare BOTH
names (a project-scoped `.mcp.json` install genuinely uses the bare form),
drop the hardcoded name from prose, add a BLOCKED guardrail so a missing
tool fails loudly. Docs: https://code.claude.com/docs/en/mcp.md#plugin-provided-mcp-servers

## Gotchas — check these FIRST

1. **Agent/skill edits need a plugin REINSTALL, not just a restart.** The
   install is a plain COPY (not symlink) at
   `~/.claude/plugins/cache/superpowers-dev/superpowers/6.1.1/`. Version is
   PINNED, so a bare `install` can no-op against the cache. Use the full
   disable → uninstall → reinstall → verify sequence below.

2. **A reinstall RESETS `config.yaml`** in the plugin data dir (back to
   `qwen3-coder:30b`, `extra_backend_args: []`) — losing the scipy
   workaround. **OPEN QUESTION worth a cheap check:** does an ordinary
   `claude plugin update` also wipe it? PR #4 moved config to
   `${CLAUDE_PLUGIN_DATA}` to survive UPDATES; if update wipes it too, that
   work is undermined.

3. **Different sessions can use DIFFERENT data dirs** (`superpowers-inline`
   vs `superpowers-superpowers-dev`). Check the newest
   `local-coder-output-*.log` mtime to find which one a session used before
   editing config.

4. **The MCP server code lives in the WORKTREE**
   (`.worktrees/local-coder-impl/`), because that's where the plugin's
   marketplace `source` points — regardless of the chat session's shell cwd.
   But the **live runtime config** an installed session reads/writes is
   `${CLAUDE_PLUGIN_DATA}/local-coder/config.yaml` (PR #4 moved it there),
   NOT the worktree copy (which is only the bundled default) and NOT the
   repo-root copy. Change it via the `configure` MCP tool rather than
   hand-editing, and if you must inspect on disk, look under
   `${CLAUDE_PLUGIN_DATA}` (see gotcha 3 for finding the right data dir).

5. **When a delegation fails, read
   `${CLAUDE_PLUGIN_DATA}/local-coder/local-coder-output-*.log` FIRST** —
   per-call logs make most "why did it fail" questions answerable in one step.

## KNOWN ISSUE — scipy import crash on macOS 27 beta (environmental, not ours)

aider crashes on startup with a dyld error loading
`_spropack.cpython-312-darwin.so` ("`__DATA/__thread_bss` has a zero-fill
section type, but offset field is not zero"). Confirmed NOT ours: reproduces
with fresh scipy, under Python 3.12 and 3.13, and with standalone `aider` (no
local-coder). It's macOS 27 beta's dyld rejecting a Fortran-compiled TLS
section. Only aider's **repo-map** pulls scipy in (networkx pagerank →
`to_scipy_sparse_array` → `scipy.sparse` → `_propack`), so **workaround:
`extra_backend_args: ["--map-tokens", "0"]`** disables the repo map and
avoids the import. It presents as a MID-CALL crash (server connects, aider
crashes building the repo-map before contacting Ollama), not a connection
failure — local-coder streams the traceback and fails promptly.

**Do NOT "fix" by reinstalling scipy/aider** — that path twice BROKE the
aider install. Recovery: `uv tool uninstall aider-chat && uv tool install
aider-chat --python 3.12`.

Open decision: document the workaround as a known-issue note (recommended —
root cause is an OS regression local-coder shouldn't permanently paper over),
make it a default, or leave it as user config.

## How to re-smoke-test (installed cache is a stale plain-copy)

From the MAIN REPO ROOT (where the plugin is enabled at project scope):
1. `claude plugin marketplace update superpowers-dev`
2. `claude plugin disable superpowers@superpowers-dev --scope project`
3. `claude plugin uninstall superpowers@superpowers-dev --scope project`
4. `claude plugin install superpowers@superpowers-dev --scope project`
5. Verify the fix landed in cache — check BOTH the `.mcp.json` env var AND
   that the cached agent file declares the plugin-namespaced tool name (a
   connected server does NOT prove the installed agent has the new name —
   that mismatch is the exact silent no-op this PR fixes):
   - `grep -o "CLAUDE_PLUGIN_DATA" ~/.claude/plugins/cache/superpowers-dev/superpowers/*/.mcp.json`
   - `grep -o "mcp__plugin_superpowers_local-coder__delegate_implementation" ~/.claude/plugins/cache/superpowers-dev/superpowers/*/agents/local-coder-implementer.md`
6. Fresh session from the main repo root; `claude mcp list`; confirm
   `plugin:superpowers:local-coder` is **Connected**. (Remove any stray
   project `.mcp.json` reg first: `claude mcp remove local-coder -s project`.)
7. Run the smoke test through the ACTUAL fixed path — dispatch the
   `local-coder-implementer` subagent (via `subagent-driven-development`,
   or directly) and confirm THAT SUBAGENT invokes `delegate_implementation`
   end-to-end. A bare direct `delegate_implementation` call does NOT
   exercise the subagent tool-name resolution this PR repairs, so it can
   pass while the real bug survives.

## Phase 2 backlog (remaining, unstarted)

- **Item 4 — skill-eval evidence — DEFERRED** (insurance for a future
  UPSTREAM PR, not a live fix; this fork lands on `dev`, where the
  eval-evidence bar is optional). Covers the Phase 1 change to
  `skills/subagent-driven-development/SKILL.md` (an UPSTREAM tuned file that
  `bf6d731` modified to delegate implementation to the local-coder MCP
  server) plus `implementer-prompt.md`. Do this the day before any upstream
  PR, not sooner.
  - **Eval harness:** https://github.com/prime-radiant-inc/superpowers-evals
    (public). Quorum drives real coding-agent CLIs through a Gauntlet QA
    agent and grades workflow compliance. Clones into `evals/` (gitignored).
  - **Run recipe (non-container):**
    1. `bun` is already installed (`~/.bun/bin/bun`, v1.3.14, on PATH via
       `~/.zshrc`).
    2. Clone superpowers-evals into `evals/`; `cd evals && bun install`.
    3. Needs a **Gauntlet checkout** (the QA driver) discovered via
       `GAUNTLET_ROOT` or a `bun link` — URL not in the public README;
       confirm whether it's public/private before starting.
    4. `export SUPERPOWERS_ROOT=<this worktree>` and
       `export ANTHROPIC_API_KEY=...` (or `CLAUDE_CODE_OAUTH_TOKEN` from
       `claude setup-token` — subscription caps make the API key better for
       batches). **Real API spend; human provides creds.**
    5. Relevant scenarios (exercise our SDD change):
       `scenarios/sdd-*` (25+ of them — e.g. `sdd-svelte-todo`,
       `sdd-quality-reviewer-catches-planted-defect`,
       `sdd-rejects-extra-features`, `sdd-final-review-single-wave`),
       plus `subagent-dispatch-no-overtrigger`.
    6. `bun run quorum run scenarios/<name> --coding-agent claude` then
       `bun run quorum show <run-dir>`. Live evals launch Claude with
       `--dangerously-skip-permissions` in a throwaway HOME — trusted-local
       only. Container path (`scripts/evals-container`) needs Docker, which
       is NOT installed on this host.
  - **Why deferred:** heaviest backlog item, weakest immediate payoff. Needs
    Docker/Gauntlet/creds + real spend; nothing is currently broken by the
    Phase 1 edit. Shipped PRs #5/#6/#7 already carry strong evidence (TDD,
    152 tests, live smoke). Revisit only for an upstream submission.
- **Item 5 — TDD-under-delegation is structurally weaker.** The implementer
  subagent can't independently verify RED-before-GREEN (no Edit/Write), so
  the backend owns TDD discipline. Documented as an accepted trade-off in
  `implementer-prompt.md`. Revisit only if it causes a real problem — do
  NOT preemptively redesign.
- **Item 6 — cold-load vs stall timeout — DONE, MERGED AS PR #6** (`a175e1f`).
  A large local model's cold-load
  produces no output, so its load time counted against `stall_timeout_seconds`
  (300s) and killed it (an 18GB/30B model hit this). New
  `first_output_timeout_seconds` (shipped 600s, falls back to the stall value
  when absent). Spec `2026-07-27-cold-start-grace-window-design.md`, plan
  `2026-07-27-cold-start-grace-window.md`.
  - **DESIGN CORRECTED during cubic review (P1, verified empirically).** The
    first implementation ended the grace on the first byte of output — but
    aider prints a ~14-line startup banner at ~1.08s, BEFORE the model loads,
    so grace ended on the banner and the still-loading model was killed on the
    short stall clock. The mechanism is now a **hard minimum-runtime floor**:
    no stall is declared until the process has run `first_output_timeout_seconds`;
    after the floor, normal `stall_timeout_seconds` inactivity governs.
    Self-bounding (a never-emitting cold-load still dies just past the floor).
    A steady-state stall that begins within the floor waits out the floor — by
    design. Empirical proof of the banner timing is in the PR #6 thread reply.
  - Codex review earlier found the knob wasn't wired into the `configure`
    tool — fixed in `fd2f2de`.
  - **Round 2 (cubic, verified):** a `first_output_timeout_seconds` SHORTER
    than `stall_timeout_seconds` was not honored — the kill condition ANDed
    both budgets, so a fully-silent backend lingered until `max(floor, stall)`.
    Fixed by splitting the check on `never_emitted`: a silent process is killed
    once the floor elapses (short OR long), an emitted one at `stall_timeout`
    inactivity after the floor. A follow-up (same round) also bounded the poll
    wait by the nearest deadline, so a short floor is honored to within a small
    slop instead of being overshot by a longer `idle_notify_interval_seconds`.
    Reproduced empirically (0.2s floor / 0.5s stall / 20s notify → was killed
    at ~0.5s, now ~0.2s). README first-byte prose corrected to the floor model.
- **Item 7 — DONE, MERGED AS PR #7** (`5c8720c`). Three real server.py
  error-handling bugs on the macOS/Linux path, found by the Codex review of
  the cold-start work. All ours, all pre-dated it; shipped as their own PR
  (one problem = "delegate_implementation error-handling robustness"). Fixed
  via TDD (152 tests) + a 10/10 live smoke run forcing all three fault edges;
  cubic round-1 P3s (progress-warn throttle, stale comment) also fixed.
  - **P1 progress-report can kill aider + leak partial edits** — in
    `server.py`'s periodic `make_on_tick`, if `ctx.report_progress` raises
    (progress token/transport gone) the exception escapes on_tick →
    `run_monitored_subprocess` kills aider → but it's not a `StallError`, so
    `AiderBackend`'s working-tree restore doesn't run, and the server's broad
    except immediately starts the fallback model on top of partial edits. The
    initial log-announce path already guards its `report_progress` with
    try/except; the periodic one doesn't. Introduced `16c292c3`/`64b8346b`
    (PR #3). Fix: catch+warn in the tick callback like the announce path does.
  - **P2 `gh` missing during PR setup** — with `open_pr` enabled but `gh`
    absent, `_has_open_pr` (and `gh pr create`) raise
    `FileNotFoundError`/`OSError`, uncaught, AFTER the implementation was
    committed and pushed — reporting failure for work that landed. Introduced
    `90ed4ad` (PR #2). Fix: handle process-launch errors on both `gh pr view`
    and `gh pr create`, return success with a PR-related note.
  - **P2 per-call log announced before it exists** — the unique log path is
    created only when the backend emits its first chunk, but it's announced
    beforehand with `tail -f` instructions; during a cold load (exactly when
    early observation matters) `tail -f` exits because the file doesn't exist.
    Introduced `6c14cef` (PR #3). Fix: create the empty per-call log before
    announcing it.

**Deferred (tracked, do NOT resolve without doing the work):**
- Stall-tail retention — a stalled attempt returns `output_tail=""`;
  retaining the captured tail through StallError is a real feature change,
  scoped out of PR #3 (cubic thread `3636019858`).
- Log-retention policy — per-call log files accumulate indefinitely; a
  size/age/count policy is a design question, not a one-line fix (cubic
  thread `3636207385`).
- Windows verification on real hardware (POSIX/Windows lock + venv path
  handling is written but unverified on Windows). Codex flagged two concrete
  Windows blockers here (both ours, both need a real Windows box to fix+test,
  which is why they stay deferred rather than fixed blind): (1) `.mcp.json`'s
  `command` points at the extensionless bash `launch-local-coder`, which
  Windows won't run via shebang — needs a directly-executable cross-platform
  wrapper or a Windows-specific command (introduced `ba297aa`); (2)
  `common.py`'s `run_monitored_subprocess` uses `selectors.DefaultSelector`,
  which on Windows is `select()` and does NOT support anonymous stdout pipes
  — every backend run would fail registering the pipe; needs a thread-based
  or overlapped-I/O reader on Windows (introduced `062c1e9`). Do NOT fix
  either without Windows hardware to verify — untested Windows code is worse
  than an honest gap.
- Concurrency-safe provisioning/config seeding as a designed change with a
  real OS locking primitive — the hand-rolled lock was stripped from PR #4
  (see below).

## Review process — hard-won discipline (applies to every PR round)

- **Verify each finding empirically before fixing** — reproduce it against
  the real current code; don't trust the bot's claim text.
- Fix real issues with TDD; reply on the thread + resolve; skip out-of-scope
  items with a documented reason.
- **Review-round cap: 3 rounds.** Exceptions only for a genuine shipped-code
  correctness/security bug (surface to the human first). Ignore
  doc-consistency / style / nit churn past the cap.
- **The `restore_working_tree` function went through 5 fix rounds in Phase 1.**
  If any NEW finding hits that function, STOP and raise it with the human
  before fixing — don't keep patching indefinitely.
- **PR #4's hand-rolled concurrency code was STRIPPED after its own fixes
  kept needing fixes** (lock TOCTOU → unlocked fallthrough → O_EXCL partial
  publish → stale sentinel/clobber, 4 rounds). Doing it right needs a real
  OS locking primitive — a design change, tracked as future work, not a
  patch. `ensure-local-coder-venv` now provisions lock-free with
  adopt-existing-venv + pip retry (verified clean under concurrent runs).
- Keep THIS handoff current after each round. Respect session-usage limits —
  if approaching, update this doc and stop rather than burning paid credits.

## Environment facts

- `gh` authenticated. Ollama running with `qwen3-coder:30b` +
  `qwen2.5-coder:7b` (small/fast, good fallback) pulled. `aider` 0.86.2 on
  PATH. Python 3.13.7 / pytest 8.2.2 system-wide.
- Import convention: `mcp-servers/local-coder/` is hyphenated (not a valid
  package name), so ALL internal imports are **flat** (`import config`,
  `from backends.aider import AiderBackend`). A `conftest.py` there makes
  this resolve identically under pytest and `server.py`'s direct launch.
- MCP command legitimately points into `.worktrees/local-coder-impl/` —
  that's the marketplace source, NOT a stale path. Do not "repoint" it, and
  do NOT add a duplicate `.mcp.json` registration (that misdiagnosis has
  surfaced twice).

## Key artifacts

- Spec: `docs/superpowers/specs/2026-07-21-local-coder-delegation-design.md`
- Phase 1 plan: `docs/superpowers/plans/2026-07-21-local-coder-phase1.md`
- Progress ledger: `.superpowers/sdd/progress.md` (git-ignored; reconstruct
  from `git log --oneline` if missing).
- Working branch: `local-coder-impl` worktree at `.worktrees/local-coder-impl/`.

## Key design decisions (don't re-litigate — see spec)

- `delegate_implementation` has NO `model` param — model is a `config.yaml`
  setting, changed via `configure` before a plan starts.
- `open_pr` defaults `false` for SDD — delegation only pushes;
  `finishing-a-development-branch` owns the PR.
- No `origin` remote on `target_repo_path` → success with `pr_url: null`,
  not a failure.
- `configure` takes 11 explicit named params, NOT `**overrides` (FastMCP
  3.4.4 rejects `**kwargs` tools at decoration time).
- MCP servers register via root `.mcp.json`; subagents live in `agents/` at
  plugin root — NOT `.claude/agents/` or a `plugin.json` `mcpServers` key.
