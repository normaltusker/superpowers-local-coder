import os
import subprocess
import sys
import uuid
from pathlib import Path

import anyio
from fastmcp import Context, FastMCP

import config as config_module
from backends.aider import AiderBackend
from backends.codex import CodexBackend
from backends.gemini import GeminiBackend
from backends.openrouter import OpenRouterBackend
from backends.common import KNOWN_BACKENDS, validate_branch_name

mcp = FastMCP("local-coder")

# Applied to git push / gh pr view / gh pr create — these are normally fast,
# but with no timeout at all a stalled network call could hang indefinitely
# even though the local implementation already succeeded. Generous enough
# to not false-positive on a slow connection, bounded enough to actually
# protect against a true hang.
NETWORK_SUBPROCESS_TIMEOUT_SECONDS = 45

# A delegated backend's live output is also written here, in addition to
# this server's own stderr. Claude Code pipes an MCP server's stdout/
# stderr over an internal socket it owns, with no reliable external tap
# point (confirmed: neither `claude --debug-file` nor any
# filesystem-visible fd surfaces it) — a plain file, written directly by
# this process, is the only way to guarantee `tail -f` shows a delegated
# attempt's real output live, regardless of how the parent process pipes
# stderr.
#
# This is the BASE path. Each delegate_implementation call computes a
# UNIQUE per-call file next to it (see _make_output_log_path) — two
# overlapping calls must not share one file, or the second call's
# start-of-call truncation would wipe the first's active log and their
# output would interleave. The concrete per-call path is returned in the
# result dict (`output_log`) so a human knows which file to tail.
OUTPUT_LOG_PATH = Path(__file__).parent / "local-coder-output.log"


def _make_output_log_path() -> Path:
    """A unique per-call log file next to OUTPUT_LOG_PATH, so concurrent
    delegate_implementation calls never share (and corrupt) one file."""
    unique = f"{os.getpid()}-{uuid.uuid4().hex[:8]}"
    return OUTPUT_LOG_PATH.with_name(
        f"{OUTPUT_LOG_PATH.stem}-{unique}{OUTPUT_LOG_PATH.suffix}"
    )


# Cap on the in-memory buffer that holds an incomplete (not-yet-delimited)
# output line for the progress pulse. A backend emitting a very long
# delimiter-free stream (e.g. a huge single line, or carriage-return
# progress on a terminal that we treat as delimited below) must not grow
# this without bound. The durable transcript is capped separately in
# run_monitored_subprocess (_MAX_OUTPUT_CHARS); this is only the pulse's
# working buffer, so a small cap is plenty.
_PARTIAL_LINE_MAX_CHARS = 8_000

BACKENDS = {
    "aider": AiderBackend,
    "codex": CodexBackend,
    "gemini": GeminiBackend,
    "openrouter": OpenRouterBackend,
}

assert set(BACKENDS) == set(KNOWN_BACKENDS), (
    "server.py's BACKENDS dict and backends.common.KNOWN_BACKENDS have "
    "drifted apart — keep them in sync."
)


def _has_origin_remote(repo_path: str) -> bool:
    result = subprocess.run(
        ["git", "-C", repo_path, "remote"],
        capture_output=True, text=True,
    )
    return "origin" in result.stdout.split()


class GhPrStatusUnknown(Exception):
    """Raised when `gh pr view` fails for a reason other than "no PR exists
    for this branch" — e.g. a network issue, `gh` not authenticated, or a
    transient API error. These must not be treated the same as "no PR", or
    the caller might attempt an unwanted `gh pr create` (risking a
    duplicate PR) or silently mask a real `gh` problem."""


def _has_open_pr(repo_path: str, branch: str) -> bool:
    result = subprocess.run(
        ["gh", "pr", "view", branch],
        cwd=repo_path, capture_output=True, text=True,
        timeout=NETWORK_SUBPROCESS_TIMEOUT_SECONDS,
    )
    if result.returncode == 0:
        return True
    if "no pull requests found" in result.stderr.lower():
        return False
    raise GhPrStatusUnknown(result.stderr.strip() or "gh pr view failed with no stderr output")


