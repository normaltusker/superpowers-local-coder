import subprocess
import shutil
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

import server
import config as config_module
from backends.base import CompletionResult

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def isolated_config(tmp_path, monkeypatch):
    real_config = Path(__file__).parent.parent / "config.yaml"
    temp_config = tmp_path / "config.yaml"
    shutil.copy(real_config, temp_config)
    monkeypatch.setattr(config_module, "CONFIG_PATH", temp_config)
    return temp_config


# git_repo_with_remote / git_repo_no_remote fixtures are shared via conftest.py


async def test_delegate_implementation_applies_configured_branch_prefix(isolated_config, git_repo_no_remote):
    # config.yaml's default branch_prefix is "local-coder/" — a caller
    # passing a bare branch name should end up on the prefixed branch.
    captured = {}

    def fake_run_backend(task, repo_path, branch, config, model=None, on_tick=None, on_output=None):
        captured["branch"] = branch
        return CompletionResult(success=True, files_changed=["a.py"], commit_sha="abc123")

    with patch("backends.aider.AiderBackend.run_backend", side_effect=fake_run_backend):
        result = await server._delegate_implementation_impl(
            task="add a.py", branch="my-task",
            target_repo_path=str(git_repo_no_remote),
        )

    assert result["success"] is True
    assert captured["branch"] == "local-coder/my-task"
    assert result["branch"] == "local-coder/my-task"


async def test_delegate_implementation_does_not_double_prefix_branch(isolated_config, git_repo_no_remote):
    # A caller that already includes the configured prefix must not get it
    # applied twice.
    captured = {}

    def fake_run_backend(task, repo_path, branch, config, model=None, on_tick=None, on_output=None):
        captured["branch"] = branch
        return CompletionResult(success=True, files_changed=["a.py"], commit_sha="abc123")

    with patch("backends.aider.AiderBackend.run_backend", side_effect=fake_run_backend):
        result = await server._delegate_implementation_impl(
            task="add a.py", branch="local-coder/my-task",
            target_repo_path=str(git_repo_no_remote),
        )

    assert result["success"] is True
    assert captured["branch"] == "local-coder/my-task"


async def test_delegate_implementation_skips_prefix_when_configured_empty(isolated_config, git_repo_no_remote):
    config_module.merge_config({"branch_prefix": ""})
    captured = {}

    def fake_run_backend(task, repo_path, branch, config, model=None, on_tick=None, on_output=None):
        captured["branch"] = branch
        return CompletionResult(success=True, files_changed=["a.py"], commit_sha="abc123")

    with patch("backends.aider.AiderBackend.run_backend", side_effect=fake_run_backend):
        result = await server._delegate_implementation_impl(
            task="add a.py", branch="my-task",
            target_repo_path=str(git_repo_no_remote),
        )

    assert result["success"] is True
    assert captured["branch"] == "my-task"


async def test_delegate_implementation_missing_repo_path_returns_error(isolated_config):
    result = await server._delegate_implementation_impl(task="do something", branch="test-branch", target_repo_path=None)
    assert result["success"] is False
    assert "target_repo_path" in result["error"]


async def test_delegate_implementation_success_no_remote(isolated_config, git_repo_no_remote):
    fake_result = CompletionResult(success=True, files_changed=["a.py"], commit_sha="abc123")
    with patch("backends.aider.AiderBackend.run_backend", return_value=fake_result):
        result = await server._delegate_implementation_impl(
            task="add a.py", branch="feature-branch",
            target_repo_path=str(git_repo_no_remote),
        )

    assert result["success"] is True
    assert result["pr_url"] is None
    assert "note" in result
    assert result["model_used"] == "ollama/qwen3-coder:30b"


async def test_delegate_implementation_pushes_when_remote_exists(isolated_config, git_repo_with_remote):
    # AiderBackend.run_backend is mocked out below, so it never performs its
    # real side effect of creating/checking out the target branch (see
    # backends.common.ensure_branch). Create it here so the branch exists
    # locally before _delegate_implementation_impl attempts to push it.
    # Passed already carrying the config's branch_prefix so this test's
    # branch setup is independent of the prefix-application logic under
    # test elsewhere (test_delegate_implementation_applies_branch_prefix*).
    subprocess.run(
        ["git", "-C", str(git_repo_with_remote), "checkout", "-b", "local-coder/feature-branch"],
        check=True, capture_output=True,
    )
    fake_result = CompletionResult(success=True, files_changed=["a.py"], commit_sha="abc123")
    with patch("backends.aider.AiderBackend.run_backend", return_value=fake_result):
        with patch("subprocess.run", wraps=subprocess.run) as spy:
            result = await server._delegate_implementation_impl(
                task="add a.py", branch="local-coder/feature-branch",
                target_repo_path=str(git_repo_with_remote),
            )

    assert result["success"] is True
    push_calls = [c for c in spy.call_args_list if "push" in c.args[0]]
    assert len(push_calls) >= 1


