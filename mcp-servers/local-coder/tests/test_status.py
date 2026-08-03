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

def test_progress_updater_dedupes_unchanged_activity(repo, monkeypatch):
    # Count real writes rather than comparing bytes: updatedAt is only
    # second-granular, so a redundant same-second write would be byte-identical
    # and a byte comparison would miss a lost dedupe early-exit. Spying on
    # _save_record catches the write itself.
    rec = status.create_record(repo, "dev", "m", "/tmp/x.log")
    up = status.ProgressUpdater(repo, rec["id"])
    writes = []
    real_save = status._save_record
    def counting_save(target_repo, record, **kw):
        writes.append(record.get("latestActivity"))
        return real_save(target_repo, record, **kw)
    monkeypatch.setattr(status, "_save_record", counting_save)
    up.on_activity("line A")
    up.on_activity("line A")   # duplicate — must NOT write
    up.on_activity("line B")   # change — must write
    assert writes == ["line A", "line B"]

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

def test_cleanup_session_rereads_pid_set_after_snapshot(repo, monkeypatch):
    # 3698391574: list_records snapshots the record; a pid set by a racing
    # on_start AFTER the snapshot must still be seen (cleanup re-reads under the
    # lock) so the just-started process is terminated, not stranded.
    rec = status.create_record(repo, "dev", "m", "/tmp/x.log", session_id="S")
    # Simulate the snapshot lacking a pid, but the on-disk record having gained
    # one after the snapshot was taken.
    status.set_pid(rec, 999999)  # dead pid on disk (post-snapshot)
    terminated = []
    monkeypatch.setattr(status, "_pid_is_ours", lambda r: r.get("pid") == 999999)
    monkeypatch.setattr(status, "_terminate_process_tree",
                        lambda pid, **k: terminated.append(pid))
    # Feed cleanup a stale snapshot (pid None) to prove it re-reads on disk.
    real_list = status.list_records
    def stale_list(target_repo, **kw):
        recs = real_list(target_repo, **kw)
        for r in recs:
            if r.get("id") == rec["id"]:
                r["pid"] = None  # snapshot is stale
        return recs
    monkeypatch.setattr(status, "list_records", stale_list)
    cleaned = status.cleanup_session(repo, "S")
    assert rec["id"] in cleaned
    assert terminated == [999999]  # re-read picked up the real pid

def test_progress_update_does_not_revive_terminal_record(repo):
    # A late ProgressUpdater write must not touch a record that a concurrent
    # cleanup/finalize already retired (all writers share the per-record lock +
    # terminal guard).
    rec = status.create_record(repo, "dev", "m", "/tmp/x.log", session_id="S")
    up = status.ProgressUpdater(repo, rec["id"])
    up.on_output("x")
    status.cleanup_session(repo, "S")  # -> orphaned
    up.on_activity("late line")
    up.on_output("more")
    jf = status.read_record(status._record_path(repo, rec["id"]))
    assert jf["status"] == "orphaned"
    assert jf["latestActivity"] is None

def test_sweep_does_not_clobber_finalized_record(repo):
    rec = status.create_record(repo, "dev", "m", "/tmp/x.log")
    status.set_pid(rec, 999999)  # dead pid
    status.finalize(rec, status="completed", phase="done", commit_sha="c")
    status.sweep_orphans(repo)  # must not flip completed -> orphaned
    jf = status.read_record(status._record_path(repo, rec["id"]))
    assert jf["status"] == "completed"

def test_patch_on_disk_preserves_concurrent_orphan(repo):
    # 3698391572: even after the record is orphaned on disk, a later patch must
    # not revive it (the terminal re-check happens inside the record lock).
    rec = status.create_record(repo, "dev", "m", "/tmp/x.log", session_id="S")
    status.cleanup_session(repo, "S")  # marks orphaned on disk
    status.set_pid(rec, 4242)          # a late worker patch
    status.finalize(rec, status="completed", phase="done", commit_sha="z")
    jf = status.read_record(status._record_path(repo, rec["id"]))
    assert jf["status"] == "orphaned" and jf["pid"] is None

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


