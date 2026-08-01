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
    m1 = jf.stat().st_mtime_ns; time.sleep(0.01)
    up.on_activity("line A"); assert jf.stat().st_mtime_ns == m1
    up.on_activity("line B"); assert jf.stat().st_mtime_ns != m1

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


def test_cleanup_session_noop_when_nothing_running(repo):
    rec = status.create_record(repo, "dev", "m", "/tmp/x.log", session_id="S3")
    status.finalize(rec, status="completed", phase="done", commit_sha="abc")
    cleaned = status.cleanup_session(repo, "S3")
    assert cleaned == []
