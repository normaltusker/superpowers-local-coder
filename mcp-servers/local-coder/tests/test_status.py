import json, time
from pathlib import Path
import pytest
import config
import status

@pytest.fixture
def repo(tmp_path, monkeypatch):
    data = tmp_path / "plugindata"; data.mkdir()
    monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(data))
    r = tmp_path / "repo"; r.mkdir()
    return str(r)

def test_create_record_writes_running_record(repo):
    rec = status.create_record(repo, branch="dev", model="ollama/qwen2.5-coder:7b",
                               output_log="/tmp/x.log", session_id="sess1")
    assert rec["status"] == "running" and rec["phase"] == "starting"
    assert rec["id"].startswith("lc-")
    jf = status.resolve_state_dir(repo) / f"{rec['id']}.json"
    assert jf.exists()
    assert json.loads(jf.read_text())["sessionId"] == "sess1"

def test_progress_updater_first_chunk_moves_to_working(repo):
    rec = status.create_record(repo, "dev", "m", "/tmp/x.log")
    up = status.ProgressUpdater(repo, rec["id"])
    up.on_output("some output")
    jf = status.resolve_state_dir(repo) / f"{rec['id']}.json"
    assert status.read_record(jf)["phase"] == "working"

def test_progress_updater_dedupes_unchanged_activity(repo):
    rec = status.create_record(repo, "dev", "m", "/tmp/x.log")
    up = status.ProgressUpdater(repo, rec["id"])
    up.on_activity("line A")
    jf = status.resolve_state_dir(repo) / f"{rec['id']}.json"
    after_first_activity = jf.read_bytes()
    up.on_activity("line A")
    assert jf.read_bytes() == after_first_activity
    up.on_activity("line B")
    assert jf.read_bytes() != after_first_activity

def test_finalize_sets_terminal_state(repo):
    rec = status.create_record(repo, "dev", "m", "/tmp/x.log")
    status.finalize(rec, status="completed", phase="done", commit_sha="abc123")
    jf = json.loads((status.resolve_state_dir(repo) / f"{rec['id']}.json").read_text())
    assert jf["status"] == "completed" and jf["phase"] == "done"
    assert jf["commitSha"] == "abc123" and jf["pid"] is None and jf["completedAt"]

def test_sweep_marks_dead_pid_running_as_orphaned(repo):
    rec = status.create_record(repo, "dev", "m", "/tmp/x.log")
    status.set_pid(rec, 999999)
    status.sweep_orphans(repo)
    jf = json.loads((status.resolve_state_dir(repo) / f"{rec['id']}.json").read_text())
    assert jf["status"] == "orphaned"

def test_state_dir_is_workspace_keyed(repo, tmp_path):
    other = tmp_path / "other_repo"; other.mkdir()
    assert status.resolve_state_dir(repo) != status.resolve_state_dir(str(other))

def test_index_write_does_not_mutate_config_path(repo):
    # Regression: the index lock must NOT hijack config.CONFIG_PATH. A write
    # that mutated the process-global would race concurrent writers and
    # corrupt a concurrent config read/write.
    before = config.CONFIG_PATH
    rec = status.create_record(repo, "dev", "m", "/tmp/x.log")
    status.finalize(rec, status="completed", phase="done")
    assert config.CONFIG_PATH == before

def test_index_lockfile_is_workspace_local(repo):
    # The index is guarded by its OWN dedicated lockfile beside state.json,
    # not config's lockfile.
    status.create_record(repo, "dev", "m", "/tmp/x.log")
    state_dir = status.resolve_state_dir(repo)
    assert (state_dir / ".state.json.lock").exists()


def test_cleanup_session_kills_live_orphan_and_marks_orphaned(repo):
    import subprocess, time
    proc = subprocess.Popen(["sleep", "30"])
    try:
        rec = status.create_record(repo, "dev", "m", "/tmp/x.log", session_id="S1")
        status.set_pid(rec, proc.pid)
        cleaned = status.cleanup_session(repo, "S1")
        assert rec["id"] in cleaned
        # process terminated
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and proc.poll() is None:
            time.sleep(0.05)
        assert proc.poll() is not None, "orphan process was not terminated"
        jf = json.loads((status.resolve_state_dir(repo) / f"{rec['id']}.json").read_text())
        assert jf["status"] == "orphaned" and jf["phase"] == "failed" and jf["pid"] is None
    finally:
        if proc.poll() is None:
            proc.kill()


