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

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import pool_delivery as pd  # noqa: E402


def assigned_workspace():
    """The workspace the WATCHER was assigned, or None when it named none.

    Authoritative there ("whoever named that inbox names the workspace too"), so
    a resolver deriving its own can answer for a tree the caller is not serving.
    """
    v = (os.environ.get("SUTANDO_WORKSPACE_DIR") or "").strip()
    return Path(v) if v else None


def resolve(entry: str, workspace=None) -> Path:
    """The payload `entry` stands for.

    The assigned workspace wins when set, and `parse_entry` refuses if the entry
    does not live in it — a disagreement is two trees, not a redirect to obey.
    """
    if workspace is None:
        workspace = assigned_workspace()
    ws, _recipient, task_id, _accepted = pd.parse_entry(entry, workspace)
    # abspath, NOT resolve(): resolving would follow a symlink at the payload
    # name, and the caller adopts the returned basename as the task's identity.
    path = Path(os.path.abspath(pd.payload_path(ws, task_id)))
    if not pd.is_regular_file(path):
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
