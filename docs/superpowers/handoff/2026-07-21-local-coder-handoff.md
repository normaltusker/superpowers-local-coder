# Local-Coder Delegation — Session Handoff

**Purpose of this file:** if this session ends mid-work, read this first —
it should make resumption fast without re-deriving context from the whole
conversation. **Update this file whenever a task completes**, not just at
session end — a stale handoff is worse than none.

## Where things stand NOW (read this first)

**Phase 1 is merged. Phase 2 is starting.** PR #2 (`local-coder-impl` →
`dev`) merged at commit `70f8ba3` on 2026-07-22. `dev` now has the full
Phase 1 build: the local-coder MCP server (Aider backend real,
Codex/Gemini/OpenRouter stubbed), the SDD skill rewiring, and
`agents/local-coder-implementer.md`.

**Working branch:** `local-coder-impl` (same branch, same worktree at
`.worktrees/local-coder-impl/`) — reused for Phase 2 rather than cutting a
new branch, per explicit direction. It was fast-forwarded to match `dev`
right after the merge, so it currently has zero diff against `dev`; Phase
2 commits land on top of it from here. **`main` is untouched — Phase 2
work merges to `dev` only. Merging `dev` to `main` happens later, once
Phase 2's basics are complete, and is explicitly the human's call, not
something to do unprompted.**

### Phase 2 backlog

Everything below was identified as pending after a post-merge review of
the original design spec against what Phase 1 actually shipped (Codex/
Gemini/OpenRouter backends excluded — those remain their own later
phase, not part of this Phase 2 pass):

1. **Run the Part 3 manual smoke test for real — STARTED, BLOCKED, see
   below.** Design spec's own acceptance check — brainstorm → plan → SDD
   dispatches `local-coder-implementer` → `delegate_implementation`
   actually invokes aider against local Ollama → a commit lands on the
   shared branch → the implementer verifies via Read and reports DONE →
   task reviewer approves → `finishing-a-development-branch` opens the
   PR — has **still never been run end-to-end**. Attempting it on
   2026-07-22 surfaced a real, more-fundamental-than-expected blocker —
   see "Item 1 attempt — findings and next step" immediately below.
2. **Fresh-clone / plugin-install bootstrap provisioning — TURNS OUT TO
   BLOCK ITEM 1, NOT JUST FRESH INSTALLS.** `.mcp.json` uses
   `${CLAUDE_PLUGIN_ROOT}`, a variable Claude Code only sets when the
   repo is loaded as an **installed plugin** — not when it's a plain
   project checkout (which is how this session and this worktree are
   normally used). This isn't only a "fresh clone" problem as originally
   scoped from the cubic-dev-ai finding; it means the local-coder MCP
   tools are **unreachable in the very session doing Phase 2
   development**, unless that session specifically has this fork
   installed as a plugin. See the item 1 findings below for the concrete
   repro and the exact install steps to unblock it.
3. **Windows support.** Currently impossible as-is: `config.py`'s file
   locking uses `fcntl` (POSIX-only, no Windows equivalent), and
   `.mcp.json`'s hardcoded `.venv/bin/python` path doesn't resolve on
   Windows (`.venv/Scripts/python.exe` there). Also raised by cubic-dev-ai,
   also won't-fixed on PR #2, now Phase 2 scope. Fixing #2 (a proper
   launcher/bootstrap) and this one likely overlap — worth scoping
   together rather than as two independent tasks.
4. **Skill-eval evidence for the SKILL.md/implementer-prompt.md rewrite.**
   This repo's own CLAUDE.md requires eval-harness evidence (via
   `superpowers:writing-skills`, adversarial pressure testing across
   multiple sessions) for changes to behavior-shaping skill content.
   Phase 1's SDD rewiring shipped without this. Raised by cubic-dev-ai,
   won't-fixed on PR #2 as a separate follow-up, now Phase 2 scope. Note:
   this repo's own eval harness lives in `evals/` (see root `CLAUDE.md`'s
   "Eval harness" section) — read that before starting this item.
