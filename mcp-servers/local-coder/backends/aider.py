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
        on_start: Callable[[int], None] | None = None,
    ) -> CompletionResult:
        model = model or config.get("model")
        if not model:
            return CompletionResult(success=False, error="no model specified")

        common.ensure_branch(repo_path, branch)
        pre_head, pre_porcelain = common.snapshot_working_tree(repo_path)

        # Refuse to run when the tree has pre-existing STAGED changes. aider
        # auto-commits whatever is already in the index into its own commit,
        # and this adapter treats any HEAD movement as task success — so
        # pre-existing staged work would be committed and (with open_pr)
        # pushed as if it were the delegated task. Untracked/unstaged changes
        # are fine: aider does not auto-commit those, and restore_working_tree
        # discards only the paths THIS attempt creates, leaving pre-existing
        # dirty state intact (see restore_working_tree). Only the staged case
        # is the false-success hole, so only that is rejected here.
        staged = common.staged_paths(repo_path)
        if staged:
            return CompletionResult(
                success=False,
                error=(
                    "target repo has staged (indexed) changes before "
                    "delegation: " + ", ".join(sorted(staged)[:10])
                    + (" …" if len(staged) > 10 else "")
                    + ". aider would auto-commit them into its own commit and "
                    "report them as the delegated work. Commit or unstage them "
                    "first."
                ),
            )

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
                first_output_timeout_seconds=config.get("first_output_timeout_seconds"),
                on_tick=on_tick,
                on_output=on_output,
                on_start=on_start,
            )
        except common.StallError as e:
            # aider writes edited files to disk before committing them (two
            # separate, non-atomic steps) — a stall-kill can land between
            # those steps and leave partial, uncommitted writes on disk.
            # Clean those up now so the next failover attempt starts from
            # the same state this attempt did.
            common.restore_working_tree(repo_path, pre_head, pre_porcelain)
            return CompletionResult(success=False, error=str(e), output_tail=e.output_tail)
        except Exception:
            # Any other monitor/subprocess failure (OSError, etc.) can also
            # leave partial on-disk edits from aider. Restore before letting
            # the exception propagate, so a fallback attempt doesn't start on
            # a contaminated worktree. Re-raise: the caller's failover loop
            # decides whether to try the next model.
            common.restore_working_tree(repo_path, pre_head, pre_porcelain)
            raise

        if result.returncode != 0:
            common.restore_working_tree(repo_path, pre_head, pre_porcelain)
            return CompletionResult(
                success=False,
                error=result.stdout.strip()[-2000:] or "aider exited non-zero",
                output_tail=result.stdout,
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
            return CompletionResult(
                success=False,
                error="aider made no commits",
                output_tail=result.stdout,
            )

        files_changed = subprocess.run(
            ["git", "-C", repo_path, "diff", "--name-only", pre_head, post_head],
            capture_output=True, text=True, check=True,
        ).stdout.splitlines()

        return CompletionResult(
            success=True,
            files_changed=files_changed,
            commit_sha=post_head,
            output_tail=result.stdout,
        )
