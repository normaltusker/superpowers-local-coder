import contextlib
import os
import subprocess
import shutil
from pathlib import Path
from unittest.mock import patch, MagicMock, AsyncMock

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


async def test_delegate_implementation_stall_surfaces_output_tail(isolated_config, git_repo_no_remote):
    # End-to-end: a stall-failed backend now carries its pre-kill tail on the
    # CompletionResult, and the server must surface it in the failure result
    # dict — otherwise "why did it stall?" is unanswerable from the MCP reply.
    stall_fail = CompletionResult(
        success=False,
        error="stalled: no output for 300s",
        output_tail="Applying edit to calc.py\nawaiting model...",
    )
    with patch("backends.aider.AiderBackend.run_backend", return_value=stall_fail):
        result = await server._delegate_implementation_impl(
            task="add a.py", branch="feature-branch",
            target_repo_path=str(git_repo_no_remote),
        )

    assert result["success"] is False
    assert result["output_tail"] == "Applying edit to calc.py\nawaiting model..."


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
    mock_ctx.report_progress = AsyncMock()

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
    assert "Running ollama/qwen3-coder:30b" in captured.err


async def test_on_tick_pulse_reflects_latest_output_line(isolated_config, git_repo_no_remote):
    captured = {}

    def fake_run_backend(task, repo_path, branch, config, model=None, on_tick=None, on_output=None):
        captured["on_tick"] = on_tick
        captured["on_output"] = on_output
        return CompletionResult(success=True, files_changed=["a.py"], commit_sha="abc123")

    mock_ctx = MagicMock()
    mock_ctx.report_progress = AsyncMock()

    with patch("backends.aider.AiderBackend.run_backend", side_effect=fake_run_backend):
        with patch("anyio.from_thread.run") as mock_from_thread_run:
            await server._delegate_implementation_impl(
                task="add a.py", branch="feature-branch",
                target_repo_path=str(git_repo_no_remote), ctx=mock_ctx,
            )

            on_tick = captured["on_tick"]
            on_output = captured["on_output"]

            # Simulate the backend emitting output, then a tick firing.
            on_output("Applying edit...\n")
            on_output("Running tests...\n")
            on_tick()

            # The progress message bridged to report_progress should carry
            # the latest output line, not a static "still running".
            assert mock_from_thread_run.called
            progress_args = mock_from_thread_run.call_args.args
            # args: (ctx.report_progress, progress, total, message)
            message = progress_args[3]
            assert "Running tests..." in message


async def test_on_tick_pulse_ignores_incomplete_trailing_line(isolated_config, git_repo_no_remote):
    # on_output is fed RAW read chunks, which can split mid-line. The pulse
    # must show the latest COMPLETE line, not an arbitrary partial suffix —
    # so a chunk ending without a newline should not overwrite the pulse
    # with its incomplete tail; the partial text is held until its line
    # finishes in a later chunk.
    captured = {}

    def fake_run_backend(task, repo_path, branch, config, model=None, on_tick=None, on_output=None):
        captured["on_tick"] = on_tick
        captured["on_output"] = on_output
        return CompletionResult(success=True, files_changed=["a.py"], commit_sha="abc123")

    mock_ctx = MagicMock()
    mock_ctx.report_progress = AsyncMock()

    with patch("backends.aider.AiderBackend.run_backend", side_effect=fake_run_backend):
        with patch("anyio.from_thread.run") as mock_from_thread_run:
            await server._delegate_implementation_impl(
                task="add a.py", branch="feature-branch",
                target_repo_path=str(git_repo_no_remote), ctx=mock_ctx,
            )

            on_tick = captured["on_tick"]
            on_output = captured["on_output"]

            # A complete line, then a partial (no trailing newline).
            on_output("Applying edit...\n")
            on_output("Running te")  # incomplete — must NOT become the pulse
            on_tick()

            message = mock_from_thread_run.call_args.args[3]
            assert "Applying edit..." in message
            assert "Running te" not in message

            # Now the line completes in a later chunk — pulse updates.
            on_output("sts...\n")
            on_tick()
            message2 = mock_from_thread_run.call_args.args[3]
            assert "Running tests..." in message2


