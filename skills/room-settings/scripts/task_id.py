#!/usr/bin/env python3
"""Print the s.space task id of an owner's task file, for room.agent_policy.set.

    python3 skills/room-settings/scripts/task_id.py <task-file>

Exit 0 prints the id. Exit 2: the task is not the owner's. Exit 3: the file names no
s.space task id (a local chat task, another channel, or a requeued copy). Headers are
read the attested way, so body text cannot claim either.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / "packages" / "ag2-sparrow"))

from ag2_sparrow import gateway_instance, task_io  # noqa: E402
from ag2_sparrow.local_task_protocol import canonical_access_tier  # noqa: E402

NOT_OWNER, NO_TASK = 2, 3


def wire_task_id(text: str) -> "tuple[int, str]":
    """(exit code, task id or reason) for a task file's text."""
    headers = task_io.attested_headers(text).headers
    if canonical_access_tier(headers.get("access_tier")) != "owner":
        return NOT_OWNER, "not the owner's task"
    tid = (headers.get("task_id") or "").strip()
    if tid:
        return 0, tid
    local = (headers.get("id") or "").strip()
    if not local:
        return NO_TASK, "no task id"
    room = (headers.get("source_room_id") or headers.get("channel_id") or "").strip()
    if not room.startswith("!"):
        return NO_TASK, "not an s.space room task"
    # A requeue rewrites `id:`, so only a `task_id:` header names the broker's task.
    if re.search(r"(?m)^dedup_requeue_count:", text):
        return NO_TASK, "requeued task has no broker id"
    # A named instance's local id is `task-<instance>~<broker id>`; the primary's has no `~`.
    base = gateway_instance.first_run(local)
    instance = base[len("task-"):base.index("~")] if base.startswith("task-") and "~" in base else ""
    return 0, gateway_instance.broker_tid(local, instance)


def main(argv: "list[str]") -> int:
    if len(argv) != 2:
        print(__doc__.strip().splitlines()[2].strip(), file=sys.stderr)
        return 64
    try:
        text = Path(argv[1]).read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        print(f"cannot read task file: {exc.strerror}", file=sys.stderr)
        return NO_TASK
    code, out = wire_task_id(text)
    print(out, file=sys.stdout if code == 0 else sys.stderr)
    return code


if __name__ == "__main__":
    sys.exit(main(sys.argv))
