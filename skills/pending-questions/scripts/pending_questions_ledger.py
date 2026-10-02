"""Locked, atomic file mutation for the pending-questions stores: one mkdir lock shared
by every writer of a path, a read-transform-replace under it, and a temp-file + rename
that preserves the file's mode. A lock is removed only by the writer that took it; a
held lock makes the write give up after LOCK_WAIT_SEC, untouched, with the manual
remedy in the error.

The skill's own: pending_questions_store.RoomDbStore (`under_lock` around status
transitions) and the transitional pending_questions_compat (`update`, the moved mark).
"""
from __future__ import annotations

import os
import tempfile
import time
from pathlib import Path
from typing import Callable, Optional

LOCK_WAIT_SEC = 10
NEW_FILE_MODE = 0o644


class LedgerError(Exception):
    """A transform declining to write (entry not found, token not unique)."""


def lock_path(pq: Path) -> Path:
    return Path(str(pq) + ".lock")


def _acquire(lock: Path) -> Optional[str]:
    deadline = time.monotonic() + LOCK_WAIT_SEC
    while True:
        try:
            lock.mkdir()
            return None
        except FileExistsError:
            # Never reclaimed: rmdir names a path, so a stat-then-rmdir can delete
            # a lock a new writer just took. A wedged lock is removed by hand.
            if time.monotonic() >= deadline:
                return (f"could not acquire {lock} within {LOCK_WAIT_SEC}s; the file is "
                        f"untouched. If no writer is running the lock is stale: rmdir '{lock}'")
            time.sleep(0.05)


def under_lock(lock: Path, fn: Callable[[], object]):
    """(error, result): `fn()` run while holding the mkdir lock `lock`; a held
    lock gives up after LOCK_WAIT_SEC with the error and does not run `fn`."""
    lock.parent.mkdir(parents=True, exist_ok=True)
    err = _acquire(lock)
    if err:
        return err, None
    try:
        return None, fn()
    finally:
        try:
            lock.rmdir()
        except OSError:
            pass


def replace_file(pq: Path, text: str) -> None:
    """Appear whole at `pq` in one rename; a crash mid-write leaves the old file."""
    try:
        mode = pq.stat().st_mode & 0o777
    except FileNotFoundError:
        mode = NEW_FILE_MODE
    fd, tmp = tempfile.mkstemp(dir=str(pq.parent), prefix=f".{pq.name}.", suffix=".tmp")
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp, pq)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def update(pq: Path, transform: Callable[[str], str]) -> Optional[str]:
    """Read, `transform`, replace — under the lock. Returns an error string (lock
    timeout, or the message of a LedgerError the transform raised) or None."""
    pq.parent.mkdir(parents=True, exist_ok=True)
    lock = lock_path(pq)
    err = _acquire(lock)
    if err:
        return err
    try:
        old = pq.read_text(encoding="utf-8") if pq.exists() else ""
        try:
            new = transform(old)
        except LedgerError as e:
            return str(e)
        if new != old:
            replace_file(pq, new)
        return None
    finally:
        try:
            lock.rmdir()
        except OSError:
            pass