5. **TDD-under-delegation is structurally weaker than before the fork.**
   Not a bug to fix outright, but worth deciding whether Phase 2 does
   anything about it. The implementer subagent can no longer
   independently verify RED-before-GREEN (no Edit/Write tools to run a
   failing test itself), so `delegate_implementation`'s backend is
   solely responsible for TDD discipline, and the subagent's report can
   only state what evidence it observed after the fact. Currently
   documented as an accepted trade-off in `implementer-prompt.md`'s
   Report Format section. Revisit only if it causes a real problem in
   practice (e.g. during item 1's smoke test) — don't preemptively
   redesign it.
6. **NEW, found during item 1's smoke test: `stall_timeout_seconds`'s
   300s default may be too tight for a cold-loading large local model.**
   The first real `delegate_implementation` call against
   `ollama/qwen3-coder:30b` (18GB) stalled with zero output for the full
   300s and gave up — `ollama ps` showed nothing loaded into memory right
   before the call, so the likely cause is cold-load time alone (before
   any token is generated) exceeding the stall window, not a hung/broken
   model. The stall-detection mechanism itself worked correctly (clean
   failure, no hang, no crash) — this is a config-default/guidance gap,
   not a code bug. Consider for Phase 2: document the cold-load risk in
   the README, recommend always setting `fallback_models` for large
   primary models, and/or evaluate raising the default. Don't fix
   speculatively — see how the fallback retry (in progress as of this
   handoff) behaves first; if `qwen2.5-coder:7b` also stalls, that points
   at a different problem entirely (e.g. Ollama itself under load) and
   changes what fix actually makes sense.
7. **NEW, explicitly requested and explicitly scope-merged: design a real
   mid-flight communication channel for `delegate_implementation`,
   covering BOTH live visibility and interactive prompt-answering as one
   problem, not two.** This item started as two separate findings this
   session and was deliberately merged, because both symptoms trace to
   the same root gap: `delegate_implementation` is a single opaque,
   synchronous call with no way to talk to the user WHILE it runs, only
   before (the task description) and after (the final result).
   - **Visibility symptom:** the `tail -f local-coder-output.log`
     workaround built this session (commit `b3835fb`) works, but is
     explicitly acknowledged as NOT a real feature — no realistic user
     opens a second terminal and manually tails a file to check if a
     delegation is progressing or stuck. Real visibility belongs INSIDE
     the same Claude Code conversation that triggered the delegation,
     surfaced natively as the call progresses (e.g. the existing
     `on_tick`/`ctx.report_progress` heartbeat, currently just "still
     running (model)...", could instead carry the actual latest output
     chunk(s), so real progress shows up in-chat with no file, no second
     terminal, no tailing).
   - **Interactivity symptom:** a real hang was found and fixed this
     session (`stdin=subprocess.DEVNULL`, commit `620cfc0`) — aider was
     inheriting the MCP server's own stdin (Claude Code's stdio JSON-RPC
     pipe, which never sends EOF) and blocked forever on any read
     attempt, uncaught by the stall-timeout mechanism (which only
     watches output, not "is this process actually stuck"). That fix
     converts a silent infinite hang into a fast, clean failure — any
     stdin read now gets immediate EOF — but if aider hits a prompt
     `--yes` does NOT auto-answer (a genuine judgment call, not a
     default yes/no), the run now fails fast with the prompt text
     captured rather than hanging, but nothing lets a human actually
     ANSWER that prompt and let the run continue.
   - **Both need the same underlying capability**: some way for
     `delegate_implementation` to communicate with the user WHILE
     running, not just via a final return value. A real design should
     solve both symptoms with one mechanism, not build a progress-only
     fix and a separate prompt-answering fix that later need
     reconciling.
   - **Real constraints to design against, learned the hard way this
     session** (still apply with the merged scope):
     - `delegate_implementation` is currently single request/response —
       no existing pause/resume mechanism mid-call.
     - The subprocess's stdin is intentionally severed
       (`subprocess.DEVNULL`) because leaving it attached to the MCP
       server's own stdin causes real, silent hangs — any redesign that
       reopens a stdin path for the backend must NOT simply revert that
       fix; it needs a deliberately managed pipe the server itself
       writes to only when a real answer is ready, not the server's
       inherited stdin.
     - FastMCP's `Context` already supports `report_progress` (used
       today for the on_tick heartbeat) — investigate whether it or a
       similar mechanism (elicitation, sampling, or another FastMCP
       primitive) supports genuine bidirectional communication, or
       whether this needs a custom protocol on top (e.g. pause, return a
       "needs input" response with the prompt text, expose a new
       `answer_prompt` MCP tool, resume).
     - Consider scope carefully before building the interactive-answer
       half specifically: how often does aider actually hit an
       unanswerable prompt with `--yes` in practice? If genuinely rare,
       a lighter-weight fallback (a documented manual recovery path —
       "if delegate_implementation fails with a prompt-related error,
       re-run aider manually against the same branch with the answer
       baked into a modified task description") may be proportionate for
       THAT half even if the visibility half still gets built properly.
       Investigate real frequency before committing to the heavier
       design for interactivity; visibility is worth building regardless
       of frequency, since it's needed on every successful run too, not
       just the rare stuck one.

### Item 1 attempt — findings and next step (2026-07-22)

Verified environment was ready first: `ollama list` has `qwen3-coder:30b`
pulled (the config default), `aider` 0.86.2 on PATH, `gh` authenticated,
the server's venv (`mcp-servers/local-coder/.venv/`) has fastmcp 3.4.4
installed and `server.py` imports cleanly. None of that was the problem.

**The actual blocker:** `claude mcp list` in this session shows:
```
local-coder: ${CLAUDE_PLUGIN_ROOT}/mcp-servers/local-coder/.venv/bin/python ... - ⏸ Pending approval
```
and the diagnostics report `Missing environment variables:
CLAUDE_PLUGIN_ROOT` for `.mcp.json`. Confirmed with `env | grep
CLAUDE_PLUGIN_ROOT` (empty). This session has the **official**
`superpowers@claude-plugins-official` plugin installed (see
`~/.claude/settings.json`'s `enabledPlugins`), not this fork — so
`CLAUDE_PLUGIN_ROOT` never points at this checkout, and the
`mcp__local-coder__*` tools are not available to call (confirmed via
`ToolSearch`, no match).

This can't be fixed by exporting the env var in a shell — Claude Code
resolves `.mcp.json` once, before/outside the running session, so a
`export CLAUDE_PLUGIN_ROOT=...` in Bash has no effect on the
already-resolved MCP client config. Manually running
`server.py`'s `_delegate_implementation_impl` directly via a Python
script was considered and explicitly rejected: it would prove the
backend logic works (already covered by the 101-test suite), but would
**not** test the actual thing Part 3 verifies — Claude Code's real
subagent-dispatch → tool-restricted implementer → MCP call chain. Faking
that with a script would produce a false "smoke test passed" result.

**Confirmed fix path — this repo IS structured to self-install:**
`.claude-plugin/marketplace.json` at repo root defines a `superpowers-dev`
marketplace with one plugin (`superpowers`, `source: "./"`) — this fork
ships everything needed to install itself locally. **This requires a
FRESH Claude Code session** (a plugin install needs a session restart to
take effect, and the smoke test itself needs to be the acceptance test
described in root `CLAUDE.md` — a clean session). Exact steps for that
fresh session:

```bash
claude plugin marketplace add /Users/niravthakker/Downloads/Nirav/Personal/Coding/superpowers-local-coder/.worktrees/local-coder-impl --scope project
claude plugin install superpowers@superpowers-dev --scope project
```
Then restart/start a new `claude` session from
`.worktrees/local-coder-impl/`, confirm `claude mcp list` shows
`local-coder` connected (not "Pending approval" / missing env var), then
run the actual Part 3 smoke test: ask it to brainstorm+plan+implement a
one-line change (e.g. "add a one-line comment to README.md") via
`subagent-driven-development`, and confirm `delegate_implementation` is
what implements it, not direct Edit/Write.

**UPDATE 2026-07-22, later same day — plugin install fix confirmed
working, MCP connection UNBLOCKED.** The install genuinely worked, but
one path detail bit us: `claude plugin marketplace add`/`install
--scope project` write their enablement record to **the main repo
root's** `.claude/settings.json`
(`/Users/niravthakker/Downloads/Nirav/Personal/Coding/superpowers-local-coder/.claude/settings.json`),
not into the worktree — this is git-worktree-shared `--scope project`
behavior in Claude Code, not a bug: all worktrees of one repo share one
project-scope settings file at the git common dir. A session started
`cd`'d into `.worktrees/local-coder-impl/` before running `claude`
didn't pick up the enablement, so `local-coder` still failed
(`CLAUDE_PLUGIN_ROOT` still missing) on the first retry. **Fix: start
the session from the MAIN REPO ROOT
(`/Users/niravthakker/Downloads/Nirav/Personal/Coding/superpowers-local-coder`,
currently on `dev`, which already has everything from the merged PR),
not from the worktree.** Confirmed via `claude mcp list` in that
session:
```
local-coder (worktree copy)              ✔ Connected
local-coder (plugin, .mcp.json)          ✘ Failed to connect — CLAUDE_PLUGIN_ROOT env var missing
```
The second line is an expected, harmless duplicate — the client is
showing two resolutions of the same server name (one via the installed
plugin, which now works; one via any lingering plain `.mcp.json`
reference, which still can't resolve `CLAUDE_PLUGIN_ROOT` on its own).
**The plugin-sourced `local-coder` is what matters and it is live.**
`installed_plugins.json` now correctly shows
`superpowers@superpowers-dev` registered; `.claude/settings.json` at the
main repo root has `enabledPlugins: {"superpowers@superpowers-dev":
true}` and `extraKnownMarketplaces.superpowers-dev` pointing at the
worktree path as the marketplace source. No uninstall/reinstall was
needed once the directory mismatch was understood — the original install
was correct all along.

**Smoke test attempt #1 — correctly declined, not a bug.** Asked the
connected repo-root session: "add a one-line comment to README.md, use
subagent-driven-development." It loaded the `subagent-driven-development`
skill successfully, then judged (correctly) that a one-line doc edit is
too trivial to justify SDD's machinery (worktrees, plan files, per-task
implementer/reviewer dispatch, ledgers) and offered to just make the
edit directly instead of forcing the process. This is the skill working
as intended, not a failure — but it means `delegate_implementation` was
never actually called, so the smoke test still hasn't run.

**Smoke test attempt #2 — IN PROGRESS AS OF THIS UPDATE, looking
genuinely healthy.** Sent the Quick Reference task from the "next
action" note. The repo-root session again offered to skip SDD given its
small size; user confirmed "delegate directly, skip SDD scaffolding" —
a deliberate, reasonable simplification of the original ask (this is
functionally still exercising the exact thing item 1 needs to verify:
`delegate_implementation` really invoking aider against local Ollama —
just without the full brainstorm→plan→implementer-subagent→reviewer
ceremony around it, since that ceremony isn't what's in question here).
The session then: read the current README, confirmed `local-coder` was
reachable, called `mcp__local-coder__delegate_implementation` with
`target_repo_path` explicit, and is now waiting on it in the background
rather than polling.

**Confirmed via `ps aux` from a separate terminal (not the Claude Code
session itself) that this is REAL, not a stall or hallucinated tool
call:**
- `server.py` (the local-coder MCP server) is running as a real
  background process, PID 79730.
- It has actually spawned a real `aider` subprocess, PID 86750:
  `aider --model ollama/qwen3-coder:30b --yes --message "..."`, with the
  exact synthesized task text (add a "## Local-Coder Quick Reference"
  section, given verbatim content, restricted to that one file/section).
- Ollama (`ollama serve`) is running and available to serve the request.
- As of this check, `mcp-servers/local-coder/README.md` has NOT yet been
  modified (checked both the repo-root copy and the worktree copy) —
  aider is still mid-run, not stuck; 30B local models take real wall-clock
  time for a real inference pass, this is expected, not a hang.

**A browser tab opened to `https://aider.chat/docs/llms/warnings.html`
during this run — this is normal aider behavior, not an error.** Aider
ships a `--show-model-warnings` flag (`True` by default, confirmed via
`aider --help`) that opens this docs page when it wants to flag a
model/provider quirk for an unfamiliar or unusual model — it is NOT a
sign delegate_implementation is broken, and does NOT mean aider stopped
running (the process was still alive and burning CPU when checked).

**If resuming: check `ps aux | grep aider` first.** If the aider PID
from this note is still running, wait for it — do not re-send the task
or assume it's stuck. If it's gone, check whether
`mcp-servers/local-coder/README.md` was modified (`git status` /
`git diff` in the repo-root checkout, which is where this attempt is
running, on `dev` directly) — a modified README with no corresponding
commit likely means aider finished editing but
`delegate_implementation`'s own auto-commit step hasn't run yet or
failed; a modified+committed README means it fully succeeded and this
item can be marked COMPLETE (record the commit SHA, branch, and whether
a PR was offered); no modification at all with the process gone likely
means it failed silently and needs investigating fresh (check the
session's own conversation for the tool result, don't just re-run
blindly).

**Smoke test attempt #2, first delegate_implementation call — FAILED,
but usefully (stall, not a crash).** The MCP tool call
(`kruub07t`) completed and returned `success: false`:
`ollama/qwen3-coder:30b` stalled with **zero output for the full 300s
`stall_timeout_seconds`**, no `fallback_models` were configured, so the
call gave up cleanly with a clear error rather than hanging or crashing.
This is a genuinely valuable result even though the task didn't
complete: it's real evidence the stall-detection/no-fallback-configured
path in `delegate_implementation` works exactly as designed. Checked
`ollama ps` at the time — **nothing was loaded into memory** — so the
most likely explanation is a cold-load of an 18GB/30B model exceeding
300s before producing a single token (the stall detector watches for
*output*, and cold-load time produces none). This is a real Phase 2
finding worth its own line item: **`stall_timeout_seconds`'s 300s
default may be too tight for a cold-loading large local model** — not
raised as a bug in Phase 1's code (the mechanism did exactly what it was
built to do), but as a possible config-default/README-guidance gap for
Phase 2 to consider (e.g. documenting that first-use-after-idle can be
slow, or raising the default, or recommending fallback_models always be
set for large primary models).

**Attempt #2 retry — configure fallback, retry — IN PROGRESS AS OF THIS
UPDATE.** Decision made: rather than a bare retry (doesn't test
failover, risks same stall for an unrelated reason) or abandoning
delegation (would leave item 1 still never having succeeded), configure
`ollama/qwen2.5-coder:7b` (already pulled, small/fast) as
`fallback_models`, keep `qwen3-coder:30b` as primary, then retry the
same README task. Told to the repo-root session as:
```
Configure qwen2.5-coder:7b as a fallback model (call configure with
fallback_models: ["ollama/qwen2.5-coder:7b"], keep qwen3-coder:30b as
primary), then retry the delegation for the same README task.
```
**Result of this retry not yet seen as of this handoff update.**

**IMPORTANT discovery while checking on this: the running local-coder
MCP server reads/writes the WORKTREE's `config.yaml`
(`.worktrees/local-coder-impl/mcp-servers/local-coder/config.yaml`), NOT
the repo-root checkout's copy — even though the Claude Code session
issuing the `configure`/`delegate_implementation` calls has its shell
`cd`'d into the repo root.** Confirmed by diffing the two files: the
repo-root copy is still untouched (`fallback_models: []`,
`target_repo_path: null`); the worktree copy now has
`fallback_models: [ollama/qwen2.5-coder:7b]` and
`target_repo_path: /Users/niravthakker/Downloads/Nirav/Personal/Coding/superpowers-local-coder`
(the repo root's own absolute path, written by the `configure` call
presumably resolving its own cwd). This makes sense once you trace it:
`${CLAUDE_PLUGIN_ROOT}` resolves to wherever the plugin's marketplace
`source` points — which is the worktree
(`.claude-plugin/marketplace.json`'s `source: "./"` combined with the
marketplace being added FROM the worktree path) — so the server process
itself, and everything it reads/writes including `config.yaml`, lives in
the worktree regardless of which directory the chat session's own shell
happens to be in. **Practical implication: from now on, always check
`.worktrees/local-coder-impl/mcp-servers/local-coder/config.yaml` for
the live config, not the repo-root copy — the repo-root copy is
effectively dead/unused as long as the plugin is installed from the
worktree.**

**Do NOT hand-edit `config.yaml` while a `delegate_implementation`/
`configure` call may still be in flight against it** — the running
server process owns reads/writes to this file mid-call; editing it
concurrently risks a race. (This was respected: the config cleanup
below only happened after confirming via `ps aux` that nothing was
still running against it — see "`config.yaml` cleanup — DONE" further
down for the final outcome, which ended up reverting BOTH fields, not
just `target_repo_path` as originally planned here.)

**Retry outcome: INCONCLUSIVE — interrupted deliberately, not a failure
or a stall.** The retry (primary `qwen3-coder:30b`, fallback
`qwen2.5-coder:7b`) was mid-run — confirmed via `ps aux` it had actually
failed over to `qwen2.5-coder:7b` (the process's own argv showed
`--model ollama/qwen2.5-coder:7b`, proving the failover path fired for
real) — when the user raised a legitimate concern: **running aider fully
headless, with zero live visibility into what it's doing, is genuinely
concerning, not just a minor inconvenience.** User chose to stop that
run and fix visibility before continuing rather than let it finish and
get today's pass/fail signal. `README.md` was never modified by either
attempt, so item 1 is still not complete — but this is now considered
correctly paused, not stalled/broken.

**Visibility gap — FIXED, commit `870cb9a`.** Root cause: aider's
stdout/stderr was fully captured by `run_monitored_subprocess` (bounded
20K-char tail) but never surfaced anywhere live — not to a terminal, not
to the MCP client, not even via the existing `on_tick` progress hook
(which only ever printed a generic "still running" heartbeat with no
access to the actual output). The only visibility was the tail dumped
into an error message after the whole call already finished or failed.

**Fix implemented, full TDD (106/106 tests passing):** added
`on_output: Callable[[str], None] | None` to
`run_monitored_subprocess` in `backends/common.py`, fired with each
decoded chunk as it's read (main loop + the post-exit drain loop) —
additive, backward-compatible, existing callers unaffected. Threaded
through `BackendAdapter.run_backend`'s abstract interface and all 4
backend implementations/stubs (aider real, codex/gemini/openrouter
stubs). Wired in `server.py`'s `make_on_output` factory (mirroring the
existing `make_on_tick`) to `print()` each chunk to the **local-coder
MCP server's own stderr** immediately, prefixed with the model name
(`[local-coder:ollama/qwen3-coder:30b] <chunk>`), alongside the existing
on_tick heartbeat. Manually verified end-to-end with a real subprocess
(not just unit tests) that chunks stream through as they arrive, not
buffered until exit.

