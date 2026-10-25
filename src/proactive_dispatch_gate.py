"""Whether a proactive message about a task may still be sent, asked right before the send.

The gateway's outbox lane calls dispatch_veto() after it has claimed the message: a task that
has ended, is waiting on the owner, or has a cancel pending gets nothing more. A reason
withdraws the message; None lets it go. The gateway checks the task's own result itself.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from activity_bus import TERMINAL, ActivityStore, cancel_target
from workspace_default import resolve_workspace


def dispatch_veto(task_id: str, workspace: Optional[Path] = None) -> Optional[str]:
    """The reason `task_id` must not be messaged now, or None; the workspace defaults to the resolved one."""
    ws = Path(workspace) if workspace is not None else resolve_workspace()
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
