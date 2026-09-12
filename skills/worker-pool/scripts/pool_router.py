#!/usr/bin/env python3
"""The router pass: resolve one admitted task against the roster, write deliveries.

A role with a contract, not a process. This is a function the core's task watcher
calls — there is no daemon here, and there must not be one: a second process would
re-watch `tasks/`, which the watcher already does across code hardened by many
incidents.

Its whole input is the roster and one task. Nothing else may be read, which is
what makes a pass testable by replay: same roster, same task, same deliveries,
with no clock, no directory listing and no liveness probe in the decision.

The one thing it may NOT do is substitute one WORKER for another. A target not on
the roster goes to the core, which is a recipient rather than a fallback.
recipient is how a task reaches someone the owner never addressed.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
# helpers live in the core; repo root is parents[3] from this directory
sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "src"))

import pool_delivery as pd  # noqa: E402
import pool_roster as pr  # noqa: E402

PRIORITY_ORDER = {"urgent": 0, "normal": 1, "low": 2}


class RouterRefused(Exception):
    """The pass cannot run. Nothing was delivered."""


def order_candidates(tasks) -> list:
    """`urgent > normal > low`, then oldest payload first. A task with no
    priority sorts as `normal` rather than last — an absent field is a default,
    not a demotion."""
    return sorted(
        tasks,
        key=lambda t: (PRIORITY_ORDER.get((t.get("priority") or "normal"), 1),
                       t.get("created_at") or "", t.get("id") or ""))


def deliver_one(workspace, recipient: str, task_id: str) -> str:
    """Create one sentinel. Returns 'delivered' | 'already' | 'no-payload'.

    Checks BOTH names: a sentinel already renamed to `.accepted` is work in
    flight, and recreating the pending name would deliver it a second time.
    `EEXIST` is success — another pass won the race and the delivery exists.

    A sentinel names a payload; without one it names nothing, and its recipient
    can only delete it. An ARCHIVED payload is the dangerous case — that is
    finished work, and a sentinel would offer it again.
    """
    d = pd.deliveries_dir(workspace, recipient)
    if pd.find(workspace, recipient, task_id) is not None:
        return "already"
    if not pd.payload_path(Path(workspace), task_id).is_file():
        return "no-payload"
    d.mkdir(parents=True, exist_ok=True)
    try:
        os.close(os.open(d / (task_id + pd.PENDING_SUFFIX),
                         os.O_CREAT | os.O_EXCL))
    except FileExistsError:
        return "already"
    return "delivered"


def route(workspace, task: dict, roster=None) -> dict:
    """One task through one pass.

    `roster=None` loads it; an absent or unreadable roster REFUSES the pass
    rather than defaulting to the core, which would aim every task at one
    recipient the moment the file is unwritable.
    """
    r = roster if roster is not None else pr.load_roster(workspace)
    if r is None:
        raise RouterRefused("roster is absent or unreadable — refusing the pass")

    task_id = task.get("id")
    if not task_id:
        raise RouterRefused("task has no id")

    source = task.get("channel_id") or task.get("source") or ""
    targets = pr.targets_for(r, source, task.get("requested_worker"))

    # A name not on the roster is not a worker: the core takes it. The core is
    # a recipient, not a fallback, and no OTHER worker ever gets the task.
    unknown = pr.unknown_targets(r, targets)
    if unknown:
        targets = [pr.CORE]

    # Liveness is not consulted: a durable sentinel is found by a worker that starts later.
    # "no-payload" stays its own list: folded into `already` a refusal reads as delivered.
    buckets = {"delivered": [], "already": [], "no-payload": []}
    for t in targets:
        buckets[deliver_one(workspace, t, task_id)].append(t)
    return {"task_id": task_id, "version": r.get("version"),
            "delivered": buckets["delivered"], "already": buckets["already"],
            "skipped": buckets["no-payload"],
            "redirected": unknown, "error": None}


def route_all(workspace, tasks, roster=None) -> list:
    """A pass over many tasks, in priority order. The roster is read ONCE so
    every task in one pass is decided against the same version."""
    r = roster if roster is not None else pr.load_roster(workspace)
    if r is None:
        raise RouterRefused("roster is absent or unreadable — refusing the pass")
    return [route(workspace, t, r) for t in order_candidates(tasks)]