async def test_pulse_holder_resets_between_failover_attempts(isolated_config, git_repo_no_remote):
    # latest_line is shared across the failover loop. Without a reset, the
    # first tick of a fallback model — before it has emitted anything —
    # would relabel the PREVIOUS model's last line as the new model's
    # output. Each attempt must start the pulse holder clean.
    config_module.merge_config({"fallback_models": ["ollama/fallback-model:1b"]})

    captured = {}

    def fake_run_backend(task, repo_path, branch, config, model=None, on_tick=None, on_output=None):
        # First (primary) model emits a line then "fails"; capture the
        # SECOND (fallback) model's on_tick to inspect its first pulse.
        if model == "ollama/fallback-model:1b":
            captured["fallback_on_tick"] = on_tick
            return CompletionResult(success=True, files_changed=["a.py"], commit_sha="abc123")
        # primary: emit output, then fail so we fall over
        on_output("primary model was working on X\n")
        return CompletionResult(success=False, error="primary failed", output_tail="x")

    mock_ctx = MagicMock()
    mock_ctx.report_progress = AsyncMock()
    with patch("backends.aider.AiderBackend.run_backend", side_effect=fake_run_backend):
        with patch("anyio.from_thread.run") as mock_from_thread_run:
            await server._delegate_implementation_impl(
                task="add a.py", branch="feature-branch",
                target_repo_path=str(git_repo_no_remote), ctx=mock_ctx,
            )
            # Fire the fallback's first tick BEFORE it emits anything.
            captured["fallback_on_tick"]()
            message = mock_from_thread_run.call_args.args[3]

    # The fallback's opening pulse must NOT carry the primary model's line.
    assert "primary model was working on X" not in message
    assert "fallback-model" in message  # falls back to "Running {model}..."


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
    # The concrete per-call log path is surfaced in the result dict.
    per_call_log = Path(result["output_log"])
    on_output = captured_on_output["on_output"]
    on_output("hello from aider\n")
    on_output("more output\n")

    assert per_call_log.read_text() == "hello from aider\nmore output\n"


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


async def test_each_call_uses_a_unique_output_log_path(
    isolated_config, git_repo_no_remote, tmp_path, monkeypatch
):
    # Two overlapping delegate_implementation calls must NOT share one log
    # file — otherwise the second call would clobber the first's active
    # log and their output would interleave. Each call computes a unique
    # per-call path (derived from OUTPUT_LOG_PATH) and returns it as
    # `output_log`. Two calls must therefore report different paths, and
    # each reported path must be a real sibling of the base path (so
    # concurrent runs never collide).
    base = tmp_path / "local-coder-output.log"
    monkeypatch.setattr(server, "OUTPUT_LOG_PATH", base)

    def fake_run_backend(task, repo_path, branch, config, model=None, on_tick=None, on_output=None):
        return CompletionResult(success=True, files_changed=["a.py"], commit_sha="abc123")

    with patch("backends.aider.AiderBackend.run_backend", side_effect=fake_run_backend):
        r1 = await server._delegate_implementation_impl(
            task="add a.py", branch="feature-branch",
            target_repo_path=str(git_repo_no_remote), ctx=None,
        )
        r2 = await server._delegate_implementation_impl(
            task="add b.py", branch="feature-branch",
            target_repo_path=str(git_repo_no_remote), ctx=None,
        )

    assert r1["output_log"] != r2["output_log"]
    # Each is a sibling of the base path (same directory), not the base itself.
    assert Path(r1["output_log"]).parent == base.parent
    assert Path(r1["output_log"]) != base
    assert Path(r2["output_log"]) != base


async def test_on_output_log_write_failure_does_not_abort_the_attempt(
    isolated_config, git_repo_no_remote, tmp_path, monkeypatch, capsys
):
    # on_output writes each chunk to OUTPUT_LOG_PATH. If that write fails
    # (disk full, permission denied, bad path), the exception must NOT
    # propagate out of on_output: run_monitored_subprocess propagates any
    # exception raised in on_output and kills the subprocess, but only the
    # StallError path in AiderBackend.run_backend runs the working-tree
    # cleanup — so a raw OSError here would bypass cleanup and leave partial
    # aider writes on disk to pollute the next fallback attempt. A debug-log
    # write failure must never abort a real, in-progress delegated attempt.
    missing_parent = tmp_path / "does_not_exist" / "local-coder-output.log"
    monkeypatch.setattr(server, "OUTPUT_LOG_PATH", missing_parent)

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
    # Must not raise even though the log path is unwritable.
    on_output("hello from aider\n")

    captured = capsys.readouterr()
    # The chunk still reached stderr (the primary live channel)...
    assert "hello from aider" in captured.err
    # ...and the log-write failure was surfaced as a warning, not an abort.
    assert "failed to write output log" in captured.err


async def test_on_output_updates_pulse_on_carriage_return_progress(isolated_config, git_repo_no_remote):
    # Progress bars (aider/pip style) emit carriage-return-delimited lines
    # with NO newline. If on_output only splits on "\n", such a stream
    # never updates the pulse and grows partial_line without bound. Treat
    # "\r" as a line delimiter too.
    captured = {}

    def fake_run_backend(task, repo_path, branch, config, model=None, on_tick=None, on_output=None):
        captured["on_tick"] = on_tick
        captured["on_output"] = on_output
        return CompletionResult(success=True, files_changed=["a.py"], commit_sha="abc123")

    mock_ctx = MagicMock()
    mock_ctx.report_progress = AsyncMock()
    with patch("backends.aider.AiderBackend.run_backend", side_effect=fake_run_backend):
        with patch("anyio.from_thread.run") as mock_from_thread_run:
            await server._delegate_implementation_impl(
                task="add a.py", branch="feature-branch",
                target_repo_path=str(git_repo_no_remote), ctx=mock_ctx,
            )
            on_output = captured["on_output"]
            on_tick = captured["on_tick"]
            # Carriage-return-delimited progress, no trailing newline.
            on_output("Scanning 10%\rScanning 55%\rScanning 90%\r")
            on_tick()
            message = mock_from_thread_run.call_args.args[3]
            assert "Scanning 90%" in message


