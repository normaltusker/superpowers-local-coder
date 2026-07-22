import subprocess
import sys
import time

import pytest

from backends import common
from backends.base import BackendAdapter, CompletionResult


def test_backend_adapter_subclass_missing_self_commits_fails_at_definition_time():
    with pytest.raises(TypeError, match="self_commits"):
        class IncompleteBackend(BackendAdapter):
            def run_backend(self, task, repo_path, branch, config, model=None, on_tick=None):
                return CompletionResult(success=True)


def test_backend_adapter_subclass_with_self_commits_defines_cleanly():
    class CompleteBackend(BackendAdapter):
        self_commits = True

        def run_backend(self, task, repo_path, branch, config, model=None, on_tick=None):
            return CompletionResult(success=True)

    assert CompleteBackend.self_commits is True
    backend = CompleteBackend()
    assert backend.run_backend("t", "r", "b", {}).success is True

# git_repo fixture is shared via conftest.py


def test_ensure_branch_creates_new_branch(git_repo):
    common.ensure_branch(str(git_repo), "feature/test-branch")
    result = subprocess.run(
        ["git", "branch", "--show-current"], cwd=git_repo,
        capture_output=True, text=True, check=True,
    )
    assert result.stdout.strip() == "feature/test-branch"


def test_ensure_branch_checks_out_existing_branch(git_repo):
    # Don't hardcode "main" — the git_repo fixture does a bare `git init`
    # without setting init.defaultBranch, so the actual default branch name
    # depends on the system's git config/version (could be "master" on an
    # older git or one without init.defaultBranch set). Capture the real
    # default branch name instead of assuming "main".
    default_branch = subprocess.run(
        ["git", "branch", "--show-current"], cwd=git_repo,
        capture_output=True, text=True, check=True,
    ).stdout.strip()

    subprocess.run(["git", "checkout", "-b", "existing-branch"], cwd=git_repo, check=True, capture_output=True)
    subprocess.run(["git", "checkout", default_branch], cwd=git_repo, check=True, capture_output=True)

    common.ensure_branch(str(git_repo), "existing-branch")

    result = subprocess.run(
        ["git", "branch", "--show-current"], cwd=git_repo,
        capture_output=True, text=True, check=True,
    )
    assert result.stdout.strip() == "existing-branch"


def test_ensure_branch_rejects_branch_name_starting_with_dash(git_repo):
    with pytest.raises(ValueError, match="[Ii]nvalid branch name"):
        common.ensure_branch(str(git_repo), "--orphan")


def test_ensure_branch_rejects_short_flag_like_branch_name(git_repo):
    with pytest.raises(ValueError, match="[Ii]nvalid branch name"):
        common.ensure_branch(str(git_repo), "-x")


def test_ensure_branch_creates_new_branch_when_name_collides_with_tag(git_repo):
    # A tag named the same as a not-yet-existing branch resolves fine with
    # `git rev-parse --verify <name>`, but it is NOT a branch. ensure_branch
    # must still create a new branch, not check out the tag (which would
    # leave the repo in detached HEAD).
    subprocess.run(["git", "tag", "release-1.0"], cwd=git_repo, check=True, capture_output=True)

    common.ensure_branch(str(git_repo), "release-1.0")

    result = subprocess.run(
        ["git", "branch", "--show-current"], cwd=git_repo,
        capture_output=True, text=True, check=True,
    )
    assert result.stdout.strip() == "release-1.0"
    # confirm we are NOT in detached HEAD (branch --show-current is empty
    # when detached, but assert explicitly for clarity)
    symbolic = subprocess.run(
        ["git", "symbolic-ref", "-q", "HEAD"], cwd=git_repo,
        capture_output=True, text=True,
    )
    assert symbolic.returncode == 0


def test_ensure_branch_creates_new_branch_when_name_collides_with_commit_sha(git_repo):
    # A branch name that happens to equal an existing commit SHA also
    # resolves via `git rev-parse --verify <name>`, but is not a branch.
    commit_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=git_repo,
        capture_output=True, text=True, check=True,
    ).stdout.strip()

    common.ensure_branch(str(git_repo), commit_sha)

    result = subprocess.run(
        ["git", "branch", "--show-current"], cwd=git_repo,
        capture_output=True, text=True, check=True,
    )
    assert result.stdout.strip() == commit_sha


