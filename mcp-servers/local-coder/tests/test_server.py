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