async def test_on_output_bounds_partial_line_buffer(isolated_config, git_repo_no_remote):
    # A backend that emits a very long newline-free (and CR-free) stream
    # must not make partial_line grow without bound (memory exhaustion),
    # even though the durable output_tail is already capped elsewhere. The
    # eventual complete line must reflect only the bounded recent tail of
    # that stream, not the whole thing.
    assert server._PARTIAL_LINE_MAX_CHARS <= 20_000

    captured = {}

    def fake_run_backend(task, repo_path, branch, config, model=None, on_tick=None, on_output=None):
        captured["on_tick"] = on_tick
        captured["on_output"] = on_output
        return CompletionResult(success=True, files_changed=["a.py"], commit_sha="abc123")

    mock_ctx = MagicMock()
    mock_ctx.report_progress = AsyncMock()
    with patch("backends.aider.AiderBackend.run_backend", side_effect=fake_run_backend):
        with patch("anyio.from_thread.run") as mock_from_thread_run:
            await server._delegate_implementation_impl(
                task="add a.py", branch="feature-branch",
                target_repo_path=str(git_repo_no_remote), ctx=mock_ctx,
            )
            on_output = captured["on_output"]
            on_tick = captured["on_tick"]
            # Feed far more than the cap with no line delimiter, then end the
            # line so it gets promoted to the pulse.
            for _ in range(100):
                on_output("x" * 10_000)  # 1,000,000 chars total, no delimiter
            on_output("END\n")
            on_tick()
            message = mock_from_thread_run.call_args.args[3]
            # The promoted line must be bounded (buffer was capped), not ~1MB.
            assert len(message) <= server._PARTIAL_LINE_MAX_CHARS + 200
            assert "END" in message


async def test_on_output_log_write_unicode_error_is_non_fatal(isolated_config, git_repo_no_remote, tmp_path, monkeypatch, capsys):
    # The log open() must be deterministic UTF-8 and must not let a
    # UnicodeEncodeError (or any OSError) abort the attempt. On a non-UTF-8
    # default locale, writing a non-ASCII backend message with the default
    # encoding could raise UnicodeEncodeError, which is NOT an OSError.
    log_path = tmp_path / "local-coder-output.log"
    monkeypatch.setattr(server, "OUTPUT_LOG_PATH", log_path)

    captured = {}

    def fake_run_backend(task, repo_path, branch, config, model=None, on_tick=None, on_output=None):
        captured["on_output"] = on_output
        return CompletionResult(success=True, files_changed=["a.py"], commit_sha="abc123")

    with patch("backends.aider.AiderBackend.run_backend", side_effect=fake_run_backend):
        await server._delegate_implementation_impl(
            task="add a.py", branch="feature-branch",
            target_repo_path=str(git_repo_no_remote), ctx=None,
        )
    on_output = captured["on_output"]
    # A non-ASCII chunk must be written without raising, regardless of locale.
    on_output("café — ✓ résumé\n")
    per_call = list(log_path.parent.glob("local-coder-output-*.log"))
    assert per_call, "a per-call log file should have been created"
    assert "café" in per_call[0].read_text(encoding="utf-8")


async def test_on_output_log_write_failure_warns_only_once(isolated_config, git_repo_no_remote, tmp_path, monkeypatch, capsys):
    # A persistent log-write failure must not flood stderr with a warning
    # on every single chunk (which would bury the live backend output it's
    # meant to surface). Warn once, then stop retrying/warning.
    missing_parent = tmp_path / "does_not_exist" / "local-coder-output.log"
    monkeypatch.setattr(server, "OUTPUT_LOG_PATH", missing_parent)

    captured = {}

    def fake_run_backend(task, repo_path, branch, config, model=None, on_tick=None, on_output=None):
        captured["on_output"] = on_output
        return CompletionResult(success=True, files_changed=["a.py"], commit_sha="abc123")

    with patch("backends.aider.AiderBackend.run_backend", side_effect=fake_run_backend):
        await server._delegate_implementation_impl(
            task="add a.py", branch="feature-branch",
            target_repo_path=str(git_repo_no_remote), ctx=None,
        )
    on_output = captured["on_output"]
    for _ in range(5):
        on_output("some output line\n")

    warnings = capsys.readouterr().err.count("failed to write output log")
    assert warnings == 1, f"expected exactly one warning, got {warnings}"


