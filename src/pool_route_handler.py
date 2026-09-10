#!/usr/bin/env python3
"""The router, as the core watcher's task-event handler.

One process, two watches: the core's watcher already watches `tasks/`, so the
router runs inside it rather than as a second daemon. This file is dispatch
only — every routing decision belongs to `pool_router`/`pool_roster`.

The watcher's handler protocol carries the design's recipient rules exactly:

    probe 3   decline    not a roster hit -- unbound, or a name never created.
                          The core takes it; it is a recipient, not a fallback.
    probe 0   accept     every target is on the roster; the real run delivers.

Run: called by src/watch-tasks-stream.sh; see dispatch_task there.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import local_task_protocol as ltp  # noqa: E402

import pool_roster as pr  # noqa: E402

import pool_router as rt  # noqa: E402

DECLINE = 3
MUST_HANDLE = 4


def read_task(task_file: str) -> dict:
    """The watcher hands a task FILE; the router takes a task DICT.

    `requested_worker` is read only from ABOVE `task:`, so a body cannot forge
    it. `channel_id`/`source` are read leniently: the gateway stamps them
    below `task:`, where the strict parse never looks.
    """
    text = Path(task_file).read_text(encoding="utf-8", errors="replace")
    task: dict = {"id": Path(task_file).stem}
    for line in text.splitlines():
        if line.startswith("task:"):
            break
        key, _, value = line.partition(":")
        if _ and key.strip() in ("id", "channel_id", "source", "requested_worker"):
            task[key.strip()] = value.strip()
    if not task.get("channel_id") or not task.get("source"):
        lenient = ltp.parse_task_headers_lenient(text).headers
        for k in ("channel_id", "source"):
            if not task.get(k) and lenient.get(k):
                task[k] = str(lenient.get(k)).strip()
    return task


def classify(workspace, task: dict) -> tuple[int, list]:
    """(exit code, targets) without delivering anything."""
    roster = pr.load_roster(workspace)
    if roster is None:
        return DECLINE, []
    targets = pr.targets_for(roster, task.get("channel_id") or task.get("source") or "",
                             task.get("requested_worker"))
    # One question only: is every target on the roster? Anything else -- no
    # binding, a name never created -- is the core's, which is a real recipient.
    if targets == [pr.CORE] or pr.unknown_targets(roster, targets):
        return DECLINE, targets
    # Liveness is deliberately NOT asked: the sentinel is durable, so a worker
    # that starts later finds its work.
    return 0, targets


def main(argv=None) -> int:
    p = argparse.ArgumentParser(add_help=False)
    p.add_argument("--task-file", required=True)
    p.add_argument("--workspace", default=None)
    p.add_argument("--probe", action="store_true")
    for ignored in ("--runtime", "--results-dir", "--repo"):
        p.add_argument(ignored, default=None)
    args, _unknown = p.parse_known_args(argv)

    ws = args.workspace
    task = read_task(args.task_file)
    code, _targets = classify(ws, task)
    if args.probe:
        return code
    if code == DECLINE:
        return DECLINE

    out = rt.route(ws, task, None)
    if out.get("failed"):
        # Unreachable via the probe, which declines these; kept so a direct
        # caller cannot turn an unknown name into a delivery.
        print(json.dumps(out), file=sys.stderr)
        return DECLINE
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
