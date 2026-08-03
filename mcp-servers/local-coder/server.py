import os
import subprocess
import sys
import uuid
from pathlib import Path

import anyio
from fastmcp import Context, FastMCP

import config as config_module
import status as status_module
from backends.aider import AiderBackend
from backends.codex import CodexBackend
from backends.gemini import GeminiBackend
from backends.openrouter import OpenRouterBackend
from backends.common import KNOWN_BACKENDS, validate_branch_name
from paths import plugin_data_dir

mcp = FastMCP("local-coder")

# Applied to git push / gh pr view / gh pr create — these are normally fast,
# but with no timeout at all a stalled network call could hang indefinitely
# even though the local implementation already succeeded. Generous enough
# to not false-positive on a slow connection, bounded enough to actually
# protect against a true hang.
NETWORK_SUBPROCESS_TIMEOUT_SECONDS = 45

# A delegated backend's live output is also written here, in addition to
# this server's own stderr. Claude Code pipes an MCP server's stdout/
# stderr over an internal socket it owns, with no reliable external tap
# point (confirmed: neither `claude --debug-file` nor any
# filesystem-visible fd surfaces it) — a plain file, written directly by
# this process, is the only way to guarantee `tail -f` shows a delegated
# attempt's real output live, regardless of how the parent process pipes
# stderr.
#
# This is the BASE path. Each delegate_implementation call computes a
# UNIQUE per-call file next to it (see _make_output_log_path) — two
# overlapping calls must not share one file, or the second call's
# start-of-call truncation would wipe the first's active log and their
# output would interleave. The concrete per-call path is returned in the
# result dict (`output_log`) so a human knows which file to tail.
# The log lives in the persistent plugin-data dir (survives plugin updates),
# not next to this server.py file.
OUTPUT_LOG_PATH = plugin_data_dir() / "local-coder-output.log"


def _make_output_log_path() -> Path:
    """A unique per-call log file next to OUTPUT_LOG_PATH, so concurrent
    delegate_implementation calls never share (and corrupt) one file."""
    unique = f"{os.getpid()}-{uuid.uuid4().hex[:8]}"
    return OUTPUT_LOG_PATH.with_name(
        f"{OUTPUT_LOG_PATH.stem}-{unique}{OUTPUT_LOG_PATH.suffix}"
    )


# Per-call logs whose delegation is still in flight IN THIS PROCESS. _prune_old_logs
# never deletes a path in this set, so a call that is cold-loading quietly (its
# log has a stale mtime because the backend hasn't emitted yet) is not pruned by
# newer calls and then lazily recreated by its own on_output OUTSIDE the lock —
# which would drift the on-disk count above the cap. Keyed by resolved str path;
# entries are added under _config_lock (in _prune_and_create_output_log) and
# removed in _delegate_implementation_impl's finally.
#
# Same-process scope only: this is the dominant case (the MCP server is one
# long-lived process and concurrent delegations are concurrent async calls
# within it). A log owned by a *different* process is still prunable — a rare
# multi-instance-sharing-one-data-dir case that remains a transient bound, not
# a leak (the owning call recreates its own log and the next prune re-levels).
_ACTIVE_OUTPUT_LOGS: set[str] = set()


def _coerce_retention(raw: object) -> int:
    """Return `raw` if it is a strictly-positive int (not a bool), else the
    shipped default (50). configure() validates the value, but config.yaml can
    be hand-edited to a non-int or non-positive that would otherwise crash
    _prune_old_logs (a `<=`/slice against a str) and abort the delegation
    before the backend even runs. Coerce defensively — matching the "invalid
    falls back to default" intent."""
    if not isinstance(raw, int) or isinstance(raw, bool) or raw <= 0:
        return 50
    return raw


def _prune_and_create_output_log(path: Path, retention: int) -> None:
    """Prune old per-call logs and create this call's own fresh log as one
    atomic step, so the configured cap holds even when delegations overlap.

    `retention` is the total-file cap the user configured. Keep (retention - 1)
    OLD logs and let `path` (this call's about-to-be-created log) fill the last
    slot: N old + 1 new == N total, matching the documented cap.

    The prune and the create are serialized under the same cross-process file
    lock configure() uses (config_module._config_lock), so two delegations
    running at once cannot each prune-to-(N-1) and then each create, which
    would leave N+1 on disk. Whichever call holds the lock prunes and creates
    before the other observes the directory, so the cap is strict — not merely
    a steady-state bound that overlapping calls could transiently exceed.

    Best-effort: a failure to acquire the lock or to touch the file must never
    abort the delegation (on_output re-creates the file lazily). Only the touch
    is guarded here; _prune_old_logs guards its own failures internally.
    """
    try:
        with config_module._config_lock():
            # Mark this call's log active BEFORE pruning, so the prune in THIS
            # critical section already treats it as non-prunable and a
            # concurrent call entering the lock next sees it as live too.
            _ACTIVE_OUTPUT_LOGS.add(str(path))
            _prune_old_logs(max(retention - 1, 0))
            # Create the empty per-call log NOW (still under the lock), before
            # it is announced. The path is otherwise only materialized on the
            # backend's first on_output write — but during a cold load (exactly
            # when live observation matters most) the backend produces no output
            # for a long time, so a human who runs the announced `tail -f` would
            # hit "no such file" and the tail would exit immediately. Touch it
            # first so `tail -f` attaches and waits.
            path.touch(exist_ok=True)
    except OSError as e:
        # A lock or touch failure is non-fatal: on_output still creates the
        # file lazily and guards its own write failures. The pruning that did
        # or didn't happen is pure housekeeping and never blocks the workflow.
        print(
            f"[local-coder] warning: could not prune/pre-create output log "
            f"{path}: {e}",
            file=sys.stderr, flush=True,
        )


