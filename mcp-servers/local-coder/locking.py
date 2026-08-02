"""Cross-platform file locking primitive — stdlib only.

Kept in its own module (no yaml / config / backend imports) so lock users
that must run in a minimal interpreter — e.g. the SessionEnd cleanup hook on
a host without the plugin's third-party deps installed — can import it (via
status.py) without pulling the full config import chain.
"""

import contextlib
import os
import time
from pathlib import Path

# How long to wait for a contended lock on Windows before giving up. Generous
# relative to the guarded critical sections, so this is only reached when
# something is genuinely wrong rather than merely busy.
_WINDOWS_LOCK_TIMEOUT_SECONDS = 60.0

# fcntl does not exist on Windows (importing it there crashes); msvcrt is the
# Windows stdlib equivalent. Both are wrapped behind file_lock() so callers
# are platform-agnostic.
_IS_WINDOWS = os.name == "nt"
if _IS_WINDOWS:
    import msvcrt as _lock_module
else:
    import fcntl as _lock_module


@contextlib.contextmanager
def file_lock(lock_path: Path):
    """Exclusive, cross-platform file lock held on a dedicated lockfile.

    Blocks until the lock is free (POSIX: indefinite; Windows: bounded retry
    so a permanent lock failure surfaces instead of hanging). Callers pass
    their own lockfile path so this primitive can guard any file.

    Cross-platform: fcntl.flock on POSIX, msvcrt.locking on Windows — no
    third-party dependency needed.
    """
    lock_path.touch(exist_ok=True)
    # Open r+ (never "w"): "w" TRUNCATES, so a second process opening the
    # lockfile would clobber it while the first holds a lock on it.
    with open(lock_path, "r+b") as lock_file:
        if _IS_WINDOWS:
            # Lock 1 byte at offset 0. Do NOT write before locking — on
            # Windows a write into a range another process has locked fails,
            # so writing first made contenders error out instead of waiting.
            # Windows locks a byte RANGE and the range need not contain data,
            # so locking offset 0 of an empty file is valid.
            #
            # LK_LOCK does not block indefinitely: it retries ~10 times at
            # 1-second intervals, then raises OSError. That contradicts this
            # function's "blocks until free" contract, so retry around it —
            # matching flock's indefinite wait on POSIX.
            # Bounded, so a PERMANENT failure (bad descriptor, permissions,
            # a filesystem that cannot lock) surfaces as an error instead of
            # hanging forever.
            deadline = time.monotonic() + _WINDOWS_LOCK_TIMEOUT_SECONDS
            while True:
                try:
                    lock_file.seek(0)
                    _lock_module.locking(lock_file.fileno(), _lock_module.LK_LOCK, 1)
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise
                    time.sleep(0.1)
            try:
                yield
            finally:
                lock_file.seek(0)
                _lock_module.locking(lock_file.fileno(), _lock_module.LK_UNLCK, 1)
        else:
            _lock_module.flock(lock_file, _lock_module.LOCK_EX)
            try:
                yield
            finally:
                _lock_module.flock(lock_file, _lock_module.LOCK_UN)
