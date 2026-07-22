import codecs
import os
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
    # Check specifically whether `branch` is an existing local branch (a ref
    # under refs/heads/), not just any resolvable revision. A bare
    # `git rev-parse --verify branch` also succeeds for tags, commit SHAs,
    # and other revision-like inputs — checking out one of those instead of
    # creating a branch would leave the repo in detached HEAD.
    verify = subprocess.run(
        ["git", "-C", repo_path, "rev-parse", "--verify", f"refs/heads/{branch}"],
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


_READ_CHUNK_SIZE = 4096


def run_monitored_subprocess(
    cmd: list[str],
    cwd: str,
    stall_timeout_seconds: float,
    idle_notify_interval_seconds: float,
    on_tick: Callable[[], None] | None = None,
) -> subprocess.CompletedProcess:
    # Run with an unbuffered binary pipe (not text=True) so we can read
    # whatever bytes are actually available via a non-blocking os.read()
    # rather than being forced through readline(), which blocks until a
    # newline or EOF arrives. A subprocess that writes a partial line (no
    # trailing newline) and then goes quiet without closing its pipe would
    # otherwise block readline() past the next poll interval, bypassing the
    # tick/stall checks for that period.
    process = subprocess.Popen(
        cmd, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    )

    output_chunks: list[str] = []
    last_activity = time.monotonic()
    last_tick = time.monotonic()

    # Incremental UTF-8 decoder so multi-byte characters split across two
    # reads aren't corrupted — partial bytes are buffered internally by the
    # decoder until a full character is available.
    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")

    fd = process.stdout.fileno()
    selector = selectors.DefaultSelector()
    selector.register(fd, selectors.EVENT_READ)

    try:
        while True:
            # Poll for output without blocking indefinitely, so the loop
            # keeps evaluating tick/stall timing even when the subprocess
            # produces no output at all (e.g. `sleep`).
            poll_timeout = min(idle_notify_interval_seconds, 0.5)
            ready = selector.select(timeout=poll_timeout)

            data = b""
            if ready:
                try:
                    data = os.read(fd, _READ_CHUNK_SIZE)
                except OSError:
                    data = b""
                if data:
                    output_chunks.append(decoder.decode(data))
                    last_activity = time.monotonic()

            if process.poll() is not None and not data:
                # Drain any remaining buffered output before exiting.
                while True:
                    try:
                        remaining = os.read(fd, _READ_CHUNK_SIZE)
                    except OSError:
                        remaining = b""
                    if not remaining:
                        break
                    output_chunks.append(decoder.decode(remaining))
                # Flush any trailing partial multi-byte sequence.
                output_chunks.append(decoder.decode(b"", final=True))
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
            # selector.select()/os.read()) left the subprocess running.
            # Don't leak it — kill and reap it here.
            process.kill()
            process.wait()
        process.stdout.close()

    returncode = process.wait()
    return subprocess.CompletedProcess(
        cmd, returncode, stdout="".join(output_chunks), stderr=""
    )
