#!/usr/bin/env python3
"""Resolve one delivery sentinel to the task payload it stands for.

The pool is the only layer that knows a sentinel in `deliveries/<worker>/`
names a body in `tasks/`, so the mapping lives here and the core runs this as
an opaque executable (`SUTANDO_INBOX_RESOLVER`) and verifies the answer.

Contract, because the caller enforces it and a violation fails closed there:
stdout is the payload's ABSOLUTE path and nothing else; any other outcome is a
non-zero exit with the reason on stderr and an empty stdout.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import pool_delivery as pd  # noqa: E402


def _workspace(explicit=None) -> Path | None:
    """The tree this worker was assigned. `SUTANDO_WORKSPACE_DIR` is the variable
    the spawner sets and the watcher honours, so both inspect one tree."""
    if explicit is not None:
        return Path(explicit)
    env = os.environ.get("SUTANDO_WORKSPACE_DIR", "").strip()
    return Path(env) if env else None


def resolve(entry: str, workspace=None) -> Path:
    """The payload `entry` stands for. Raises ValueError when it stands for none.

    `entry` may arrive as a bare name or a path; only its basename carries the
    sentinel, so a caller that already resolved one does not get a second read.
    """
    got = pd.parse_sentinel(os.path.basename(entry))
    if got is None:
        raise ValueError(f"not a delivery sentinel: {entry!r}")
    task_id, _accepted = got
    # Accepted and pending spell the same task, so both resolve: the watcher
    # announces whichever name it saw and neither implies a different body.
    path = pd.payload_path(_workspace(workspace), task_id).resolve()
    if not path.is_file():
        raise ValueError(f"sentinel {task_id} names no payload at {path}")
    return path


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1:
        print(f"usage: {Path(sys.argv[0]).name} <sentinel-name-or-path>", file=sys.stderr)
        return 2
    try:
        print(resolve(args[0]))
    except ValueError as e:
        print(f"resolve_inbox_entry: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