def test_cleanup_session_marks_dead_pid_orphan_without_error(repo):
    rec = status.create_record(repo, "dev", "m", "/tmp/x.log", session_id="S2")
    status.set_pid(rec, 999999)  # not alive
    cleaned = status.cleanup_session(repo, "S2")
    assert rec["id"] in cleaned
    jf = json.loads((status.resolve_state_dir(repo) / f"{rec['id']}.json").read_text())
    assert jf["status"] == "orphaned"


def test_cleanup_session_ignores_other_sessions(repo):
    rec = status.create_record(repo, "dev", "m", "/tmp/x.log", session_id="MINE")
    status.set_pid(rec, 999999)
    cleaned = status.cleanup_session(repo, "OTHER")
    assert cleaned == []
    jf = json.loads((status.resolve_state_dir(repo) / f"{rec['id']}.json").read_text())
    assert jf["status"] == "running"  # untouched


def test_list_records_skips_unreadable_file(repo):
    good = status.create_record(repo, "dev", "m", "/tmp/x.log")
    # A partial/corrupt record file must not blank the whole listing.
    bad = status.resolve_state_dir(repo) / "lc-bad-partial.json"
    bad.write_text("{ this is not valid json")
    records = status.list_records(repo, all_sessions=True)
    ids = [r["id"] for r in records]
    assert good["id"] in ids


def test_list_records_skips_valid_json_non_object(repo):
    # Valid JSON that isn't an object (`[]`, `null`, `42`, `"s"`) parses fine
    # but has no .get — it must be skipped, not crash the listing, including
    # the session-filtered path (which also calls record.get).
    good = status.create_record(repo, "dev", "m", "/tmp/x.log", session_id="S")
    sd = status.resolve_state_dir(repo)
    for name, body in [
        ("lc-arr.json", "[]"),
        ("lc-null.json", "null"),
        ("lc-num.json", "42"),
        ("lc-str.json", "\"hello\""),
    ]:
        (sd / name).write_text(body)
    # all_sessions path (sort calls .get)
    assert good["id"] in [r["id"] for r in status.list_records(repo, all_sessions=True)]
    # session-filtered path (filter + sort both call .get)
    assert good["id"] in [r["id"] for r in status.list_records(repo, session_id="S")]


def test_cleanup_session_noop_when_nothing_running(repo):
    rec = status.create_record(repo, "dev", "m", "/tmp/x.log", session_id="S3")
    status.finalize(rec, status="completed", phase="done", commit_sha="abc")
    cleaned = status.cleanup_session(repo, "S3")
    assert cleaned == []

def test_finalize_preserves_on_disk_progress(repo):
    # Regression: finalize must not clobber phase/latestActivity that the
    # ProgressUpdater wrote to disk after the caller obtained its record.
    rec = status.create_record(repo, "dev", "m", "/tmp/x.log", session_id="S")
    up = status.ProgressUpdater(repo, rec["id"])
    up.on_output("compiling")            # phase -> working on disk
    up.on_activity("Applied edit to foo.py")
    status.finalize(rec, status="completed", phase="done", commit_sha="abc")
    jf = status.read_record(status._record_path(repo, rec["id"]))
    assert jf["status"] == "completed" and jf["phase"] == "done"
    assert jf["latestActivity"] == "Applied edit to foo.py"

def test_set_pid_preserves_on_disk_progress(repo):
    # set_pid on a fallback attempt must not revert a working record to
    # starting or drop the latest activity written on disk.
    rec = status.create_record(repo, "dev", "m", "/tmp/x.log")
    up = status.ProgressUpdater(repo, rec["id"])
    up.on_output("x"); up.on_activity("line A")
    status.set_pid(rec, 4242)
    jf = status.read_record(status._record_path(repo, rec["id"]))
    assert jf["phase"] == "working" and jf["latestActivity"] == "line A"
    assert jf["pid"] == 4242