def _prune_old_logs(count: int) -> None:
    """Keep only the `count` most recently modified per-call log files in the
    log directory, deleting older ones. Per-call logs otherwise accumulate
    indefinitely (one per delegate_implementation call, forever).

    Best-effort housekeeping: any failure — a permission error, a race with a
    concurrent call deleting the same file, an unreadable mtime — is swallowed
    so it can never abort a real delegation. Only files matching the per-call
    naming pattern (`<stem>-*<suffix>`) are considered; the shared base log and
    unrelated files (config.yaml, the venv, etc.) are never touched.

    Logs owned by a still-running delegation in this process (`_ACTIVE_OUTPUT_LOGS`)
    are NEVER deleted, even if their mtime is old because the backend is
    cold-loading quietly. Deleting one would let that call's on_output lazily
    recreate it OUTSIDE the lock and drift the on-disk count above the cap. They
    are also counted toward the keep budget so the total still respects `count`.
    """
    try:
        pattern = f"{OUTPUT_LOG_PATH.stem}-*{OUTPUT_LOG_PATH.suffix}"
        candidates = list(OUTPUT_LOG_PATH.parent.glob(pattern))
    except OSError:
        return
    if len(candidates) <= count:
        return

    def _mtime(p: Path) -> float:
        try:
            return p.stat().st_mtime
        except OSError:
            return 0.0  # unreadable → treat as oldest, prune first

    # Never delete an in-flight call's log; it occupies a retained slot.
    active = _ACTIVE_OUTPUT_LOGS
    prunable = [p for p in candidates if str(p) not in active]
    n_active = len(candidates) - len(prunable)

    # Keep the newest prunable logs up to whatever budget remains after the
    # active (always-kept) ones. If active alone already meets/exceeds `count`,
    # delete every prunable log.
    keep_prunable = max(count - n_active, 0)
    prunable.sort(key=_mtime, reverse=True)  # newest first
    for stale in prunable[keep_prunable:]:
        try:
            stale.unlink()
        except OSError:
            # Another call may have already removed it, or perms deny it —
            # either way, not our problem to escalate.
            pass


# Cap on the in-memory buffer that holds an incomplete (not-yet-delimited)
# output line for the progress pulse. A backend emitting a very long
# delimiter-free stream (e.g. a huge single line, or carriage-return
# progress on a terminal that we treat as delimited below) must not grow
# this without bound. The durable transcript is capped separately in
# run_monitored_subprocess (_MAX_OUTPUT_CHARS); this is only the pulse's
# working buffer, so a small cap is plenty.
_PARTIAL_LINE_MAX_CHARS = 8_000

BACKENDS = {
    "aider": AiderBackend,
    "codex": CodexBackend,
    "gemini": GeminiBackend,
    "openrouter": OpenRouterBackend,
}

assert set(BACKENDS) == set(KNOWN_BACKENDS), (
    "server.py's BACKENDS dict and backends.common.KNOWN_BACKENDS have "
    "drifted apart — keep them in sync."
)


def _has_origin_remote(repo_path: str) -> bool:
    result = subprocess.run(
        ["git", "-C", repo_path, "remote"],
        capture_output=True, text=True,
    )
    return "origin" in result.stdout.split()


class GhPrStatusUnknown(Exception):
    """Raised when `gh pr view` fails for a reason other than "no PR exists
    for this branch" — e.g. a network issue, `gh` not authenticated, or a
    transient API error. These must not be treated the same as "no PR", or
    the caller might attempt an unwanted `gh pr create` (risking a
    duplicate PR) or silently mask a real `gh` problem."""