def test_ensure_branch_accepts_normal_branch_name(git_repo):
    # sanity check the validator doesn't reject legitimate names
    common.ensure_branch(str(git_repo), "feature/valid-branch_1.0")
    result = subprocess.run(
        ["git", "branch", "--show-current"], cwd=git_repo,
        capture_output=True, text=True, check=True,
    )
    assert result.stdout.strip() == "feature/valid-branch_1.0"


def test_snapshot_working_tree_returns_head_and_clean_status(git_repo):
    pre_head, porcelain = common.snapshot_working_tree(str(git_repo))

    expected_head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=git_repo,
        capture_output=True, text=True, check=True,
    ).stdout.strip()

    assert pre_head == expected_head
    assert porcelain == set()  # clean working tree


def test_snapshot_working_tree_detects_dirty_state(git_repo):
    (git_repo / "untracked.txt").write_text("new file\n")
    _, porcelain = common.snapshot_working_tree(str(git_repo))
    assert any("untracked.txt" in line for line in porcelain)


def test_restore_working_tree_resets_head_and_discards_new_changes(git_repo):
    pre_head, pre_porcelain = common.snapshot_working_tree(str(git_repo))

    (git_repo / "new_untracked.py").write_text("# added during attempt\n")
    subprocess.run(["git", "add", "-A"], cwd=git_repo, check=True)
    subprocess.run(["git", "commit", "-m", "partial attempt commit"], cwd=git_repo, check=True, capture_output=True)

    common.restore_working_tree(str(git_repo), pre_head, pre_porcelain)

    head_after = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=git_repo,
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert head_after == pre_head
    assert not (git_repo / "new_untracked.py").exists()

    status = subprocess.run(
        ["git", "status", "--porcelain"], cwd=git_repo,
        capture_output=True, text=True, check=True,
    ).stdout
    assert status.strip() == ""


def test_restore_working_tree_preserves_pre_existing_changes(git_repo):
    (git_repo / "already_dirty.txt").write_text("dirty before attempt\n")
    pre_head, pre_porcelain = common.snapshot_working_tree(str(git_repo))
    assert pre_porcelain  # sanity: fixture is dirty before the "attempt"

    (git_repo / "new_from_attempt.txt").write_text("added during attempt\n")

    common.restore_working_tree(str(git_repo), pre_head, pre_porcelain)

    assert (git_repo / "already_dirty.txt").exists()
    assert not (git_repo / "new_from_attempt.txt").exists()


def test_restore_working_tree_discards_mixed_tracked_and_untracked_changes(git_repo):
    # A real implementation attempt normally touches BOTH an existing
    # (tracked) file and adds a new (untracked) one in the same run. `git
    # checkout -- <tracked> <untracked>` fails entirely on the untracked
    # path (not a valid checkout pathspec), which — if not handled
    # separately — silently leaves the tracked modification uncleaned too.
    tracked_path = git_repo / "README.md"
    pre_head, pre_porcelain = common.snapshot_working_tree(str(git_repo))
    original_tracked_content = tracked_path.read_text()

    tracked_path.write_text("modified during failed attempt\n")
    (git_repo / "new_from_attempt.py").write_text("# added during failed attempt\n")

    common.restore_working_tree(str(git_repo), pre_head, pre_porcelain)

    assert tracked_path.read_text() == original_tracked_content
    assert not (git_repo / "new_from_attempt.py").exists()


def test_restore_working_tree_discards_staged_but_uncommitted_changes(git_repo):
    # A failed attempt that `git add`s an edit but is killed before
    # `git commit` (e.g. a stall-kill mid-write) leaves the corrupted
    # content STAGED, not just in the working tree. `git checkout --`
    # restores from the index, so if the index itself still holds the
    # staged corruption, checkout is a no-op and the corruption survives.
    tracked_path = git_repo / "README.md"
    pre_head, pre_porcelain = common.snapshot_working_tree(str(git_repo))
    original_tracked_content = tracked_path.read_text()

    tracked_path.write_text("corrupted during failed attempt\n")
    subprocess.run(["git", "add", "README.md"], cwd=git_repo, check=True)
    # Deliberately no commit — this is the "killed before committing" case.

    common.restore_working_tree(str(git_repo), pre_head, pre_porcelain)

    assert tracked_path.read_text() == original_tracked_content
    status = subprocess.run(
        ["git", "status", "--porcelain"], cwd=git_repo,
        capture_output=True, text=True, check=True,
    ).stdout
    assert status.strip() == ""


