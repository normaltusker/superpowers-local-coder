import subprocess
from unittest.mock import patch

from backends.aider import AiderBackend
from backends import common

# git_repo fixture is shared via conftest.py

BASE_CONFIG = {
    "idle_notify_interval_seconds": 20,
    "stall_timeout_seconds": 300,
    "extra_backend_args": [],
}


def test_self_commits_is_true():
    assert AiderBackend.self_commits is True


def test_run_backend_success_when_aider_commits(git_repo):
    def fake_run(cmd, cwd, stall_timeout_seconds, idle_notify_interval_seconds, on_tick=None, on_output=None):
        # simulate aider making a commit
        (git_repo / "new_file.py").write_text("# new\n")
        subprocess.run(["git", "add", "new_file.py"], cwd=git_repo, check=True)
        subprocess.run(["git", "commit", "-m", "aider commit"], cwd=git_repo, check=True, capture_output=True)
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    backend = AiderBackend()
    with patch.object(common, "run_monitored_subprocess", side_effect=fake_run):
        result = backend.run_backend(
            task="add a file", repo_path=str(git_repo), branch="test-branch",
            config=BASE_CONFIG, model="ollama/qwen3-coder:30b",
        )

    assert result.success is True
    assert result.commit_sha is not None
    assert "new_file.py" in result.files_changed


def test_run_backend_failure_when_no_commit_made(git_repo):
    def fake_run(cmd, cwd, stall_timeout_seconds, idle_notify_interval_seconds, on_tick=None, on_output=None):
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    backend = AiderBackend()
    with patch.object(common, "run_monitored_subprocess", side_effect=fake_run):
        result = backend.run_backend(
            task="do nothing", repo_path=str(git_repo), branch="test-branch",
            config=BASE_CONFIG, model="ollama/qwen3-coder:30b",
        )

    assert result.success is False
    assert "no commits" in result.error


def test_run_backend_failure_on_nonzero_exit(git_repo):
    def fake_run(cmd, cwd, stall_timeout_seconds, idle_notify_interval_seconds, on_tick=None, on_output=None):
        return subprocess.CompletedProcess(cmd, 1, stdout="aider crashed", stderr="")

    backend = AiderBackend()
    with patch.object(common, "run_monitored_subprocess", side_effect=fake_run):
        result = backend.run_backend(
            task="do something", repo_path=str(git_repo), branch="test-branch",
            config=BASE_CONFIG, model="ollama/qwen3-coder:30b",
        )

    assert result.success is False
    assert "aider crashed" in result.error


def test_run_backend_failure_on_stall(git_repo):
    backend = AiderBackend()
    with patch.object(common, "run_monitored_subprocess", side_effect=common.StallError(300)):
        result = backend.run_backend(
            task="hang forever", repo_path=str(git_repo), branch="test-branch",
            config=BASE_CONFIG, model="ollama/qwen3-coder:30b",
        )

    assert result.success is False
    assert "stalled" in result.error


def test_run_backend_forwards_on_tick_to_run_monitored_subprocess(git_repo):
    captured = {}

    def fake_run(cmd, cwd, stall_timeout_seconds, idle_notify_interval_seconds, on_tick=None, on_output=None):
        captured["on_tick"] = on_tick
        (git_repo / "new_file.py").write_text("# new\n")
        subprocess.run(["git", "add", "new_file.py"], cwd=git_repo, check=True)
        subprocess.run(["git", "commit", "-m", "aider commit"], cwd=git_repo, check=True, capture_output=True)
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    def my_on_tick():
        pass

    backend = AiderBackend()
    with patch.object(common, "run_monitored_subprocess", side_effect=fake_run):
        backend.run_backend(
            task="add a file", repo_path=str(git_repo), branch="test-branch",
            config=BASE_CONFIG, model="ollama/qwen3-coder:30b", on_tick=my_on_tick,
        )

    assert captured["on_tick"] is my_on_tick


def test_run_backend_forwards_on_output_to_run_monitored_subprocess(git_repo):
    captured = {}

    def fake_run(cmd, cwd, stall_timeout_seconds, idle_notify_interval_seconds, on_tick=None, on_output=None):
        captured["on_output"] = on_output
        (git_repo / "new_file.py").write_text("# new\n")
        subprocess.run(["git", "add", "new_file.py"], cwd=git_repo, check=True)
        subprocess.run(["git", "commit", "-m", "aider commit"], cwd=git_repo, check=True, capture_output=True)
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    def my_on_output(chunk):
        pass

    backend = AiderBackend()
    with patch.object(common, "run_monitored_subprocess", side_effect=fake_run):
        backend.run_backend(
            task="add a file", repo_path=str(git_repo), branch="test-branch",
            config=BASE_CONFIG, model="ollama/qwen3-coder:30b", on_output=my_on_output,
        )

    assert captured["on_output"] is my_on_output