def _has_open_pr(repo_path: str, branch: str, status_record=None) -> bool:
    # Tracked so a SessionEnd during this network call can terminate it; the pid
    # is recorded on the committing record and cleared on return.
    result = _run_tracked_subprocess(
        ["gh", "pr", "view", branch],
        cwd=repo_path, timeout=NETWORK_SUBPROCESS_TIMEOUT_SECONDS,
        status_record=status_record,
    )
    if result.returncode == 0:
        return True
    if "no pull requests found" in result.stderr.lower():
        return False
    raise GhPrStatusUnknown(result.stderr.strip() or "gh pr view failed with no stderr output")


class _DelegationCancelled(Exception):
    """Raised when a tracked post-processing subprocess is cancelled because a
    concurrent SessionEnd cleanup retired the record after the child spawned.
    The record is already terminal on disk; the caller must stop post-processing
    and NOT set its own terminal status over the cleanup's."""


class _TrackedResult:
    """Minimal subprocess.run-like result for the tracked post-processing
    helper: exposes returncode/stdout/stderr so existing call sites are
    unchanged."""
    __slots__ = ("returncode", "stdout", "stderr")

    def __init__(self, returncode, stdout, stderr):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _run_tracked_subprocess(cmd, *, cwd, timeout, status_record):
    """Run a post-processing subprocess (git push / gh pr) as a TRACKED,
    session-isolated child so SessionEnd cleanup can terminate it.

    The push/PR phase runs after the backend pid was cleared. Without tracking,
    a SessionEnd during a live push would leave that subprocess running
    untracked (cleanup has no pid to kill) — so we spawn it in its OWN session
    (start_new_session=True, making it a group leader) and record its pid on the
    status record via set_pid, exactly like the backend. cleanup_session's
    _terminate_process_tree then group-kills the whole push/PR tree.

    On return (success, failure, or timeout) the pid is cleared again so a later
    orphan sweep can't see a dead pid. Raises subprocess.TimeoutExpired on
    timeout (after killing the tree), matching subprocess.run so the existing
    TimeoutExpired handlers apply. OSError (e.g. `gh` not installed) propagates
    as before.
    """
    proc = subprocess.Popen(
        cmd, cwd=cwd, stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, start_new_session=True,
    )
    if status_record is not None:
        try:
            status_module.set_pid(status_record, proc.pid)
        except Exception:
            pass
        # Cancellation handshake for the SessionEnd-right-after-Popen race:
        # cleanup may have run in the tiny gap between spawn and set_pid, seen a
        # committing record with no pid, and retired it — leaving this freshly
        # spawned child to run after its session ended. Now that the pid is
        # recorded, re-check the on-disk record: if it went terminal, this child
        # was started after cleanup, so kill it and abort rather than leave an
        # untracked live process.
        try:
            if status_module.is_terminal_on_disk(status_record):
                status_module._terminate_process_tree(proc.pid)
                try:
                    proc.communicate(timeout=5)
                except Exception:
                    pass
                raise _DelegationCancelled(
                    "session ended: post-processing subprocess cancelled")
        except _DelegationCancelled:
            raise
        except Exception:
            pass
    try:
        try:
            stdout, stderr = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            # Kill the whole session-group tree, then reap, then re-raise so the
            # caller's existing TimeoutExpired branch records the outcome.
            status_module._terminate_process_tree(proc.pid)
            try:
                proc.communicate(timeout=5)
            except Exception:
                pass
            raise
        return _TrackedResult(proc.returncode, stdout, stderr)
    finally:
        if status_record is not None:
            try:
                status_module.set_pid(status_record, None)
            except Exception:
                pass


