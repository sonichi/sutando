"""Whether a task's initial status ack would duplicate what the room already sees about it.

One predicate, asked by the status-ack runner before it publishes and by the gateway's
dispatch gate right before an initial ack is sent: a task a session has picked up, one whose
activity card is up, or one that has a progress notice gets no "not picked up" line.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ack_claim  # noqa: E402
from activity_bus import ActivityStore  # noqa: E402

from task_archive import find_task_file  # noqa: E402
from workspace_default import resolve_workspace  # noqa: E402


def _ws(ws: Optional[Path]) -> Path:
    return Path(ws) if ws is not None else resolve_workspace(migrate=False)


def card_shown(ws: Optional[Path], task_id: str) -> bool:
    """True when the activity bus has projected (or still owes) a row for the task: its card is up.
    An activity snapshot that exists but cannot be read counts as a card."""
    path = ActivityStore(_ws(ws)).path(task_id)
    try:
        d = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return False
    except (OSError, ValueError):
        return True
    if not isinstance(d, dict):
        return True
    emitted = d.get("emitted")
    return bool(d.get("pending")) or not isinstance(emitted, int) or emitted > 0 or d.get("phase") not in (None, "RECEIVED")


def progress_shown(ws: Optional[Path], task_id: str) -> bool:
    """True once task-progress has taken the task's first-notify marker: a progress notice went out."""
    return ack_claim.state(_ws(ws), f"progress:{task_id}:first-notify") != "none"


def picked_up(ws: Optional[Path], task_id: str) -> bool:
    """True when the task file has left its bare name for a claimed, assigned or other state."""
    path = find_task_file(_ws(ws) / "tasks", task_id)
    return path is not None and path.name != f"{task_id}.txt"


def overlap_veto(ws: Optional[Path], task_id: str) -> Optional[str]:
    """Why an initial ack for the task would duplicate the card or the progress notifier, or None.
    The workspace defaults to the resolved one."""
    if picked_up(ws, task_id):
        return "a session picked the task up"
    if card_shown(ws, task_id):
        return "an activity card shows the task"
    if progress_shown(ws, task_id):
        return "a progress notice went out"
    return None
