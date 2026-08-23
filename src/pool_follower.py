#!/usr/bin/env python3
"""Follower-side work acquisition (lead-follower pool, slice L2).

Under a live lead a follower executes ONLY its own assignments — taking
unassigned work would bypass every lead-side policy (priority, affinity,
consolidation). When the lead's beat goes stale the follower degrades to
the leaderless #884 claim so the queue keeps draining with no election;
assignment-only resumes the moment the beat is fresh again.

Injected paths/clock; stdlib only. See docs/lead-follower-pool.md.
"""
from __future__ import annotations

import os
import re
import sys
import time
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))
from task_priority import sort_tasks_by_priority  # noqa: E402

LEAD_STALE_S = 90  # 3 missed 30s beats — same threshold every reader uses

_UNASSIGNED_RE = re.compile(
    r"^task-(?!.*\.(?:assigned|claimed)-)([A-Za-z0-9._~-]+)\.txt$")


def lead_alive(state_dir, lead_label: str, now_fn=time.time) -> bool:
    """False on a missing OR future-dated beat — clock skew must degrade,
    never keep followers deferring to a lead that is not really there."""
    f = Path(state_dir) / "cores" / f"{lead_label}.alive"
    try:
        age = now_fn() - f.stat().st_mtime
    except OSError:
        return False
    return 0 <= age < LEAD_STALE_S


def _claim_assignment(tasks_dir: Path, f: Path, instance: str) -> "Path | None":
    suffix = f".assigned-{instance}.txt"
    target = f.with_name(
        f.name[:-len(suffix)] + f".claimed-{instance}.txt")
    try:
        os.rename(f, target)
        return target
    except OSError:
        return None  # lead reclaimed or a restart raced us — not an error


def acquire_work(tasks_dir, state_dir, instance: str,
                 lead_label: str, now_fn=time.time) -> "Path | None":
    """Claim the next unit of work for `instance`, or None when idle.
    Own assignments are honored in priority order in BOTH modes — the
    fallback additionally opens the unassigned pool."""
    tasks = Path(tasks_dir)
    suffix = f".assigned-{instance}.txt"
    try:
        assigned = [f for f in tasks.iterdir() if f.name.endswith(suffix)
                    and f.name.startswith("task-")]
    except OSError:
        assigned = []
    for f in sort_tasks_by_priority(assigned):
        got = _claim_assignment(tasks, f, instance)
        if got is not None:
            return got
    if lead_alive(state_dir, lead_label, now_fn):
        return None  # a live lead owns the unassigned pool — never steal
    try:
        pending = [f for f in tasks.iterdir() if _UNASSIGNED_RE.match(f.name)]
    except OSError:
        return None
    for f in sort_tasks_by_priority(pending):
        # assignment-suffix convention so lead-side load counting and
        # reclaim see fallback claims (legacy .claimed-core-N stays put)
        target = f.with_name(f.name[:-4] + f".claimed-{instance}.txt")
        try:
            os.rename(f, target)
            return target
        except OSError:
            continue
    return None