def test_terminate_uses_taskkill_tree_on_windows(monkeypatch):
    # cubic P2: on Windows there's no POSIX process group, so tree teardown must
    # go through `taskkill /F /T /PID <pid>` to reap aider's descendants.
    monkeypatch.setattr(status.os, "name", "nt")
    calls = []
    def fake_run(cmd, **kw):
        calls.append(cmd)
        class R: pass
        return R()
    monkeypatch.setattr(status.subprocess, "run", fake_run)
    status._terminate_process_tree(4321, grace_seconds=0.2)
    assert calls == [["taskkill", "/F", "/T", "/PID", "4321"]]


def test_state_dir_same_for_symlinked_workspace(repo, tmp_path):
    # cubic P1: a symlink and its target resolve to the same real repo, so they
    # MUST key to one state dir — otherwise a delegation launched via the
    # symlink is invisible to /status and SessionEnd cleanup.
    import os
    real = tmp_path / "realrepo"; real.mkdir()
    link = tmp_path / "linkrepo"; link.symlink_to(real, target_is_directory=True)
    assert status.resolve_state_dir(str(real)) == status.resolve_state_dir(str(link))


def test_terminal_record_rejects_live_pid_but_allows_clear(repo):
    # cubic P1: once a record is terminal (orphaned by cleanup, pid cleared), a
    # late set_pid with a LIVE pid must not be written back — that would leave a
    # running, untracked process cleanup never revisits. A pid=None clear is
    # still allowed.
    rec = status.create_record(repo, "dev", "m", "/tmp/x.log", session_id="S")
    status.finalize(rec, status="orphaned", phase="failed")
    status.set_pid(rec, 424242)  # racing on_start after cleanup retired it
    disk = status.read_record(status._record_path(repo, rec["id"]))
    assert disk["pid"] is None and disk["status"] == "orphaned"
    status.set_pid(rec, None)  # a clear is still permitted on a terminal record
    assert status.read_record(status._record_path(repo, rec["id"]))["pid"] is None


def test_terminal_record_rejects_late_model_patch(repo):
    # A late set_model on a retired record must not mutate it either.
    rec = status.create_record(repo, "dev", "ollama/primary", "/tmp/x.log")
    status.finalize(rec, status="orphaned", phase="failed")
    status.set_model(rec, "ollama/late")
    assert status.read_record(status._record_path(repo, rec["id"]))["model"] == "ollama/primary"


def test_record_path_rejects_path_traversal_id(repo):
    # cubic P2: a malformed record `id` (path separators / ..) must never be
    # used as a path component, or a write could escape the workspace state dir.
    for bad in ("../evil", "lc-../x", "/etc/passwd", "no-prefix", "lc-x"):
        with pytest.raises(ValueError):
            status._record_path(repo, bad)
        with pytest.raises(ValueError):
            status._record_lock_path(repo, bad)
    # A well-formed generated id is accepted.
    rec = status.create_record(repo, "dev", "m", "/tmp/x.log")
    assert status._record_path(repo, rec["id"]).name == rec["id"] + ".json"


def test_committing_phase_is_not_orphaned_by_sweep(repo):
    # cubic P2 (#3/#17): a record whose backend finished (pid cleared) and is in
    # the committing phase is LANDED work doing push/PR — the orphan sweep must
    # not mark it orphaned.
    rec = status.create_record(repo, "dev", "m", "/tmp/x.log", session_id="S")
    status.set_pid(rec, None)
    status.mark_committing(rec)
    status.sweep_orphans(repo)
    disk = status.read_record(status._record_path(repo, rec["id"]))
    assert disk["status"] == "running" and disk["phase"] == "committing"


