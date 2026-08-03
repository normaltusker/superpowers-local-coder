"""Persistent, workspace-scoped status records for local-coder delegations."""

import calendar
import errno
import hashlib
import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

# Import the lock primitive + data-dir helper from their own stdlib-only
# modules rather than from config — config imports yaml (a third-party dep),
# and the SessionEnd cleanup hook must be able to import status on a host
# where the venv/deps aren't present.
from locking import file_lock
from paths import plugin_data_dir


_SLUG_RE = re.compile(r"[^A-Za-z0-9._-]+")
_INDEX_NAME = "state.json"
_MAX_INDEX_JOBS = 50
# A running record that never received a pid (crash between create_record and
# on_start) would otherwise stay "running" forever, since the pid-liveness
# sweep can't inspect a None pid. After this many seconds with no pid, the
# sweep retires it as orphaned. Generous enough not to trip a normal startup
# (backend spawn + cold model load happen well within it).
_NO_PID_ORPHAN_SECONDS = 600
# A record in the non-orphanable `committing` phase (backend done, doing the
# network-bound git push / PR creation) is protected from the sweep so landed
# work isn't misreported orphaned mid-push. But if the process crashes or is
# cancelled during that work, nothing finalizes the record — without a bound it
# would stay `running`/`committing` forever with no recovery path. So the sweep
# retires a committing record too once it exceeds this window. Generous, since
# push/PR is network-bound and slower than a local backend run.
_COMMITTING_ORPHAN_SECONDS = 1800
# Default SIGTERM→SIGKILL grace for a single process-tree teardown. cleanup
# clamps this to the budget remaining on its overall deadline so many live
# records can't push SessionEnd past its hook timeout.
_DEFAULT_TERMINATE_GRACE_SECONDS = 3.0
# A terminal record is only pruned once it has been terminal at least this long
# (off updatedAt). Guards against deleting a record a worker just finalized (or
# cleanup just orphaned) while that worker may still be mid-write — a
# prune-then-resurrect race. Generous: prune reclaims disk lazily, not urgently.
_PRUNE_MIN_AGE_SECONDS = 3600


def _warn(operation: str, exc: Exception) -> None:
    print(f"local-coder status: {operation} failed: {exc}", file=sys.stderr)


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _base36(value: int) -> str:
    alphabet = "0123456789abcdefghijklmnopqrstuvwxyz"
    if value == 0:
        return "0"
    result = ""
    while value:
        value, remainder = divmod(value, 36)
        result = alphabet[remainder] + result
    return result


def resolve_state_dir(target_repo: str) -> Path:
    """Return (and create) the status directory for one real repository path."""
    try:
        # Derive BOTH the slug and the digest from the canonical (realpath)
        # form, so two paths that resolve to the same real repository — e.g. a
        # symlinked workspace and its target — key to one state directory.
        # Keying the slug off the raw basename instead would give symlink and
        # target different dirs (same digest, different slug), leaving one
        # invisible to /status and SessionEnd cleanup.
        canonical = os.path.realpath(target_repo)
        slug = _SLUG_RE.sub("-", os.path.basename(canonical)).strip("-") or "workspace"
        digest = hashlib.sha256(canonical.encode()).hexdigest()[:16]
        if os.environ.get("CLAUDE_PLUGIN_DATA"):
            root = plugin_data_dir() / "status"
        else:
            root = Path(tempfile.gettempdir()) / "local-coder-status"
        state_dir = root / f"{slug}-{digest}"
        state_dir.mkdir(parents=True, exist_ok=True)
        return state_dir
    except Exception as exc:
        _warn("resolve_state_dir", exc)
        # Fallback preserves the non-fatal contract when the configured
        # location is inaccessible — but it must STILL be per-repository, or
        # unrelated repos would share one directory and cross-contaminate
        # records (so cleanup could act on another repo's delegation). Key the
        # fallback on a hash of the repo path too; degrade to a fixed bucket
        # only if even hashing fails.
        try:
            digest = hashlib.sha256(
                os.path.realpath(target_repo).encode()
            ).hexdigest()[:16]
            leaf = f"fallback-{digest}"
        except Exception:
            leaf = "fallback-unknown"
        fallback = Path(tempfile.gettempdir()) / "local-coder-status" / leaf
        try:
            fallback.mkdir(parents=True, exist_ok=True)
        except Exception as fallback_exc:
            _warn("resolve_state_dir fallback", fallback_exc)
        return fallback