async def _delegate_implementation_impl(
    task: str, branch: str, target_repo_path: str | None = None,
    ctx: Context | None = None,
) -> dict:
    # Unique per-call log file so two overlapping calls never share (and
    # corrupt) one file. Because it's fresh per call, there is no stale
    # prior-call content to truncate. The empty file is created below, before
    # the path is announced, so a cold-load `tail -f` attaches immediately
    # (see the touch call); on_output then appends to it. The path is also
    # surfaced in the result dict so a human knows which file to tail.
    output_log_path = _make_output_log_path()

    try:
        validate_branch_name(branch)
    except ValueError as e:
        return {"success": False, "error": str(e)}

    cfg = config_module.load_config()
    retention = _coerce_retention(cfg.get("log_retention_count"))

    repo_path = target_repo_path or cfg.get("target_repo_path")
    if not repo_path:
        return {"success": False, "error": "target_repo_path not provided and not set in config"}

    # Apply the configured branch_prefix if the caller-supplied branch name
    # doesn't already carry it. This is the only point where `branch` is
    # actually used (branch creation, push, PR), so applying it here makes
    # branch_prefix take effect for every call, transparently to whatever
    # constructed the raw branch name upstream (e.g. the SDD skill).
    branch_prefix = cfg.get("branch_prefix") or ""
    if branch_prefix and not branch.startswith(branch_prefix):
        branch = f"{branch_prefix}{branch}"
        try:
            validate_branch_name(branch)
        except ValueError as e:
            return {"success": False, "error": str(e)}

    backend_name = cfg["backend"]
    backend_cls = BACKENDS.get(backend_name)
    if backend_cls is None:
        return {"success": False, "error": f"unknown backend: {backend_name}"}
    backend = backend_cls()

    # Prune accumulated per-call logs from earlier calls and create this call's
    # own fresh log as one atomic, lock-guarded step (see the helper): pruning
    # to (retention - 1) then adding this log keeps the total at the configured
    # cap, and serializing the two under a cross-process lock keeps that cap
    # strict even when delegations overlap. Done here, after the early returns,
    # so a call that bails out early does no log housekeeping at all. The parent
    # dir is already ensured by plugin_data_dir().
    #
    # Offloaded to a worker thread: the helper takes a blocking cross-process
    # file lock (fcntl.flock / msvcrt.locking) and does synchronous disk IO, so
    # running it inline would stall the event loop under lock contention and
    # delay unrelated tool work and progress notifications. Every other blocking
    # op in this coroutine (backend run, git checks, push, PR) is offloaded the
    # same way.
    #
    # Status holders declared BEFORE the try so the finally can always read
    # them, even if an exception fires inside the try before the record is
    # created. Default status_final = failed/failed; a reached success/commit
    # return path overwrites it.
    status_record = None
    status_updater = None
    status_final = {"status": "failed", "phase": "failed", "error_message": None}
    try:
        await anyio.to_thread.run_sync(
            _prune_and_create_output_log, output_log_path, retention
        )

        # Announce the per-call log path up front — before run_backend — so a
        # human can start `tail -f`-ing it while the backend is still running.
        # (The same path is also returned in the final result dict, but that
        # only arrives after the whole call, including push/PR, has finished,
        # which is too late for live tailing.) Best-effort: to stderr always,
        # and via the progress channel when a Context is available. We're in
        # the async body here (not a worker thread), so await report_progress
        # directly rather than bridging through anyio.from_thread.
        print(
            f"[local-coder] streaming backend output to {output_log_path} "
            "(tail -f it to follow live)",
            file=sys.stderr, flush=True,
        )
        if ctx is not None:
            # Best-effort: a progress-notification failure (dropped token,
            # transport hiccup, client without progress support) must NOT abort
            # the real implementation workflow before the backend even runs.
            # Warn and continue.
            try:
                await ctx.report_progress(0, None, f"Output log: {output_log_path}")
            except Exception as e:
                print(
                    f"[local-coder] warning: failed to announce output log path: {e}",
                    file=sys.stderr, flush=True,
                )

        # Observability: create a pollable status record for this delegation.
        # Isolated + non-fatal — status.* swallow their own errors, and this
        # create is additionally guarded so nothing here can abort the run.
        # `status_record` stays None if creation fails, which every later
        # status hook tolerates. `status_final` (declared above the try)
        # carries the terminal outcome that the finally-block persists once,
        # so the many return paths below don't each have to finalize.
        try:
            status_record = status_module.create_record(
                target_repo=repo_path, branch=branch, model=cfg["model"],
                output_log=str(output_log_path),
                session_id=os.environ.get("CLAUDE_SESSION_ID"),
            )
            if status_record and status_record.get("id"):
                status_updater = status_module.ProgressUpdater(
                    repo_path, status_record["id"]
                )
        except Exception as e:
            print(
                f"[local-coder] warning: status record init failed: {e}",
                file=sys.stderr, flush=True,
            )

        attempt_models = [cfg["model"], *cfg.get("fallback_models", [])]
        attempt_errors = []
        last_output_tail = ""

        # Shared holders (one-element mutable cells both closures can see):
        #   latest_line   — the most recent COMPLETE output line; on_tick reads
        #                   it so the periodic progress pulse carries real
        #                   backend output instead of a static heartbeat.
        #   partial_line  — buffer for a chunk that ended mid-line (no line
        #                   delimiter yet); carried across on_output calls so
        #                   the pulse never shows an arbitrary partial suffix.
        #                   Bounded to _PARTIAL_LINE_MAX_CHARS so a long
        #                   delimiter-free stream can't grow it without bound.
        #   log_write_ok  — flips False after the first log-write failure so we
        #                   warn once and stop retrying (a persistent failure
        #                   must not flood stderr every chunk and bury the live
        #                   output it's meant to surface).
        #   progress_ok   — same idea for the progress pulse: flips False after
        #                   the first report_progress failure so a permanently
        #                   dead channel (client gone, token invalidated) is
        #                   warned once and then skipped for the rest of the run,
        #                   instead of warning on every tick.
        # latest_line + partial_line are RESET at the top of each failover
        # attempt (see the loop below) so a fallback model's opening pulse
        # can't relabel the previous model's last line as its own.
        latest_line = [""]
        partial_line = [""]
        log_write_ok = [True]
        progress_ok = [True]

        def make_on_tick(model_name: str):
            def on_tick():
                line = latest_line[0]
                pulse = f"{model_name}: {line}" if line else f"Running {model_name}..."
                print(f"[local-coder] {pulse}", file=sys.stderr, flush=True)
                if ctx is not None and progress_ok[0]:
                    # Best-effort progress: a failure here (dropped progress
                    # token, transport hiccup, client without progress support)
                    # must NOT propagate. run_monitored_subprocess kills the
                    # backend on ANY on_tick exception, and that kill is not a
                    # StallError — so AiderBackend.run_backend's working-tree
                    # restore would be skipped and the server's broad except
                    # would start the fallback model on top of partial, uncleaned
                    # edits. Warn and continue, exactly like the announce path.
                    # After the first failure, stop reporting (and stop warning):
                    # a permanently dead channel must not flood stderr with a
                    # warning on every tick for the rest of a long run. The stderr
                    # print above still carries the pulse regardless.
                    try:
                        anyio.from_thread.run(
                            ctx.report_progress, 0, None, pulse
                        )
                    except Exception as e:
                        progress_ok[0] = False
                        print(
                            f"[local-coder] warning: failed to report progress "
                            f"pulse (disabling further progress reports for this "
                            f"call): {e}",
                            file=sys.stderr, flush=True,
                        )
                # Observability: record the latest complete line as the status
                # record's free-text activity. on_activity is self-guarded and
                # deduped (rewrites only when the line changed).
                if status_updater is not None and line:
                    status_updater.on_activity(line)
            return on_tick

        def make_on_output(model_name: str):
            # Streams the backend subprocess's actual output as it arrives, to
            # BOTH stderr and the per-call output_log_path, and records the
            # latest complete line so the on_tick pulse above can surface it
            # in-chat. Kept throttled to the tick cadence — the tick is what
            # emits to report_progress; this callback only records.
            def on_output(chunk: str) -> None:
                line = f"[local-coder:{model_name}] {chunk}"
                print(line, end="", file=sys.stderr, flush=True)
                # The log file is a best-effort debugging convenience, not the
                # primary channel (stderr above + the on_tick pulse are). A
                # write failure here must NEVER propagate: run_monitored_subprocess
                # kills the subprocess on any on_output exception, but only the
                # StallError path in AiderBackend.run_backend runs the
                # working-tree cleanup — so an unhandled exception would bypass
                # cleanup and leave partial backend writes to pollute the next
                # fallback attempt. So: write with explicit UTF-8 (the stream is
                # already UTF-8-decoded upstream; the default encoding on a
                # non-UTF-8 locale could otherwise raise UnicodeEncodeError,
                # which is NOT an OSError), and catch broadly. After the first
                # failure, stop retrying and warn only once — a persistent
                # failure must not flood stderr on every chunk and bury the live
                # output this is meant to surface.
                if log_write_ok[0]:
                    try:
                        with open(output_log_path, "a", encoding="utf-8", errors="replace") as f:
                            f.write(chunk)
                    except Exception as e:
                        log_write_ok[0] = False
                        print(
                            f"[local-coder] warning: failed to write output log "
                            f"(disabling further log writes for this call): {e}",
                            file=sys.stderr, flush=True,
                        )
                # on_output receives RAW read chunks, which can split mid-line.
                # Prepend any buffered partial from the previous chunk, then
                # promote only COMPLETE lines to latest_line; whatever follows
                # the last delimiter is an incomplete line — buffer it (bounded)
                # until a later chunk finishes it, so the pulse never shows an
                # arbitrary chunk suffix. "\r" is treated as a delimiter too, so
                # carriage-return progress output (aider/pip-style bars, which
                # never send "\n") still updates the pulse and can't grow the
                # buffer forever.
                buffered = partial_line[0] + chunk
                normalized = buffered.replace("\r\n", "\n").replace("\r", "\n")
                if "\n" in normalized:
                    complete, _, remainder = normalized.rpartition("\n")
                    complete_lines = [ln for ln in complete.splitlines() if ln.strip()]
                    if complete_lines:
                        latest_line[0] = complete_lines[-1].strip()
                else:
                    remainder = normalized
                # Bound the carried-over partial: keep only its tail, so a long
                # delimiter-free stream can't exhaust memory.
                partial_line[0] = remainder[-_PARTIAL_LINE_MAX_CHARS:]
                # Observability: flip the status phase starting->working on the
                # first real chunk. ProgressUpdater.on_output is self-guarded
                # and deduped, so this is a cheap no-op after the first flip.
                if status_updater is not None:
                    status_updater.on_output(chunk)
            return on_output

        for model in attempt_models:
            # If SessionEnd cleanup retired this record mid-run (marked it
            # orphaned and killed the prior attempt's process), do not spawn
            # another attempt — the session that requested this work is gone.
            # Return now, leaving the terminal on-disk record untouched
            # (status_final=None tells the finally not to finalize over it).
            if status_record and status_module.is_terminal_on_disk(status_record):
                status_final = None
                return {
                    "success": False,
                    "error": "delegation orphaned: session ended mid-run",
                    "output_tail": last_output_tail,
                    "output_log": str(output_log_path),
                }
            # Reset the per-attempt holders so this attempt starts clean:
            #  - pulse holders, so this model's first tick (before it emits
            #    anything) can't surface the PREVIOUS model's last line as its
            #    own;
            #  - log_write_ok, so a log-write failure during one model's attempt
            #    doesn't permanently disable logging for the next fallback.
            latest_line[0] = ""
            partial_line[0] = ""
            log_write_ok[0] = True
            # Clear any pid carried over from a previous (failed) attempt BEFORE
            # this attempt spawns its own. Otherwise, between attempts the
            # record would still name the previous attempt's now-dead pid while
            # marked running — which the orphan sweep would mark orphaned
            # mid-delegation, and which session cleanup could act on as a stale
            # (or reused) pid. on_start below sets this attempt's real pid.
            if status_record:
                try:
                    status_module.set_pid(status_record, None)
                    # Persist the model this attempt is actually using, so
                    # /status reports the fallback model when the primary
                    # failed — not the primary from config.
                    status_module.set_model(status_record, model)
                except Exception:
                    pass
            try:
                result = await anyio.to_thread.run_sync(
                    lambda model=model: backend.run_backend(
                        task, repo_path, branch, cfg, model=model,
                        on_tick=make_on_tick(model), on_output=make_on_output(model),
                        on_start=(
                            (lambda pid: status_module.set_pid(status_record, pid))
                            if status_record else None
                        ),
                    )
                )
            except NotImplementedError as e:
                status_final = {
                    "status": "failed", "phase": "failed", "error_message": str(e),
                }
                return {"success": False, "error": str(e)}
            except Exception as e:
                # Any other unexpected exception from a backend attempt (e.g. an
                # uncaught CalledProcessError from a backend's own git calls)
                # shouldn't crash the whole tool call — treat it like a failed
                # attempt and continue the failover loop to the next model.
                if status_record:
                    try:
                        status_module.set_pid(status_record, None)
                    except Exception:
                        pass
                attempt_errors.append(f"{model}: {e}")
                continue

            # The attempt's process has now exited.
            if result.success:
                # Clear the dead pid AND enter the non-orphanable `committing`
                # phase in ONE locked transition (begin_committing). Doing these
                # as two separate writes left a gap where the record was
                # `running` with pid=None — a concurrent SessionEnd cleanup could
                # observe that intermediate state and orphan an already-committed
                # delegation. The single transition closes that window before the
                # (possibly slow) push/PR work below.
                if status_record:
                    try:
                        status_module.begin_committing(status_record)
                    except Exception:
                        pass
                # Safe intermediate terminal state: the backend already committed
                # locally, but status_final still holds the loop's default
                # failed/failed here. If an UNhandled exception escapes the push/PR
                # block below (e.g. an OSError from git push, which only the
                # TimeoutExpired branch catches), the finally would finalize this
                # already-committed delegation as `failed`. Record it as
                # completed/committing now; the push-timeout, push-failure, and
                # final-success paths overwrite this as needed.
                status_final = {
                    "status": "completed", "phase": "committing",
                    "commit_sha": result.commit_sha,
                }
                note = None
                pr_url = None
                has_remote = await anyio.to_thread.run_sync(_has_origin_remote, repo_path)
                if has_remote:
                    try:
                        push = await anyio.to_thread.run_sync(
                            lambda: _run_tracked_subprocess(
                                ["git", "-C", repo_path, "push", "-u", "origin", branch],
                                cwd=None,
                                timeout=NETWORK_SUBPROCESS_TIMEOUT_SECONDS,
                                status_record=status_record,
                            )
                        )
                    except subprocess.TimeoutExpired:
                        # Implementation succeeded and committed locally; only
                        # the push's outcome is unknown. Record it as completed
                        # work (phase=committing) with the push blocker noted,
                        # not a failed delegation.
                        status_final = {
                            "status": "completed", "phase": "committing",
                            "commit_sha": result.commit_sha,
                            "error_message": (
                                f"git push timed out after "
                                f"{NETWORK_SUBPROCESS_TIMEOUT_SECONDS}s; remote state unknown"
                            ),
                        }
                        return {
                            "success": False,
                            "error": (
                                f"git push timed out after {NETWORK_SUBPROCESS_TIMEOUT_SECONDS}s "
                                f"— the commit exists locally on branch {branch!r}, but the "
                                "push's remote state is UNKNOWN (it may have partially or fully "
                                "completed). Verify the remote before retrying, and do not "
                                "re-run the delegation blindly — the local commit is already there"
                            ),
                            "files_changed": result.files_changed,
                            "commit_sha": result.commit_sha,
                            "model_used": model,
                            "output_tail": result.output_tail,
                            "output_log": str(output_log_path),
                        }
                    if push.returncode != 0:
                        status_final = {
                            "status": "completed", "phase": "committing",
                            "commit_sha": result.commit_sha,
                            "error_message": f"git push failed: {push.stderr.strip()}",
                        }
                        return {
                            "success": False,
                            "error": f"git push failed: {push.stderr.strip()}",
                            "files_changed": result.files_changed,
                            "commit_sha": result.commit_sha,
                            "model_used": model,
                            "output_tail": result.output_tail,
                            "output_log": str(output_log_path),
                        }

                    if cfg.get("open_pr"):
                        try:
                            has_open_pr = await anyio.to_thread.run_sync(
                                _has_open_pr, repo_path, branch, status_record)
                        except GhPrStatusUnknown as e:
                            # gh pr view failed for a reason other than "no PR" —
                            # don't guess; skip PR creation rather than risk a
                            # duplicate PR or mask a real `gh` problem.
                            note = f"could not verify PR status, skipped PR creation: {e}"
                            has_open_pr = True  # skip the create-PR branch below
                        except subprocess.TimeoutExpired:
                            note = (
                                f"gh pr view timed out after {NETWORK_SUBPROCESS_TIMEOUT_SECONDS}s, "
                                "skipped PR creation"
                            )
                            has_open_pr = True  # skip the create-PR branch below
                        except OSError as e:
                            # `gh` not installed (FileNotFoundError) or otherwise
                            # not launchable — the implementation was already
                            # committed and pushed, so don't report failure for
                            # landed work. Skip PR creation and note it.
                            note = f"could not run `gh` to check PR status, skipped PR creation: {e}"
                            has_open_pr = True  # skip the create-PR branch below
                        if not has_open_pr:
                            try:
                                pr = await anyio.to_thread.run_sync(
                                    lambda: _run_tracked_subprocess(
                                        ["gh", "pr", "create", "--fill", "--head", branch,
                                         "--base", cfg["pr_base_branch"]],
                                        cwd=repo_path,
                                        timeout=NETWORK_SUBPROCESS_TIMEOUT_SECONDS,
                                        status_record=status_record,
                                    )
                                )
                            except subprocess.TimeoutExpired:
                                note = (
                                    f"PR creation was attempted and timed out after "
                                    f"{NETWORK_SUBPROCESS_TIMEOUT_SECONDS}s"
                                )
                            except OSError as e:
                                # `gh` not installed or not launchable at create
                                # time — same as the view case: the work is already
                                # committed and pushed, so keep success:true and
                                # note that PR creation couldn't run.
                                note = f"could not run `gh` to create PR: {e}"
                            else:
                                if pr.returncode == 0:
                                    pr_url = pr.stdout.strip()
                                else:
                                    # Don't silently report success with
                                    # pr_url=None indistinguishable from "PR
                                    # creation wasn't requested" — the local
                                    # implementation still succeeded and was
                                    # pushed, so this stays success:true, but
                                    # flag that PR creation was attempted and
                                    # failed.
                                    note = f"PR creation was attempted and failed: {pr.stderr.strip()}"
                else:
                    note = "no origin remote configured; commit created locally, nothing pushed"

                status_final = {
                    "status": "completed", "phase": "done",
                    "commit_sha": result.commit_sha,
                }
                return {
                    "success": True,
                    "pr_url": pr_url,
                    "branch": branch,
                    "files_changed": result.files_changed,
                    "model_used": model,
                    "summary": f"Implemented via {backend_name} ({model})",
                    "output_tail": result.output_tail,
                    "output_log": str(output_log_path),
                    **({"note": note} if note else {}),
                }

            # Failed attempt. Clear the now-dead backend pid before looping to
            # the next model (or exiting to the failed finalize), so a
            # concurrent sweep doesn't see a `running` record naming a dead pid.
            if status_record:
                try:
                    status_module.set_pid(status_record, None)
                except Exception:
                    pass
            attempt_errors.append(f"{model}: {result.error}")
            last_output_tail = result.output_tail

        status_final = {
            "status": "failed", "phase": "failed",
            "error_message": "all models failed — " + "; ".join(attempt_errors),
        }
        return {
            "success": False,
            "error": "all models failed — " + "; ".join(attempt_errors),
            "output_tail": last_output_tail,
            "output_log": str(output_log_path),
        }
    except _DelegationCancelled as e:
        # A tracked push/PR child was cancelled because SessionEnd cleanup
        # retired this record after the child spawned. The record is already
        # terminal on disk (orphaned) — leave it: status_final=None tells the
        # finally not to finalize over the cleanup's outcome. The dead child was
        # already killed inside the tracked helper.
        status_final = None
        return {
            "success": False,
            "error": str(e),
            "output_tail": last_output_tail,
            "output_log": str(output_log_path),
        }
    finally:
        # Observability: persist the terminal status once, from whatever
        # status_final the reached return path set (default failed/failed if a
        # validation return or an exception got here before any was set). Guard
        # broadly: finalizing status must never mask the real result or raise
        # out of the finally.
        # status_final is None only when the loop broke because SessionEnd
        # cleanup already retired the record (orphaned) — in that case there is
        # nothing to finalize; the terminal on-disk record stands.
        if status_record and status_final is not None:
            try:
                status_module.finalize(status_record, **status_final)
            except Exception as e:
                print(
                    f"[local-coder] warning: status finalize failed: {e}",
                    file=sys.stderr, flush=True,
                )
        # This call's log is no longer in flight — allow future prunes to
        # reclaim it. discard() is idempotent, so a helper that never
        # registered the path (lock/touch failed before add) is harmless.
        _ACTIVE_OUTPUT_LOGS.discard(str(output_log_path))


