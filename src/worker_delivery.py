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

from pathlib import Path

#: Suffixes the router writes. Kept in one place so a new stage is added once.
SENTINEL_SUFFIXES = (".txt", ".accepted", ".claimed")


def holder_of(workspace: Path, task_id: str) -> str | None:
    """The recipient id holding ``task_id``, or None when nobody does.

    An unreadable deliveries/ is NOT "nobody holds it" — that is the reading
    that hands a worker's task to the core — so only a genuinely absent
    directory answers None; anything else propagates.
    """
    deliveries = Path(workspace) / "deliveries"
    try:
        recipients = sorted(p for p in deliveries.iterdir() if p.is_dir())
    except FileNotFoundError:
        return None
    for recipient in recipients:
        for suffix in SENTINEL_SUFFIXES:
            if (recipient / f"{task_id}{suffix}").exists():
                return recipient.name
    return None
