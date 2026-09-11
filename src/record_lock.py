#!/usr/bin/env python3
"""One writer at a time for a shared record, via a sidecar lock file.

`os.replace` makes a publication atomic FOR THE READER: nobody sees a truncated
file. It does nothing for two WRITERS that each read the record, decide against
what they read, and then publish — both decisions were valid against the same
predecessor and the second publication erases the first. Serialising the whole
read-decide-write section is what makes such a decision still true when it is
acted on; a compare-and-swap on what was read is what makes a writer that loaded
before the section notice it is stale.

The lock is a sidecar (`<record>.lock`), never the record itself: locking the
record would mean holding a descriptor on the inode `os.replace` is about to
swap away, so the next writer would lock a file nobody else can see.
"""
from __future__ import annotations

import contextlib
import fcntl
from pathlib import Path

LOCK_SUFFIX = ".lock"


def lock_path(path) -> Path:
    """The sidecar guarding `path`. Exposed so a record's own listings can
    exclude it — it is a guard, not one of the records it protects."""
    p = Path(path)
    return p.with_name(p.name + LOCK_SUFFIX)


@contextlib.contextmanager
def record_lock(path):
    """Hold an exclusive lock on `<path>.lock` for the critical section.

    The sidecar is created once and never unlinked: deleting it would let the
    next writer lock a new inode while a current holder still owns the old one.
    """
    lock = lock_path(path)
    lock.parent.mkdir(parents=True, exist_ok=True)
    with open(lock, "a+") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)
