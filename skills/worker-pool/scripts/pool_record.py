#!/usr/bin/env python3
"""Worker-pool completion record: who may hold one, and what one *is*.

The pool's delivery script writes these records and the gateway bridge reads
them, so the recipient grammar, the directory layout, the two stage names and
the "is this a record?" predicate live here rather than once per side. The
bridge is a standalone package that cannot import the skill; it bundles this
module verbatim (packages/ag2-sparrow/tools/sync_from_src.py).

Stdlib only, no workspace resolution: a caller passes the root it already
resolved.
"""
from __future__ import annotations

import enum
import errno
import os
import re
import stat
from pathlib import Path

# A recipient id is also a directory name under the workers root, so the
# grammar bounds it: lowercase, no dots, no separators, no leading dash.
RECIPIENT_PATTERN = r"^[a-z0-9][a-z0-9-]{0,31}$"
RECIPIENT = re.compile(RECIPIENT_PATTERN)

PENDING_STAGE = "pending"
DONE_STAGE = "flag"
# Pending FIRST. The writer creates `.flag` and only then unlinks `.pending`,
# so a flag-first probe can observe neither name across a promotion.
STAGES = (PENDING_STAGE, DONE_STAGE)


class RecordState(enum.Enum):
    """What is actually at a record path."""

    ABSENT = "absent"
    PRESENT = "present"
    MALFORMED = "malformed"


def is_recipient(name) -> bool:
    """True only for a name the writer would accept. Everything else under the
    workers root is proven not to be a recipient, not merely unreadable."""
    return isinstance(name, str) and RECIPIENT.match(name) is not None


def require_recipient(name: str) -> str:
    if not is_recipient(name):
        raise ValueError(f"recipient id must match {RECIPIENT_PATTERN!r}: {name!r}")
    return name


class RecipientAliasError(OSError):
    """A recipient-named entry that is a symlink. It aliases some other folder,
    so any record beneath it is that folder's claim wearing this name: neither
    the writer nor the reader may treat it as this recipient's own."""


def require_own_dir(path) -> Path:
    """`path` may be absent or a real directory; a symlink at it is refused.
    Shared by the writer (before publishing under a recipient) and the reader
    (while enumerating recipients), so both refuse the same state."""
    p = Path(path)
    if p.is_symlink():
        raise RecipientAliasError(errno.ELOOP, "recipient directory is a symlink alias", str(p))
    return p


def workers_root(state_dir) -> Path:
    """The one directory holding every recipient's record folder."""
    return Path(state_dir) / "workers"


def record_dir(root, recipient: str) -> Path:
    return Path(root) / require_recipient(recipient) / "done"


def record_path(root, recipient: str, task_id: str, stage: str = DONE_STAGE) -> Path:
    if stage not in STAGES:
        raise ValueError(f"stage must be one of {STAGES!r}: {stage!r}")
    return record_dir(root, recipient) / f"{task_id}.{stage}"


# Non-blocking: a blocking O_RDONLY open of a FIFO waits for a writer, so the
# classification below would never be reached for one.
_PROBE_FLAGS = (os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_NONBLOCK", 0))


def record_state(path) -> RecordState:
    """Open-based, never lstat: a record is a REGULAR file openable without
    following a symlink. The open cannot wait, so a FIFO or device at the name
    classifies as MALFORMED instead of stalling the caller. An unreadable one
    raises rather than reading as absent, so a caller can abstain instead of
    naming someone else."""
    fd = os.open(str(path), _PROBE_FLAGS)
    try:
        regular = stat.S_ISREG(os.fstat(fd).st_mode)
    finally:
        os.close(fd)
    return RecordState.PRESENT if regular else RecordState.MALFORMED


def read_record_state(path) -> RecordState:
    """`record_state` with the absent case folded in; other OSError still
    propagates, because unreadable is not the same answer as absent."""
    try:
        return record_state(path)
    except FileNotFoundError:
        return RecordState.ABSENT


def iter_recipients(root) -> list[str]:
    """Names under `root` that could be a recipient's own folder, sorted.

    Entries PROVEN not to be recipients — a name the writer would refuse, or
    an entry that is not a directory — are skipped, never treated as an
    unreadable claimant. A recipient-named SYMLINK raises RecipientAliasError
    (an OSError): it is malformed state a caller must abstain on. An
    unreadable root raises.
    """
    names = []
    with os.scandir(str(root)) as entries:
        for entry in entries:
            if not is_recipient(entry.name):
                continue
            try:
                # An alias is malformed state, not a stray: the writer follows
                # it, so a record under the target could be published under this name.
                if entry.is_symlink():
                    raise RecipientAliasError(errno.ELOOP, "recipient directory is a symlink alias",
                                              entry.path)
                if not entry.is_dir(follow_symlinks=False):
                    continue
            except RecipientAliasError:
                raise
            except OSError:
                pass  # cannot prove otherwise: leave it for the record probe
            names.append(entry.name)
    return sorted(names)