def test_committing_with_live_tracked_pid_is_terminated_by_cleanup(repo, monkeypatch):
    # When a push/PR subprocess is live during committing, its pid is tracked on
    # the record. SessionEnd cleanup must terminate that process AND mark the
    # record orphaned truthfully — not leave a live untracked subprocess.
    rec = status.create_record(repo, "dev", "m", "/tmp/x.log", session_id="S")
    status.mark_committing(rec)
    status.set_pid(rec, 550050)  # a tracked, live push/PR pid
    monkeypatch.setattr(status, "_pid_is_ours", lambda cur: True)
    monkeypatch.setattr(status, "_pid_start_time",
                        lambda pid: status.read_record(
                            status._record_path(repo, rec["id"])).get("pidStart"))
    killed = {}
    monkeypatch.setattr(status, "_terminate_process_tree",
                        lambda pid, grace_seconds=3.0: killed.setdefault("pid", pid))
    cleaned = status.cleanup_session(repo, "S")
    disk = status.read_record(status._record_path(repo, rec["id"]))
    assert killed.get("pid") == 550050        # the live push/PR was terminated
    assert rec["id"] in cleaned
    assert disk["status"] == "orphaned" and disk["phase"] == "failed"


def test_committing_between_tracked_calls_survives_cleanup(repo):
    # Committing with NO tracked pid (the brief window between two push/PR
    # subprocesses, or just before finalize): nothing is running to kill, and
    # orphaning would trip the terminal guard. Leave it for the in-flight call
    # to finalize (sweep age-recovery covers a real crash).
    rec = status.create_record(repo, "dev", "m", "/tmp/x.log", session_id="S")
    status.set_pid(rec, None)
    status.mark_committing(rec)
    cleaned = status.cleanup_session(repo, "S")
    disk = status.read_record(status._record_path(repo, rec["id"]))
    assert rec["id"] not in cleaned
    assert disk["status"] == "running" and disk["phase"] == "committing"


def test_begin_committing_clears_pid_and_sets_phase_atomically(repo):
    # cubic P2: pid clear + committing must be ONE transition so a concurrent
    # cleanup never sees an intermediate running/pid=None record.
    rec = status.create_record(repo, "dev", "m", "/tmp/x.log", session_id="S")
    status.set_pid(rec, 4321)
    status.begin_committing(rec)
    disk = status.read_record(status._record_path(repo, rec["id"]))
    assert disk["pid"] is None
    assert disk["pidStart"] is None
    assert disk["phase"] == "committing" and disk["status"] == "running"


def test_stale_committing_record_is_retired_by_session_end_cleanup(repo):
    # cubic P2: a committing record with no pid that has sat past the grace
    # (crash during push/PR) must be retired by cleanup too, not left running
    # until the next delegation's sweep.
    rec = status.create_record(repo, "dev", "m", "/tmp/x.log", session_id="S")
    status.begin_committing(rec)
    p = status._record_path(repo, rec["id"])
    j = status.read_record(p)
    j["updatedAt"] = time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                   time.gmtime(time.time() - status._COMMITTING_ORPHAN_SECONDS - 60))
    status._write_json(p, j)
    cleaned = status.cleanup_session(repo, "S")
    disk = status.read_record(p)
    assert rec["id"] in cleaned
    assert disk["status"] == "orphaned" and disk["phase"] == "failed"


def test_cleanup_preserves_leaked_pid_when_not_terminated(repo, monkeypatch):
    # CodeRabbit Major: when cleanup marks a record orphaned WITHOUT terminating
    # its process (e.g. _pid_is_ours false — no pidStart token), the pid must be
    # preserved under a non-authoritative key so the potential leak is visible.
    rec = status.create_record(repo, "dev", "m", "/tmp/x.log", session_id="S")
    status.set_pid(rec, 700700)
    monkeypatch.setattr(status, "_pid_is_ours", lambda cur: False)  # not ours -> no kill
    status.cleanup_session(repo, "S")
    disk = status.read_record(status._record_path(repo, rec["id"]))
    assert disk["pid"] is None
    assert disk["status"] == "orphaned"
    assert disk.get("leakedPid") == 700700