def test_finalize_does_not_revive_orphaned_record(repo):
    # A record marked orphaned by SessionEnd cleanup must survive a late
    # finalize from a still-running-in-memory attempt.
    rec = status.create_record(repo, "dev", "m", "/tmp/x.log", session_id="S")
    d = status.read_record(status._record_path(repo, rec["id"]))
    d["status"] = "orphaned"; d["phase"] = "failed"
    status._write_json(status._record_path(repo, rec["id"]), d)
    status.finalize(rec, status="completed", phase="done", commit_sha="xyz")
    jf = status.read_record(status._record_path(repo, rec["id"]))
    assert jf["status"] == "orphaned" and jf["phase"] == "failed"

def test_set_model_persists_active_model(repo):
    rec = status.create_record(repo, "dev", "ollama/primary", "/tmp/x.log")
    status.set_model(rec, "ollama/fallback")
    jf = status.read_record(status._record_path(repo, rec["id"]))
    assert jf["model"] == "ollama/fallback"

def test_is_terminal_on_disk_reflects_persisted_status(repo):
    rec = status.create_record(repo, "dev", "m", "/tmp/x.log")
    assert status.is_terminal_on_disk(rec) is False
    status.finalize(rec, status="failed", phase="failed", error_message="boom")
    assert status.is_terminal_on_disk(rec) is True

def test_cleanup_session_survives_malformed_pid_record(repo):
    # A hand-corrupted record with a string pid must not abort cleanup for the
    # rest of the session's records.
    bad = status.create_record(repo, "dev", "m", "/tmp/x.log", session_id="S")
    d = status.read_record(status._record_path(repo, bad["id"])); d["pid"] = "123"
    status._write_json(status._record_path(repo, bad["id"]), d)
    good = status.create_record(repo, "dev", "m", "/tmp/y.log", session_id="S")
    status.set_pid(good, 999999)  # dead pid
    cleaned = status.cleanup_session(repo, "S")
    assert bad["id"] in cleaned and good["id"] in cleaned

def test_sweep_survives_malformed_pid_record(repo):
    bad = status.create_record(repo, "dev", "m", "/tmp/x.log")
    d = status.read_record(status._record_path(repo, bad["id"])); d["pid"] = "abc"
    status._write_json(status._record_path(repo, bad["id"]), d)
    other = status.create_record(repo, "dev", "m", "/tmp/y.log")
    status.set_pid(other, 999999)
    status.sweep_orphans(repo)  # must not raise
    assert status.read_record(status._record_path(repo, other["id"]))["status"] == "orphaned"

def test_sweep_orphans_stale_pidless_running_record(repo):
    rec = status.create_record(repo, "dev", "m", "/tmp/x.log")
    d = status.read_record(status._record_path(repo, rec["id"]))
    d["createdAt"] = "2000-01-01T00:00:00Z"  # far in the past, pid still None
    status._write_json(status._record_path(repo, rec["id"]), d)
    status.sweep_orphans(repo)
    assert status.read_record(status._record_path(repo, rec["id"]))["status"] == "orphaned"

def test_sweep_keeps_fresh_pidless_running_record(repo):
    # A just-created record that hasn't reached on_start yet (pid None) must
    # NOT be orphaned — it's still inside the startup window.
    rec = status.create_record(repo, "dev", "m", "/tmp/x.log")
    status.sweep_orphans(repo)
    assert status.read_record(status._record_path(repo, rec["id"]))["status"] == "running"

def test_terminate_sends_final_group_sigkill_after_leader_dies(monkeypatch):
    # Regression (grandchild survival): even when the group leader exits after
    # SIGTERM, the final SIGKILL must still go to the group so a grandchild in
    # the same group is reaped. Simulate pid as its own group leader.
    import signal as _sig
    sent = []
    monkeypatch.setattr(status.os, "getpgid", lambda pid: pid)
    monkeypatch.setattr(status.os, "killpg", lambda pgid, sig: sent.append(("pg", sig)))
    monkeypatch.setattr(status.os, "kill", lambda pid, sig: None)
    # Leader is already dead the moment we check, so the loop breaks immediately.
    monkeypatch.setattr(status, "_pid_alive", lambda pid: False)
    status._terminate_process_tree(1234, grace_seconds=0.2)
    assert ("pg", _sig.SIGTERM) in sent
    assert ("pg", _sig.SIGKILL) in sent  # the fix: SIGKILL still fires