def generate_job_id() -> str:
    """Generate a compact, time-sortable local-coder job identifier."""
    return f"lc-{_base36(time.time_ns())}-{os.urandom(3).hex()}"


def read_record(job_file: Path) -> dict:
    """Read a record file. Readers intentionally surface malformed I/O."""
    return json.loads(job_file.read_text())


# A generated job id looks like `lc-<base36>-<hex>` (see generate_job_id).
# We ONLY ever address records by an id of this shape. A malformed record whose
# `id` field carries path separators or `..` must not be able to redirect a
# record/lock write outside the workspace state dir, so every path derivation
# validates the id first.
_JOB_ID_RE = re.compile(r"\Alc-[0-9a-z]+-[0-9a-f]+\Z")


def _validated_job_id(job_id) -> str:
    if not isinstance(job_id, str) or not _JOB_ID_RE.match(job_id):
        raise ValueError(f"refusing to use non-conforming job id {job_id!r} as a path")
    return job_id


def _record_path(target_repo: str, job_id: str) -> Path:
    return resolve_state_dir(target_repo) / f"{_validated_job_id(job_id)}.json"


def _record_lock_path(target_repo: str, job_id: str) -> Path:
    """Dedicated per-record lockfile beside the record. Serializes the
    read/merge/write of a single record ACROSS PROCESSES — the in-flight
    delegation (this MCP server) and the SessionEnd cleanup hook (a separate
    process) can otherwise interleave their read-modify-write and clobber each
    other's terminal transition (e.g. a worker patch reviving an `orphaned`
    record cleanup just wrote)."""
    return resolve_state_dir(target_repo) / f".{_validated_job_id(job_id)}.json.lock"


def _write_json(path: Path, value: dict) -> None:
    # Write to a unique temp file in the same dir, then atomically rename over
    # the target. A crash or a concurrent reader therefore never sees a
    # half-written (truncated) record — os.replace is atomic on POSIX and
    # Windows, and the reader sees either the old file or the complete new one.
    data = json.dumps(value, sort_keys=True) + "\n"
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(data)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _upsert_index(target_repo: str, record: dict) -> None:
    """Merge a record's compact view into the shared, bounded index."""
    state_dir = resolve_state_dir(target_repo)
    index_path = state_dir / _INDEX_NAME
    summary = {key: record.get(key) for key in ("id", "status", "phase", "updatedAt")}
    # Guard the per-workspace index with its OWN dedicated lockfile via the
    # shared cross-platform primitive. Do NOT reuse config's CONFIG_PATH-
    # derived lock: each workspace has a distinct index with its own lockfile,
    # via the shared stdlib-only locking primitive.
    lock_path = index_path.parent / f".{index_path.name}.lock"
    with file_lock(lock_path):
        index = None
        if index_path.exists():
            # Tolerate a truncated/partial index (json.loads raises) or a valid-
            # but-non-object index (`[]`, `null` -> .get raises AttributeError),
            # exactly like list_records tolerates bad record files. Without this,
            # a corrupt index would make EVERY later upsert fail — and worse, in
            # cleanup_session it would raise AFTER the record was marked orphaned
            # on disk but BEFORE the id is appended to `cleaned`, so SessionEnd
            # reports the wrong cleaned set. Rebuild from empty instead.
            try:
                parsed = json.loads(index_path.read_text())
                if isinstance(parsed, dict):
                    index = parsed
            except Exception as exc:
                _warn("_upsert_index: rebuilding corrupt index", exc)
        if index is None:
            index = {"version": 1, "jobs": []}
        jobs = [job for job in index.get("jobs", [])
                if isinstance(job, dict) and job.get("id") != summary["id"]]
        jobs.append(summary)
        jobs.sort(key=lambda job: job.get("updatedAt") or "", reverse=True)
        _write_json(index_path, {"version": 1, "jobs": jobs[:_MAX_INDEX_JOBS]})


