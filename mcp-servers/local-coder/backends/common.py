import re
import selectors
import subprocess
import time
from typing import Callable

# Branch names must start with an alphanumeric character and may only
# contain alphanumerics, `.`, `_`, `/`, and `-` after that. This rejects
# names starting with `-` (e.g. `--orphan`, `-x`), which git would
# otherwise interpret as a flag rather than a ref name when passed as a
# bare positional argument.
_VALID_BRANCH_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]*$")


class StallError(Exception):
    def __init__(self, stall_timeout_seconds: float):
        self.stall_timeout_seconds = stall_timeout_seconds
        super().__init__(f"stalled: no output for {stall_timeout_seconds}s")


def validate_branch_name(branch: str) -> None:
    if not branch or not _VALID_BRANCH_NAME.match(branch):
        raise ValueError(
            f"Invalid branch name: {branch!r}. Branch names must start "
            "with an alphanumeric character and contain only letters, "
            "digits, '.', '_', '/', or '-'."
        )


def ensure_branch(repo_path: str, branch: str) -> None:
    validate_branch_name(branch)
    verify = subprocess.run(
        ["git", "-C", repo_path, "rev-parse", "--verify", branch],
        capture_output=True,
    )
    if verify.returncode == 0:
        subprocess.run(
            ["git", "-C", repo_path, "checkout", branch],
            check=True, capture_output=True,
        )
    else:
        subprocess.run(
            ["git", "-C", repo_path, "checkout", "-b", branch],
            check=True, capture_output=True,
        )


def snapshot_working_tree(repo_path: str) -> tuple[str, set[str]]:
    pre_head = subprocess.run(
        ["git", "-C", repo_path, "rev-parse", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()

    status = subprocess.run(
        ["git", "-C", repo_path, "status", "--porcelain"],
        capture_output=True, text=True, check=True,
    ).stdout

    porcelain = {line for line in status.splitlines() if line.strip()}
    return pre_head, porcelain


def run_monitored_subprocess(
    cmd: list[str],
    cwd: str,
    stall_timeout_seconds: float,
    idle_notify_interval_seconds: float,
    on_tick: Callable[[], None] | None = None,
) -> subprocess.CompletedProcess:
    process = subprocess.Popen(
        cmd, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )

    output_lines: list[str] = []
    last_activity = time.monotonic()
    last_tick = time.monotonic()

    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ)

    try:
        while True:
            # Poll for output without blocking indefinitely, so the loop
            # keeps evaluating tick/stall timing even when the subprocess
            # produces no output at all (e.g. `sleep`).
            poll_timeout = min(idle_notify_interval_seconds, 0.5)
            ready = selector.select(timeout=poll_timeout)

            line = ""
            if ready:
                line = process.stdout.readline()
                if line:
                    output_lines.append(line)
                    last_activity = time.monotonic()

            if process.poll() is not None and not line:
                # Drain any remaining buffered output before exiting.
                remaining = process.stdout.read()
                if remaining:
                    output_lines.append(remaining)
                break

            now = time.monotonic()
            if now - last_tick >= idle_notify_interval_seconds:
                if on_tick is not None:
                    on_tick()
                last_tick = now

            if now - last_activity > stall_timeout_seconds:
                process.kill()
                process.wait()
                raise StallError(stall_timeout_seconds)
    finally:
        selector.close()
        if process.poll() is None:
            # An unexpected exception (e.g. from on_tick, or from
            # selector.select()/readline()) left the subprocess running.
            # Don't leak it — kill and reap it here.
            process.kill()
            process.wait()

    returncode = process.wait()
    return subprocess.CompletedProcess(
        cmd, returncode, stdout="".join(output_lines), stderr=""
    )