async def test_delegate_implementation_failover_to_second_model(isolated_config, git_repo_no_remote):
    config_module.merge_config({"fallback_models": ["ollama/qwen2.5-coder:14b"]})
    fail_result = CompletionResult(success=False, error="stalled: no output for 300s")
    success_result = CompletionResult(success=True, files_changed=["a.py"], commit_sha="def456")

    with patch(
        "backends.aider.AiderBackend.run_backend",
        side_effect=[fail_result, success_result],
    ):
        result = await server._delegate_implementation_impl(
            task="add a.py", branch="feature-branch",
            target_repo_path=str(git_repo_no_remote),
        )

    assert result["success"] is True
    assert result["model_used"] == "ollama/qwen2.5-coder:14b"


async def test_delegate_implementation_all_models_fail(isolated_config, git_repo_no_remote):
    config_module.merge_config({"fallback_models": ["ollama/qwen2.5-coder:14b"]})
    fail_result = CompletionResult(success=False, error="aider made no commits")

    with patch("backends.aider.AiderBackend.run_backend", return_value=fail_result):
        result = await server._delegate_implementation_impl(
            task="add a.py", branch="feature-branch",
            target_repo_path=str(git_repo_no_remote),
        )

    assert result["success"] is False
    assert "ollama/qwen3-coder:30b" in result["error"]
    assert "ollama/qwen2.5-coder:14b" in result["error"]


async def test_delegate_implementation_unimplemented_backend_returns_clean_error(isolated_config, git_repo_no_remote):
    config_module.merge_config({"backend": "codex"})
    result = await server._delegate_implementation_impl(
        task="add a.py", branch="feature-branch",
        target_repo_path=str(git_repo_no_remote),
    )
    assert result["success"] is False
    assert "not yet implemented" in result["error"]


async def test_delegate_implementation_on_tick_reports_progress_via_ctx(isolated_config, git_repo_no_remote):
    captured_on_tick = {}

    def fake_run_backend(task, repo_path, branch, config, model=None, on_tick=None, on_output=None):
        captured_on_tick["on_tick"] = on_tick
        return CompletionResult(success=True, files_changed=["a.py"], commit_sha="abc123")

    mock_ctx = MagicMock()
    mock_ctx.report_progress = MagicMock()

    with patch("backends.aider.AiderBackend.run_backend", side_effect=fake_run_backend):
        with patch("anyio.from_thread.run") as mock_from_thread_run:
            result = await server._delegate_implementation_impl(
                task="add a.py", branch="feature-branch",
                target_repo_path=str(git_repo_no_remote),
                ctx=mock_ctx,
            )

            assert result["success"] is True
            on_tick = captured_on_tick["on_tick"]
            assert on_tick is not None

            # Invoke the captured on_tick as run_monitored_subprocess would,
            # and confirm it bridges to ctx.report_progress via
            # anyio.from_thread.run (mocked here so this test doesn't depend
            # on real thread-bridging machinery — it verifies the wiring,
            # not anyio itself).
            on_tick()
            assert mock_from_thread_run.called
            args = mock_from_thread_run.call_args.args
            assert args[0] == mock_ctx.report_progress


async def test_delegate_implementation_on_tick_logs_to_stderr_without_ctx(isolated_config, git_repo_no_remote, capsys):
    captured_on_tick = {}

    def fake_run_backend(task, repo_path, branch, config, model=None, on_tick=None, on_output=None):
        captured_on_tick["on_tick"] = on_tick
        return CompletionResult(success=True, files_changed=["a.py"], commit_sha="abc123")

    with patch("backends.aider.AiderBackend.run_backend", side_effect=fake_run_backend):
        result = await server._delegate_implementation_impl(
            task="add a.py", branch="feature-branch",
            target_repo_path=str(git_repo_no_remote),
            ctx=None,
        )

    assert result["success"] is True
    on_tick = captured_on_tick["on_tick"]
    on_tick()

    captured = capsys.readouterr()
    assert "still running" in captured.err


