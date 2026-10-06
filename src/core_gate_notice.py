#!/usr/bin/env python3
"""Tell each task waiting behind a blocked core why it is on hold.

When the supervisor monitor (core-input-watch.py) raises its `core-blocked` HITL card, every
task waiting in the core's queue gets ONE notice row under its own message (the agent-activity
projection, room audience) saying the core is stopped at a gate that needs the owner and that
the task is kept. The task is NOT answered or closed: it runs once the gate clears.

The dedup ledger is the outage, not the requirement: a changed prompt mints a new requirement
but the same outage, so the ledger (<workspace>/state/core-gate-noticed.json) lives until the
monitor sees the core leave the blocked set (`end_outage`). A task arriving later in the same
outage is noticed on the next tick; a new outage notices again. The room sees only a gate
category, never the prompt text.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import List, Optional

import activity_rows
import task_queue
from workspace_default import status_path

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
        return len(task_queue.pending_files(workspace))
    except OSError:
        return 0


def ledger_path(workspace: Path) -> Path:
    return status_path("core-gate-noticed.json", Path(workspace))


def _noticed(workspace: Path) -> List[str]:
    try:
        ids = json.loads(ledger_path(workspace).read_text()).get("tasks")
        return [t for t in ids if isinstance(t, str)] if isinstance(ids, list) else []
    except (OSError, ValueError, AttributeError):
        return []


def _save_noticed(workspace: Path, ids: List[str]) -> None:
    path = ledger_path(workspace)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps({"tasks": ids}))
    os.replace(tmp, path)


def end_outage(workspace: Path) -> None:
    """The core left the blocked set: the next outage notices every queued task again."""
    try:
        ledger_path(workspace).unlink()
    except FileNotFoundError:
        pass


def notice_queued(manager, req, workspace: Path, state: str, kind: Optional[str]) -> List[str]:
    """Write the notice for every pending task this outage has not noticed yet; returns their ids."""
    if manager is None or req is None:
        return []
    try:
        files = {}
        for p in task_queue.pending_files(workspace):
            files.setdefault(activity_rows.task_from_file(p)[0]["id"], p)
    except OSError:
        return []
    with manager.store.locked():
        if manager.get(req.id) is None:
            return []
        seen = _noticed(workspace)
        fresh = [tid for tid in files if tid not in seen]
        if not fresh:
            return []
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
        if done:
            _save_noticed(workspace, [t for t in seen if t in files] + done)
    return done
