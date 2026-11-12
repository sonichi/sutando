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

from pool_delivery import ACCEPTED_SUFFIX, LEGACY_ACCEPTED_SUFFIX, PENDING_SUFFIX, NON_EXCLUSIVE_MARKER, regular_file_state
import pool_claim_policy
import pool_record
import pool_roster

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


def holder_evidence(workspace: Path, task_id: str, *, now: float) -> list[dict]:
    """Read every holder and its youngest sentinel's age without creating locks.
    Unreadable or malformed evidence is undecidable, never an expired assignment."""
    deliveries = Path(workspace) / "deliveries"
    try:
        recipients = pool_record.iter_recipients(deliveries)
    except FileNotFoundError:
        return []
    out, roster = [], None
    roster_loaded = False
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    for recipient_name in recipients:
        recipient = deliveries / recipient_name
        try:
            dfd = os.open(recipient, flags)
        except FileNotFoundError:
            continue
        try:
            mtimes = []
            for suffix in SENTINEL_SUFFIXES:
                name = f"{task_id}{suffix}"
                try:
                    fd = os.open(name, os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0), dir_fd=dfd)
                except FileNotFoundError:
                    continue
                try:
                    st = os.fstat(fd)
                finally:
                    os.close(fd)
                if not _stat.S_ISREG(st.st_mode):
                    raise OSError(f"{recipient / name} is non-regular; refusing to decide")
                mtimes.append(st.st_mtime)
            if not mtimes:
                continue
            marker = regular_file_state(NON_EXCLUSIVE_MARKER, dir_fd=dfd)
            if marker == "regular" and not roster_loaded:
                roster, roster_loaded = pool_roster.load_roster(workspace), True
            try:
                non_exclusive = pool_claim_policy.is_non_exclusive(recipient.name, marker, roster)
            except pool_claim_policy.InvalidClaimMarker as exc:
                raise OSError(f"{recipient / NON_EXCLUSIVE_MARKER} {exc}") from exc
            out.append({"recipient": recipient.name, "non_exclusive": non_exclusive,
                        "claim_age_s": max(0.0, now - max(mtimes))})
        finally:
            os.close(dfd)
    return out