def test_upsert_index_rebuilds_corrupt_index(repo):
    # CodeRabbit Major: a corrupt/non-object state.json must not make every later
    # upsert fail. The index is rebuilt from empty instead.
    rec = status.create_record(repo, "dev", "m", "/tmp/x.log", session_id="S")
    index_path = status.resolve_state_dir(repo) / status._INDEX_NAME
    index_path.write_text("[]")  # valid JSON but non-object
    # A subsequent write must not raise, and must produce a valid object index.
    status.finalize(rec, status="completed", phase="done", commit_sha="z")
    parsed = json.loads(index_path.read_text())
    assert isinstance(parsed, dict) and "jobs" in parsed


def test_prune_records_bounds_state_dir(repo, monkeypatch):
    # CodeRabbit Major: the state dir must not grow without bound. Terminal
    # records beyond the cap are deleted (files + lockfiles); live ones are kept.
    monkeypatch.setattr(status, "_MAX_INDEX_JOBS", 3)
    ids = []
    for _ in range(6):
        r = status.create_record(repo, "dev", "m", "/tmp/x.log", session_id="S")
        status.finalize(r, status="completed", phase="done", commit_sha="c")
        ids.append(r["id"])
    status._prune_records(repo)
    state_dir = status.resolve_state_dir(repo)
    remaining = list(state_dir.glob("lc-*.json"))
    assert len(remaining) <= 3
    # oldest record's lockfile is gone too
    oldest = ids[0]
    assert not (state_dir / f".{oldest}.json.lock").exists()


def test_prune_records_keeps_live_records_beyond_cap(repo, monkeypatch):
    monkeypatch.setattr(status, "_MAX_INDEX_JOBS", 1)
    live = status.create_record(repo, "dev", "m", "/tmp/x.log", session_id="S")
    for _ in range(3):
        r = status.create_record(repo, "dev", "m", "/tmp/x.log", session_id="S")
        status.finalize(r, status="completed", phase="done", commit_sha="c")
    status._prune_records(repo)
    # the still-running record must survive even though it's beyond the cap
    assert (status._record_path(repo, live["id"])).exists()


def test_stale_committing_record_is_retired_by_sweep(repo):
    # A crash/cancellation during push/PR leaves a committing record with no
    # finalizer. The sweep must retire it once it exceeds the committing grace
    # window (aged off updatedAt), so it can't stay running forever.
    rec = status.create_record(repo, "dev", "m", "/tmp/x.log", session_id="S")
    status.set_pid(rec, None)
    status.mark_committing(rec)
    # Backdate updatedAt beyond the committing grace.
    p = status._record_path(repo, rec["id"])
    j = status.read_record(p)
    old = time.strftime("%Y-%m-%dT%H:%M:%SZ",
                        time.gmtime(time.time() - status._COMMITTING_ORPHAN_SECONDS - 60))
    j["updatedAt"] = old
    status._write_json(p, j)
    status.sweep_orphans(repo)
    disk = status.read_record(p)
    assert disk["status"] == "orphaned" and disk["phase"] == "failed"


def test_cleanup_grace_is_clamped_to_remaining_budget(repo, monkeypatch):
    # cubic P2: the SIGTERM grace passed to _terminate_process_tree must be
    # bounded by the budget left on the overall cleanup deadline, so many live
    # records can't push SessionEnd past its hook timeout.
    rec = status.create_record(repo, "dev", "m", "/tmp/x.log", session_id="S")
    status.set_pid(rec, 555001)
    rec_token = status.read_record(status._record_path(repo, rec["id"])).get("pidStart")
    monkeypatch.setattr(status, "_pid_is_ours", lambda cur: True)
    # Make the pre-signal revalidation pass so terminate is reached.
    monkeypatch.setattr(status, "_pid_start_time", lambda pid: rec_token)
    grabbed = {}
    def fake_term(pid, grace_seconds=3.0):
        grabbed["grace"] = grace_seconds
    monkeypatch.setattr(status, "_terminate_process_tree", fake_term)
    # Tiny overall budget -> grace must be clamped well below the 3s default.
    status.cleanup_session(repo, "S", deadline_seconds=0.5)
    assert "grace" in grabbed and grabbed["grace"] <= 0.5
