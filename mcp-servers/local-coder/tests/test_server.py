import subprocess
import shutil
from pathlib import Path
from unittest.mock import patch

import pytest

import server
import config as config_module
from backends.base import CompletionResult


@pytest.fixture
def isolated_config(tmp_path, monkeypatch):
    real_config = Path(__file__).parent.parent / "config.yaml"
    temp_config = tmp_path / "config.yaml"
    shutil.copy(real_config, temp_config)
    monkeypatch.setattr(config_module, "CONFIG_PATH", temp_config)
    return temp_config


@pytest.fixture
def git_repo_with_remote(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
    (repo / "README.md").write_text("hello\n")
    subprocess.run(["git", "add", "README.md"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=repo, check=True, capture_output=True)
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)
    subprocess.run(["git", "remote", "add", "origin", str(remote)], cwd=repo, check=True)
    subprocess.run(["git", "push", "-u", "origin", "main"], cwd=repo, check=True, capture_output=True)
    return repo


@pytest.fixture
def git_repo_no_remote(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
    (repo / "README.md").write_text("hello\n")
    subprocess.run(["git", "add", "README.md"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=repo, check=True, capture_output=True)
    return repo


def test_delegate_implementation_missing_repo_path_returns_error(isolated_config):
    result = server._delegate_implementation_impl(task="do something", branch="test-branch", target_repo_path=None)
    assert result["success"] is False
    assert "target_repo_path" in result["error"]


def test_delegate_implementation_success_no_remote(isolated_config, git_repo_no_remote):
    fake_result = CompletionResult(success=True, files_changed=["a.py"], commit_sha="abc123")
    with patch("backends.aider.AiderBackend.run_backend", return_value=fake_result):
        result = server._delegate_implementation_impl(
            task="add a.py", branch="feature-branch",
            target_repo_path=str(git_repo_no_remote),
        )

    assert result["success"] is True
    assert result["pr_url"] is None
    assert "note" in result
    assert result["model_used"] == "ollama/qwen3-coder:30b"


def test_delegate_implementation_pushes_when_remote_exists(isolated_config, git_repo_with_remote):
    # AiderBackend.run_backend is mocked out below, so it never performs its
    # real side effect of creating/checking out the target branch (see
    # backends.common.ensure_branch). Create it here so the branch exists
    # locally before _delegate_implementation_impl attempts to push it.
    subprocess.run(
        ["git", "-C", str(git_repo_with_remote), "checkout", "-b", "feature-branch"],
        check=True, capture_output=True,
    )
    fake_result = CompletionResult(success=True, files_changed=["a.py"], commit_sha="abc123")
    with patch("backends.aider.AiderBackend.run_backend", return_value=fake_result):
        with patch("subprocess.run", wraps=subprocess.run) as spy:
            result = server._delegate_implementation_impl(
                task="add a.py", branch="feature-branch",
                target_repo_path=str(git_repo_with_remote),
            )

    assert result["success"] is True
    push_calls = [c for c in spy.call_args_list if "push" in c.args[0]]
    assert len(push_calls) >= 1


def test_delegate_implementation_failover_to_second_model(isolated_config, git_repo_no_remote):
    config_module.merge_config({"fallback_models": ["ollama/qwen2.5-coder:14b"]})
    fail_result = CompletionResult(success=False, error="stalled: no output for 300s")
    success_result = CompletionResult(success=True, files_changed=["a.py"], commit_sha="def456")

    with patch(
        "backends.aider.AiderBackend.run_backend",
        side_effect=[fail_result, success_result],
    ):
        result = server._delegate_implementation_impl(
            task="add a.py", branch="feature-branch",
            target_repo_path=str(git_repo_no_remote),
        )

    assert result["success"] is True
    assert result["model_used"] == "ollama/qwen2.5-coder:14b"


def test_delegate_implementation_all_models_fail(isolated_config, git_repo_no_remote):
    config_module.merge_config({"fallback_models": ["ollama/qwen2.5-coder:14b"]})
    fail_result = CompletionResult(success=False, error="aider made no commits")

    with patch("backends.aider.AiderBackend.run_backend", return_value=fail_result):
        result = server._delegate_implementation_impl(
            task="add a.py", branch="feature-branch",
            target_repo_path=str(git_repo_no_remote),
        )

    assert result["success"] is False
    assert "ollama/qwen3-coder:30b" in result["error"]
    assert "ollama/qwen2.5-coder:14b" in result["error"]


def test_delegate_implementation_unimplemented_backend_returns_clean_error(isolated_config, git_repo_no_remote):
    config_module.merge_config({"backend": "codex"})
    result = server._delegate_implementation_impl(
        task="add a.py", branch="feature-branch",
        target_repo_path=str(git_repo_no_remote),
    )
    assert result["success"] is False
    assert "not yet implemented" in result["error"]


def test_configure_returns_full_config(isolated_config):
    with patch("ollama.list_ollama_models", return_value=["qwen2.5-coder:14b"]):
        result = server._configure_impl(model="ollama/qwen2.5-coder:14b")
    assert result["model"] == "ollama/qwen2.5-coder:14b"
    assert result["backend"] == "aider"


def test_configure_rejects_invalid_model_with_clean_error(isolated_config):
    with patch("ollama.list_ollama_models", return_value=[]):
        result = server._configure_impl(model="ollama/nonexistent:1b")
    assert result["success"] is False
    assert "error" in result


def test_list_available_models_returns_prefixed_list(isolated_config):
    with patch("ollama.list_ollama_models", return_value=["qwen3-coder:30b"]):
        result = server._list_available_models_impl()
    assert result["models"] == ["ollama/qwen3-coder:30b"]


def test_list_available_models_returns_clean_error_when_unreachable(isolated_config):
    from ollama import OllamaUnavailableError
    with patch("ollama.list_ollama_models", side_effect=OllamaUnavailableError("no ollama")):
        result = server._list_available_models_impl()
    assert "error" in result
    assert "models" not in result or result.get("models") is None
