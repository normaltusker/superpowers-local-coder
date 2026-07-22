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
