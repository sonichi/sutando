#!/usr/bin/env python3
"""A task the Stop hook hands to the core inline (its text in the block reason) has been read by the
core, with no Read call for the activity hook to see. One `processing` row per task records that, so
readers of agent activity (the voice cancel above all) see the core has it. `processing` is not
`working`, so it never counts as progress and the task keeps blocking until it is answered.

    task_handover.py <task-file>...   # writes each missing row; exit 0 always (fail open)
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from activity_rows import append, task_from_file  # noqa: E402

LINE = "picked up: handed to the core at turn end"


def note_handed_over(task_file: Path, workspace: Path | None = None) -> dict | None:
    """Append the task's handover row once; a stable pid makes a repeat block a no-op."""
    task, room = task_from_file(task_file)
    return append(LINE, kind="processing", room=room, task=task, workspace=workspace,
                  pid=f"{task['id']}:handover")


def main(argv: list[str]) -> int:
    for arg in argv:
        try:
            note_handed_over(Path(arg))
        except Exception as exc:  # noqa: BLE001 — a hook must never block the stop it reports
            print(f"task_handover: {arg}: {exc}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