async def test_delegate_implementation_on_output_writes_to_log_file(
    isolated_config, git_repo_no_remote, tmp_path, monkeypatch
):
    # Claude Code pipes an MCP server's stdout/stderr over an internal
    # socket it owns — there is no reliable external tap point (confirmed:
    # neither `--debug-file` nor any filesystem-visible fd shows this
    # output). A plain log FILE, written directly by this server process,
    # is the only way to guarantee a human can `tail -f` a delegated
    # backend's real output regardless of how the parent process pipes
    # stderr.
    log_path = tmp_path / "local-coder-output.log"
    monkeypatch.setattr(server, "OUTPUT_LOG_PATH", log_path)

    captured_on_output = {}

    def fake_run_backend(task, repo_path, branch, config, model=None, on_tick=None, on_output=None):
        captured_on_output["on_output"] = on_output
        return CompletionResult(success=True, files_changed=["a.py"], commit_sha="abc123")

    with patch("backends.aider.AiderBackend.run_backend", side_effect=fake_run_backend):
        result = await server._delegate_implementation_impl(
            task="add a.py", branch="feature-branch",
            target_repo_path=str(git_repo_no_remote),
            ctx=None,
        )

    assert result["success"] is True
    on_output = captured_on_output["on_output"]
    on_output("hello from aider\n")
    on_output("more output\n")

    assert log_path.read_text() == "hello from aider\nmore output\n"


async def test_delegate_implementation_on_output_still_writes_to_stderr(
    isolated_config, git_repo_no_remote, tmp_path, monkeypatch, capsys
):
    # The stderr stream stays in place alongside the log file — some
    # setups may capture it even if this one doesn't, and removing it
    # would be a pure regression for no benefit.
    log_path = tmp_path / "local-coder-output.log"
    monkeypatch.setattr(server, "OUTPUT_LOG_PATH", log_path)

    captured_on_output = {}

    def fake_run_backend(task, repo_path, branch, config, model=None, on_tick=None, on_output=None):
        captured_on_output["on_output"] = on_output
        return CompletionResult(success=True, files_changed=["a.py"], commit_sha="abc123")

    with patch("backends.aider.AiderBackend.run_backend", side_effect=fake_run_backend):
        await server._delegate_implementation_impl(
            task="add a.py", branch="feature-branch",
            target_repo_path=str(git_repo_no_remote),
            ctx=None,
        )

    on_output = captured_on_output["on_output"]
    on_output("hello from aider\n")

    captured = capsys.readouterr()
    assert "hello from aider" in captured.err


async def test_delegate_implementation_truncates_output_log_at_call_start(
    isolated_config, git_repo_no_remote, tmp_path, monkeypatch
):
    # OUTPUT_LOG_PATH is a fixed path reused across every
    # delegate_implementation call on this server's lifetime. Without
    # truncation, `tail -f`-ing it during a new call would show stale
    # output mixed in from a previous, unrelated attempt — confusing at
    # best, actively misleading at worst (e.g. thinking the current
    # attempt already produced output it hasn't yet).
    log_path = tmp_path / "local-coder-output.log"
    log_path.write_text("stale output from a previous call\n")
    monkeypatch.setattr(server, "OUTPUT_LOG_PATH", log_path)

    def fake_run_backend(task, repo_path, branch, config, model=None, on_tick=None, on_output=None):
        return CompletionResult(success=True, files_changed=["a.py"], commit_sha="abc123")

    with patch("backends.aider.AiderBackend.run_backend", side_effect=fake_run_backend):
        await server._delegate_implementation_impl(
            task="add a.py", branch="feature-branch",
            target_repo_path=str(git_repo_no_remote),
            ctx=None,
        )

    assert "stale output from a previous call" not in log_path.read_text()


async def test_delegate_implementation_rejects_invalid_branch_name(isolated_config, git_repo_no_remote):
    with patch("subprocess.run") as mock_run:
        result = await server._delegate_implementation_impl(
            task="add a.py", branch="--force",
            target_repo_path=str(git_repo_no_remote),
        )

    assert result["success"] is False
    assert "error" in result
    mock_run.assert_not_called()


async def test_delegate_implementation_generic_backend_exception_does_not_crash_and_continues_failover(isolated_config, git_repo_no_remote):
    config_module.merge_config({"fallback_models": ["ollama/qwen2.5-coder:14b"]})
    success_result = CompletionResult(success=True, files_changed=["a.py"], commit_sha="def456")

    with patch(
        "backends.aider.AiderBackend.run_backend",
        side_effect=[RuntimeError("boom"), success_result],
    ):
        result = await server._delegate_implementation_impl(
            task="add a.py", branch="feature-branch",
            target_repo_path=str(git_repo_no_remote),
        )

    assert result["success"] is True
    assert result["model_used"] == "ollama/qwen2.5-coder:14b"


