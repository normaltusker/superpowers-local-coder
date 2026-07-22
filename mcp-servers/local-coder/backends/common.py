import selectors
import subprocess
import time
from typing import Callable


class StallError(Exception):
    def __init__(self, stall_timeout_seconds: float):
        self.stall_timeout_seconds = stall_timeout_seconds
        super().__init__(f"stalled: no output for {stall_timeout_seconds}s")


def ensure_branch(repo_path: str, branch: str) -> None:
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

    returncode = process.wait()
    return subprocess.CompletedProcess(
        cmd, returncode, stdout="".join(output_lines), stderr=""
    )
