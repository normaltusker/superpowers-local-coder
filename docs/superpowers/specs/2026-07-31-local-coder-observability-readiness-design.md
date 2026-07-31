# local-coder Observability & Readiness (Sync) — Design

**Date:** 2026-07-31
**Status:** Approved (brainstorming), pending implementation plan
**Scope:** Add pollable status, phase visibility, a readiness-check command, and
orphan cleanup to the *synchronous* local-coder MCP delegation path. No async
conversion.

## Motivation

A local-coder delegation runs a slow local model (e.g. `qwen2.5-coder:7b` via
aider + ollama) inside a **synchronous** MCP tool call. During that call the
delegation is a black box: the invoking subagent is blocked inside the tool, and
CC's native background-tasks pane never shows it (CC only tracks processes it
spawns itself — the aider subprocess runs inside the MCP server, which CC did not
launch as a tracked task). The only pain that slowness actually causes here is
*lack of visibility*, not timeouts — so the fix is observability, not async.

Several ideas from the `openai/codex-plugin-cc` plugin transfer cleanly to a sync
model. This design pulls in the sync-compatible subset:

- Live, pollable **status record** (file-based, cross-process).
- A coarse-but-robust **phase** model plus a best-effort **latest-activity** line.
- A **`/local-coder:setup`** readiness check.
- A **`/local-coder:status`** command.
- **Orphan cleanup** (SessionEnd kill + startup sweep).
- **Workspace-keyed** state directory.

Explicitly *not* pulled in (async- or Codex-workflow-specific): detached broker,
`/cancel`, `/transfer`, `/review`, `/adversarial-review`, the stop-review gate,
and the rescue subagent.

## Non-Goals

- No conversion to an async submit+poll tool contract. The tool stays
  synchronous; `delegate_implementation` still returns the final result.
- No change to the tuned SDD `implementer-prompt.md` contract.
- No native CC background-tasks-pane integration (that requires async).
- No remote/cloud calls and no handling of API credentials of any kind. The
  only network probe is a local, unauthenticated ollama daemon check on
  `http://localhost:11434`.

## Architecture

A new **isolated** module `mcp-servers/local-coder/status.py` owns all record
read/write, phase tracking, monotonic dedupe, and the orphan sweep. It mirrors
Codex's `state.mjs` + `tracked-jobs.mjs`, adapted to Python and to a synchronous
run model. `server.py` receives only minimal **additive** hooks — no existing,
review-hardened logic is rewritten.

Three artifacts per delegation, under a workspace-keyed state directory:

- `state.json` — index of recent delegations, capped and pruned by `updatedAt`.
  Writes go through the existing `config._config_lock()` for cross-process safety.
- `<job-id>.json` — the per-delegation record (full shape below).
- the existing per-call **output log** — unchanged; the record references it by
  path.

**State directory:**
`${CLAUDE_PLUGIN_DATA}/status/<slug>-<sha256(realpath(target_repo))[:16]>`

where `<slug>` is the sanitized basename of the target repo. This isolates
concurrent multi-repo delegations. If `CLAUDE_PLUGIN_DATA` is unset, fall back to
a `codex-companion`-style tmpdir root (consistent with the server's existing
fallback patterns).

**Consumers (both):**

1. **Live (out-of-band):** the human, from a *separate* terminal/shell, via
   `/local-coder:status` or by tailing the output log, while a delegation blocks
   the SDD session.
2. **Post-hoc (in-band):** the record survives the call as a durable trace the
   SDD flow can read after the tool returns.

## Status Record

`<job-id>.json`:

```json
{
  "id": "lc-<base36-time>-<rand>",
  "sessionId": "<CC session id if available, else absent>",
  "status": "running | completed | failed | orphaned",
  "phase":  "starting | working | committing | done | failed",
  "latestActivity": "<best-effort last complete log line>",
  "model": "qwen2.5-coder:7b",
  "targetRepo": "/abs/path",
  "branch": "...",
  "pid": 12345,
  "outputLog": "/abs/path/to/per-call.log",
  "createdAt": "ISO-8601",
  "updatedAt": "ISO-8601",
  "completedAt": "ISO-8601 (on finish)",
  "commitSha": "<on success>",
  "errorMessage": "<on failure>"
}
```

`pid` is populated while the aider subprocess runs and nulled at finalize.

## Phase Model (Hybrid)

aider emits **unstructured text**, not a structured event protocol like Codex's
app-server. So phase classification is deliberately **coarse and lifecycle-driven**,
not regex-on-output — regex over aider's wording would rot when aider changes its
output format.

Phase backbone (driven by subprocess lifecycle + git HEAD movement):

- `starting` — record created, no output produced yet.
- `working` — first `on_output` chunk seen (subprocess is producing output).
- `committing` — HEAD movement detected at finalize. Honestly best-effort: with
  self-committing aider we cannot reliably observe the commit *start*, only that
  HEAD moved by finalize, so this phase may be brief or skipped. We prefer an
  honest coarse phase to a faked granular one.
- `done` / `failed` — terminal.

The informative layer is **`latestActivity`**: the most recent *complete* log
line, reusing the `latest_line` value `server.py` already captures in `on_tick`.
This is free text ("what's happening now"), never parsed into a phase enum — zero
new brittleness. When aider prints `Applied edit to foo.py`, that string surfaces
as data, not as a classified phase.