async def test_output_log_path_announced_early_for_live_tailing(isolated_config, git_repo_no_remote):
    # The per-call log path is returned in the result dict, but that only
    # arrives AFTER the whole delegation (including push/PR) finishes —
    # useless for live `tail -f`. Announce it early, before run_backend, so
    # a user can start tailing while the backend is still running. We
    # verify the path shows up via ctx.report_progress before the backend
    # result is produced.
    announced = []

    def fake_run_backend(task, repo_path, branch, config, model=None, on_tick=None, on_output=None):
        # By the time the backend runs, the log path must already have been
        # announced (the announce happens before this call).
        return CompletionResult(success=True, files_changed=["a.py"], commit_sha="abc123")

    mock_ctx = MagicMock()

    async def record_progress(progress, total=None, message=None):
        if message is not None:
            announced.append(message)

    mock_ctx.report_progress = record_progress

    with patch("backends.aider.AiderBackend.run_backend", side_effect=fake_run_backend):
        # on_tick's report_progress is bridged via anyio.from_thread.run;
        # mock that so the tick path is inert and we only observe the direct
        # early-announce report_progress call.
        with patch("anyio.from_thread.run"):
            result = await server._delegate_implementation_impl(
                task="add a.py", branch="feature-branch",
                target_repo_path=str(git_repo_no_remote), ctx=mock_ctx,
            )

    log_name = Path(result["output_log"]).name
    assert any(log_name in msg for msg in announced), (
        f"expected the per-call log path {log_name!r} to be announced via "
        f"report_progress before the call finished; got {announced!r}"
    )


async def test_early_log_announce_failure_does_not_abort_delegation(isolated_config, git_repo_no_remote, capsys):
    # The early log-path announce is a best-effort visibility convenience —
    # if ctx.report_progress raises (client dropped the progress token,
    # transport hiccup, unsupported capability), it must NOT abort the whole
    # delegation before the backend even runs. Otherwise a notification
    # failure turns a best-effort channel into a hard dependency for the
    # primary implementation workflow.
    ran = {"backend": False}

    def fake_run_backend(task, repo_path, branch, config, model=None, on_tick=None, on_output=None):
        ran["backend"] = True
        return CompletionResult(success=True, files_changed=["a.py"], commit_sha="abc123")

    mock_ctx = MagicMock()

    async def boom(*args, **kwargs):
        raise RuntimeError("progress channel is gone")

    mock_ctx.report_progress = boom

    with patch("backends.aider.AiderBackend.run_backend", side_effect=fake_run_backend):
        with patch("anyio.from_thread.run"):  # keep on_tick bridging inert
            result = await server._delegate_implementation_impl(
                task="add a.py", branch="feature-branch",
                target_repo_path=str(git_repo_no_remote), ctx=mock_ctx,
            )

    # The delegation still ran and succeeded despite the announce failure.
    assert ran["backend"] is True
    assert result["success"] is True
    # The failure was surfaced as a warning, not an abort.
    assert "failed to announce output log" in capsys.readouterr().err


async def test_push_failure_response_includes_output_tail(isolated_config, git_repo_with_remote, monkeypatch):
    # A push failure after a successful backend attempt still has the
    # backend transcript in scope — surface it so the caller can see what
    # the model did, even though the failure was in the push step, not the
    # backend. Mirrors the success + all-failed paths.
    def fake_run_backend(task, repo_path, branch, config, model=None, on_tick=None, on_output=None):
        return CompletionResult(
            success=True, files_changed=["a.py"], commit_sha="abc123",
            output_tail="backend transcript before push failed",
        )

    real_run = subprocess.run

    def fake_push(*args, **kwargs):
        cmd = args[0] if args else kwargs.get("args", [])
        if isinstance(cmd, list) and "push" in cmd:
            return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="remote rejected")
        return real_run(*args, **kwargs)

    with patch("backends.aider.AiderBackend.run_backend", side_effect=fake_run_backend):
        with patch("subprocess.run", side_effect=fake_push):
            result = await server._delegate_implementation_impl(
                task="add a.py", branch="feature-branch",
                target_repo_path=str(git_repo_with_remote),
                ctx=None,
            )

    assert result["success"] is False
    assert "git push failed" in result["error"]
    assert result["output_tail"] == "backend transcript before push failed"


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


def test_configure_persists_first_output_timeout_seconds(isolated_config):
    # The cold-load grace window must be settable through the configure MCP
    # tool (the README tells users never to hand-edit config.yaml), not just
    # present in config.py's validation.
    result = server._configure_impl(first_output_timeout_seconds=900)
    assert result["first_output_timeout_seconds"] == 900


def test_configure_persists_log_retention_count(isolated_config):
    # Log retention must be settable through the configure MCP tool (users are
    # told never to hand-edit config.yaml), not just present in config.py.
    result = server._configure_impl(log_retention_count=25)
    assert result["log_retention_count"] == 25


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