def _save_record(target_repo: str, record: dict, *, update_index: bool = True) -> None:
    record["updatedAt"] = _now()
    _write_json(_record_path(target_repo, record["id"]), record)
    if update_index:
        _upsert_index(target_repo, record)


# States a SessionEnd cleanup (or an earlier terminal write) may have already
# persisted. Once a record reaches one of these on disk, a late patch (set_pid/
# finalize/progress/sweep) from a still-running-in-memory attempt must NOT
# resurrect it back to an active/other-terminal state.
_TERMINAL_STATUSES = frozenset({"completed", "failed", "orphaned"})


def _locked_record_update(target_repo, job_id, mutate, *, fallback=None):
    """Serialize a read-modify-write of ONE record under its per-record lock.

    `mutate(current)` receives the freshly-read on-disk record (already checked
    to be a dict) and should mutate it in place; it returns True to persist the
    change or False to skip the write. Runs under the same per-record lock that
    _patch_on_disk / cleanup_session use, so EVERY writer of a given record —
    set_pid, finalize, ProgressUpdater, sweep_orphans, cleanup_session — is
    mutually exclusive and always sees the latest on-disk state. Without this,
    a stale progress/sweep write could clobber a concurrent `orphaned`
    transition back to `running`.

    Returns the persisted record dict, or None if nothing was written.
    """
    with file_lock(_record_lock_path(target_repo, job_id)):
        try:
            current = read_record(_record_path(target_repo, job_id))
            if not isinstance(current, dict):
                current = None
        except Exception:
            current = None
        if current is None:
            if fallback is None:
                return None
            current = dict(fallback)
        if mutate(current):
            _save_record(target_repo, current)
            return current
        return None


def _patch_on_disk(record: dict, fields: dict) -> None:
    """Apply `fields` onto the CURRENT on-disk record, not the stale in-memory
    one the caller has held since create_record.

    Rationale: ProgressUpdater writes phase/latestActivity straight to disk on
    a fresh read each time and never touches the caller's in-memory object.
    Writing that whole stale object back (as set_pid/finalize used to) reverts
    those live fields. So we re-read, merge only our fields, and write.

    If the on-disk record is already terminal — e.g. SessionEnd cleanup marked
    it `orphaned` — we do NOT overwrite its status/phase/completion; a killed
    attempt must not revive a record the session teardown already retired. We
    still record a non-status field like `pid` so a cleared pid stays cleared.

    If the on-disk record is MISSING (pruned after it went terminal), we do NOT
    recreate it from the caller's stale in-memory copy — that copy may still say
    `running`, which would resurrect a finished/cleaned-up record and defeat
    SessionEnd cleanup. `fallback=None` makes a missing record a no-op.
    """
    target_repo = record["targetRepo"]

    def _apply(current):
        disk_terminal = current.get("status") in _TERMINAL_STATUSES
        for key, value in fields.items():
            if disk_terminal:
                # The record is already terminal (e.g. SessionEnd cleanup
                # marked it orphaned and cleared its pid). A late attempt must
                # NOT resurrect it:
                #  - status/phase/completion fields: keep the terminal outcome.
                #  - pid: only a CLEAR (None) is allowed. Writing a live pid
                #    back onto a retired record would leave a running,
                #    UNTRACKED process — cleanup already ran and won't revisit
                #    a terminal record, so nothing would ever reap it.
                #  - any other late metadata (pidStart, model, ...): ignore.
                if key == "pid" and value is None:
                    current[key] = None
                continue
            current[key] = value
        return True

    persisted = _locked_record_update(
        target_repo, record["id"], _apply, fallback=None)
    if persisted is not None:
        # Keep the caller's in-memory view coherent with what we persisted.
        record.update(persisted)


def create_record(target_repo, branch, model, output_log, session_id=None) -> dict:
    """Create and persist a new running delegation record."""
    record = {}
    try:
        sweep_orphans(target_repo)
        now = _now()
        record = {
            "id": generate_job_id(),
            "sessionId": session_id,
            "status": "running",
            "phase": "starting",
            "latestActivity": None,
            "model": model,
            "targetRepo": target_repo,
            "branch": branch,
            "pid": None,
            "outputLog": output_log,
            "createdAt": now,
            "updatedAt": now,
            "completedAt": None,
            "commitSha": None,
            "errorMessage": None,
        }
        _write_json(_record_path(target_repo, record["id"]), record)
        _upsert_index(target_repo, record)
    except Exception as exc:
        _warn("create_record", exc)
    return record


