# Local-Coder Delegation — Session Handoff

**Purpose:** fast resume if session end mid-work. Update whenever task
complete, not just session end. Stale handoff worse than none.

## Where things stand NOW

**MVP loop closed, verified end-to-end.**
`subagent-driven-development` → `local-coder-implementer` subagent →
`delegate_implementation` → aider → Ollama → real commit works, verified
by reading repo (not trusting tool report).

Merged to `dev`:
- **PR #2** (`70f8ba3`) — Phase 1: whole `mcp-servers/local-coder/`
  FastMCP server + SDD rewiring.
- **PR #3** (`ea3288c`) — item 7: made `delegate_implementation` observable
  (live output pulse, `output_tail`) and non-hanging (`stdin=DEVNULL`).
- **PR #4** (`90199a0`) — launch/bootstrap: `SessionStart` +
  at-launch venv auto-provisioning into `${CLAUDE_PLUGIN_DATA}`.

**Open now:**
- **PR #5** (`local-coder/subagent-tool-name` → `dev`) — fixes subagent
  tool-name gap (root cause below). Review round 1 handled (2 handoff
  doc stale-guidance fixes, `2690f65`). Merge = human's call.

`main` untouched — everything lands on `dev`; `dev`→`main` later,
human's call.

## Tool-name fix (PR #5 core — was whole MVP blocker)

Old 17s no-op: agent declared
`mcp__local-coder__delegate_implementation`, but Claude Code namespaces
**plugin-bundled** MCP server tools as `mcp__plugin_<plugin>_<server>__<tool>`.
Under plugin install bare name matched nothing → subagent had no
delegation tool, silently did nothing (no tool call → no server log,
looked like connection failure). Fixed in `c0d7092`: declare BOTH
names (project-scoped `.mcp.json` install genuinely uses bare form),
drop hardcoded name from prose, add BLOCKED guardrail so missing
tool fails loud. Docs: https://code.claude.com/docs/en/mcp.md#plugin-provided-mcp-servers

## Gotchas — check FIRST

1. **Agent/skill edits need plugin REINSTALL, not just restart.** Install
   plain COPY (not symlink) at
   `~/.claude/plugins/cache/superpowers-dev/superpowers/6.1.1/`. Version
   PINNED, bare `install` can no-op against cache. Use full
   disable → uninstall → reinstall → verify sequence below.

2. **Reinstall RESETS `config.yaml`** in plugin data dir (back to
   `qwen3-coder:30b`, `extra_backend_args: []`) — lose scipy
   workaround. **OPEN QUESTION worth cheap check:** does ordinary
   `claude plugin update` also wipe it? PR #4 moved config to
   `${CLAUDE_PLUGIN_DATA}` to survive UPDATES; if update wipes too, that
   work undermined.

3. **Different sessions can use DIFFERENT data dirs** (`superpowers-inline`
   vs `superpowers-superpowers-dev`). Check newest
   `local-coder-output-*.log` mtime to find which session used before
   editing config.