def _configure_impl(
    backend: str | None = None,
    model: str | None = None,
    fallback_models: list[str] | None = None,
    max_fallback_models: int | None = None,
    stall_timeout_seconds: float | None = None,
    first_output_timeout_seconds: float | None = None,
    target_repo_path: str | None = None,
    branch_prefix: str | None = None,
    open_pr: bool | None = None,
    pr_base_branch: str | None = None,
    idle_notify_interval_seconds: float | None = None,
    extra_backend_args: list[str] | None = None,
    log_retention_count: int | None = None,
) -> dict:
    all_overrides = {
        "backend": backend,
        "model": model,
        "fallback_models": fallback_models,
        "max_fallback_models": max_fallback_models,
        "stall_timeout_seconds": stall_timeout_seconds,
        "first_output_timeout_seconds": first_output_timeout_seconds,
        "target_repo_path": target_repo_path,
        "branch_prefix": branch_prefix,
        "open_pr": open_pr,
        "pr_base_branch": pr_base_branch,
        "idle_notify_interval_seconds": idle_notify_interval_seconds,
        "extra_backend_args": extra_backend_args,
        "log_retention_count": log_retention_count,
    }
    all_overrides = {k: v for k, v in all_overrides.items() if v is not None}

    try:
        return config_module.configure_with_validation(all_overrides)
    except config_module.ConfigValidationError as e:
        return {"success": False, "error": str(e)}
    except Exception as e:
        # configure_with_validation calls _validate_ollama_model, which
        # calls ollama_module.list_ollama_models() — that can raise
        # OllamaUnavailableError (not a ConfigValidationError) if Ollama is
        # down. Match the broad-except convention _list_available_models_impl
        # already uses so this returns the documented structured error
        # response instead of an uncaught exception through the MCP tool
        # call.
        return {"success": False, "error": str(e)}


