"""Cross-platform advisory file-lock primitives for shared runtime state."""

from __future__ import annotations

import os
from contextlib import contextmanager
from pathlib import Path

try:
    import fcntl
except ModuleNotFoundError:
    fcntl = None
    import msvcrt


_WIN_LOCK_OFFSET = 1 << 20


def lock_fd(fd: int, *, blocking: bool = True) -> None:
    """Lock one stable byte range (Windows) or the whole file (POSIX)."""
    if fcntl is not None:
        flags = fcntl.LOCK_EX
        if not blocking:
            flags |= fcntl.LOCK_NB
        fcntl.flock(fd, flags)
        return
    os.lseek(fd, _WIN_LOCK_OFFSET, os.SEEK_SET)
    mode = msvcrt.LK_LOCK if blocking else msvcrt.LK_NBLCK
    msvcrt.locking(fd, mode, 1)


def unlock_fd(fd: int) -> None:
    if fcntl is not None:
        fcntl.flock(fd, fcntl.LOCK_UN)
        return
    os.lseek(fd, _WIN_LOCK_OFFSET, os.SEEK_SET)
    msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)


@contextmanager
def locked_file(path: Path, *, create_mode: int = 0o644):
    """Hold a blocking exclusive lock for the duration of a transaction."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(path), os.O_CREAT | os.O_RDWR, create_mode)
    try:
        lock_fd(fd)
        yield fd
    finally:
        try:
            unlock_fd(fd)
        finally:
            os.close(fd)