class ProgressUpdater:
    """Coalesce streaming output and activity updates for one job."""

    def __init__(self, target_repo, job_id):
        self.target_repo = target_repo
        self.job_id = job_id
        self.last_phase = "starting"
        self.last_activity = None

    def on_output(self, chunk: str) -> None:
        try:
            if not chunk or not chunk.strip() or self.last_phase != "starting":
                return

            def _apply(current):
                # Never move a record that is already terminal (orphaned by
                # SessionEnd cleanup, or finalized) — a late progress write
                # must not revive it. Skip once it has left "starting".
                if current.get("status") in _TERMINAL_STATUSES:
                    self.last_phase = current.get("phase")
                    return False
                if current.get("phase") != "starting":
                    self.last_phase = current.get("phase")
                    return False
                current["phase"] = "working"
                return True

            _locked_record_update(self.target_repo, self.job_id, _apply)
            if self.last_phase == "starting":
                self.last_phase = "working"
        except Exception as exc:
            _warn("ProgressUpdater.on_output", exc)

    def on_activity(self, line: str) -> None:
        try:
            if line == self.last_activity:
                return

            def _apply(current):
                # A late activity write must not touch a record a concurrent
                # cleanup/finalize already retired.
                if current.get("status") in _TERMINAL_STATUSES:
                    return False
                current["latestActivity"] = line
                return True

            _locked_record_update(self.target_repo, self.job_id, _apply)
            self.last_activity = line
        except Exception as exc:
            _warn("ProgressUpdater.on_activity", exc)


def _pid_start_time(pid) -> str | None:
    """Best-effort process start-time string, used as a weak identity token to
    guard against PID reuse. `ps -o lstart` works on macOS and Linux; returns
    None if it can't be determined (in which case callers must not treat a
    live pid as definitely-ours)."""
    try:
        out = subprocess.run(
            ["ps", "-o", "lstart=", "-p", str(pid)],
            capture_output=True, text=True, timeout=5,
        )
        if out.returncode == 0 and out.stdout.strip():
            return out.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        pass
    return None


def set_pid(record, pid) -> None:
    """Associate a spawned process with a record, capturing a start-time token
    so later cleanup can detect PID reuse before terminating. Passing pid=None
    clears the association (used between failover attempts)."""
    try:
        _patch_on_disk(record, {
            "pid": pid,
            "pidStart": _pid_start_time(pid) if pid is not None else None,
        })
    except Exception as exc:
        _warn("set_pid", exc)


def is_terminal_on_disk(record) -> bool:
    """True if the persisted record has reached a terminal status (completed,
    failed, or orphaned). Used by the delegation loop to stop starting new
    fallback attempts once SessionEnd cleanup has retired the record mid-run.
    Best-effort: a failed read reports False (don't block work on a read hiccup).
    """
    try:
        current = read_record(_record_path(record["targetRepo"], record["id"]))
        return isinstance(current, dict) and current.get("status") in _TERMINAL_STATUSES
    except Exception:
        return False


# Phase marking a delegation whose backend has finished and whose local commit
# exists, but which is still doing post-processing (git push, PR creation).
# The pid is already cleared here, so an orphan sweep or SessionEnd cleanup that
# only checks `status == "running"` would otherwise mark this LANDED work
# `orphaned` before finalize can record it as completed. sweep_orphans and
# cleanup_session skip a running record in this phase for that reason.
_COMMITTING_PHASE = "committing"


def mark_committing(record) -> None:
    """Move a still-`running` record into the non-orphanable committing phase
    once its backend has returned successfully and the local commit exists, so
    slow push/PR work can't be misreported as orphaned. Leaves a terminal
    record untouched (the terminal guard in _patch_on_disk handles that)."""
    try:
        _patch_on_disk(record, {"phase": _COMMITTING_PHASE})
    except Exception as exc:
        _warn("mark_committing", exc)


