import subprocess
from typing import Callable

from backends.base import BackendAdapter, CompletionResult
from backends import common


class AiderBackend(BackendAdapter):
    self_commits = True

    def run_backend(
        self,
        task: str,
        repo_path: str,
        branch: str,
        config: dict,
        model: str | None = None,
        on_tick: Callable[[], None] | None = None,
        on_output: Callable[[str], None] | None = None,
    ) -> CompletionResult:
        model = model or config.get("model")
        if not model:
            return CompletionResult(success=False, error="no model specified")

        common.ensure_branch(repo_path, branch)
        pre_head, pre_porcelain = common.snapshot_working_tree(repo_path)

        cmd = [
            "aider", "--model", model, "--yes", "--message", task,
            *config.get("extra_backend_args", []),
        ]

        try:
            result = common.run_monitored_subprocess(
                cmd,
                cwd=repo_path,
                stall_timeout_seconds=config["stall_timeout_seconds"],
                idle_notify_interval_seconds=config["idle_notify_interval_seconds"],
                on_tick=on_tick,
                on_output=on_output,
            )
        except common.StallError as e:
            # aider writes edited files to disk before committing them (two
            # separate, non-atomic steps) — a stall-kill can land between
            # those steps and leave partial, uncommitted writes on disk.
            # Clean those up now so the next failover attempt starts from
            # the same state this attempt did.
            common.restore_working_tree(repo_path, pre_head, pre_porcelain)
            return CompletionResult(success=False, error=str(e))

        if result.returncode != 0:
            common.restore_working_tree(repo_path, pre_head, pre_porcelain)
            return CompletionResult(
                success=False,
                error=result.stdout.strip()[-2000:] or "aider exited non-zero",
            )

        post_head = subprocess.run(
            ["git", "-C", repo_path, "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()

        if post_head == pre_head:
            # aider exited cleanly (returncode 0) without committing — this
            # can still happen with dirty, uncommitted writes on disk (e.g.
            # aider wrote files but a lint/commit step declined or failed
            # silently), so clean up here too.
            common.restore_working_tree(repo_path, pre_head, pre_porcelain)
            return CompletionResult(success=False, error="aider made no commits")

        files_changed = subprocess.run(
            ["git", "-C", repo_path, "diff", "--name-only", pre_head, post_head],
            capture_output=True, text=True, check=True,
        ).stdout.splitlines()

        return CompletionResult(
            success=True,
            files_changed=files_changed,
            commit_sha=post_head,
        )
