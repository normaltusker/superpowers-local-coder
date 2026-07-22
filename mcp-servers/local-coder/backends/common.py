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
    NUL-terminated fields — new path, then old path — rather than one.
    BOTH are included in the result: the new path under its real status
    code (e.g. "R "), and the old path under a synthetic "D " (deleted)
    code. This matters for restore_working_tree's cleanup — a rename
    marks the OLD path as deleted in the index; restoring only the new
    path back to pre_head content leaves the old path still missing.
    Both halves of the rename need to be restored for the working tree to
    genuinely return to its pre-attempt state.
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
            # Rename/copy records carry the old path as a second field.
            # For a rename (R), the old path is now genuinely absent from
            # the working tree, so it needs restoring too — treat it as
            # a synthetic deletion. For a copy (C), the old path is
            # untouched (the copy created a NEW path, the original still
            # exists as it was), so no entry is needed for it.
            i += 1
            if i < len(fields) and code[0] == "R":
                old_path = fields[i]
                result[old_path] = "D "
        i += 1
    return result


def _git_status_z(repo_path: str) -> str:
    # --untracked-files=all reports every file inside an untracked
    # directory individually, rather than collapsing the whole directory
    # into one "?? dirname/" entry. Without this, a directory that was
    # already untracked before an attempt started (so it's in
    # pre_porcelain as a single collapsed entry) hides any NEW file the
    # attempt adds inside that same directory — both the pre- and
    # post-attempt snapshots report the identical single directory entry,
    # so the set-difference this function relies on never sees the new
    # file at all.
    # --ignored surfaces "!!" entries too. Without it, a failed attempt's
    # newly created ignored files (generated build output, .pyc, etc.) are
    # invisible to both the pre- and post-attempt snapshot -- git omits
    # ignored paths from status entirely by default -- so they silently
    # survive cleanup and leak into whatever fallback model runs next.
    return subprocess.run(
        [
            "git", "-C", repo_path, "status", "--porcelain", "-z",
            "--untracked-files=all", "--ignored",
        ],
        capture_output=True, text=True, check=True,
    ).stdout


# Forces git to treat every pathspec argument as a literal path rather than
# parsing pathspec "magic" syntax (e.g. a filename that happens to start
# with `:(glob)` or `:(exclude)`). Without this, a backend-created file
# whose name is itself valid pathspec magic can make `git clean`/`checkout`
# interpret that filename as a glob pattern instead of a literal path —
# verified empirically: a file named `:(glob)victim*` passed to
# `git clean -fd --` deleted an unrelated pre-existing `victim.txt`, not
# the maliciously-named file itself. Applied via env var (not a CLI flag —
# older git versions don't support `--literal-pathspecs`) to every
# subprocess call in this function that takes attempt-supplied paths.
_LITERAL_PATHSPECS_ENV = {"GIT_LITERAL_PATHSPECS": "1"}


