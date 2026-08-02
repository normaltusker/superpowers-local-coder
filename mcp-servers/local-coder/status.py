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
        slug = _SLUG_RE.sub("-", os.path.basename(target_repo)).strip("-") or "workspace"
        digest = hashlib.sha256(os.path.realpath(target_repo).encode()).hexdigest()[:16]
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


def _record_path(target_repo: str, job_id: str) -> Path:
    return resolve_state_dir(target_repo) / f"{job_id}.json"


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
        if index_path.exists():
            index = json.loads(index_path.read_text())
        else:
            index = {"version": 1, "jobs": []}
        jobs = [job for job in index.get("jobs", []) if job.get("id") != summary["id"]]
        jobs.append(summary)
        jobs.sort(key=lambda job: job.get("updatedAt") or "", reverse=True)
        _write_json(index_path, {"version": 1, "jobs": jobs[:_MAX_INDEX_JOBS]})


def _save_record(target_repo: str, record: dict, *, update_index: bool = True) -> None:
    record["updatedAt"] = _now()
    _write_json(_record_path(target_repo, record["id"]), record)
    if update_index:
        _upsert_index(target_repo, record)


# States a SessionEnd cleanup (or an earlier terminal write) may have already
# persisted. Once a record reaches one of these on disk, a late in-memory
# patch (set_pid/finalize) from a still-running-in-memory attempt must NOT
# resurrect it back to an active/other-terminal state.
_TERMINAL_STATUSES = frozenset({"completed", "failed", "orphaned"})


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
    """
    target_repo = record["targetRepo"]
    try:
        current = read_record(_record_path(target_repo, record["id"]))
        if not isinstance(current, dict):
            current = dict(record)
    except Exception:
        # On-disk read failed — fall back to the in-memory record so we still
        # persist *something* (non-fatal contract), then apply our fields.
        current = dict(record)

    disk_terminal = current.get("status") in _TERMINAL_STATUSES
    for key, value in fields.items():
        if disk_terminal and key in ("status", "phase", "completedAt",
                                     "commitSha", "errorMessage"):
            # Preserve the terminal/orphaned outcome already on disk.
            continue
        current[key] = value

    # Keep the caller's in-memory view coherent with what we persisted.
    record.update(current)
    _save_record(target_repo, current)


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
            record = read_record(_record_path(self.target_repo, self.job_id))
            if record.get("phase") != "starting":
                self.last_phase = record.get("phase")
                return
            record["phase"] = "working"
            _save_record(self.target_repo, record)
            self.last_phase = "working"
        except Exception as exc:
            _warn("ProgressUpdater.on_output", exc)

    def on_activity(self, line: str) -> None:
        try:
            if line == self.last_activity:
                return
            record = read_record(_record_path(self.target_repo, self.job_id))
            record["latestActivity"] = line
            _save_record(self.target_repo, record)
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
        for record in list_records(target_repo, all_sessions=True):
            try:
                if record.get("status") != "running":
                    continue
                pid = _as_valid_pid(record.get("pid"))
                is_orphan = False
                if pid is not None:
                    is_orphan = not _pid_alive(pid)
                else:
                    # No usable pid — orphan it only once past the startup grace
                    # window, so an in-flight delegation that hasn't reached
                    # on_start yet isn't mistaken for a crash.
                    age = _age_seconds(record.get("createdAt"))
                    is_orphan = age is not None and age > _NO_PID_ORPHAN_SECONDS
                if is_orphan:
                    record["status"] = "orphaned"
                    record["phase"] = "failed"
                    record["completedAt"] = _now()
                    _save_record(target_repo, record)
            except Exception as inner:
                _warn("sweep_orphans(record)", inner)
                continue
    except Exception as exc:
        _warn("sweep_orphans", exc)


def _terminate_process_tree(pid: int, grace_seconds: float = 3.0) -> None:
    """Best-effort terminate a delegation process — SIGTERM, then SIGKILL
    after a short grace. Never raises.

    We signal the process GROUP only when `pid` is its OWN group leader
    (pgid == pid), so killing the group reaches the backend's children (git,
    the model runner) without ever touching an unrelated shared group. A
    delegation subprocess that was NOT started in its own session shares this
    process's group — signalling that group would kill this process (and, in
    tests, the test runner) — so in that case we signal only the pid itself.
    """
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
        for record in list_records(target_repo, all_sessions=True):
            # Handle each record in its own guard so one malformed record can't
            # abort cleanup for the rest of this session's delegations.
            try:
                if record.get("sessionId") != session_id:
                    continue
                if record.get("status") != "running":
                    continue
                # Only terminate a process we can still positively identify as
                # the delegation's own (guards PID reuse). If the deadline has
                # passed, skip the (blocking) terminate and just mark the record.
                pid = _as_valid_pid(record.get("pid"))
                if pid is not None and time.monotonic() < overall_deadline \
                        and _pid_is_ours(record):
                    _terminate_process_tree(pid)
                record["status"] = "orphaned"
                record["phase"] = "failed"
                record["completedAt"] = _now()
                record["pid"] = None
                _save_record(target_repo, record)
                cleaned.append(record.get("id"))
            except Exception as inner:
                _warn("cleanup_session(record)", inner)
                continue
    except Exception as exc:
        _warn("cleanup_session", exc)
    return cleaned