def test_restore_working_tree_handles_filenames_with_spaces(git_repo):
    # git status's default output display-escapes (quotes) paths containing
    # spaces/special characters — treating that quoted text as the literal
    # filesystem path fails to match the real file at cleanup time.
    pre_head, pre_porcelain = common.snapshot_working_tree(str(git_repo))

    (git_repo / "file with space.py").write_text("# added during failed attempt\n")

    common.restore_working_tree(str(git_repo), pre_head, pre_porcelain)

    assert not (git_repo / "file with space.py").exists()


def test_run_monitored_subprocess_returns_completed_process_on_success():
    result = common.run_monitored_subprocess(
        ["echo", "hello"], cwd=".",
        stall_timeout_seconds=5, idle_notify_interval_seconds=1,
    )
    assert result.returncode == 0


def test_run_monitored_subprocess_calls_on_tick():
    ticks = []
    common.run_monitored_subprocess(
        ["sleep", "0.3"], cwd=".",
        stall_timeout_seconds=5, idle_notify_interval_seconds=0.1,
        on_tick=lambda: ticks.append(time.time()),
    )
    assert len(ticks) >= 1


def test_run_monitored_subprocess_bounds_output_to_tail():
    # A subprocess that writes far more than _MAX_OUTPUT_CHARS must not have
    # its full transcript accumulated in memory — only a bounded tail should
    # be kept, and that tail must be the LATEST content (a truncated tail is
    # only useful for downstream "what was the last thing that happened"
    # error reporting if it's actually the end of the output, not the start).
    over_cap_lines = common._MAX_OUTPUT_CHARS // 10 + 100  # each line ~10 chars, well over the cap
    result = common.run_monitored_subprocess(
        ["python3", "-c", f"import sys\nfor i in range({over_cap_lines}): print(f'line-{{i:06d}}')\nsys.stdout.flush()"],
        cwd=".",
        stall_timeout_seconds=5, idle_notify_interval_seconds=1,
    )
    assert len(result.stdout) <= common._MAX_OUTPUT_CHARS
    # The tail must contain the LAST line written, not the first.
    assert f"line-{over_cap_lines - 1:06d}" in result.stdout
    assert "line-000000" not in result.stdout


def test_run_monitored_subprocess_raises_stall_error_when_no_output():
    with pytest.raises(common.StallError):
        common.run_monitored_subprocess(
            ["sleep", "2"], cwd=".",
            stall_timeout_seconds=0.2, idle_notify_interval_seconds=0.05,
        )


def test_run_monitored_subprocess_ticks_during_partial_line_with_no_newline():
    # A subprocess that writes a partial line (no trailing newline) and then
    # goes quiet without closing its stdout pipe must not block readline()
    # past the next poll interval — on_tick must still fire during the gap,
    # and the stall check must still be evaluated.
    script = (
        "import sys, time\n"
        "sys.stdout.write('partial-line-no-newline')\n"
        "sys.stdout.flush()\n"
        "time.sleep(0.6)\n"
    )
    ticks = []
    result = common.run_monitored_subprocess(
        [sys.executable, "-c", script], cwd=".",
        stall_timeout_seconds=5, idle_notify_interval_seconds=0.1,
        on_tick=lambda: ticks.append(time.monotonic()),
    )
    assert result.returncode == 0
    assert "partial-line-no-newline" in result.stdout
    # on_tick should have fired multiple times during the 0.6s quiet period
    # after the partial write (idle_notify_interval_seconds=0.1).
    assert len(ticks) >= 3


def test_run_monitored_subprocess_kills_process_when_on_tick_raises():
    captured_pid = {}
    real_popen = subprocess.Popen

    def spying_popen(*args, **kwargs):
        proc = real_popen(*args, **kwargs)
        captured_pid["pid"] = proc.pid
        captured_pid["proc"] = proc
        return proc

    def blowup_on_tick():
        raise RuntimeError("on_tick blew up")

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(subprocess, "Popen", spying_popen)
        with pytest.raises(RuntimeError, match="on_tick blew up"):
            common.run_monitored_subprocess(
                ["sleep", "5"], cwd=".",
                stall_timeout_seconds=5, idle_notify_interval_seconds=0.05,
                on_tick=blowup_on_tick,
            )

    # give the OS a moment to reap; poll() returns None while still running
    proc = captured_pid["proc"]
    proc.wait(timeout=2)
    assert proc.poll() is not None  # process must have been killed, not leaked