4. **MCP server code lives in WORKTREE**
   (`.worktrees/local-coder-impl/`), that's where plugin's
   marketplace `source` points — regardless of chat session shell cwd.
   But **live runtime config** installed session reads/writes is
   `${CLAUDE_PLUGIN_DATA}/local-coder/config.yaml` (PR #4 moved it there),
   NOT worktree copy (only bundled default) and NOT repo-root copy.
   Change via `configure` MCP tool rather than hand-editing; if must
   inspect on disk, look under
   `${CLAUDE_PLUGIN_DATA}` (see gotcha 3 for finding right data dir).

5. **When delegation fails, read
   `${CLAUDE_PLUGIN_DATA}/local-coder/local-coder-output-*.log` FIRST** —
   per-call logs answer most "why fail" questions in one step.

## KNOWN ISSUE — scipy import crash on macOS 27 beta (environmental, not ours)

aider crashes on startup with dyld error loading
`_spropack.cpython-312-darwin.so` ("`__DATA/__thread_bss` has zero-fill
section type, but offset field not zero"). Confirmed NOT ours: reproduces
with fresh scipy, under Python 3.12 and 3.13, and standalone `aider` (no
local-coder). macOS 27 beta's dyld rejects Fortran-compiled TLS
section. Only aider's **repo-map** pulls scipy in (networkx pagerank →
`to_scipy_sparse_array` → `scipy.sparse` → `_propack`), so **workaround:
`extra_backend_args: ["--map-tokens", "0"]`** disables repo map, avoids
import. Presents as MID-CALL crash (server connects, aider crashes
building repo-map before contacting Ollama), not connection failure —
local-coder streams traceback, fails promptly.

**Do NOT "fix" by reinstalling scipy/aider** — that path twice BROKE
aider install. Recovery: `uv tool uninstall aider-chat && uv tool install
aider-chat --python 3.12`.

Open decision: document workaround as known-issue note (recommended —
root cause OS regression, local-coder shouldn't permanently paper over),
make default, or leave as user config.

## How to re-smoke-test (installed cache stale plain-copy)

From MAIN REPO ROOT (plugin enabled at project scope):
1. `claude plugin marketplace update superpowers-dev`
2. `claude plugin disable superpowers@superpowers-dev --scope project`
3. `claude plugin uninstall superpowers@superpowers-dev --scope project`
4. `claude plugin install superpowers@superpowers-dev --scope project`
5. Verify fix landed in cache — check BOTH `.mcp.json` env var AND
   cached agent file declares plugin-namespaced tool name (connected
   server does NOT prove installed agent has new name — that mismatch
   exact silent no-op this PR fixes):
   - `grep -o "CLAUDE_PLUGIN_DATA" ~/.claude/plugins/cache/superpowers-dev/superpowers/*/.mcp.json`
   - `grep -o "mcp__plugin_superpowers_local-coder__delegate_implementation" ~/.claude/plugins/cache/superpowers-dev/superpowers/*/agents/local-coder-implementer.md`
6. Fresh session from main repo root; `claude mcp list`; confirm
   `plugin:superpowers:local-coder` **Connected**. (Remove stray
   project `.mcp.json` reg first: `claude mcp remove local-coder -s project`.)
7. Run smoke test through ACTUAL fixed path — dispatch
   `local-coder-implementer` subagent (via `subagent-driven-development`,
   or direct) and confirm THAT SUBAGENT invokes `delegate_implementation`
   end-to-end. Bare direct `delegate_implementation` call does NOT
   exercise subagent tool-name resolution this PR repairs, can
   pass while real bug survives.

## Phase 2 backlog (remaining, unstarted)

- **Item 4 — skill-eval evidence** for Phase 1 SKILL.md/implementer-prompt.md
  rewrite. Repo CLAUDE.md requires eval-harness evidence
  (`superpowers:writing-skills`, adversarial pressure testing) for
  behavior-shaping skill changes; Phase 1 shipped without it. Now viable
  since delegation path works. Eval harness:
  https://github.com/prime-radiant-inc/superpowers-evals/ (into `evals/`,
  gitignored, not cloned locally).
- **Item 5 — TDD-under-delegation structurally weaker.** Implementer
  subagent can't independently verify RED-before-GREEN (no Edit/Write),
  so backend owns TDD discipline. Documented as accepted trade-off in
  `implementer-prompt.md`. Revisit only if real problem — do
  NOT preemptively redesign.
- **Item 6 — cold-load vs stall timeout — DONE, OPEN AS PR #6**
  (`local-coder/phase2-next` → `dev`). Large local model cold-load
  produces no output, so load time counted against `stall_timeout_seconds`
  (300s), killed it (18GB/30B model hit this). New
  `first_output_timeout_seconds` (shipped 600s, falls back to stall value
  when absent). Spec `2026-07-27-cold-start-grace-window-design.md`, plan
  `2026-07-27-cold-start-grace-window.md`.
  - **DESIGN CORRECTED during cubic review (P1, verified empirically).**
    First implementation ended grace on first byte of output — but
    aider prints ~14-line startup banner at ~1.08s, BEFORE model loads,
    so grace ended on banner and still-loading model got killed on
    short stall clock. Mechanism now **hard minimum-runtime floor**:
    no stall declared until process ran `first_output_timeout_seconds`;
    after floor, normal `stall_timeout_seconds` inactivity governs.
    Self-bounding (never-emitting cold-load still dies just past floor).
    Steady-state stall beginning within floor waits out floor — by
    design. Empirical proof of banner timing in PR #6 thread reply.
  - Codex review earlier found knob not wired into `configure`
    tool — fixed in `fd2f2de`.
- **Item 7 (NEW, from Codex review of cold-start work) — three real
  server.py error-handling bugs on macOS/Linux path.** All ours, all
  pre-date cold-start work; carved out as own PR (one problem =
  "delegate_implementation error-handling robustness") rather than bundled.
  - **P1 progress-report can kill aider + leak partial edits** — in
    `server.py`'s periodic `make_on_tick`, if `ctx.report_progress` raises
    (progress token/transport gone) exception escapes on_tick →
    `run_monitored_subprocess` kills aider — but not a `StallError`, so
    `AiderBackend`'s working-tree restore doesn't run, server's broad
    except immediately starts fallback model on top of partial edits.
    Initial log-announce path already guards its `report_progress` with
    try/except; periodic one doesn't. Introduced `16c292c3`/`64b8346b`
    (PR #3). Fix: catch+warn in tick callback like announce path does.
  - **P2 `gh` missing during PR setup** — with `open_pr` enabled but `gh`
    absent, `_has_open_pr` (and `gh pr create`) raise
    `FileNotFoundError`/`OSError`, uncaught, AFTER implementation
    committed and pushed — reports failure for work that landed.
    Introduced `90ed4ad` (PR #2). Fix: handle process-launch errors on
    both `gh pr view` and `gh pr create`, return success with PR-related
    note.
  - **P2 per-call log announced before exists** — unique log path
    created only when backend emits first chunk, but announced
    beforehand with `tail -f` instructions; during cold load (exactly
    when early observation matters) `tail -f` exits, file doesn't exist.
    Introduced `6c14cef` (PR #3). Fix: create empty per-call log before
    announcing.

**Deferred (tracked, do NOT resolve without doing work):**
- Stall-tail retention — stalled attempt returns `output_tail=""`;
  retaining captured tail through StallError real feature change,
  scoped out of PR #3 (cubic thread `3636019858`).
- Log-retention policy — per-call log files accumulate indefinitely;
  size/age/count policy design question, not one-line fix (cubic
  thread `3636207385`).
- Windows verification on real hardware (POSIX/Windows lock + venv path
  handling written but unverified on Windows). Codex flagged two concrete
  Windows blockers here (both ours, both need real Windows box to fix+test,
  why stay deferred rather than fixed blind): (1) `.mcp.json`'s
  `command` points at extensionless bash `launch-local-coder`, Windows
  won't run via shebang — needs directly-executable cross-platform
  wrapper or Windows-specific command (introduced `ba297aa`); (2)
  `common.py`'s `run_monitored_subprocess` uses `selectors.DefaultSelector`,
  on Windows that's `select()`, does NOT support anonymous stdout pipes
  — every backend run would fail registering pipe; needs thread-based
  or overlapped-I/O reader on Windows (introduced `062c1e9`). Do NOT fix
  either without Windows hardware to verify — untested Windows code
  worse than honest gap.
- Concurrency-safe provisioning/config seeding as designed change with
  real OS locking primitive — hand-rolled lock stripped from PR #4
  (see below).

## Review process — hard-won discipline (applies every PR round)

- **Verify each finding empirically before fixing** — reproduce against
  real current code; don't trust bot's claim text.
- Fix real issues with TDD; reply on thread + resolve; skip out-of-scope
  items with documented reason.
- **Review-round cap: 3 rounds.** Exceptions only for genuine shipped-code
  correctness/security bug (surface to human first). Ignore
  doc-consistency / style / nit churn past cap.
- **`restore_working_tree` function went through 5 fix rounds in Phase 1.**
  If any NEW finding hits that function, STOP, raise with human
  before fixing — don't keep patching indefinitely.
- **PR #4's hand-rolled concurrency code STRIPPED after its own fixes
  kept needing fixes** (lock TOCTOU → unlocked fallthrough → O_EXCL partial
  publish → stale sentinel/clobber, 4 rounds). Doing right needs real
  OS locking primitive — design change, tracked as future work, not
  patch. `ensure-local-coder-venv` now provisions lock-free with
  adopt-existing-venv + pip retry (verified clean under concurrent runs).
- Keep THIS handoff current after each round. Respect session-usage limits —
  if approaching, update doc and stop rather than burn paid credits.

## Environment facts

- `gh` authenticated. Ollama running with `qwen3-coder:30b` +
  `qwen2.5-coder:7b` (small/fast, good fallback) pulled. `aider` 0.86.2 on
  PATH. Python 3.13.7 / pytest 8.2.2 system-wide.
- Import convention: `mcp-servers/local-coder/` hyphenated (not valid
  package name), so ALL internal imports **flat** (`import config`,
  `from backends.aider import AiderBackend`). `conftest.py` there makes
  this resolve identically under pytest and `server.py`'s direct launch.
- MCP command legitimately points into `.worktrees/local-coder-impl/` —
  that's marketplace source, NOT stale path. Do not "repoint" it, and
  do NOT add duplicate `.mcp.json` registration (misdiagnosis surfaced
  twice already).

## Key artifacts

- Spec: `docs/superpowers/specs/2026-07-21-local-coder-delegation-design.md`
- Phase 1 plan: `docs/superpowers/plans/2026-07-21-local-coder-phase1.md`
- Progress ledger: `.superpowers/sdd/progress.md` (git-ignored; reconstruct
  from `git log --oneline` if missing).
- Working branch: `local-coder-impl` worktree at `.worktrees/local-coder-impl/`.

## Key design decisions (don't re-litigate — see spec)

- `delegate_implementation` has NO `model` param — model is `config.yaml`
  setting, changed via `configure` before plan starts.
- `open_pr` defaults `false` for SDD — delegation only pushes;
  `finishing-a-development-branch` owns PR.
- No `origin` remote on `target_repo_path` → success with `pr_url: null`,
  not failure.
- `configure` takes 11 explicit named params, NOT `**overrides` (FastMCP
  3.4.4 rejects `**kwargs` tools at decoration time).
- MCP servers register via root `.mcp.json`; subagents live in `agents/` at
  plugin root — NOT `.claude/agents/` or `plugin.json` `mcpServers` key.