def snapshot_working_tree(repo_path: str) -> tuple[str, set[str]]:
    # Normalized to absolute up front so a caller-supplied relative
    # repo_path can't later collide with restore_working_tree's cwd=
    # usage of the same string (see restore_working_tree's docstring).
    repo_path = os.path.abspath(repo_path)
    pre_head = subprocess.run(
        ["git", "-C", repo_path, "rev-parse", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()

    porcelain = set(_parse_porcelain_z(_git_status_z(repo_path)).keys())
    return pre_head, porcelain


def restore_working_tree(repo_path: str, pre_head: str, pre_porcelain: set[str]) -> None:
    """Reset a working tree to its pre-attempt state after a FAILED backend
    attempt, so the next failover attempt doesn't inherit partial,
    uncommitted edits the failed attempt may have left on disk (e.g. a
    backend that writes files before committing, killed mid-way by a stall
    timeout).

    Only undoes changes attributable to THIS attempt — anything already
    present in `pre_porcelain` (modified, staged, or untracked before the
    attempt started) is left completely alone, including its staged
    state. This is intentional and has a known, accepted edge: a path
    that was ALREADY dirty before the attempt started, which the attempt
    then modifies FURTHER, is not cleaned — the attempt's changes on top
    of the user's pre-existing edit survive. Distinguishing "the user's
    prior edit" from "the attempt's edit on the same path" would require
    a per-path content snapshot (e.g. blob hashes), not just a path set;
    this function only tracks which paths were dirty, not their content,
    so it cannot make that distinction. This is a pre-existing limitation
    of every version of this function, not something this design changed
    — the earlier blanket-reset version had the same gap, plus it also
    destroyed unrelated pre-existing staged work in the process (see
    below), which this version no longer does.

    This function does NOT run a blanket `git reset --mixed`: an
    earlier version did, to handle the case of a failed attempt leaving
    staged-but-uncommitted corruption, but a blanket reset unstages
    EVERY staged path in the index, not just the ones this attempt
    touched — silently discarding work the user had already staged
    before delegation even started. Instead, each new/changed path this
    attempt is responsible for is individually restored to its `pre_head`
    content via `git restore --source=pre_head --staged --worktree`,
    which resets only that path's index+worktree entry, leaving every
    other path's index state untouched.

    `pre_porcelain` is a set of bare paths (from `snapshot_working_tree`,
    which parses `-z --untracked-files=all --ignored` output), not raw
    porcelain lines — status codes are re-fetched fresh here rather than
    reused from the snapshot, since a path's status can change between
    snapshot time and restore time (e.g. a file that was untracked before
    the attempt could have been staged by the attempt itself). `--ignored`
    is required alongside `--untracked-files=all`: git omits "!!"
    (gitignore-matched) paths from status entirely otherwise, so an
    attempt's newly generated ignored files (build output, .pyc, etc.)
    would be invisible to the pre/post diff and survive into the next
    failover attempt. Ignored paths are cleaned via `git clean -fdx`
    (the `-x` is what makes clean remove ignored, not just untracked,
    paths) rather than `git restore`, since they were never tracked and
    have no `pre_head` content to restore from.

    `repo_path` is normalized to an absolute path at the top of both this
    function and `snapshot_working_tree`. The calls below combine
    `-C repo_path` (resolved against the process's cwd at call time) with
    `cwd=repo_path` (which changes that cwd first) — a relative
    `repo_path` would otherwise be resolved twice, collapsing to
    `<repo_path>/<repo_path>`, which doesn't exist. Those calls don't set
    `check=True`, so that failure was silent and cleanup became a total
    no-op for any relative `target_repo_path`.

    If the attempt DID commit (e.g. aider's own auto-commit) before later
    failing, HEAD itself has moved and must be moved back — but with
    `--soft`, which only repoints the branch and leaves the index and
    worktree exactly as they are. This is deliberately different from
    `--mixed`: a `--mixed` reset here would touch the index the same way
    the removed blanket reset did. `--soft` just makes the commit's
    changes show up as staged (as if `git add` had been run against
    pre_head's tree) — the per-path `restore`/`clean` calls below then
    naturally discard them like anything else new, without a separate
    code path.
    """
    # Normalized to absolute up front: the calls below combine "-C
    # repo_path" (an argv flag resolved against the process's current
    # cwd) with cwd=repo_path (which changes that cwd to repo_path
    # first). A relative repo_path then gets resolved TWICE -- once by
    # cwd=, then again by -C relative to the new cwd -- collapsing to
    # "<repo_path>/<repo_path>", which doesn't exist. Those calls also
    # don't set check=True, so the failure was previously silent and
    # cleanup became a total no-op. Normalizing here keeps -C and cwd=
    # pointing at the same real directory regardless of what the caller
    # passed in.
    repo_path = os.path.abspath(repo_path)
    current_head = subprocess.run(
        ["git", "-C", repo_path, "rev-parse", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    if current_head != pre_head:
        subprocess.run(
            ["git", "-C", repo_path, "reset", "--soft", pre_head],
            check=True, capture_output=True,
        )

    current_entries = _parse_porcelain_z(_git_status_z(repo_path))

    new_paths = {p: code for p, code in current_entries.items() if p not in pre_porcelain}
    if not new_paths:
        return

    # A tracked-in-HEAD path (anything git already knows about, i.e. not
    # "??" or "!!") is restored to its pre_head content in both the index
    # and the worktree — this is what correctly discards staged-but-
    # uncommitted corruption without touching any OTHER path's staged
    # state. Untracked ("??") and ignored ("!!") paths have no HEAD
    # content to restore from — those go through `git clean` instead,
    # which is the only tool that can remove a path git has no history
    # for. `-x` is required alongside `-fd` for the ignored case: plain
    # `git clean -fd` only removes untracked paths, silently skipping
    # anything gitignore-matched.
    tracked_modified = [p for p, code in new_paths.items() if code not in ("??", "!!")]
    untracked_or_ignored = [p for p, code in new_paths.items() if code in ("??", "!!")]

    if tracked_modified:
        subprocess.run(
            [
                "git", "-C", repo_path, "restore",
                f"--source={pre_head}", "--staged", "--worktree", "--",
                *tracked_modified,
            ],
            cwd=repo_path, capture_output=True,
            env={**os.environ, **_LITERAL_PATHSPECS_ENV},
        )
    if untracked_or_ignored:
        subprocess.run(
            ["git", "-C", repo_path, "clean", "-fdx", "--", *untracked_or_ignored],
            cwd=repo_path, capture_output=True,
            env={**os.environ, **_LITERAL_PATHSPECS_ENV},
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
    on_output: Callable[[str], None] | None = None,
) -> subprocess.CompletedProcess:
    # Run with an unbuffered binary pipe (not text=True) so we can read
    # whatever bytes are actually available via a non-blocking os.read()
    # rather than being forced through readline(), which blocks until a
    # newline or EOF arrives. A subprocess that writes a partial line (no
    # trailing newline) and then goes quiet without closing its pipe would
    # otherwise block readline() past the next poll interval, bypassing the
    # tick/stall checks for that period.
    # stdin=DEVNULL severs the child from this process's own stdin. Without
    # it, Popen defaults to inheriting the parent's stdin — for this MCP
    # server, that's the stdio JSON-RPC pipe from Claude Code: an open
    # pipe that receives data but never sends EOF. A backend subprocess
    # that tries to read stdin for any reason (confirmed in practice: a
    # real aider run hung with its main thread parked in a stdin read
    # syscall, having consumed only ~5s of CPU across 4+ minutes of
    # wall-clock time) then blocks forever waiting for a byte that can
    # never arrive — and the stall-timeout mechanism below does NOT catch
    # this, since it only watches for OUTPUT activity; a process blocked
    # reading stdin can still look "recently active" from earlier startup
    # output, so the stall timer never restarts and never fires. With
    # stdin explicitly closed, any read attempt gets immediate EOF instead.
    process = subprocess.Popen(
        cmd, cwd=cwd, stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
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
                    decoded = decoder.decode(data)
                    output_tail += decoded
                    if len(output_tail) > _MAX_OUTPUT_CHARS:
                        output_tail = output_tail[-_MAX_OUTPUT_CHARS:]
                    last_activity = time.monotonic()
                    if on_output is not None and decoded:
                        on_output(decoded)

            if process.poll() is not None and not data:
                # Drain any remaining buffered output before exiting.
                while True:
                    try:
                        remaining = os.read(fd, _READ_CHUNK_SIZE)
                    except OSError:
                        remaining = b""
                    if not remaining:
                        break
                    decoded = decoder.decode(remaining)
                    output_tail += decoded
                    if len(output_tail) > _MAX_OUTPUT_CHARS:
                        output_tail = output_tail[-_MAX_OUTPUT_CHARS:]
                    if on_output is not None and decoded:
                        on_output(decoded)
                # Flush any trailing partial multi-byte sequence.
                final_decoded = decoder.decode(b"", final=True)
                output_tail += final_decoded
                if on_output is not None and final_decoded:
                    on_output(final_decoded)
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