async def test_on_tick_progress_failure_does_not_propagate(isolated_config, git_repo_no_remote):
    # The periodic on_tick pulse bridges to ctx.report_progress via
    # anyio.from_thread.run. If that raises (a dropped progress token, a
    # transport hiccup, a client without progress support), the exception
    # must NOT escape on_tick: run_monitored_subprocess kills the backend on
    # any on_tick exception, and because that kill is not a StallError,
    # AiderBackend.run_backend's working-tree restore never runs — so the
    # server's broad except would start the fallback model on top of partial,
    # uncleaned edits. The announce path already guards its report_progress;
    # the periodic tick must too. on_tick must swallow-and-warn instead.
    captured_on_tick = {}

    def fake_run_backend(task, repo_path, branch, config, model=None, on_tick=None, on_output=None):
        captured_on_tick["on_tick"] = on_tick
        return CompletionResult(success=True, files_changed=["a.py"], commit_sha="abc123")

    mock_ctx = MagicMock()
    mock_ctx.report_progress = AsyncMock()

    with patch("backends.aider.AiderBackend.run_backend", side_effect=fake_run_backend):
        # Make the thread-bridge raise, simulating report_progress failing.
        with patch("anyio.from_thread.run", side_effect=RuntimeError("progress token gone")):
            result = await server._delegate_implementation_impl(
                task="add a.py", branch="feature-branch",
                target_repo_path=str(git_repo_no_remote),
                ctx=mock_ctx,
            )

            assert result["success"] is True
            on_tick = captured_on_tick["on_tick"]
            # Invoking on_tick as run_monitored_subprocess would must NOT raise.
            on_tick()  # would raise RuntimeError today — the bug this guards


async def test_on_tick_progress_failure_warns_only_once(isolated_config, git_repo_no_remote, capsys):
    # A permanently dead progress channel (client gone, token invalidated)
    # must warn ONCE and then stop reporting for the rest of the call —
    # otherwise a long run floods MCP stderr with a warning every tick and
    # buries the live pulse the stderr print is meant to surface. Mirrors the
    # on_output log-write warn-once guard.
    captured_on_tick = {}

    def fake_run_backend(task, repo_path, branch, config, model=None, on_tick=None, on_output=None):
        captured_on_tick["on_tick"] = on_tick
        return CompletionResult(success=True, files_changed=["a.py"], commit_sha="abc123")

    mock_ctx = MagicMock()
    mock_ctx.report_progress = AsyncMock()

    with patch("backends.aider.AiderBackend.run_backend", side_effect=fake_run_backend):
        with patch("anyio.from_thread.run", side_effect=RuntimeError("progress token gone")):
            await server._delegate_implementation_impl(
                task="add a.py", branch="feature-branch",
                target_repo_path=str(git_repo_no_remote), ctx=mock_ctx,
            )
            on_tick = captured_on_tick["on_tick"]
            capsys.readouterr()  # drain output from the call itself
            # Fire the tick several times as run_monitored_subprocess would
            # over a long run. None may raise, and only the FIRST emits the
            # disable-warning.
            for _ in range(5):
                on_tick()

    err = capsys.readouterr().err
    assert err.count("disabling further progress reports") == 1
    # The live pulse still reaches stderr on every tick regardless.
    assert err.count("Running") == 5


async def test_pr_setup_survives_missing_gh_binary(isolated_config, git_repo_with_remote):
    # With open_pr enabled but the `gh` binary absent, subprocess.run(["gh",
    # ...]) raises FileNotFoundError BEFORE any process starts. _has_open_pr
    # (and the gh pr create call) only catch GhPrStatusUnknown/TimeoutExpired,
    # so the FileNotFoundError escapes uncaught — AFTER the implementation was
    # committed and pushed. That reports total failure for work that actually
    # landed. The call must stay success:true with a note that PR setup
    # couldn't run because gh is missing.
    config_module.merge_config({"open_pr": True, "pr_base_branch": "main"})
    subprocess.run(
        ["git", "-C", str(git_repo_with_remote), "checkout", "-b", "local-coder/feature-branch"],
        check=True, capture_output=True,
    )
    fake_result = CompletionResult(success=True, files_changed=["a.py"], commit_sha="abc123")

    real_run = subprocess.run

    def run_but_gh_missing(cmd, *args, **kwargs):
        # Simulate `gh` not being installed: only gh invocations raise
        # FileNotFoundError; git push etc. run for real.
        if cmd and cmd[0] == "gh":
            raise FileNotFoundError(2, "No such file or directory: 'gh'")
        return real_run(cmd, *args, **kwargs)

    with patch("backends.aider.AiderBackend.run_backend", return_value=fake_result):
        with patch("subprocess.run", side_effect=run_but_gh_missing):
            result = await server._delegate_implementation_impl(
                task="add a.py", branch="local-coder/feature-branch",
                target_repo_path=str(git_repo_with_remote),
            )

    assert result["success"] is True, result
    assert result["files_changed"] == ["a.py"]
    assert result["pr_url"] is None  # PR creation was skipped, not attempted
    assert "note" in result
    assert "gh" in result["note"].lower()


