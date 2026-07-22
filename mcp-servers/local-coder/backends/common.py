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

# The single authoritative list of backend names local-coder knows about.
# server.py's BACKENDS dict and config.py's configure_with_validation both
# reference this so the two can't drift out of sync.
KNOWN_BACKENDS = ("aider", "codex", "gemini", "openrouter")


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


def _parse_porcelain_z(status_z: str) -> dict[str, str]:
    """Parse `git status --porcelain -z` output into {path: status_code}.

    `-z` mode reports paths unquoted and NUL-terminated instead of the
    default mode's display-escaping (quoting paths with spaces, tabs,
    non-ASCII, etc.) — parsing the default mode's quoted text as a literal
    filesystem path silently fails to match such files at cleanup time.
    A rename/copy record (status code starting with R or C) is two
    NUL-terminated fields — new path, then old path — rather than one;
    only the new path is tracked here, since that's the one that exists
    on disk after the rename and is what cleanup needs to act on.
    """
    fields = status_z.split("\0")
    result: dict[str, str] = {}
    i = 0
    while i < len(fields):
        record = fields[i]
        if not record:
            i += 1
            continue
        code = record[:2]
        path = record[3:]
        result[path] = code
        if code[0] in ("R", "C"):
            # Rename/copy records carry the old path as a second field;
            # consume it without tracking it as a "changed" path.
            i += 1
        i += 1
    return result


def snapshot_working_tree(repo_path: str) -> tuple[str, set[str]]:
    pre_head = subprocess.run(
        ["git", "-C", repo_path, "rev-parse", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()

    status_z = subprocess.run(
        ["git", "-C", repo_path, "status", "--porcelain", "-z"],
        capture_output=True, text=True, check=True,
    ).stdout

    porcelain = set(_parse_porcelain_z(status_z).keys())
    return pre_head, porcelain


def restore_working_tree(repo_path: str, pre_head: str, pre_porcelain: set[str]) -> None:
    """Reset a working tree to its pre-attempt state after a FAILED backend
    attempt, so the next failover attempt doesn't inherit partial,
    uncommitted edits the failed attempt may have left on disk (e.g. a
    backend that writes files before committing, killed mid-way by a stall
    timeout).

    Only undoes changes attributable to THIS attempt: HEAD is reset back to
    `pre_head` (a no-op if the attempt never committed, which is the normal
    case for a failure), and only paths NOT already present in
    `pre_porcelain` are discarded — anything that was already
    modified/untracked before the attempt started is left alone rather than
    being blindly wiped by e.g. an unscoped `git reset --hard`.

    `pre_porcelain` is a set of bare paths (from `snapshot_working_tree`,
    which parses `-z` output), not raw porcelain lines — status codes are
    re-fetched fresh here rather than reused from the snapshot, since a
    path's status can change between snapshot time and restore time (e.g.
    a file that was untracked before the attempt could have been staged
    by the attempt itself).

    The `reset --mixed pre_head` below runs unconditionally, even if HEAD
    never moved (the normal case for a failure). This is required, not
    redundant: `git checkout -- <path>` restores a path's content from the
    INDEX, not from HEAD. If a failed attempt staged an edit to a tracked
    file without ever committing it (e.g. killed mid-stall right after
    `git add`, before `git commit`), the index still holds the corrupted
    content — `checkout --` would "restore" the working tree from that
    same corrupted staged content, doing nothing. `reset --mixed` clears
    the index back to `pre_head`'s tree first, so the subsequent
    `checkout --` has clean, pre-attempt content to restore from.
    """
    subprocess.run(
        ["git", "-C", repo_path, "reset", "--mixed", pre_head],
        check=True, capture_output=True,
    )

    status_z = subprocess.run(
        ["git", "-C", repo_path, "status", "--porcelain", "-z"],
        capture_output=True, text=True, check=True,
    ).stdout
    current_entries = _parse_porcelain_z(status_z)

    new_paths = {p: code for p, code in current_entries.items() if p not in pre_porcelain}
    if not new_paths:
        return

    # Untracked paths ("??") can't be passed to `git checkout --` (it only
    # accepts paths git already knows about) — mixing the two in one
    # command makes the WHOLE command fail on the untracked entries,
    # silently leaving tracked modifications uncleaned too. Split into two
    # separate, independently-run commands instead, so one path class's
    # cleanup can never mask the other's.
    tracked_modified = [p for p, code in new_paths.items() if code != "??"]
    untracked = [p for p, code in new_paths.items() if code == "??"]

    if tracked_modified:
        subprocess.run(
            ["git", "-C", repo_path, "checkout", "--", *tracked_modified],
            cwd=repo_path, capture_output=True,
        )
    if untracked:
        subprocess.run(
            ["git", "-C", repo_path, "clean", "-fd", "--", *untracked],
            cwd=repo_path, capture_output=True,
        )


_READ_CHUNK_SIZE = 4096

# Only the failure-tail of subprocess output is ever consumed downstream
# (aider.py truncates to the last 2000 chars for an error message), so
# accumulating the full transcript of a long-running, output-heavy backend
# run in memory is unbounded growth for no benefit. Keep a bounded tail
# instead — generous enough that no realistic downstream consumer's
# truncation window is ever starved of content.
_MAX_OUTPUT_CHARS = 20_000


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

    # Bounded tail buffer: append new text, then trim from the front
    # whenever it exceeds the cap, so memory stays flat regardless of how
    # much a long-running subprocess writes.
    output_tail = ""
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
                    output_tail += decoder.decode(data)
                    if len(output_tail) > _MAX_OUTPUT_CHARS:
                        output_tail = output_tail[-_MAX_OUTPUT_CHARS:]
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
                    output_tail += decoder.decode(remaining)
                    if len(output_tail) > _MAX_OUTPUT_CHARS:
                        output_tail = output_tail[-_MAX_OUTPUT_CHARS:]
                # Flush any trailing partial multi-byte sequence.
                output_tail += decoder.decode(b"", final=True)
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
        cmd, returncode, stdout=output_tail, stderr=""
    )