async def test_delegate_implementation_generic_exception_all_models_returns_clean_error(isolated_config, git_repo_no_remote):
    with patch(
        "backends.aider.AiderBackend.run_backend",
        side_effect=RuntimeError("boom"),
    ):
        result = await server._delegate_implementation_impl(
            task="add a.py", branch="feature-branch",
            target_repo_path=str(git_repo_no_remote),
        )

    assert result["success"] is False
    assert "error" in result
    assert "boom" in result["error"]


async def test_delegate_implementation_push_timeout_returns_clean_error(isolated_config, git_repo_with_remote):
    subprocess.run(
        ["git", "-C", str(git_repo_with_remote), "checkout", "-b", "feature-branch"],
        check=True, capture_output=True,
    )
    fake_result = CompletionResult(success=True, files_changed=["a.py"], commit_sha="abc123")

    real_run = subprocess.run

    def fake_subprocess_run(cmd, *args, **kwargs):
        if "push" in cmd:
            raise subprocess.TimeoutExpired(cmd, kwargs.get("timeout", 30))
        return real_run(cmd, *args, **kwargs)

    with patch("backends.aider.AiderBackend.run_backend", return_value=fake_result):
        with patch("subprocess.run", side_effect=fake_subprocess_run):
            result = await server._delegate_implementation_impl(
                task="add a.py", branch="feature-branch",
                target_repo_path=str(git_repo_with_remote),
            )

    assert result["success"] is False
    assert "timed out" in result["error"].lower() or "timeout" in result["error"].lower()
    # the commit still exists locally even though the push timed out
    assert result["commit_sha"] == "abc123"
    assert result["files_changed"] == ["a.py"]


async def test_delegate_implementation_push_failure_includes_evidence(isolated_config, git_repo_with_remote):
    subprocess.run(
        ["git", "-C", str(git_repo_with_remote), "checkout", "-b", "feature-branch"],
        check=True, capture_output=True,
    )
    fake_result = CompletionResult(success=True, files_changed=["a.py"], commit_sha="abc123")

    real_run = subprocess.run

    def fake_subprocess_run(cmd, *args, **kwargs):
        if "push" in cmd:
            return MagicMock(returncode=1, stdout="", stderr="rejected: non-fast-forward")
        return real_run(cmd, *args, **kwargs)

    with patch("backends.aider.AiderBackend.run_backend", return_value=fake_result):
        with patch("subprocess.run", side_effect=fake_subprocess_run):
            result = await server._delegate_implementation_impl(
                task="add a.py", branch="feature-branch",
                target_repo_path=str(git_repo_with_remote),
            )

    assert result["success"] is False
    assert "push failed" in result["error"]
    assert result["files_changed"] == ["a.py"]
    assert result["commit_sha"] == "abc123"
    assert result["model_used"] == "ollama/qwen3-coder:30b"


def test_has_open_pr_returns_false_for_clean_no_pr_case():
    fake_result = MagicMock(returncode=1, stdout="", stderr='no pull requests found for branch "feature-x"\n')
    with patch("subprocess.run", return_value=fake_result):
        assert server._has_open_pr("/some/repo", "feature-x") is False


def test_has_open_pr_returns_true_when_pr_exists():
    fake_result = MagicMock(returncode=0, stdout="https://github.com/org/repo/pull/1\n", stderr="")
    with patch("subprocess.run", return_value=fake_result):
        assert server._has_open_pr("/some/repo", "feature-x") is True


def test_has_open_pr_raises_on_ambiguous_gh_failure():
    # A non-zero exit whose stderr does NOT indicate "no PR" (e.g. an auth
    # failure or network error) must not be silently treated as "no PR" —
    # that could trigger an unwanted `gh pr create` and either mask a real
    # `gh` problem or create a duplicate PR.
    fake_result = MagicMock(returncode=1, stdout="", stderr="error connecting to api.github.com\n")
    with patch("subprocess.run", return_value=fake_result):
        with pytest.raises(server.GhPrStatusUnknown):
            server._has_open_pr("/some/repo", "feature-x")