def begin_committing(record) -> None:
    """Clear the backend pid AND enter the committing phase in ONE locked
    transition. Doing these as two writes (set_pid(None) then mark_committing)
    left an intermediate `running`/pid=None state a concurrent SessionEnd
    cleanup could observe and orphan — even though the delegation had already
    committed. Applying both fields under a single _patch_on_disk closes that
    window. A terminal on-disk record is preserved by the guard."""
    try:
        _patch_on_disk(record, {"pid": None, "pidStart": None,
                                "phase": _COMMITTING_PHASE})
    except Exception as exc:
        _warn("begin_committing", exc)


def set_model(record, model) -> None:
    """Record the model this attempt is actually using. Called before each
    failover attempt so /status reports the model that performed (or is
    performing) the delegation, not just the primary from config."""
    try:
        _patch_on_disk(record, {"model": model})
    except Exception as exc:
        _warn("set_model", exc)


def finalize(record, *, status, phase, commit_sha=None, error_message=None) -> None:
    """Persist a terminal record state."""
    try:
        _patch_on_disk(record, {
            "status": status,
            "phase": phase,
            "pid": None,
            "completedAt": _now(),
            "commitSha": commit_sha,
            "errorMessage": error_message,
        })
    except Exception as exc:
        _warn("finalize", exc)


def list_records(target_repo, all_sessions=False, session_id=None) -> list[dict]:
    """Return persisted records newest first, optionally scoped to a session.

    A single record file that is unreadable or mid-write (partial JSON from a
    concurrent, non-atomic write) is skipped rather than failing the whole
    listing — the status command and the orphan sweep must tolerate one bad
    file. read_record stays strict for single-record reads.
    """
    records = []
    for path in resolve_state_dir(target_repo).glob("lc-*.json"):
        try:
            parsed = read_record(path)
        except Exception as exc:
            # A file mid-write (partial JSON) is the common benign case, but a
            # genuine permission/disk fault would otherwise vanish with no
            # trace. Emit a stderr warning like the rest of the module, then
            # skip so one bad file never blanks the whole listing.
            _warn(f"list_records: skipping {path.name}", exc)
            continue
        # Skip valid-JSON-but-non-object files too (e.g. `[]`, `null`, `42`):
        # they parse cleanly but every consumer here calls record.get(...),
        # which would raise AttributeError on a non-dict and blank the whole
        # listing / the orphan sweep.
        if not isinstance(parsed, dict):
            continue
        records.append(parsed)
    if not all_sessions and session_id is not None:
        records = [record for record in records if record.get("sessionId") == session_id]
    return sorted(records, key=lambda record: record.get("updatedAt") or "", reverse=True)


def _as_valid_pid(pid):
    """Return pid as a positive int, or None if it is missing/malformed.
    A hand-edited or partially-corrupted record can carry a non-int pid
    (e.g. the string "123"); passing that to os.kill raises TypeError, which
    could otherwise abort a sweep/cleanup mid-scan. bool is rejected (it is an
    int subclass but never a real pid)."""
    if isinstance(pid, bool) or not isinstance(pid, int):
        return None
    return pid if pid > 0 else None


def _pid_alive(pid) -> bool:
    pid = _as_valid_pid(pid)
    if pid is None:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError as exc:
        if exc.errno == errno.ESRCH:
            return False
        if exc.errno == errno.EPERM:
            return True
        return False
    return True


def _age_seconds(iso_ts) -> float | None:
    """Seconds since an ISO-8601 UTC timestamp string, or None if unparseable."""
    if not iso_ts:
        return None
    try:
        parsed = time.strptime(iso_ts, "%Y-%m-%dT%H:%M:%SZ")
        return time.time() - calendar.timegm(parsed)
    except (ValueError, TypeError):
        return None


