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
- **PR #3** (`ea3288c`) — item 7: made `delegate_implementation` observable
  (live output pulse, `output_tail`) and non-hanging (`stdin=DEVNULL`).
- **PR #4** (`90199a0`) — launch/bootstrap: `SessionStart` +
  at-launch venv auto-provisioning into `${CLAUDE_PLUGIN_DATA}`.

**Open now:**
- **PR #5** (`local-coder/subagent-tool-name` → `dev`) — fixes the subagent
  tool-name gap (see root cause below). Review round 1 handled (2 handoff
  doc stale-guidance fixes, `2690f65`). Merge is the human's call.

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

4. **The MCP server + its `config.yaml` live in the WORKTREE**
   (`.worktrees/local-coder-impl/`), because that's where the plugin's
   marketplace `source` points — regardless of the chat session's shell cwd.
   Always check the worktree copy for live config, not the repo-root copy.

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
5. Verify the fix landed in cache:
   `grep -o "CLAUDE_PLUGIN_DATA" ~/.claude/plugins/cache/superpowers-dev/superpowers/*/.mcp.json`
6. Fresh session from the main repo root; `claude mcp list`; confirm
   `plugin:superpowers:local-coder` is **Connected**. (Remove any stray
   project `.mcp.json` reg first: `claude mcp remove local-coder -s project`.)
7. Run a real `delegate_implementation` end-to-end.

## Phase 2 backlog (remaining, unstarted)

- **Item 4 — skill-eval evidence** for the Phase 1 SKILL.md/implementer-prompt.md
  rewrite. Repo CLAUDE.md requires eval-harness evidence
  (`superpowers:writing-skills`, adversarial pressure testing) for
  behavior-shaping skill changes; Phase 1 shipped without it. Now viable
  since the delegation path works. Eval harness:
  https://github.com/prime-radiant-inc/superpowers-evals/ (into `evals/`,
  gitignored, not cloned locally).
- **Item 5 — TDD-under-delegation is structurally weaker.** The implementer
  subagent can't independently verify RED-before-GREEN (no Edit/Write), so
  the backend owns TDD discipline. Documented as an accepted trade-off in
  `implementer-prompt.md`. Revisit only if it causes a real problem — do
  NOT preemptively redesign.
- **Item 6 — `stall_timeout_seconds` 300s default may be too tight** for a
  cold-loading large local model (an 18GB/30B model stalled the full 300s
  cold — cold-load produced zero output before the stall detector's window).
  The mechanism worked correctly; this is a config-default/README-guidance
  gap. Consider: document the cold-load risk, recommend `fallback_models`
  for large primaries, and/or raise the default. Don't fix speculatively.

**Deferred (tracked, do NOT resolve without doing the work):**
- Stall-tail retention — a stalled attempt returns `output_tail=""`;
  retaining the captured tail through StallError is a real feature change,
  scoped out of PR #3 (cubic thread `3636019858`).
- Log-retention policy — per-call log files accumulate indefinitely; a
  size/age/count policy is a design question, not a one-line fix (cubic
  thread `3636207385`).
- Windows verification on real hardware (POSIX/Windows lock + venv path
  handling is written but unverified on Windows).
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