async def test_delegate_implementation_ambiguous_pr_status_skips_pr_create_with_note(isolated_config, git_repo_with_remote):
    # Passed already carrying the config's branch_prefix — see the note on
    # test_delegate_implementation_pushes_when_remote_exists above.
    subprocess.run(
        ["git", "-C", str(git_repo_with_remote), "checkout", "-b", "local-coder/feature-branch"],
        check=True, capture_output=True,
    )
    config_module.merge_config({"open_pr": True})
    fake_result = CompletionResult(success=True, files_changed=["a.py"], commit_sha="abc123")

    real_run = subprocess.run

    def fake_subprocess_run(cmd, *args, **kwargs):
        if "gh" in cmd and "view" in cmd:
            return MagicMock(returncode=1, stdout="", stderr="error connecting to api.github.com\n")
        if "gh" in cmd and "create" in cmd:
            raise AssertionError("gh pr create should not be attempted when PR status is ambiguous")
        return real_run(cmd, *args, **kwargs)

    with patch("backends.aider.AiderBackend.run_backend", return_value=fake_result):
        with patch("subprocess.run", side_effect=fake_subprocess_run):
            result = await server._delegate_implementation_impl(
                task="add a.py", branch="local-coder/feature-branch",
                target_repo_path=str(git_repo_with_remote),
            )

    assert result["success"] is True
    assert result["pr_url"] is None
    assert "note" in result
    assert "PR status" in result["note"] or "pr status" in result["note"].lower()


async def test_delegate_implementation_pr_create_failure_reported_as_note_not_silent(isolated_config, git_repo_with_remote):
    # Passed already carrying the config's branch_prefix — see the note on
    # test_delegate_implementation_pushes_when_remote_exists above.
    subprocess.run(
        ["git", "-C", str(git_repo_with_remote), "checkout", "-b", "local-coder/feature-branch"],
        check=True, capture_output=True,
    )
    config_module.merge_config({"open_pr": True})
    fake_result = CompletionResult(success=True, files_changed=["a.py"], commit_sha="abc123")

    real_run = subprocess.run

    def fake_subprocess_run(cmd, *args, **kwargs):
        if "gh" in cmd and "view" in cmd:
            return MagicMock(returncode=1, stdout="", stderr='no pull requests found for branch "local-coder/feature-branch"\n')
        if "gh" in cmd and "create" in cmd:
            return MagicMock(returncode=1, stdout="", stderr="pull request create failed: already exists")
        return real_run(cmd, *args, **kwargs)

    with patch("backends.aider.AiderBackend.run_backend", return_value=fake_result):
        with patch("subprocess.run", side_effect=fake_subprocess_run):
            result = await server._delegate_implementation_impl(
                task="add a.py", branch="local-coder/feature-branch",
                target_repo_path=str(git_repo_with_remote),
            )

    assert result["success"] is True
    assert result["pr_url"] is None
    assert "note" in result
    assert "PR creation" in result["note"]
    assert "already exists" in result["note"]


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


def test_configure_returns_structured_error_when_ollama_unavailable(isolated_config):
    from ollama import OllamaUnavailableError
    with patch("ollama.list_ollama_models", side_effect=OllamaUnavailableError("ollama is not on PATH")):
        result = server._configure_impl(model="ollama/some-model:1b")
    assert result["success"] is False
    assert "error" in result
    assert "ollama" in result["error"].lower()


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


async def test_delegate_implementation_success_includes_output_tail(isolated_config, git_repo_no_remote):
    def fake_run_backend(task, repo_path, branch, config, model=None, on_tick=None, on_output=None):
        return CompletionResult(
            success=True, files_changed=["a.py"], commit_sha="abc123",
            output_tail="full aider transcript here",
        )

    with patch("backends.aider.AiderBackend.run_backend", side_effect=fake_run_backend):
        result = await server._delegate_implementation_impl(
            task="add a.py", branch="feature-branch",
            target_repo_path=str(git_repo_no_remote), ctx=None,
        )

    assert result["success"] is True
    assert result["output_tail"] == "full aider transcript here"


async def test_delegate_implementation_all_failed_includes_last_output_tail(isolated_config, git_repo_no_remote):
    def fake_run_backend(task, repo_path, branch, config, model=None, on_tick=None, on_output=None):
        return CompletionResult(
            success=False, error=f"{model} failed",
            output_tail=f"transcript from {model}",
        )

    # config has one primary model, no fallbacks (isolated_config default)
    with patch("backends.aider.AiderBackend.run_backend", side_effect=fake_run_backend):
        result = await server._delegate_implementation_impl(
            task="add a.py", branch="feature-branch",
            target_repo_path=str(git_repo_no_remote), ctx=None,
        )

    assert result["success"] is False
    assert "output_tail" in result
    assert result["output_tail"].startswith("transcript from ")
