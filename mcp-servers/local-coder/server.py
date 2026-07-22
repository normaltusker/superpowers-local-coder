import subprocess
import sys
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
# stderr. Fixed path (not per-run) so a human always knows where to look
# without having to first ask the running call for a path.
OUTPUT_LOG_PATH = Path(__file__).parent / "local-coder-output.log"

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
    # Truncate at the start of every call, not just append forever — this
    # is a fixed path reused across the server's whole lifetime, so
    # without truncation a human tailing it during a new call would see
    # stale output mixed in from a prior, unrelated attempt.
    OUTPUT_LOG_PATH.write_text("")

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

    attempt_models = [cfg["model"], *cfg.get("fallback_models", [])]
    attempt_errors = []
    last_output_tail = ""

    def make_on_tick(model_name: str):
        def on_tick():
            print(f"[local-coder] still running ({model_name})...", file=sys.stderr, flush=True)
            if ctx is not None:
                anyio.from_thread.run(
                    ctx.report_progress, 0, None, f"Running {model_name}..."
                )
        return on_tick

    def make_on_output(model_name: str):
        # Streams the backend subprocess's actual output (e.g. aider's own
        # live progress/diff/commit messages) as it arrives, rather than
        # only the bounded tail surfaced in an error message after the
        # whole call finishes. Without this, a long-running delegated
        # attempt is effectively headless — nothing about what the backend
        # is actually doing is visible until it's already done (or already
        # failed). Written to BOTH stderr (in case some setup does capture
        # it) and OUTPUT_LOG_PATH (the guaranteed-visible path — see that
        # constant's comment for why a log file, not just stderr, is
        # needed).
        def on_output(chunk: str):
            line = f"[local-coder:{model_name}] {chunk}"
            print(line, end="", file=sys.stderr, flush=True)
            with open(OUTPUT_LOG_PATH, "a") as f:
                f.write(chunk)
        return on_output

    for model in attempt_models:
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
                    }
                if push.returncode != 0:
                    return {
                        "success": False,
                        "error": f"git push failed: {push.stderr.strip()}",
                        "files_changed": result.files_changed,
                        "commit_sha": result.commit_sha,
                        "model_used": model,
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
                **({"note": note} if note else {}),
            }

        attempt_errors.append(f"{model}: {result.error}")
        last_output_tail = result.output_tail

    return {
        "success": False,
        "error": "all models failed — " + "; ".join(attempt_errors),
        "output_tail": last_output_tail,
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
