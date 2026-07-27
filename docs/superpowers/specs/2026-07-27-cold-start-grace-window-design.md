# Cold-Start Grace Window for Stall Detection — Design

**Status:** approved (2026-07-27)
**Scope:** one behavior change to `run_monitored_subprocess` + one new config
knob + README guidance. Targets `dev`.

## Problem

`run_monitored_subprocess` (`backends/common.py`) detects a stalled backend
by watching for subprocess **output**: it resets `last_activity` on every
decoded chunk and raises `StallError` when
`now - last_activity > stall_timeout_seconds` (default 300s).

A large local model loading into Ollama for the first time produces **zero
output while it loads** (the weights are read into memory before any token
is generated). That cold-load time therefore counts against the stall
window, so a legitimately-loading model is killed as if it had stalled.

**This was observed, not theorized.** A real `delegate_implementation` call
against `ollama/qwen3-coder:30b` (18GB) produced zero output for the full
300s and was killed; `ollama ps` showed nothing loaded into memory at the
time, so the cause was cold-load latency exceeding the stall window before a
single token, not a hung model. The stall mechanism did exactly what it was
built to do — the gap is that it cannot distinguish "still loading" from
"stalled mid-run."

## Fix

Split the single output-inactivity timer into two phases, keyed on whether
the subprocess has emitted its first byte of output yet:

- **Phase 1 — before first output byte.** Measure elapsed time since process
  start against a new `first_output_timeout_seconds`. This is the cold-load
  grace window.
- **Phase 2 — after first output byte.** Switch permanently to the existing
  behavior: measure inactivity (`now - last_activity`) against
  `stall_timeout_seconds`. Unchanged from today.

The transition is one-way: the first decoded chunk flips the timer from
phase 1 to phase 2 for the rest of the run.

The grace window is **self-bounding** — no separate cap is needed. A model
that never emits a byte still fails when `first_output_timeout_seconds`
elapses; it just fails on the longer grace clock instead of the steady-state
clock. So a genuinely wedged cold load is still caught.

## Config knob

New field: `first_output_timeout_seconds`.

- **Shipped `config.yaml` default:** `600` (10 min — 2× the 300s stall
  window, comfortable headroom for a large-model cold-load without waiting
  absurdly long on a wedged one).
- **Code fallback when the key is ABSENT from a config:** the effective
  `stall_timeout_seconds` value, **not** a hardcoded 600. This means a
  pre-existing user config that predates this knob gets **zero behavior
  change** — phase 1 and phase 2 use the same duration, i.e. exactly today's
  behavior — until the user opts in by adding the key. New installs get the
  600s default because the shipped `config.yaml` carries the key explicitly.
- **Validation:** strictly positive, identical to the sibling timeouts
  (`stall_timeout_seconds`, `idle_notify_interval_seconds`). No cross-field
  constraint: because the absent-key fallback equals the stall value, a
  first-output window shorter than the stall window can only arise from an
  explicit, deliberate user setting, so the code does not forbid it.

## Threading

The value follows the exact path the sibling timeouts already travel:

```
config.yaml
  → config.py (merge + validate)
    → server.py _delegate_implementation_impl
      → AiderBackend.run_backend
        → run_monitored_subprocess
```

`run_monitored_subprocess` gains a `first_output_timeout_seconds` parameter.
It is defaulted (see below) so existing direct callers and tests are
unaffected until updated. The default at the function signature should
preserve today's behavior when the argument is omitted — i.e. behave as if
phase 1's budget equals `stall_timeout_seconds`. Concretely: the parameter
defaults to `None`, and when it is `None` the function uses
`stall_timeout_seconds` for the phase-1 budget. This keeps the "absent →
fall back to stall value" contract in one place and means no existing caller
or test changes behavior.

## Timer logic (precise)

In the monitor loop, track whether any output has been seen:

- `seen_output` starts `False`; set `True` the first time a decoded chunk is
  non-empty (same point `last_activity` is currently updated).
- Compute the effective phase-1 budget once:
  `first_budget = first_output_timeout_seconds if first_output_timeout_seconds is not None else stall_timeout_seconds`.
- Stall check each iteration:
  - if `not seen_output`: kill + `StallError` when
    `now - start_time > first_budget`.
  - if `seen_output`: kill + `StallError` when
    `now - last_activity > stall_timeout_seconds` (today's check, verbatim).

`start_time` is captured once at process launch (reuse the existing
`last_activity`/`last_tick` initialization point).

`StallError`'s message/attribute should report the budget that actually
fired, so a cold-load failure and a steady-state stall are distinguishable
in the surfaced error. (Implementation detail for the plan: either pass the
fired budget into `StallError`, or add a phase flag — decide in the plan.)

## README

Add to the config-reference / operational-guidance section:

- Explain that the FIRST call to a large local model after idle can be slow
  purely from cold-load, producing no output while weights load.
- Document `first_output_timeout_seconds`: what it governs, the 600s shipped
  default, and the "falls back to `stall_timeout_seconds` when omitted"
  contract.
- Recommend configuring `fallback_models` for large primary models so a
  genuine cold-load failure fails over rather than aborting the call.

## Testing (TDD)

`run_monitored_subprocess` (in `tests/test_backends_common.py`):

1. **Cold-load survives:** a subprocess that emits nothing until after
   `stall_timeout_seconds` but before `first_output_timeout_seconds`, then
   emits output, is NOT killed. RED against current code (today it dies at
   `stall_timeout_seconds`).
2. **Steady-state stall unchanged:** first byte arrives, then the process
   goes silent past `stall_timeout_seconds` → killed. Guards against
   loosening mid-run detection.
3. **Wedged cold-load still caught:** no output ever, elapsed past
   `first_output_timeout_seconds` → killed on the grace clock.
4. **Omitted param = today's behavior:** with
   `first_output_timeout_seconds=None`, phase-1 budget equals
   `stall_timeout_seconds` (a no-output process dies at
   `stall_timeout_seconds`, exactly as before).

Config (`tests/test_config.py`):

5. Absent key → effective value falls back to `stall_timeout_seconds`.
6. Present key → honored.
7. Non-positive value → rejected with a clear error, same as siblings.

Use short fake timeouts in tests (fractions of a second), same pattern as
the existing stall tests — do not sleep for real minutes.

## Non-goals

- Not raising `stall_timeout_seconds`'s default (steady-state detection is
  correct as-is).
- Not adding a separate hard cap on the grace window (it is self-bounding).
- Not changing `idle_notify_interval_seconds` or the tick/progress path.
- Not touching the stdin-DEVNULL hang fix or `output_tail` logic.