**"Where do you watch stderr" OPEN QUESTION — ANSWERED (the answer is:
you can't, so a log file was added instead).** Investigated via `lsof`
on the running server process: Claude Code pipes an MCP server's stdout/
stderr over an internal unix socket it owns directly (the socket's other
end is held by the `claude` binary's own PID) — there is no external tap
point without root/sudo access, which this session doesn't have. Also
tested `claude --debug-file <path>`: confirmed it captures Claude Code's
own tool-dispatch lifecycle (`Calling MCP tool: delegate_implementation`,
`still running (Ns elapsed)`, etc.) but NOT the actual subprocess output
`on_output` prints — so it does not solve this. **Fix: `on_output` now
ALSO writes to a fixed log file,
`mcp-servers/local-coder/local-coder-output.log`** (gitignored, in the
worktree next to `server.py`), truncated fresh at the start of every
`delegate_implementation` call, alongside the existing stderr print.
`tail -f .worktrees/local-coder-impl/mcp-servers/local-coder/local-coder-output.log`
in a separate terminal is the confirmed, verified way to watch a
delegated backend's real output live going forward. Committed as
`b3835fb` (TDD, 3 new tests, all genuinely RED-before/GREEN-after).

**MUCH more important: attempting to actually USE this watch-live setup
surfaced a real, previously-undiscovered bug — a genuine hang, not a
slow model.** While waiting to watch the retry (before the log-file fix
had even been tried), the delegation call sat at "still running" for
480+ seconds — well past the 300s `stall_timeout_seconds` — with no
failover and no error. Investigated with `ps aux` (the aider process had
consumed only ~5s of CPU across 4+ minutes of wall-clock time — a strong
signal of "blocked," not "slow") and `sample` (macOS's built-in
non-invasive stack sampler, no sudo needed): **the aider subprocess's
main thread was parked in a `read()` syscall under
`builtin_input_impl`/`PyFile_GetLine` — genuinely blocked trying to read
from stdin.** Root cause: `run_monitored_subprocess`'s `Popen` call never
set `stdin=`, so the backend subprocess inherited THIS SERVER'S OWN
stdin — the MCP stdio JSON-RPC pipe from Claude Code, which is an open
pipe that receives data but never sends EOF. Whatever caused aider to
attempt a stdin read (despite `--yes`) then blocked forever, and — this
is the important part — **the stall-timeout mechanism did not catch it**,
because that mechanism only watches for OUTPUT activity; a process
blocked reading stdin can still look "recently active" from earlier
startup output, so the stall timer's clock never restarts and never
fires. This was a real, unbounded hang with no automatic recovery path.

**Fixed: `stdin=subprocess.DEVNULL`** added to the `Popen` call, severing
the child from the parent's stdin entirely — any read attempt now gets
immediate EOF instead of blocking. TDD note worth preserving: the
straightforward version of this test passed even against the buggy code
(pytest's own stdin is already non-blocking in this environment, so it
didn't reproduce the bug) — had to construct the actual failure scenario
directly (a real open pipe, held open with no EOF, dup2'd onto a forked
child's stdin before calling the function under test) to get a genuine
RED result (`STALLED`) before the fix and GREEN (immediate EOF) after.
Committed as `620cfc0`. **110/110 tests passing.**

**`config.yaml` cleanup — DONE, same procedure as before.** Confirmed via
`ps aux` that nothing was running (the hung process had been killed —
see below), then `git checkout -- config.yaml` to fully revert the
drift (both `fallback_models` and `target_repo_path`) back to match
`dev`. `git diff config.yaml` shows zero changes.

**The hung aider process (PID 8099) was killed manually** (`kill 8099`)
rather than left to hang indefinitely, since no automatic recovery
existed before the stdin fix landed. **This means the
`delegate_implementation` MCP tool call in whatever Claude Code session
initiated it never received a normal return** — that call's parent
process (the local-coder server, PID 4335) also appears to have exited
around the same time (not confirmed why — possibly it noticed its child
died and exited, or the MCP connection itself dropped). **If resuming in
that same session: it likely needs to be restarted/reconnected** — check
`claude mcp list` for `local-coder`'s connection status, and if it shows
disconnected, that session needs a fresh reconnect (or a new session
entirely) before delegating again.

**If resuming: item 1 (the smoke test) has still never completed
successfully — but this time for a well-understood, now-fixed reason,
not an open question.** The next attempt should just work: (a) ensure
`local-coder` is connected in a session that has today's commits loaded
(a fresh session, or a reconnected one), (b) start
`tail -f mcp-servers/local-coder/local-coder-output.log` (from the
worktree) in a separate terminal BEFORE sending the task, (c) re-send
the same Quick Reference task (text preserved above under "Smoke test
attempt #2"), (d) watch the tail output live this time, (e) record
whether it completes. Both real gaps found this round (no live
visibility, the stdin hang) are now fixed and tested — there is no known
reason left for this to fail, but "no known reason" is not the same as
"verified working," which is exactly what item 1 still needs.

### Housekeeping for Phase 2

- **Open question: which directory should Phase 2 sessions actually run
  from?** The main repo root is on `dev` directly (not a worktree
  checkout of `local-coder-impl`) but is where `local-coder` connects,
  since that's where the plugin got enabled. The worktree at
  `.worktrees/local-coder-impl/` is where all the git history/commits in
  this handoff doc actually happened, and is the isolated branch Phase 2
  commits are meant to land on — but a session started there doesn't see
  `local-coder` as connected. **Until this is reconciled, treat them as
  two different jobs**: use the main-repo-root session (on `dev`) for
  anything that needs the actual `local-coder` MCP tools (the smoke test,
  any future manual delegate_implementation testing); keep using the
  worktree session for git/code/doc work on the `local-coder-impl`
  branch. Don't commit from the repo-root session while it's sitting on
  `dev` directly — check `git branch --show-current` before any commit
  there to avoid accidentally committing straight to `dev`.
- **Keep this handoff doc's "Where things stand NOW" section current.**
  Update it after every Phase 2 item completes or every time a session
  is about to end mid-work — the same discipline that governed Phase 1
  below.
- The `.superpowers/sdd/progress.md` ledger (git-ignored, local-only) was
  Phase 1's task ledger. If Phase 2 work also goes through
  `subagent-driven-development`, either reuse it (noting the phase
  boundary) or start a fresh one — decide when Phase 2's first task
  actually kicks off, don't decide preemptively here.
- All of Phase 1's history (execution gotchas, the 5 rounds of
  review-bot fix cycles, key design decisions, environment facts) is
  preserved below under "Archive: Phase 1" — it's still useful context
  (e.g. the `restore_working_tree` fix history matters if item 1's smoke
  test surfaces a NEW bug in that function — see the explicit escalation
  note in the archive about not auto-patching a 6th time), just no
  longer the first thing a resuming session needs to read.

### Resume prompt (paste this if a session ends mid-work / hits its limit)

**Two different directories are in play right now — read this before
picking which one to resume in:**
- **Main repo root** (`/Users/niravthakker/Downloads/Nirav/Personal/Coding/superpowers-local-coder`,
  currently on `dev`) — this is where the `local-coder` plugin is
  installed/connected. **Use this one to check on or re-run the smoke
  test** (see "Smoke test attempt #2" above for the exact task to send).
  Do NOT commit here without first checking `git branch --show-current`
  — it's sitting on `dev` directly, not a feature branch.
- **Worktree** (`.worktrees/local-coder-impl/`, branch `local-coder-impl`)
  — this is where all git history/commits for this project have
  actually happened, and where Phase 2 code/doc changes belong.
  `local-coder` is NOT connected in a session started here (see the
  "Housekeeping for Phase 2" open question above) — don't try to run the
  smoke test from this one.

If you don't know which one the smoke test's result landed in, start
with the **main repo root** session (that's where attempt #2 was sent)
and paste this:

```
Read docs/superpowers/handoff/2026-07-21-local-coder-handoff.md in the
superpowers-local-coder repo and resume Phase 2 work from exactly where
it left off, per the "Where things stand NOW" section at the top. TWO
real fixes landed this round, both DONE and tested — do not redo either:
(1) on_output now also writes to a log file,
mcp-servers/local-coder/local-coder-output.log (commit b3835fb), since
Claude Code owns the MCP server's stderr internally with no external tap
point; (2) a genuine hang bug is fixed — run_monitored_subprocess's
Popen call now sets stdin=subprocess.DEVNULL, because the backend
subprocess was inheriting the MCP server's own stdin (an open pipe that
never sends EOF) and could block forever reading it, past the stall
timeout, with no recovery (commit 620cfc0). What's still open: item 1,
the Part 3 smoke test, has still never completed successfully. Re-send
the Quick Reference smoke-test task (exact text preserved in the doc
under "Smoke test attempt #2") to a session where local-coder is
connected (check `claude mcp list` first — a prior session's hung
delegate_implementation call may have left it disconnected), and START
`tail -f mcp-servers/local-coder/local-coder-output.log` (from the
worktree) in a separate terminal BEFORE sending the task this time, so
you actually watch it run rather than waiting blind. Record whether it
finally completes. Don't re-derive context from git log or re-read the
design spec/plan from scratch — the handoff doc is the current source of
truth. Keep it updated as you go. Note: a session at the main repo root
sits on `dev` directly — do not commit anything there without switching
branches first; actual code/doc commits belong in the worktree at
.worktrees/local-coder-impl/ on branch local-coder-impl, which is also
where the LIVE config.yaml and the new log file actually live (see the
doc's note on CLAUDE_PLUGIN_ROOT resolving to the worktree, not the repo
root).
```

If the plugin-connection blocker somehow regresses (e.g. `local-coder`
shows disconnected again), the doc already contains the exact `claude
plugin marketplace add` / `claude plugin install` commands and the
directory-mismatch fix (start from repo root, not the worktree) under
"Item 1 attempt — findings and next step" above.

---

## Archive: Phase 1 (complete, merged to `dev` via PR #2)

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

**PR #2 merged to `dev` at commit `70f8ba3` on 2026-07-22** (see
"Where things stand NOW" at the top of this file for current state).
`restore_working_tree` went through 5 review-driven fix rounds during
Phase 1. **If any NEW finding shows up against that same function in a
future session (e.g. during Phase 2 item 1's smoke test), stop and raise
it with the repo owner before fixing** — don't keep patching
indefinitely; either the function needs a more fundamental rethink, or
review-bot findings on it should stop being auto-actioned without a
cost/benefit check first.

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
on branch `local-coder-impl`, pushed to `origin`. **PR #2 merged to `dev`
on 2026-07-22** (commit `70f8ba3`) — see "Where things stand NOW" at the
top of this file. The branch/worktree was reused (fast-forwarded to
match `dev` post-merge) rather than torn down, since Phase 2 continues on
it directly, per explicit direction.

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
