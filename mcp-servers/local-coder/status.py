"""Persistent, workspace-scoped status records for local-coder delegations."""

import errno
import hashlib
import json
import os
import re
import signal
import sys
import tempfile
import time
from pathlib import Path

import config


_SLUG_RE = re.compile(r"[^A-Za-z0-9._-]+")
_INDEX_NAME = "state.json"
_MAX_INDEX_JOBS = 50


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
            root = config.plugin_data_dir() / "status"
        else:
            root = Path(tempfile.gettempdir()) / "local-coder-status"
        state_dir = root / f"{slug}-{digest}"
        state_dir.mkdir(parents=True, exist_ok=True)
        return state_dir
    except Exception as exc:
        _warn("resolve_state_dir", exc)
        # This fallback preserves the non-fatal public-write contract even
        # when a configured persistent location is inaccessible.
        fallback = Path(tempfile.gettempdir()) / "local-coder-status" / "workspace-unknown"
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
    path.write_text(json.dumps(value, sort_keys=True) + "\n")


def _upsert_index(target_repo: str, record: dict) -> None:
    """Merge a record's compact view into the shared, bounded index."""
    state_dir = resolve_state_dir(target_repo)
    index_path = state_dir / _INDEX_NAME
    summary = {key: record.get(key) for key in ("id", "status", "phase", "updatedAt")}
    # Guard the per-workspace index with its OWN dedicated lockfile via the
    # shared cross-platform primitive. Do NOT reuse config's CONFIG_PATH-
    # derived lock (and never mutate config.CONFIG_PATH): each workspace has
    # a distinct index, and hijacking the process-global config path would
    # both race between concurrent writers and corrupt a concurrent config
    # read/write pointing the config lock at the wrong file.
    lock_path = index_path.parent / f".{index_path.name}.lock"
    with config.file_lock(lock_path):
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


def set_pid(record, pid: int) -> None:
    """Associate a spawned process with a record."""
    try:
        record["pid"] = pid
        _save_record(record["targetRepo"], record)
    except Exception as exc:
        _warn("set_pid", exc)


def finalize(record, *, status, phase, commit_sha=None, error_message=None) -> None:
    """Persist a terminal record state."""
    try:
        record["status"] = status
        record["phase"] = phase
        record["pid"] = None
        record["completedAt"] = _now()
        record["commitSha"] = commit_sha
        record["errorMessage"] = error_message
        _save_record(record["targetRepo"], record)
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
            records.append(read_record(path))
        except Exception:
            continue
    if not all_sessions and session_id is not None:
        records = [record for record in records if record.get("sessionId") == session_id]
    return sorted(records, key=lambda record: record.get("updatedAt") or "", reverse=True)


def _pid_alive(pid) -> bool:
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


def sweep_orphans(target_repo) -> None:
    """Mark no-longer-running processes as orphaned without raising to callers."""
    try:
        for record in list_records(target_repo, all_sessions=True):
            pid = record.get("pid")
            if record.get("status") == "running" and pid is not None and not _pid_alive(pid):
                record["status"] = "orphaned"
                record["phase"] = "failed"
                record["completedAt"] = _now()
                _save_record(target_repo, record)
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
                return
            time.sleep(0.1)
        _send(signal.SIGKILL)
    except Exception as exc:
        _warn("_terminate_process_tree", exc)


def cleanup_session(target_repo, session_id) -> list[str]:
    """SessionEnd cleanup: terminate any still-running delegation belonging to
    this session and mark its record orphaned. Returns the ids cleaned up.
    Never raises — observability/cleanup must not fail session teardown."""
    cleaned = []
    if not session_id:
        return cleaned
    try:
        for record in list_records(target_repo, all_sessions=True):
            if record.get("sessionId") != session_id:
                continue
            if record.get("status") != "running":
                continue
            pid = record.get("pid")
            if pid is not None and _pid_alive(pid):
                _terminate_process_tree(pid)
            record["status"] = "orphaned"
            record["phase"] = "failed"
            record["completedAt"] = _now()
            record["pid"] = None
            _save_record(target_repo, record)
            cleaned.append(record.get("id"))
    except Exception as exc:
        _warn("cleanup_session", exc)
    return cleaned
