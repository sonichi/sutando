"""Whether a queued task is owed by a recipient other than this core.

A task routed to another recipient stays in `<workspace>/tasks/` — the delivery
sentinel holds no payload, it names one — so on disk a routed task and an
unanswered one look identical. Every consumer that reads `tasks/` as "the core's
backlog" therefore needs this question answered, and answered the same way:

  - the stop gate blocks the core's turn while the queue is non-empty, so a
    routed task wedges the core until it answers work that is not its own;
  - orphan recovery archives an unanswered task past an age line, which for a
    routed task moves the payload out from under a sentinel that still points
    at it, and the recipient can then never run it.

The second is destructive, which is why this is one module rather than a test
each consumer re-implements: a copy that drifts loses a worker's work silently.

Dependency-light by design (stdlib only), because the stop gate shells into it
on every turn end.
"""
from __future__ import annotations

from pathlib import Path

# Both stages count as held. `.txt` is delivered-not-yet-claimed and `.accepted`
# is claimed-not-yet-finished; in either the recipient, not the core, owes it.
_STAGES = (".txt", ".accepted")


def deliveries_root(workspace) -> Path:
    """The inbox tree. One name, so a consumer never spells it again."""
    return Path(workspace) / "deliveries"


def held_by_another_recipient(workspace, task_id: str) -> bool:
    """True when some recipient's inbox holds a delivery for `task_id`.

    `task_id` may carry the `.txt` suffix of the queue file or not. Absent tree,
    unreadable tree, or no match all answer False: this is the core asking "is
    this someone else's?", and an unanswerable question must not silently
    transfer ownership away from the core, which would strand the task with
    nobody treating it as theirs.
    """
    stem = task_id[:-4] if task_id.endswith(".txt") else task_id
    if not stem:
        return False
    root = deliveries_root(workspace)
    try:
        recipients = list(root.iterdir())
    except (FileNotFoundError, NotADirectoryError, OSError):
        return False
    for recipient in recipients:
        for stage in _STAGES:
            try:
                if (recipient / f"{stem}{stage}").exists():
                    return True
            except OSError:
                continue
    return False


def holders(workspace, task_id: str) -> list[str]:
    """Recipient names holding `task_id`, for a message that must name them."""
    stem = task_id[:-4] if task_id.endswith(".txt") else task_id
    out: list[str] = []
    if not stem:
        return out
    try:
        recipients = sorted(p for p in deliveries_root(workspace).iterdir())
    except (FileNotFoundError, NotADirectoryError, OSError):
        return out
    for recipient in recipients:
        for stage in _STAGES:
            try:
                if (recipient / f"{stem}{stage}").exists():
                    out.append(recipient.name)
                    break
            except OSError:
                continue
    return out


if __name__ == "__main__":
    # Shell callers (the stop gate) ask through this, so there is no second
    # implementation of the convention to keep in step with the one above.
    import sys

    if len(sys.argv) >= 4 and sys.argv[1] == "held":
        raise SystemExit(0 if held_by_another_recipient(sys.argv[2], sys.argv[3]) else 1)
    if len(sys.argv) >= 4 and sys.argv[1] == "holders":
        print(" ".join(holders(sys.argv[2], sys.argv[3])))
        raise SystemExit(0)
    print("usage: recipients.py {held|holders} <workspace> <task-id>", file=sys.stderr)
    raise SystemExit(2)