async def test_pr_create_survives_gh_launch_error(isolated_config, git_repo_with_remote):
    # Defense-in-depth for the OTHER gh call site: `gh pr view` reports no PR
    # (returncode 1, "no pull requests found"), so the code proceeds to
    # `gh pr create` — and THAT launch fails (gh removed mid-call, or a PATH
    # race). The create-time OSError must be caught too: the implementation
    # was already committed and pushed, so the call stays success:true with a
    # note rather than raising for landed work.
    config_module.merge_config({"open_pr": True, "pr_base_branch": "main"})
    subprocess.run(
        ["git", "-C", str(git_repo_with_remote), "checkout", "-b", "local-coder/feature-branch"],
        check=True, capture_output=True,
    )
    fake_result = CompletionResult(success=True, files_changed=["a.py"], commit_sha="abc123")

    real_run = subprocess.run

    def run_gh_view_ok_create_missing(cmd, *args, **kwargs):
        if cmd[:3] == ["gh", "pr", "view"]:
            # "no PR" — a completed process, not an exception.
            return subprocess.CompletedProcess(
                cmd, 1, stdout="", stderr="no pull requests found for branch",
            )
        if cmd[:3] == ["gh", "pr", "create"]:
            raise FileNotFoundError(2, "No such file or directory: 'gh'")
        return real_run(cmd, *args, **kwargs)

    with patch("backends.aider.AiderBackend.run_backend", return_value=fake_result):
        with patch("subprocess.run", side_effect=run_gh_view_ok_create_missing):
            result = await server._delegate_implementation_impl(
                task="add a.py", branch="local-coder/feature-branch",
                target_repo_path=str(git_repo_with_remote),
            )

    assert result["success"] is True, result
    assert result["pr_url"] is None
    assert "note" in result
    assert "gh" in result["note"].lower()


async def test_per_call_log_exists_after_announce_before_output(
    isolated_config, git_repo_no_remote, tmp_path, monkeypatch
):
    # The per-call log path is announced up front (with `tail -f` guidance)
    # so a human can follow a cold-loading backend live — exactly when early
    # observation matters most and no output has arrived yet. But the file is
    # only created on the first on_output write, so during a cold load
    # `tail -f` exits immediately (the file doesn't exist). The empty file
    # must be created at announce time, before the backend produces anything.
    log_path = tmp_path / "local-coder-output.log"
    monkeypatch.setattr(server, "OUTPUT_LOG_PATH", log_path)

    saw_file_at_backend_start = {}

    def fake_run_backend(task, repo_path, branch, config, model=None, on_tick=None, on_output=None):
        # At this point the announce has already happened but no on_output
        # (backend output) has been emitted — a cold load. The per-call log
        # must already exist so `tail -f` works.
        per_call = list(tmp_path.glob("local-coder-output-*.log"))
        saw_file_at_backend_start["exists"] = len(per_call) == 1 and per_call[0].exists()
        return CompletionResult(success=True, files_changed=["a.py"], commit_sha="abc123")

    with patch("backends.aider.AiderBackend.run_backend", side_effect=fake_run_backend):
        result = await server._delegate_implementation_impl(
            task="add a.py", branch="feature-branch",
            target_repo_path=str(git_repo_no_remote), ctx=None,
        )

    assert result["success"] is True
    assert saw_file_at_backend_start.get("exists") is True, (
        "per-call log file must exist after announce and before any backend "
        "output, so a cold-load `tail -f` doesn't exit on a missing file"
    )


def test_prune_old_logs_keeps_only_newest_n(tmp_path, monkeypatch):
    # Per-call log files otherwise accumulate forever. _prune_old_logs keeps
    # the N most recently modified per-call logs and deletes the rest, so the
    # log dir stays bounded. Only files matching the per-call naming pattern
    # are touched — unrelated files in the dir must survive.
    base = tmp_path / "local-coder-output.log"
    monkeypatch.setattr(server, "OUTPUT_LOG_PATH", base)

    # Create 6 per-call logs with staggered mtimes (oldest first).
    import time as _time
    made = []
    for i in range(6):
        p = tmp_path / f"local-coder-output-{i:03d}.log"
        p.write_text(f"log {i}\n")
        os.utime(p, (1000 + i, 1000 + i))  # ascending mtime; i=5 is newest
        made.append(p)
    # An unrelated file that must NOT be pruned.
    unrelated = tmp_path / "config.yaml"
    unrelated.write_text("keep me\n")

    server._prune_old_logs(count=3)

    survivors = sorted(p.name for p in tmp_path.glob("local-coder-output-*.log"))
    # Newest 3 (indices 3,4,5) kept; oldest 3 (0,1,2) deleted.
    assert survivors == ["local-coder-output-003.log",
                         "local-coder-output-004.log",
                         "local-coder-output-005.log"]
    assert unrelated.exists()


def test_prune_old_logs_is_best_effort(tmp_path, monkeypatch):
    # A prune failure (permission error, race with another call deleting the
    # same file) must never abort the delegation — pruning is housekeeping,
    # not part of the real workflow.
    base = tmp_path / "local-coder-output.log"
    monkeypatch.setattr(server, "OUTPUT_LOG_PATH", base)
    for i in range(4):
        (tmp_path / f"local-coder-output-{i:03d}.log").write_text("x\n")

    # Make unlink raise; _prune_old_logs must swallow it and not propagate.
    with patch.object(Path, "unlink", side_effect=OSError("boom")):
        server._prune_old_logs(count=1)  # must not raise


