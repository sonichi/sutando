#!/usr/bin/env python3
"""Resolve one delivery sentinel to the task payload it stands for.

The pool is the only layer that knows a sentinel in a recipient's delivery
folder names a body in `tasks/`, so the mapping lives here and the core runs
this as an opaque executable (`SUTANDO_INBOX_RESOLVER`) and verifies the answer.

Contract, because the caller enforces it and a violation fails closed there:
stdout is the payload's ABSOLUTE path and nothing else; any other outcome is a
non-zero exit with the reason on stderr and an empty stdout.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import pool_delivery as pd  # noqa: E402


def resolve(entry: str, workspace=None) -> Path:
    """The payload `entry` stands for.

    `entry` must be the path the watcher saw: its basename carries the sentinel
    and its parents carry the workspace, so one argument fixes both. The layout
    and its validation belong to pool_delivery; this reads the payload it names.
    """
    ws, _recipient, task_id, _accepted = pd.parse_entry(entry, workspace)
    # Accepted and pending spell the same task, so both resolve: the watcher
    # announces whichever name it saw and neither implies a different body.
    path = pd.payload_path(ws, task_id).resolve()
    if not path.is_file():
        raise ValueError(f"sentinel {task_id} names no payload at {path}")
    return path


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1:
        print(f"usage: {Path(sys.argv[0]).name} <sentinel-path>", file=sys.stderr)
        return 2
    try:
        print(resolve(args[0]))
    except (ValueError, pd.NotDelivered) as e:
        print(f"resolve_inbox_entry: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