async def _delegate_implementation_impl(
    task: str, branch: str, target_repo_path: str | None = None,
    ctx: Context | None = None,
) -> dict:
    # Unique per-call log file so two overlapping calls never share (and
    # corrupt) one file. Because it's fresh per call, there is no stale
    # prior-call content to truncate — the file simply gets created on
    # first write. The path is surfaced in the result dict below so a
    # human knows which file to tail.
    output_log_path = _make_output_log_path()

    try:
        validate_branch_name(branch)
    except ValueError as e:
        return {"success": False, "error": str(e)}

    cfg = config_module.load_config()
    repo_path = target_repo_path or cfg.get("target_repo_path")
    if not repo_path:
        return {"success": False, "error": "target_repo_path not provided and not set in config"}

    # Apply the configured branch_prefix if the caller-supplied branch name
    # doesn't already carry it. This is the only point where `branch` is
    # actually used (branch creation, push, PR), so applying it here makes
    # branch_prefix take effect for every call, transparently to whatever
    # constructed the raw branch name upstream (e.g. the SDD skill).
    branch_prefix = cfg.get("branch_prefix") or ""
    if branch_prefix and not branch.startswith(branch_prefix):
        branch = f"{branch_prefix}{branch}"
        try:
            validate_branch_name(branch)
        except ValueError as e:
            return {"success": False, "error": str(e)}

    backend_name = cfg["backend"]
    backend_cls = BACKENDS.get(backend_name)
    if backend_cls is None:
        return {"success": False, "error": f"unknown backend: {backend_name}"}
    backend = backend_cls()

    # Announce the per-call log path up front — before run_backend — so a
    # human can start `tail -f`-ing it while the backend is still running.
    # (The same path is also returned in the final result dict, but that
    # only arrives after the whole call, including push/PR, has finished,
    # which is too late for live tailing.) Best-effort: to stderr always,
    # and via the progress channel when a Context is available. We're in
    # the async body here (not a worker thread), so await report_progress
    # directly rather than bridging through anyio.from_thread.
    print(
        f"[local-coder] streaming backend output to {output_log_path} "
        "(tail -f it to follow live)",
        file=sys.stderr, flush=True,
    )
    if ctx is not None:
        await ctx.report_progress(0, None, f"Output log: {output_log_path}")

    attempt_models = [cfg["model"], *cfg.get("fallback_models", [])]
    attempt_errors = []
    last_output_tail = ""

    # Shared holders (one-element mutable cells both closures can see):
    #   latest_line   — the most recent COMPLETE output line; on_tick reads
    #                   it so the periodic progress pulse carries real
    #                   backend output instead of a static heartbeat.
    #   partial_line  — buffer for a chunk that ended mid-line (no line
    #                   delimiter yet); carried across on_output calls so
    #                   the pulse never shows an arbitrary partial suffix.
    #                   Bounded to _PARTIAL_LINE_MAX_CHARS so a long
    #                   delimiter-free stream can't grow it without bound.
    #   log_write_ok  — flips False after the first log-write failure so we
    #                   warn once and stop retrying (a persistent failure
    #                   must not flood stderr every chunk and bury the live
    #                   output it's meant to surface).
    # latest_line + partial_line are RESET at the top of each failover
    # attempt (see the loop below) so a fallback model's opening pulse
    # can't relabel the previous model's last line as its own.
    latest_line = [""]
    partial_line = [""]
    log_write_ok = [True]

    def make_on_tick(model_name: str):
        def on_tick():
            line = latest_line[0]
            pulse = f"{model_name}: {line}" if line else f"Running {model_name}..."
            print(f"[local-coder] {pulse}", file=sys.stderr, flush=True)
            if ctx is not None:
                anyio.from_thread.run(
                    ctx.report_progress, 0, None, pulse
                )
        return on_tick

    def make_on_output(model_name: str):
        # Streams the backend subprocess's actual output as it arrives, to
        # BOTH stderr and the per-call output_log_path, and records the
        # latest complete line so the on_tick pulse above can surface it
        # in-chat. Kept throttled to the tick cadence — the tick is what
        # emits to report_progress; this callback only records.
        def on_output(chunk: str) -> None:
            line = f"[local-coder:{model_name}] {chunk}"
            print(line, end="", file=sys.stderr, flush=True)
            # The log file is a best-effort debugging convenience, not the
            # primary channel (stderr above + the on_tick pulse are). A
            # write failure here must NEVER propagate: run_monitored_subprocess
            # kills the subprocess on any on_output exception, but only the
            # StallError path in AiderBackend.run_backend runs the
            # working-tree cleanup — so an unhandled exception would bypass
            # cleanup and leave partial backend writes to pollute the next
            # fallback attempt. So: write with explicit UTF-8 (the stream is
            # already UTF-8-decoded upstream; the default encoding on a
            # non-UTF-8 locale could otherwise raise UnicodeEncodeError,
            # which is NOT an OSError), and catch broadly. After the first
            # failure, stop retrying and warn only once — a persistent
            # failure must not flood stderr on every chunk and bury the live
            # output this is meant to surface.
            if log_write_ok[0]:
                try:
                    with open(output_log_path, "a", encoding="utf-8", errors="replace") as f:
                        f.write(chunk)
                except Exception as e:
                    log_write_ok[0] = False
                    print(
                        f"[local-coder] warning: failed to write output log "
                        f"(disabling further log writes for this call): {e}",
                        file=sys.stderr, flush=True,
                    )
            # on_output receives RAW read chunks, which can split mid-line.
            # Prepend any buffered partial from the previous chunk, then
            # promote only COMPLETE lines to latest_line; whatever follows
            # the last delimiter is an incomplete line — buffer it (bounded)
            # until a later chunk finishes it, so the pulse never shows an
            # arbitrary chunk suffix. "\r" is treated as a delimiter too, so
            # carriage-return progress output (aider/pip-style bars, which
            # never send "\n") still updates the pulse and can't grow the
            # buffer forever.
            buffered = partial_line[0] + chunk
            normalized = buffered.replace("\r\n", "\n").replace("\r", "\n")
            if "\n" in normalized:
                complete, _, remainder = normalized.rpartition("\n")
                complete_lines = [ln for ln in complete.splitlines() if ln.strip()]
                if complete_lines:
                    latest_line[0] = complete_lines[-1].strip()
            else:
                remainder = normalized
            # Bound the carried-over partial: keep only its tail, so a long
            # delimiter-free stream can't exhaust memory.
            partial_line[0] = remainder[-_PARTIAL_LINE_MAX_CHARS:]
        return on_output

    for model in attempt_models:
        # Reset the per-attempt holders so this attempt starts clean:
        #  - pulse holders, so this model's first tick (before it emits
        #    anything) can't surface the PREVIOUS model's last line as its
        #    own;
        #  - log_write_ok, so a log-write failure during one model's attempt
        #    doesn't permanently disable logging for the next fallback.
        latest_line[0] = ""
        partial_line[0] = ""
        log_write_ok[0] = True
        try:
            result = await anyio.to_thread.run_sync(
                lambda model=model: backend.run_backend(
                    task, repo_path, branch, cfg, model=model,
                    on_tick=make_on_tick(model), on_output=make_on_output(model),
                )
            )
        except NotImplementedError as e:
            return {"success": False, "error": str(e)}
        except Exception as e:
            # Any other unexpected exception from a backend attempt (e.g. an
            # uncaught CalledProcessError from a backend's own git calls)
            # shouldn't crash the whole tool call — treat it like a failed
            # attempt and continue the failover loop to the next model.
            attempt_errors.append(f"{model}: {e}")
            continue

        if result.success:
            note = None
            pr_url = None
            has_remote = await anyio.to_thread.run_sync(_has_origin_remote, repo_path)
            if has_remote:
                try:
                    push = await anyio.to_thread.run_sync(
                        lambda: subprocess.run(
                            ["git", "-C", repo_path, "push", "-u", "origin", branch],
                            capture_output=True, text=True,
                            timeout=NETWORK_SUBPROCESS_TIMEOUT_SECONDS,
                        )
                    )
                except subprocess.TimeoutExpired:
                    return {
                        "success": False,
                        "error": (
                            f"git push timed out after {NETWORK_SUBPROCESS_TIMEOUT_SECONDS}s "
                            "— the commit still exists locally on branch "
                            f"{branch!r}, but was not pushed"
                        ),
                        "files_changed": result.files_changed,
                        "commit_sha": result.commit_sha,
                        "model_used": model,
                        "output_tail": result.output_tail,
                        "output_log": str(output_log_path),
                    }
                if push.returncode != 0:
                    return {
                        "success": False,
                        "error": f"git push failed: {push.stderr.strip()}",
                        "files_changed": result.files_changed,
                        "commit_sha": result.commit_sha,
                        "model_used": model,
                        "output_tail": result.output_tail,
                        "output_log": str(output_log_path),
                    }

                if cfg.get("open_pr"):
                    try:
                        has_open_pr = await anyio.to_thread.run_sync(_has_open_pr, repo_path, branch)
                    except GhPrStatusUnknown as e:
                        # gh pr view failed for a reason other than "no PR" —
                        # don't guess; skip PR creation rather than risk a
                        # duplicate PR or mask a real `gh` problem.
                        note = f"could not verify PR status, skipped PR creation: {e}"
                        has_open_pr = True  # skip the create-PR branch below
                    except subprocess.TimeoutExpired:
                        note = (
                            f"gh pr view timed out after {NETWORK_SUBPROCESS_TIMEOUT_SECONDS}s, "
                            "skipped PR creation"
                        )
                        has_open_pr = True  # skip the create-PR branch below
                    if not has_open_pr:
                        try:
                            pr = await anyio.to_thread.run_sync(
                                lambda: subprocess.run(
                                    ["gh", "pr", "create", "--fill", "--head", branch,
                                     "--base", cfg["pr_base_branch"]],
                                    cwd=repo_path, capture_output=True, text=True,
                                    timeout=NETWORK_SUBPROCESS_TIMEOUT_SECONDS,
                                )
                            )
                        except subprocess.TimeoutExpired:
                            note = (
                                f"PR creation was attempted and timed out after "
                                f"{NETWORK_SUBPROCESS_TIMEOUT_SECONDS}s"
                            )
                        else:
                            if pr.returncode == 0:
                                pr_url = pr.stdout.strip()
                            else:
                                # Don't silently report success with
                                # pr_url=None indistinguishable from "PR
                                # creation wasn't requested" — the local
                                # implementation still succeeded and was
                                # pushed, so this stays success:true, but
                                # flag that PR creation was attempted and
                                # failed.
                                note = f"PR creation was attempted and failed: {pr.stderr.strip()}"
            else:
                note = "no origin remote configured; commit created locally, nothing pushed"

            return {
                "success": True,
                "pr_url": pr_url,
                "branch": branch,
                "files_changed": result.files_changed,
                "model_used": model,
                "summary": f"Implemented via {backend_name} ({model})",
                "output_tail": result.output_tail,
                "output_log": str(output_log_path),
                **({"note": note} if note else {}),
            }

        attempt_errors.append(f"{model}: {result.error}")
        last_output_tail = result.output_tail

    return {
        "success": False,
        "error": "all models failed — " + "; ".join(attempt_errors),
        "output_tail": last_output_tail,
        "output_log": str(output_log_path),
    }