**Monotonic dedupe:** the record is rewritten only when `phase` OR
`latestActivity` changes (Codex's `createJobProgressUpdater` pattern), so raw
chunk streaming does not rewrite the file on every read.

## server.py Integration (Additive Only)

Exactly four touch-points, none rewriting existing logic:

1. **Delegation start** (after `output_log_path` is created, before the backend
   runs): `status.create_record(...)` writes `<job-id>.json`
   (`status=running`, `phase=starting`, `pid=null`) and upserts `state.json`.
   Also runs the cheap **startup sweep** (below).
2. **Inside existing `make_on_output`** (after the current log append): one
   `updater.on_output(chunk)` call flips `starting` → `working` on the first
   chunk. Deduped: no write unless the phase changed.
3. **Inside existing `make_on_tick`** (after the current `latest_line` capture):
   `updater.on_activity(latest_line)` rewrites the record only when the line
   changed. This is where `latestActivity` updates, piggybacking on the
   complete-line parsing already done there.
4. **In the existing `try/finally`** around the backend run:
   `status.finalize(record, result)` sets terminal `status`/`phase`, `commitSha`
   or `errorMessage`, `pid=null`, `completedAt`. It sits alongside the existing
   `_ACTIVE_OUTPUT_LOGS.discard(...)` cleanup and runs on success, failure, and
   exception — the same guarantee as the existing working-tree restore.

**PID capture:** the child PID must reach the record. `common.run_monitored_subprocess`
gains one **optional** `on_start(pid)` callback, invoked right after `Popen`,
threaded through exactly like the existing `on_tick`/`on_output`. `server.py`
passes a callback that patches the record's `pid`. Existing callers that omit
`on_start` are unaffected. This is the only change reaching into `common.py`.

**Guard discipline:** every status write is wrapped like the existing
announce/tick/output guards — a status-write failure is **non-fatal**: it warns
and continues and NEVER kills the delegation. Status is observability, not the
primary result channel.

## Commands

Both commands follow the proven Codex pattern: `disable-model-invocation: true`
(user-only; the agent cannot auto-fire them), a tight `allowed-tools` allowlist,
and `!` command-expansion so the output is injected at prompt-expansion time with
zero agent tokens spent fetching it — the agent only formats the result.

Both invoke a new CLI `mcp-servers/local-coder/status_cli.py`, run via the venv
python (falling back to system `python3`), reusing the launcher's interpreter
resolution.

### `/local-coder:status [job-id] [--all]` — `commands/status.md`

- No arg: compact Markdown table of the current session's delegations
  (id, status, phase, latest activity, elapsed, model, output-log path).
- With `job-id`: full record dump, unsummarized.
- `--all`: include other sessions.
- `allowed-tools`: `Bash(<python> status_cli.py:*)` only.

### `/local-coder:setup` — `commands/setup.md`

Runs four readiness checks; prints pass/fail plus an actionable next-step per
failure:

| Check | How | Fail → next-step |
|---|---|---|
| venv provisioned | reuse `ensure-local-coder-venv` stamp logic | venv will auto-provision on next delegation |
| aider importable | venv python `-c "import aider"` | pip install failed; check scipy/dyld on macOS 27 |
| ollama reachable + model | GET `http://localhost:11434/api/tags`; assert configured model present | start ollama / `ollama pull <model>` |
| config valid | parse `config.yaml`; sanity-check model + stall-timeout fields | fix the named field in `config.yaml` |

**Security:** the ollama check targets `http://localhost:11434` **only** — local,
unauthenticated. No remote endpoint, no token/credential handling. This is
consistent with the standing no-API-credentials constraint.

## Orphan Cleanup (Both)

New hook `hooks/cleanup-local-coder-status`, wired into `hooks.json` under
`SessionEnd`.

- **SessionEnd (live kill):** read this session's records
  (`sessionId` match) with `status=running` and a live `pid`; process-tree
  terminate each (Codex `terminateProcessTree` pattern); mark the record
  `orphaned`. Bounded by a timeout so SessionEnd never hangs.
- **Startup sweep (reconcile):** at delegation start (inside
  `status.create_record`, cheap), scan records; any `status=running` whose `pid`
  is no longer alive is marked `orphaned`. This cleans stale state left by a hard
  crash where SessionEnd never fired.

## Wiring Summary

- New: `mcp-servers/local-coder/status.py` (record + phase + dedupe + sweep).
- New: `mcp-servers/local-coder/status_cli.py` (status render + setup checks).
- Edit: `server.py` — four additive, guarded hooks.
- Edit: `backends/common.py` — one optional `on_start(pid)` param.
- New: `commands/status.md`, `commands/setup.md` (Codex-pattern plugin commands;
  the plugin already auto-loads `commands/*.md` — no `.mcp.json` change).
- New: `hooks/cleanup-local-coder-status`; edit `hooks/hooks.json` to add the
  `SessionEnd` entry.

## Error Handling

Every status and cleanup operation is non-fatal-guarded. Observability must never
break a delegation — the same discipline already applied to the announce, tick,
and output paths. State writes reuse `config._config_lock()` for cross-process
safety.

## Testing (TDD)

- `tests/test_status.py` — record create/update/finalize; monotonic dedupe (no
  write when unchanged); phase transitions; prune cap; orphan sweep marks a
  dead-PID `running` record `orphaned`; workspace-key isolation; `_config_lock`
  reuse under concurrency.
- `tests/test_server.py` (additions) — the four status hooks fire at their
  points; **a status-write failure is non-fatal** (inject a raising status
  writer; assert the delegation still succeeds) — the critical guard test.
- `tests/test_backends_common.py` (additions) — `on_start(pid)` fires with the
  real child PID; omitting the callback leaves existing behavior unchanged.
- `tests/hooks/test-cleanup-local-coder-status` — SessionEnd kills a live orphan
  and marks it `orphaned`; sweep reconciles a dead PID; no-op when nothing is
  running.
- `/setup` checks — unit-test each check's pass/fail and next-step message; the
  ollama check is mocked (no real daemon in CI) and asserts it only ever targets
  `localhost:11434`.