def _prune_records(target_repo) -> None:
    """Bound the state directory: keep only the newest _MAX_INDEX_JOBS records
    (by updatedAt), and only prune TERMINAL ones — a still-running/committing
    record is never deleted regardless of age. Deletes each pruned record's
    `<id>.json` AND its `.<id>.json.lock`. Without this, two files accumulate
    per delegation forever and list_records re-parses the whole history on every
    create_record / SessionEnd. Non-fatal: never raises to the caller."""
    try:
        records = list_records(target_repo, all_sessions=True)
        if len(records) <= _MAX_INDEX_JOBS:
            return
        # records are newest-first; everything past the cap is a prune candidate.
        state_dir = resolve_state_dir(target_repo)
        for record in records[_MAX_INDEX_JOBS:]:
            if record.get("status") not in _TERMINAL_STATUSES:
                continue  # keep live records even beyond the cap
            # Do NOT prune a FRESHLY-terminal record: a worker that just
            # finalized (or that cleanup just orphaned) may still be mid-write,
            # and deleting the file now would let its next patch see a missing
            # record. Since _patch_on_disk no longer resurrects a missing file,
            # such a patch would be silently dropped — but the record would also
            # vanish from /status prematurely. Only prune once the record has
            # been terminal longer than the prune grace (off updatedAt).
            age = _age_seconds(record.get("updatedAt"))
            if age is None or age <= _PRUNE_MIN_AGE_SECONDS:
                continue
            job_id = record.get("id")
            try:
                _validated_job_id(job_id)
            except Exception:
                continue
            for path in (state_dir / f"{job_id}.json",
                         state_dir / f".{job_id}.json.lock"):
                try:
                    path.unlink()
                except OSError:
                    pass
    except Exception as exc:
        _warn("_prune_records", exc)


def sweep_orphans(target_repo) -> None:
    """Mark no-longer-running processes as orphaned without raising to callers.

    Two orphan conditions:
      1. a running record whose pid is dead (the process exited abnormally);
      2. a running record that never received a pid (a crash between
         create_record and on_start) and is older than _NO_PID_ORPHAN_SECONDS —
         without this, such a record would stay "running" forever, since the
         pid-liveness check can't inspect a None pid.

    Each record is handled in its own try/except so one malformed or racing
    record can't abort the sweep for all the others.
    """
    try:
        # Snapshot only to enumerate ids cheaply; the actual mutation re-reads
        # and decides under the per-record lock so a concurrent
        # finalize/cleanup/progress write can't be clobbered.
        for snapshot in list_records(target_repo, all_sessions=True):
            job_id = snapshot.get("id")
            if not job_id:
                continue
            try:
                def _apply(current):
                    if current.get("status") != "running":
                        return False  # already terminal or changed since snapshot
                    if current.get("phase") == _COMMITTING_PHASE:
                        # Backend finished, local commit exists, push/PR in
                        # progress — landed work, not an orphan while it's still
                        # progressing. But a crash/cancellation during push/PR
                        # leaves nothing to finalize it, so retire it once it has
                        # sat in committing past the grace window (aged off
                        # updatedAt, which mark_committing refreshed). Fresh
                        # committing records stay protected.
                        age = _age_seconds(current.get("updatedAt"))
                        if age is None or age <= _COMMITTING_ORPHAN_SECONDS:
                            return False
                        current["status"] = "orphaned"
                        current["phase"] = "failed"
                        current["completedAt"] = _now()
                        current["errorMessage"] = (
                            "stuck in committing (push/PR) past "
                            f"{_COMMITTING_ORPHAN_SECONDS}s; retired by sweep"
                        )
                        return True
                    pid = _as_valid_pid(current.get("pid"))
                    if pid is not None:
                        if _pid_alive(pid):
                            return False
                    else:
                        # No usable pid — orphan only past the startup grace
                        # window, so an in-flight delegation that hasn't reached
                        # on_start yet isn't mistaken for a crash.
                        age = _age_seconds(current.get("createdAt"))
                        if age is None or age <= _NO_PID_ORPHAN_SECONDS:
                            return False
                    current["status"] = "orphaned"
                    current["phase"] = "failed"
                    current["completedAt"] = _now()
                    return True

                _locked_record_update(target_repo, job_id, _apply)
            except Exception as inner:
                _warn("sweep_orphans(record)", inner)
                continue
    except Exception as exc:
        _warn("sweep_orphans", exc)
    # Bound the state dir after each sweep (sweep runs on every create_record).
    _prune_records(target_repo)