def _configure_impl(
    backend: str | None = None,
    model: str | None = None,
    fallback_models: list[str] | None = None,
    max_fallback_models: int | None = None,
    stall_timeout_seconds: float | None = None,
    target_repo_path: str | None = None,
    branch_prefix: str | None = None,
    open_pr: bool | None = None,
    pr_base_branch: str | None = None,
    idle_notify_interval_seconds: float | None = None,
    extra_backend_args: list[str] | None = None,
) -> dict:
    all_overrides = {
        "backend": backend,
        "model": model,
        "fallback_models": fallback_models,
        "max_fallback_models": max_fallback_models,
        "stall_timeout_seconds": stall_timeout_seconds,
        "target_repo_path": target_repo_path,
        "branch_prefix": branch_prefix,
        "open_pr": open_pr,
        "pr_base_branch": pr_base_branch,
        "idle_notify_interval_seconds": idle_notify_interval_seconds,
        "extra_backend_args": extra_backend_args,
    }
    all_overrides = {k: v for k, v in all_overrides.items() if v is not None}

    try:
        return config_module.configure_with_validation(all_overrides)
    except config_module.ConfigValidationError as e:
        return {"success": False, "error": str(e)}
    except Exception as e:
        # configure_with_validation calls _validate_ollama_model, which
        # calls ollama_module.list_ollama_models() — that can raise
        # OllamaUnavailableError (not a ConfigValidationError) if Ollama is
        # down. Match the broad-except convention _list_available_models_impl
        # already uses so this returns the documented structured error
        # response instead of an uncaught exception through the MCP tool
        # call.
        return {"success": False, "error": str(e)}


