#!/usr/bin/env python3
"""Who, if anyone, the router already handed a task to.

The router delegates by writing a SENTINEL into
``deliveries/<recipient>/<task-id>{.txt,.accepted,.claimed}``; the payload
itself never leaves ``tasks/``. Two sessions read that state and ask different
questions, and only the first one lives here:

  - the core asks "is this still mine to report?" — no, once ANY worker holds a
    sentinel for it, even an unaccepted one: the router already made it that
    worker's, so the core is not its recipient.
  - a worker asks "do I still owe a reply?" — answered from its OWN deliveries
    folder, never from the core's queue, and deliberately not by this module.

``src/check-pending-tasks.sh`` answers the first question in shell because a
Stop hook must run without an interpreter. This module is the Python answer to
the same question, and ``tests/worker-delivery-matches-the-hook.test.py`` pins
the two to the same suffix set so they cannot drift apart silently.
"""
from __future__ import annotations

import os
import stat as _stat
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from pool_delivery import ACCEPTED_SUFFIX, LEGACY_ACCEPTED_SUFFIX, PENDING_SUFFIX

#: The router's stages, spelled once in pool_delivery; a new stage is added there.
SENTINEL_SUFFIXES = (PENDING_SUFFIX, ACCEPTED_SUFFIX, LEGACY_ACCEPTED_SUFFIX)


def _is_dir(path: Path) -> bool:
    """Path.is_dir() answers False for a stat that FAILED, which would silently
    skip a recipient we merely cannot read. Only ENOENT is absence."""
    try:
        return _stat.S_ISDIR(os.stat(path).st_mode)
    except FileNotFoundError:
        return False


def _sentinel_present(path: Path) -> bool:
    """lstat, not exists(): exists() reports EACCES as False, and a dangling
    symlink still names a delegation, so absence must mean ENOENT and nothing else."""
    try:
        os.lstat(path)
    except FileNotFoundError:
        return False
    return True


def holder_of(workspace: Path, task_id: str) -> str | None:
    """The recipient id holding ``task_id``, or None when nobody does.

    An unreadable deliveries/ is NOT "nobody holds it" — that is the reading
    that hands a worker's task to the core — so only a genuinely absent
    directory or sentinel answers None; every other OSError propagates, at each
    of the three places one can arise: listing deliveries/, stat-ing a recipient,
    and stat-ing a sentinel.
    """
    deliveries = Path(workspace) / "deliveries"
    try:
        recipients = sorted(p for p in deliveries.iterdir() if _is_dir(p))
    except FileNotFoundError:
        return None
    for recipient in recipients:
        for suffix in SENTINEL_SUFFIXES:
            if _sentinel_present(recipient / f"{task_id}{suffix}"):
                return recipient.name
    return None
