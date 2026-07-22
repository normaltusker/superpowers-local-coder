import subprocess
import time

import pytest

from backends import common


@pytest.fixture
def git_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
    (repo / "README.md").write_text("hello\n")
    subprocess.run(["git", "add", "README.md"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=repo, check=True, capture_output=True)
    return repo


def test_ensure_branch_creates_new_branch(git_repo):
    common.ensure_branch(str(git_repo), "feature/test-branch")
    result = subprocess.run(
        ["git", "branch", "--show-current"], cwd=git_repo,
        capture_output=True, text=True, check=True,
    )
    assert result.stdout.strip() == "feature/test-branch"


def test_ensure_branch_checks_out_existing_branch(git_repo):
    subprocess.run(["git", "checkout", "-b", "existing-branch"], cwd=git_repo, check=True, capture_output=True)
    subprocess.run(["git", "checkout", "main"], cwd=git_repo, check=True, capture_output=True)

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


def test_run_monitored_subprocess_raises_stall_error_when_no_output():
    with pytest.raises(common.StallError):
        common.run_monitored_subprocess(
            ["sleep", "2"], cwd=".",
            stall_timeout_seconds=0.2, idle_notify_interval_seconds=0.05,
        )


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