def test_run_backend_falls_back_to_config_model_when_model_arg_is_none(git_repo):
    captured = {}

    def fake_run(cmd, cwd, stall_timeout_seconds, idle_notify_interval_seconds, on_tick=None, on_output=None):
        captured["cmd"] = cmd
        (git_repo / "new_file.py").write_text("# new\n")
        subprocess.run(["git", "add", "new_file.py"], cwd=git_repo, check=True)
        subprocess.run(["git", "commit", "-m", "aider commit"], cwd=git_repo, check=True, capture_output=True)
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    config_with_model = {**BASE_CONFIG, "model": "ollama/qwen3-coder:30b"}
    backend = AiderBackend()
    with patch.object(common, "run_monitored_subprocess", side_effect=fake_run):
        result = backend.run_backend(
            task="add a file", repo_path=str(git_repo), branch="test-branch",
            config=config_with_model, model=None,
        )

    assert result.success is True
    assert "ollama/qwen3-coder:30b" in captured["cmd"]


def test_run_backend_returns_clean_error_when_no_model_available(git_repo):
    backend = AiderBackend()
    with patch.object(common, "run_monitored_subprocess") as mock_run:
        result = backend.run_backend(
            task="add a file", repo_path=str(git_repo), branch="test-branch",
            config=BASE_CONFIG, model=None,
        )

    assert result.success is False
    assert "no model specified" in result.error
    mock_run.assert_not_called()


def test_run_backend_cleans_up_partial_writes_after_stall(git_repo):
    # Simulate aider writing a file to disk (as it does via apply_edits)
    # before being killed by a stall timeout, i.e. before it ever reaches
    # auto_commit. The failed attempt must not leave that partial write on
    # disk for the next failover attempt to inherit.
    def fake_run(cmd, cwd, stall_timeout_seconds, idle_notify_interval_seconds, on_tick=None, on_output=None):
        (git_repo / "partial_write.py").write_text("# half-written by killed aider\n")
        raise common.StallError(300)

    backend = AiderBackend()
    with patch.object(common, "run_monitored_subprocess", side_effect=fake_run):
        result = backend.run_backend(
            task="hang forever", repo_path=str(git_repo), branch="test-branch",
            config=BASE_CONFIG, model="ollama/qwen3-coder:30b",
        )

    assert result.success is False
    status = subprocess.run(
        ["git", "status", "--porcelain"], cwd=git_repo,
        capture_output=True, text=True, check=True,
    ).stdout
    assert status.strip() == ""
    assert not (git_repo / "partial_write.py").exists()


def test_run_backend_cleans_up_partial_writes_after_nonzero_exit(git_repo):
    # Same scenario but for a plain non-zero exit rather than a stall kill.
    def fake_run(cmd, cwd, stall_timeout_seconds, idle_notify_interval_seconds, on_tick=None, on_output=None):
        (git_repo / "partial_write.py").write_text("# half-written before crash\n")
        return subprocess.CompletedProcess(cmd, 1, stdout="aider crashed", stderr="")

    backend = AiderBackend()
    with patch.object(common, "run_monitored_subprocess", side_effect=fake_run):
        result = backend.run_backend(
            task="do something", repo_path=str(git_repo), branch="test-branch",
            config=BASE_CONFIG, model="ollama/qwen3-coder:30b",
        )

    assert result.success is False
    status = subprocess.run(
        ["git", "status", "--porcelain"], cwd=git_repo,
        capture_output=True, text=True, check=True,
    ).stdout
    assert status.strip() == ""
    assert not (git_repo / "partial_write.py").exists()


def test_run_backend_cleanup_preserves_pre_existing_dirty_state(git_repo):
    # A file that was already modified/untracked BEFORE this attempt started
    # must survive cleanup — only changes made during the failed attempt
    # itself should be discarded.
    (git_repo / "pre_existing_untracked.txt").write_text("already here before the attempt\n")

    def fake_run(cmd, cwd, stall_timeout_seconds, idle_notify_interval_seconds, on_tick=None, on_output=None):
        (git_repo / "partial_write.py").write_text("# half-written by killed aider\n")
        raise common.StallError(300)

    backend = AiderBackend()
    with patch.object(common, "run_monitored_subprocess", side_effect=fake_run):
        result = backend.run_backend(
            task="hang forever", repo_path=str(git_repo), branch="test-branch",
            config=BASE_CONFIG, model="ollama/qwen3-coder:30b",
        )

    assert result.success is False
    assert (git_repo / "pre_existing_untracked.txt").exists()
    assert not (git_repo / "partial_write.py").exists()


def test_run_backend_creates_branch_if_missing(git_repo):
    def fake_run(cmd, cwd, stall_timeout_seconds, idle_notify_interval_seconds, on_tick=None, on_output=None):
        subprocess.run(["git", "commit", "--allow-empty", "-m", "aider commit"], cwd=git_repo, check=True, capture_output=True)
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    backend = AiderBackend()
    with patch.object(common, "run_monitored_subprocess", side_effect=fake_run):
        backend.run_backend(
            task="task", repo_path=str(git_repo), branch="brand-new-branch",
            config=BASE_CONFIG, model="ollama/qwen3-coder:30b",
        )

    current_branch = subprocess.run(
        ["git", "branch", "--show-current"], cwd=git_repo,
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert current_branch == "brand-new-branch"