def _terminate_process_tree(pid: int,
                            grace_seconds: float = _DEFAULT_TERMINATE_GRACE_SECONDS) -> None:
    """Best-effort terminate a delegation process — SIGTERM, then SIGKILL
    after a short grace. Never raises.

    We signal the process GROUP only when `pid` is its OWN group leader
    (pgid == pid), so killing the group reaches the backend's children (git,
    the model runner) without ever touching an unrelated shared group. A
    delegation subprocess that was NOT started in its own session shares this
    process's group — signalling that group would kill this process (and, in
    tests, the test runner) — so in that case we signal only the pid itself.

    On Windows there is no POSIX process group: `start_new_session` does not
    create one and `os.killpg`/signals don't apply. Fall back to `taskkill
    /F /T /PID`, which terminates the process AND its whole child tree, so the
    aider descendants (git, the model runner) are reaped rather than stranded.
    """
    if os.name == "nt":
        # Respect an exhausted budget: with no grace left, don't spend even the
        # taskkill wait — SessionEnd clamps grace to the deadline remainder, and
        # a forced ≥1s wait per record would let several records overrun the
        # hook timeout. The pid is already marked orphaned; leave it to the next
        # run's sweep.
        if grace_seconds <= 0:
            return
        try:
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(pid)],
                capture_output=True, timeout=grace_seconds,
            )
        except Exception as exc:
            _warn("_terminate_process_tree(taskkill)", exc)
        return

    def _signal_pid(sig):
        try:
            os.kill(pid, sig)
        except (ProcessLookupError, PermissionError, OSError):
            pass

    try:
        group_target = None
        if hasattr(os, "killpg") and hasattr(os, "getpgid"):
            try:
                pgid = os.getpgid(pid)
                # Only safe to group-kill if pid leads its own group.
                if pgid == pid:
                    group_target = pgid
            except OSError:
                group_target = None

        def _send(sig):
            if group_target is not None:
                try:
                    os.killpg(group_target, sig)
                    return
                except (ProcessLookupError, PermissionError, OSError):
                    pass
            _signal_pid(sig)

        _send(signal.SIGTERM)
        deadline = time.monotonic() + grace_seconds
        while time.monotonic() < deadline:
            if not _pid_alive(pid):
                break
            time.sleep(0.1)
        # Always send a final SIGKILL. When we have a group target, the leader
        # may exit before a grandchild (e.g. a git subprocess aider spawned);
        # returning on leader-death alone would strand that grandchild. A
        # SIGKILL to an already-empty group/pid is a harmless no-op.
        _send(signal.SIGKILL)
    except Exception as exc:
        _warn("_terminate_process_tree", exc)


def _pid_is_ours(record) -> bool:
    """Only true when the record's pid is alive AND its current start-time
    token still matches the one captured at spawn — so a recycled PID now
    belonging to an unrelated process is NOT treated as ours. If no start
    token was captured (older record, or ps unavailable), be conservative and
    do NOT claim it (avoid killing a possibly-unrelated process)."""
    pid = record.get("pid")
    if pid is None or not _pid_alive(pid):
        return False
    stored = record.get("pidStart")
    if not stored:
        return False
    return _pid_start_time(pid) == stored


