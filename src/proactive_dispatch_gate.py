"""Whether a proactive message about a task may still be sent, asked right before the send.

The gateway's outbox lane calls dispatch_veto() after it has claimed the message: a task that
has ended, is waiting on the owner, or has a cancel pending gets nothing more. A reason
withdraws the message; None lets it go. The gateway checks the task's own result itself.
route_veto() checks the room and thread the message is about to go to against the task's
HMAC-verified envelope, so a marker in a published file is never an override.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from activity_bus import TERMINAL, ActivityStore, cancel_target
from status_ack_policy import destination
from task_archive import find_task_file
from task_envelope import attested_task_headers, verify_text
from workspace_default import resolve_workspace


def dispatch_veto(task_id: str, workspace: Optional[Path] = None) -> Optional[str]:
    """The reason `task_id` must not be messaged now, or None; the workspace defaults to the resolved one."""
    ws = Path(workspace) if workspace is not None else resolve_workspace()
    if find_task_file(ws / "tasks", task_id) is None:
        return "the task has left the queue"
    phase = ActivityStore(ws).load(task_id).phase
    if phase in TERMINAL:
        return f"the task is {phase.lower()}"
    if phase == "WAITING":
        return "the task is waiting on the owner"
    for f in sorted((ws / "tasks").glob("task-*.txt")):
        try:
            text = f.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if cancel_target(text) == task_id:
            return f"a cancel for the task is pending ({f.name})"
    return None


def route_veto(task_id: str, room: str, thread_root: Optional[str], workspace: Optional[Path] = None) -> Optional[str]:
    """The reason a message about `task_id` must not go to (room, thread_root), or None. A task
    whose envelope verifies fixes both; one that does not can name no thread."""
    ws = Path(workspace) if workspace is not None else resolve_workspace()
    path = find_task_file(ws / "tasks", task_id)
    try:
        text = path.read_text(encoding="utf-8", errors="replace") if path else None
    except OSError:
        text = None
    if text is None or verify_text(text, ws).get("verdict") != "verified":
        return "a thread named for a task whose envelope is not verified" if thread_root else None
    dest, why = destination(dict(attested_task_headers(text, ws).headers))
    if dest is None:
        return "a thread named for a task whose destination is not verified" if thread_root else None
    if room != dest.room or thread_root != dest.thread_root:
        return "the route does not match the task's verified room and thread"
    return None
