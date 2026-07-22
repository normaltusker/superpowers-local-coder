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
    ) -> CompletionResult:
        common.ensure_branch(repo_path, branch)
        pre_head, _ = common.snapshot_working_tree(repo_path)

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
            )
        except common.StallError as e:
            return CompletionResult(success=False, error=str(e))

        if result.returncode != 0:
            return CompletionResult(
                success=False,
                error=result.stdout.strip()[-2000:] or "aider exited non-zero",
            )

        post_head = subprocess.run(
            ["git", "-C", repo_path, "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()

        if post_head == pre_head:
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