def _list_available_models_impl() -> dict:
    try:
        models = config_module.list_available_models_with_prefix()
    except Exception as e:
        return {"error": str(e)}
    return {"models": models}


@mcp.tool()
async def delegate_implementation(
    task: str, branch: str, ctx: Context, target_repo_path: str | None = None,
) -> dict:
    return await _delegate_implementation_impl(task, branch, target_repo_path, ctx=ctx)


@mcp.tool()
def configure(
    backend: str | None = None,
    model: str | None = None,
    fallback_models: list[str] | None = None,
    max_fallback_models: int | None = None,
    stall_timeout_seconds: float | None = None,
    first_output_timeout_seconds: float | None = None,
    target_repo_path: str | None = None,
    branch_prefix: str | None = None,
    open_pr: bool | None = None,
    pr_base_branch: str | None = None,
    idle_notify_interval_seconds: float | None = None,
    extra_backend_args: list[str] | None = None,
    log_retention_count: int | None = None,
) -> dict:
    return _configure_impl(
        backend, model, fallback_models, max_fallback_models,
        stall_timeout_seconds, first_output_timeout_seconds,
        target_repo_path, branch_prefix,
        open_pr, pr_base_branch, idle_notify_interval_seconds,
        extra_backend_args, log_retention_count,
    )


@mcp.tool()
def list_available_models() -> dict:
    return _list_available_models_impl()


if __name__ == "__main__":
    mcp.run()
