# Mid-Flight Communication for `delegate_implementation` — Design Spec

**Status:** Proposed
**Phase:** Phase 2, item 7 (see the handoff doc's Phase 2 backlog)
**Objective:** give `delegate_implementation` a way to communicate with the
user *while it runs* — both to show real progress (visibility) and, later,
to ask genuine questions (interactivity) — instead of being a single opaque
call that only talks to the user before (the task description) and after
(the final result).

## Problem

`delegate_implementation` delegates a coding task to a backend subprocess
(aider today) that can run for minutes. During that window the user sees
nothing useful. Two concrete gaps, both observed in real Phase 2 sessions:

1. **Visibility.** The Phase 1 build ran aider fully headless. A stopgap
   this session (`on_output` → stderr + a per-call log file
   `local-coder-output.log`, commits `870cb9a`/`b3835fb`) lets a user
   `tail -f` the log, but that is not a real feature — no realistic user
   opens a second terminal and tails a file to check whether a delegation
   is progressing or stuck.
2. **Interactivity.** A genuine hang was found and fixed
   (`stdin=subprocess.DEVNULL`, commit `620cfc0`): aider inherited the MCP
   server's own stdin (an open JSON-RPC pipe that never sends EOF) and
   blocked forever reading it. The fix converts that infinite hang into a
   fast clean failure — but if aider hits a prompt `--yes` does not
   auto-answer (a real judgment call, not a default yes/no), the run now
   fails fast with the prompt text captured, and nothing lets the user
   actually answer it and continue.

Both trace to the same root gap: no mid-flight channel back to the user.

## What we verified empirically (probe results, 2026-07-22)

Before designing, a throwaway `elicit-probe` MCP server was built and driven
from a live Claude Code session. Findings (the probe has since been removed):

- **`ctx.report_progress()` renders live in Claude Code and is genuinely
  visible** as the call runs — but it is **ephemeral**: each progress
  message replaces the previous one in the UI; nothing persists in the
  transcript once superseded. This is inherent to the MCP progress-
  notification protocol (a single "current progress" state), not a Claude
  Code quirk, and cannot be changed by calling it differently.
- **`ctx.elicit()` works with Claude Code as the client and PERSISTS** —
  the question and the user's answer both remained as normal, permanent
  messages in the conversation. Confirmed round-trip: probe asked "favorite
  color?", Claude Code rendered an interactive form with Accept/Decline, the
  user answered "Pink", the tool received it back. This is the real
  bidirectional primitive.
- **MCP resources are pull-based.** A `probe://transcript` resource backed by
  live-updating data read correctly on demand, but there is no evidence
  Claude Code auto-refreshes a resource view when a
  `ResourceUpdatedNotification` fires, and "interrupt a running tool call to
  re-read a resource" is not a real user flow. Resources are therefore NOT a
  live-progress channel. (Not pursued further — decided empirically to
  design around "no automatic live UI update from resources.")

**Design consequence:** live visibility uses `report_progress` (works, but
ephemeral); durable visibility uses the final tool result (persists);
interactivity uses `elicit` (works, persists). No resource-based mechanism.

## Scope and phasing

This spec covers both symptoms as one design because they share the
"mid-flight channel" root, but they ship in two phases so the high-value,
low-risk half is not blocked on the harder half:

- **Phase 2a — Visibility (build now).** Needed on *every* run, not just the
  rare stuck one. Low risk. This is the bulk of this spec.
- **Phase 2b — Interactive prompt-answering (design now, build only if
  warranted).** Needed only when aider hits an unanswerable prompt, whose
  real-world frequency with `--yes` is unknown. Spec'd here so the mechanism
  is decided, but gated on observing that it actually happens — see
  "Phase 2b" below. Do not build 2b speculatively.

## Phase 2a — Visibility (build now)

Two complementary pieces. Neither replaces the other.

### 2a.1 Live pulse — upgrade the existing progress heartbeat

Today `make_on_tick` (server.py) fires every `idle_notify_interval_seconds`
and calls `ctx.report_progress(0, None, "Running {model}...")` — a generic
heartbeat with no real content. `make_on_output` already receives the actual
subprocess output chunks (it writes them to stderr + the log file) but does
NOT feed them into `report_progress`.

**Change:** carry the latest real output into the progress message.

- Maintain a small rolling buffer of the most recent output (e.g. the last
  non-empty line, or last ~200 chars — a single progress message should be
  short and readable, not a wall of text).
- `make_on_output` updates that buffer as chunks arrive.
- The periodic `on_tick` progress message becomes something like
  `"{model}: {latest_line}"` instead of the static "still running", so the
  user sees genuine forward motion (e.g. "Applying edit...", "Running
  tests...") in real time.
- Keep the tick cadence as the throttle. Do NOT call `report_progress` on
  every raw chunk — chunks can arrive in bursts, and the ephemeral UI would
  just flicker. The tick interval already exists precisely to bound how
  often the user-facing pulse updates; reuse it.

Rationale for reusing `on_tick`'s cadence rather than `on_output`'s raw rate:
`report_progress` is ephemeral, so emitting faster buys nothing visible; it
only adds notification traffic. The tick is the right clock for the pulse.

### 2a.2 Durable record — bounded output tail in the final result

Because `report_progress` is ephemeral, once the call finishes the live
pulse is gone. The user needs a durable record of what happened to remain
in the conversation. The final tool result already persists (it's normal
tool output) — so attach the captured output tail to it. Note this is the
**bounded** tail (the last `_MAX_OUTPUT_CHARS` = 20 000 chars), not a
complete transcript: a very long or chatty backend run's earliest output
is already dropped by the time the call ends. That is an accepted
trade-off (see the bound note below), not a full log.

**Change:** add an `output_tail` field to `delegate_implementation`'s
returned dict, on BOTH success and failure paths.

- Content: the bounded tail of the successful (or last-attempted) backend
  run's output — reuse the existing `_MAX_OUTPUT_CHARS` (20 000) bound that
  `run_monitored_subprocess` already maintains as `output_tail`. Do NOT
  introduce an unbounded full-transcript field; the existing tail bound was
  chosen deliberately and is the right size for "what happened, readable
  inline."
- The value is already computed — `run_monitored_subprocess` returns it as
  `CompletedProcess.stdout`, which `AiderBackend.run_backend` currently only
  uses for the error message on failure. It needs to be surfaced up through
  `CompletionResult` so `_delegate_implementation_impl` can put it in the
  response dict.
- On failure, the aggregated error already summarizes per-model failures;
  `output_tail` additionally gives the raw backend output for the *last*
  attempt so the user can see what the model was doing when it failed —
  **with one exception**: on a stall (`StallError`), the captured output
  up to the kill point is currently NOT retained (`AiderBackend`'s
  StallError path returns an empty `output_tail`), so a stalled last
  attempt yields `output_tail == ""`. Non-stall failures (non-zero exit,
  no-commits) do carry the tail. Retaining partial output through
  `StallError` is a possible future improvement, out of scope for this
  visibility pass; the stall case is already the one gap noted here.

**Interface change:** `CompletionResult` (backends/base.py) gains an
`output_tail: str = ""` field. `AiderBackend.run_backend` populates it from
`result.stdout` on both success and failure returns. Every backend
(including the `NotImplementedError` stubs, which never return a
`CompletionResult` anyway) is unaffected structurally since it's a defaulted
field.

### 2a.3 Log file: keep, demote

`OUTPUT_LOG_PATH`/`local-coder-output.log` stays (it's cheap, gitignored,
useful for deep debugging), but it is no longer the primary visibility
story. The README/docs should present the in-chat pulse + result transcript
as the normal experience and mention the log file only as a
power-user/debugging aside — NOT as "the way to watch a delegation".

## Phase 2b — Interactive prompt-answering (design now, build only if warranted)

**Mechanism (decided): `ctx.elicit()`.** Confirmed to work and persist. When
a backend genuinely needs input that `--yes` cannot satisfy,
`delegate_implementation` pauses via `elicit()`, surfaces the actual prompt
text to the user, waits for the answer, and feeds it to the backend.

**Hard constraints (learned this session — any 2b implementation MUST honor
these):**

- The subprocess's stdin is intentionally `subprocess.DEVNULL` (commit
  `620cfc0`) precisely because inheriting the server's own stdin causes
  silent hangs. **2b must NOT revert that.** If the backend needs an answer
  piped to it, that requires a *deliberately managed* pipe the server writes
  to only when a real answer is in hand — never the server's inherited
  stdin. Detecting "aider is waiting for input" vs "aider is just slow" is
  itself nontrivial (the stall-timeout mechanism only watches output, not
  process state) and is the crux of 2b's difficulty.
- `elicit()` is async and `_delegate_implementation_impl` is already async,
  but the backend runs in a worker thread via `anyio.to_thread.run_sync`.
  Bridging a "backend needs input" signal from that thread back to the async
  `elicit()` call and the answer back down is the real engineering work —
  analogous to how `on_tick` already bridges via `anyio.from_thread.run`.

**Gate before building 2b:** do NOT build this speculatively. First observe,
across real 2a-enabled runs, whether aider actually hits unanswerable
prompts with `--yes` in practice, and how often. If it is genuinely rare, a
lighter-weight fallback may be proportionate instead of the full
detect-pause-elicit-resume protocol:

- **Fallback option:** on the prompt-related fast-failure that already
  happens today, the failure result includes the captured prompt text (via
  2a.2's `output_tail`), and the documented recovery is "re-run with the
  answer baked into a refined task description." This costs nothing to build
  (2a already delivers it) and may be entirely sufficient.

Decide 2b's build-vs-fallback only with real frequency data. This spec
commits to the *mechanism* (`elicit`) and the *constraints*, not to building
the heavy version now.

## Testing approach

Phase 2a is straightforwardly TDD-able against `server.py` and
`backends/common.py`/`aider.py` using the existing mock patterns
(`fake_run_backend`, `fake_run` stubs in `tests/test_server.py` /
`tests/test_aider.py`, and the real-subprocess tests in
`tests/test_backends_common.py`):

- `output_tail` propagation: `CompletionResult.output_tail` set from
  `run_monitored_subprocess`'s output on both success and failure;
  `delegate_implementation`'s result dict carries it on both paths; bounded
  to `_MAX_OUTPUT_CHARS`.
- Live-pulse content: the progress message emitted by the upgraded `on_tick`
  reflects the latest output line, not the static string; cadence is still
  throttled to the tick interval (not per-chunk). Mock `ctx.report_progress`
  and assert on the message argument, mirroring the existing
  `test_delegate_implementation_on_tick_reports_progress_via_ctx`.
- No new live-Claude-Code test is needed for 2a (the probe already
  established `report_progress` renders; unit tests cover the wiring).

Phase 2b's test approach is deferred with 2b itself.

## Non-goals

- Resource-based live streaming (empirically ruled out — pull-based, no
  auto-refresh).
- Removing or reverting the log file (2a.3 keeps it, demoted).
- Reverting `stdin=subprocess.DEVNULL` (constraint, not a target).
- Building 2b before observing real prompt frequency.
- Codex/Gemini/OpenRouter backends (still their own later phase; the
  `on_output`/`output_tail` plumbing they'll inherit is backend-agnostic).
