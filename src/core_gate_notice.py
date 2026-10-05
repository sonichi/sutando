#!/usr/bin/env python3
"""Tell each task waiting behind a blocked core why it is on hold.

When the supervisor monitor (core-input-watch.py) raises its `core-blocked` HITL card, every
task waiting in the core's queue gets ONE notice row under its own message (the agent-activity
projection, room audience) saying the core is stopped at a gate that needs the owner and that
the task is kept. The task is NOT answered or closed: it runs once the gate clears.

The dedup ledger is the requirement itself (`subject.queued_noticed`): one requirement is one
blocked episode, so a task arriving later in the same episode is noticed on the next tick and
a new episode notices again. The room sees only a gate category, never the prompt text.
"""
from __future__ import annotations

from pathlib import Path
from typing import List, Optional

import activity_rows
import task_queue

_USAGE = frozenset({"turn-rejected", "session-limit", "fable-limit-unfocused"})
_SIGN_IN = frozenset({"login"})


def reason(state: str, kind: Optional[str]) -> str:
    """The gate in words a room member can read; never the prompt itself."""
    if state == "logged-out" or kind in _SIGN_IN:
        return "it is signed out"
    if kind in _USAGE:
        return "it hit a usage limit"
    return "it is stopped at a prompt on its terminal"


def notice_line(state: str, kind: Optional[str]) -> str:
    return (f"On hold: my AI core cannot work right now because {reason(state, kind)}, and my owner "
            "has been asked to clear it. Your request is kept and goes ahead as soon as that is done.")


def card_line(queued: int) -> str:
    return f"{queued} queued task(s) are waiting behind this; each was told it is on hold."


def queued_count(workspace: Path) -> int:
    try:
        return len(task_queue.pending(workspace))
    except OSError:
        return 0


def notice_queued(manager, req, workspace: Path, state: str, kind: Optional[str]) -> List[str]:
    """Write the notice for every pending task `req` has not noticed yet; returns their ids."""
    if manager is None or req is None:
        return []
    try:
        pending = task_queue.pending(workspace)
    except OSError:
        return []
    with manager.store.locked():
        cur = manager.get(req.id)
        if cur is None:
            return []
        seen = list((cur.subject or {}).get("queued_noticed") or [])
        fresh = [t["id"] for t in pending if t["id"] not in seen]
        if not fresh:
            return []
        files = {}
        for p in (workspace / "tasks").glob("task-*.txt"):
            try:
                files[activity_rows.task_from_file(p)[0]["id"]] = p
            except OSError:
                continue
        done = []
        for tid in fresh:
            try:
                task, room = activity_rows.task_from_file(files[tid])
                activity_rows.append(notice_line(state, kind), kind="notice", room=room, task=task,
                                     audience="room", projection="TASK_STATUS",
                                     pid=f"{req.id}:{tid}:on-hold", workspace=workspace)
            except (KeyError, OSError, ValueError):
                continue  # vanished or unreadable: picked up or archived meanwhile
            done.append(tid)
        cur.subject = {**(cur.subject or {}), "queued_noticed": seen + done}
        manager.store.save(cur)
    return done