def _list_available_models_impl() -> dict:
    try:
        models = config_module.list_available_models_with_prefix()
    except Exception as e:
        return {"error": str(e)}
    return {"models": models}


@mcp.tool()
async def delegate_implementation(
    task: str, branch: str, ctx: Context, target_repo_path: str | None = None,
) -> dict:
    return await _delegate_implementation_impl(task, branch, target_repo_path, ctx=ctx)


@mcp.tool()
def configure(
    backend: str | None = None,
    model: str | None = None,
    fallback_models: list[str] | None = None,
    max_fallback_models: int | None = None,
    stall_timeout_seconds: float | None = None,
    target_repo_path: str | None = None,
    branch_prefix: str | None = None,
    open_pr: bool | None = None,
    pr_base_branch: str | None = None,
    idle_notify_interval_seconds: float | None = None,
    extra_backend_args: list[str] | None = None,
) -> dict:
    return _configure_impl(
        backend, model, fallback_models, max_fallback_models,
        stall_timeout_seconds, target_repo_path, branch_prefix,
        open_pr, pr_base_branch, idle_notify_interval_seconds,
        extra_backend_args,
    )


@mcp.tool()
def list_available_models() -> dict:
    return _list_available_models_impl()


if __name__ == "__main__":
    mcp.run()