def test_prune_old_logs_never_deletes_in_flight_log(tmp_path, monkeypatch):
    # A log owned by a still-running delegation (registered in _ACTIVE_OUTPUT_LOGS)
    # must NOT be pruned even if its mtime is the oldest — a cold-loading call
    # that hasn't emitted yet has a stale mtime, and deleting it would let its
    # on_output lazily recreate it outside the lock and drift over the cap.
    base = tmp_path / "local-coder-output.log"
    monkeypatch.setattr(server, "OUTPUT_LOG_PATH", base)
    # 4 logs; the OLDEST is the in-flight one.
    made = []
    for i in range(4):
        p = tmp_path / f"local-coder-output-{i:03d}.log"
        p.write_text("x\n")
        os.utime(p, (1000 + i, 1000 + i))  # index 0 oldest
        made.append(p)
    in_flight = made[0]  # oldest → would normally be pruned first
    monkeypatch.setattr(server, "_ACTIVE_OUTPUT_LOGS", {str(in_flight)})

    server._prune_old_logs(count=2)

    survivors = {p.name for p in tmp_path.glob("local-coder-output-*.log")}
    # in-flight (oldest) is kept; it occupies a slot, so with count=2 exactly one
    # OTHER (the newest) survives alongside it. Total survivors == 2.
    assert in_flight.name in survivors
    assert len(survivors) == 2, survivors
    assert made[3].name in survivors  # newest prunable kept


def test_prune_old_logs_deletes_all_prunable_when_active_fills_budget(tmp_path, monkeypatch):
    # If the in-flight (always-kept) logs already meet the keep budget, every
    # prunable log is deleted — active logs must not be double-counted into a
    # larger total than the cap allows.
    base = tmp_path / "local-coder-output.log"
    monkeypatch.setattr(server, "OUTPUT_LOG_PATH", base)
    active = []
    for i in range(2):
        p = tmp_path / f"local-coder-output-active{i}.log"
        p.write_text("x\n"); active.append(p)
    for i in range(3):
        (tmp_path / f"local-coder-output-old{i}.log").write_text("x\n")
    monkeypatch.setattr(server, "_ACTIVE_OUTPUT_LOGS", {str(p) for p in active})

    server._prune_old_logs(count=2)  # budget == number of active logs

    survivors = {p.name for p in tmp_path.glob("local-coder-output-*.log")}
    assert survivors == {p.name for p in active}, survivors


async def test_delegate_deregisters_in_flight_log_on_completion(
    isolated_config, git_repo_no_remote, tmp_path, monkeypatch
):
    # After a delegation finishes, its log must be removed from the in-flight
    # set so future prunes can reclaim it — otherwise the set grows forever and
    # finished logs are never pruned.
    base = tmp_path / "local-coder-output.log"
    monkeypatch.setattr(server, "OUTPUT_LOG_PATH", base)
    monkeypatch.setattr(server, "_ACTIVE_OUTPUT_LOGS", set())

    def fake_backend(task, repo_path, branch, config, model=None, on_tick=None, on_output=None):
        # While the backend runs, this call's log IS registered as in-flight.
        assert len(server._ACTIVE_OUTPUT_LOGS) == 1
        return CompletionResult(success=True, files_changed=["a.py"], commit_sha="abc123")

    with patch("backends.aider.AiderBackend.run_backend", side_effect=fake_backend):
        result = await server._delegate_implementation_impl(
            task="add a.py", branch="feature-branch",
            target_repo_path=str(git_repo_no_remote), ctx=None,
        )
    assert result["success"] is True
    # Deregistered on completion.
    assert server._ACTIVE_OUTPUT_LOGS == set()


def test_coerce_retention_falls_back_on_bad_values():
    # Strictly-positive ints pass through; everything else becomes the default.
    assert server._coerce_retention(25) == 25
    assert server._coerce_retention(1) == 1
    # Non-int, non-positive, and bool (a bool IS an int subclass) all fall back.
    assert server._coerce_retention("fifty") == 50
    assert server._coerce_retention(0) == 50
    assert server._coerce_retention(-3) == 50
    assert server._coerce_retention(None) == 50
    assert server._coerce_retention(2.5) == 50
    assert server._coerce_retention(True) == 50


def test_prune_and_create_output_log_is_atomic_under_lock(isolated_config, tmp_path, monkeypatch):
    # The prune+create sequence must run inside config_module._config_lock so
    # two overlapping delegations cannot each prune-to-(N-1) then each create,
    # leaving N+1 on disk. Assert the helper (a) holds the lock across BOTH the
    # prune and the touch, and (b) leaves exactly `retention` logs on disk.
    base = tmp_path / "local-coder-output.log"
    monkeypatch.setattr(server, "OUTPUT_LOG_PATH", base)
    monkeypatch.setattr(server, "_ACTIVE_OUTPUT_LOGS", set())
    # Seed 5 stale per-call logs (ascending mtime).
    for i in range(5):
        p = tmp_path / f"local-coder-output-stale{i}.log"
        p.write_text("old\n")
        os.utime(p, (1000 + i, 1000 + i))

    events = []
    real_lock = config_module._config_lock

    @contextlib.contextmanager
    def tracking_lock():
        events.append("lock-acquire")
        with real_lock():
            yield
        events.append("lock-release")

    real_prune = server._prune_old_logs

    def tracking_prune(count):
        events.append("prune")
        return real_prune(count)

    real_touch = Path.touch

    def tracking_touch(self, *a, **k):
        if self == new_log:
            events.append("touch")
        return real_touch(self, *a, **k)

    new_log = server._make_output_log_path()
    with patch.object(config_module, "_config_lock", tracking_lock), \
         patch.object(server, "_prune_old_logs", tracking_prune), \
         patch.object(Path, "touch", tracking_touch):
        server._prune_and_create_output_log(new_log, retention=3)

    # Both prune and touch happened strictly between acquire and release.
    assert events == ["lock-acquire", "prune", "touch", "lock-release"], events
    # Cap of 3 = 2 retained old logs + this call's fresh log. Exactly 3.
    remaining = list(tmp_path.glob("local-coder-output-*.log"))
    assert len(remaining) == 3, [p.name for p in remaining]
    assert new_log.exists()