def cleanup_session(target_repo, session_id, deadline_seconds: float = 12.0) -> list[str]:
    """SessionEnd cleanup: terminate any still-running delegation belonging to
    this session and mark its record orphaned. Returns the ids cleaned up.
    Never raises — observability/cleanup must not fail session teardown.

    Bounded by an overall deadline so that many unresponsive delegations can't
    make the SessionEnd hook exceed its own timeout: once the deadline passes,
    remaining records are still MARKED orphaned (cheap) but not waited-on for
    termination.
    """
    cleaned = []
    if not session_id:
        return cleaned
    overall_deadline = time.monotonic() + deadline_seconds
    try:
        for snapshot in list_records(target_repo, all_sessions=True):
            # Handle each record in its own guard so one malformed record can't
            # abort cleanup for the rest of this session's delegations.
            try:
                if snapshot.get("sessionId") != session_id:
                    continue
                job_id = snapshot.get("id")
                if not job_id:
                    continue

                # Mark the record orphaned ATOMICALLY under its per-record lock,
                # re-reading the latest on-disk state inside the lock (via the
                # shared _locked_record_update path every other writer uses).
                # This closes the check/spawn race with an in-flight delegation:
                #  - re-reading picks up a pid a racing on_start just set, so we
                #    don't miss a just-started process;
                #  - writing `orphaned` inside the lock means a concurrent
                #    progress/patch write can no longer revive the record.
                # We capture the pid to terminate inside the lock, then do the
                # (potentially slow) SIGTERM/SIGKILL OUTSIDE it so we don't block
                # workers for the grace period.
                holder = {"terminate": None, "token": None}

                def _apply(current, _holder=holder):
                    if current.get("status") != "running":
                        return False
                    pid = _as_valid_pid(current.get("pid"))
                    if current.get("phase") == _COMMITTING_PHASE and pid is None:
                        # Committing with no tracked pid: either the brief window
                        # BETWEEN two tracked push/PR subprocesses (in-flight call
                        # about to finalize), or a crashed committing record no
                        # one will finalize. Distinguish by age off updatedAt,
                        # exactly like sweep_orphans: a FRESH committing record is
                        # left alone (the in-flight call finalizes it, and
                        # orphaning it would trip the terminal guard); a STALE one
                        # past the committing grace is retired here so it doesn't
                        # stay reported `running` until another delegation starts.
                        age = _age_seconds(current.get("updatedAt"))
                        if age is None or age <= _COMMITTING_ORPHAN_SECONDS:
                            return False
                        current["status"] = "orphaned"
                        current["phase"] = "failed"
                        current["completedAt"] = _now()
                        current["errorMessage"] = (
                            "stuck in committing (push/PR) past "
                            f"{_COMMITTING_ORPHAN_SECONDS}s; retired by cleanup"
                        )
                        return True
                    if pid is not None and time.monotonic() < overall_deadline \
                            and _pid_is_ours(current):
                        _holder["terminate"] = pid
                        _holder["token"] = current.get("pidStart")
                    elif pid is not None:
                        # We're about to mark the record terminal and clear its
                        # pid, but we are NOT terminating this process (deadline
                        # expired, or _pid_is_ours was false — e.g. no pidStart
                        # token). Clearing pid outright would erase the only
                        # record of a possibly-still-live process: no later sweep
                        # can act (record is terminal), and _patch_on_disk refuses
                        # to write a live pid back. Preserve it under a
                        # non-authoritative key so /status and an operator can see
                        # the potential leak.
                        current["leakedPid"] = pid
                    current["status"] = "orphaned"
                    current["phase"] = "failed"
                    current["completedAt"] = _now()
                    current["pid"] = None
                    return True

                persisted = _locked_record_update(target_repo, job_id, _apply)
                if persisted is not None:
                    cleaned.append(job_id)
                if holder["terminate"] is not None \
                        and time.monotonic() < overall_deadline:
                    # Once the overall deadline has passed, SKIP the revalidation
                    # and termination entirely: the record is already marked
                    # orphaned, and the revalidation alone (_pid_start_time runs
                    # a `ps` with its own multi-second timeout) plus the grace
                    # wait could push SessionEnd past its hook timeout. The pid is
                    # left for the next run's sweep.
                    #
                    # Re-validate the start-time token IMMEDIATELY before
                    # signalling. Between releasing the record lock and killing,
                    # the OS could recycle the pid onto an unrelated process; the
                    # token check inside _apply alone leaves that narrow window.
                    # A token mismatch now means the pid is no longer ours — skip
                    # the kill rather than signal a stranger's process.
                    if _pid_start_time(holder["terminate"]) == holder["token"]:
                        # Bound the SIGTERM grace by the budget left on the
                        # overall deadline, not the full default grace. Otherwise
                        # every live record could add up to the default grace on
                        # top of the deadline, letting SessionEnd overrun its
                        # hook timeout.
                        remaining = overall_deadline - time.monotonic()
                        grace = max(0.0, min(_DEFAULT_TERMINATE_GRACE_SECONDS, remaining))
                        _terminate_process_tree(holder["terminate"], grace_seconds=grace)
            except Exception as inner:
                _warn("cleanup_session(record)", inner)
                continue
    except Exception as exc:
        _warn("cleanup_session", exc)
    return cleaned