def test_prune_and_create_output_log_is_best_effort_on_touch_failure(
    isolated_config, tmp_path, monkeypatch
):
    # A touch (or lock) failure must never abort the delegation — on_output
    # re-creates the file lazily. The helper swallows OSError and returns.
    base = tmp_path / "local-coder-output.log"
    monkeypatch.setattr(server, "OUTPUT_LOG_PATH", base)
    monkeypatch.setattr(server, "_ACTIVE_OUTPUT_LOGS", set())
    new_log = server._make_output_log_path()
    with patch.object(Path, "touch", side_effect=OSError("boom")):
        server._prune_and_create_output_log(new_log, retention=3)  # must not raise


async def test_delegate_prunes_logs_on_each_call(isolated_config, git_repo_no_remote, tmp_path, monkeypatch):
    # A real delegation prunes old per-call logs so the TOTAL after this call's
    # own fresh log is written stays at the configured cap — not one over it.
    # log_retention_count is the total-file cap: keep (count - 1) old logs and
    # let the new one fill the last slot.
    base = tmp_path / "local-coder-output.log"
    monkeypatch.setattr(server, "OUTPUT_LOG_PATH", base)
    config_module.merge_config({"log_retention_count": 2})
    # Seed 5 stale per-call logs.
    for i in range(5):
        p = tmp_path / f"local-coder-output-stale{i}.log"
        p.write_text("old\n")
        os.utime(p, (1000 + i, 1000 + i))

    def fake_backend(task, repo_path, branch, config, model=None, on_tick=None, on_output=None):
        return CompletionResult(success=True, files_changed=["a.py"], commit_sha="abc123")

    with patch("backends.aider.AiderBackend.run_backend", side_effect=fake_backend):
        result = await server._delegate_implementation_impl(
            task="add a.py", branch="feature-branch",
            target_repo_path=str(git_repo_no_remote), ctx=None,
        )
    assert result["success"] is True
    # Cap of 2 = 1 retained old log + this call's own fresh log. Exactly 2.
    remaining = list(tmp_path.glob("local-coder-output-*.log"))
    assert len(remaining) == 2, [p.name for p in remaining]
    # This call's own log must be among the survivors.
    assert Path(result["output_log"]).exists()


async def test_delegate_survives_non_int_retention_in_config(
    isolated_config, git_repo_no_remote, tmp_path, monkeypatch
):
    # configure() validates log_retention_count, but config.yaml can be
    # hand-edited to a non-int (or non-positive) that would otherwise crash
    # _prune_old_logs (a `<=`/slice against a str) and abort the delegation
    # before the backend runs. The call site must coerce a bad value back to
    # the default instead of raising.
    base = tmp_path / "local-coder-output.log"
    monkeypatch.setattr(server, "OUTPUT_LOG_PATH", base)
    # Write a corrupt value straight to disk, bypassing configure()'s validation.
    config_module.save_config({**config_module.load_config(), "log_retention_count": "fifty"})

    def fake_backend(task, repo_path, branch, config, model=None, on_tick=None, on_output=None):
        return CompletionResult(success=True, files_changed=["a.py"], commit_sha="abc123")

    with patch("backends.aider.AiderBackend.run_backend", side_effect=fake_backend):
        result = await server._delegate_implementation_impl(
            task="add a.py", branch="feature-branch",
            target_repo_path=str(git_repo_no_remote), ctx=None,
        )
    # The delegation completes rather than crashing on the bad retention value.
    assert result["success"] is True


def test_output_log_path_lives_under_plugin_data_dir(tmp_path, monkeypatch):
    # The per-call log base must live in the persistent plugin-data dir
    # (survives plugin updates), not the ephemeral plugin root. server.py
    # computes OUTPUT_LOG_PATH at import via plugin_data_dir(); verify the
    # resolution honors CLAUDE_PLUGIN_DATA by re-importing the module fresh.
    import importlib
    monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path))
    import server as server_module
    importlib.reload(server_module)
    try:
        assert server_module.OUTPUT_LOG_PATH.parent == tmp_path / "local-coder"
        assert server_module.OUTPUT_LOG_PATH.name == "local-coder-output.log"
    finally:
        monkeypatch.delenv("CLAUDE_PLUGIN_DATA", raising=False)
        importlib.reload(server_module)  # restore in-tree default for other tests
