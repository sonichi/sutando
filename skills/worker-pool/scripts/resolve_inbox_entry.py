#!/usr/bin/env python3
"""Resolve one delivery sentinel to the task payload it stands for.

The pool is the only layer that knows a sentinel in a recipient's delivery
folder names a body in `tasks/`, so the mapping lives here and the core runs
this as an opaque executable (`SUTANDO_INBOX_RESOLVER`) and verifies the answer.

Contract, because the caller enforces it and a violation fails closed there:
stdout is the payload's ABSOLUTE path and nothing else; any other outcome is a
non-zero exit with the reason on stderr and an empty stdout. Exit 3 is typed:
the sentinel names no payload (absent, a directory, a symlink), a verdict about
the entry; every other failure (an access error, a timeout, a crash, a bad
workspace) says nothing about the entry and is worth retrying (exit 1).
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import pool_delivery as pd  # noqa: E402


NO_PAYLOAD_RC = 3
# `--batch` proves it was understood by printing this line first; a caller that
# does not see it falls back to one call per entry.
BATCH_HEADER = "resolve-inbox-entry batch v1"


class NoPayload(ValueError):
    """The sentinel names no payload: final for the entry, not a resolver fault."""


def resolve(entry: str, workspace=None) -> Path:
    """The payload `entry` stands for.

    `entry` must be the path the watcher saw: its basename carries the sentinel
    and its parents carry the workspace, so one argument fixes both. The layout
    and its validation belong to pool_delivery; this reads the payload it names.
    """
    ws, _recipient, task_id, _accepted = pd.parse_entry(entry, workspace)
    # abspath, NOT resolve(): resolving would follow a symlink at the payload
    # name, and the caller adopts the returned basename as the task's identity.
    path = Path(os.path.abspath(pd.payload_path(ws, task_id)))
    state = pd.regular_file_state(path)
    if state == "regular":
        return path
    if state == "unknown":
        # A failure of THIS call (EACCES, EIO, ...), not a fact about the entry.
        raise pd.NotDelivered(f"sentinel {task_id}: payload at {path} could not be opened; retry")
    raise NoPayload(f"sentinel {task_id} names no payload at {path} ({state})")


def _one(entry: str, workspace) -> tuple[int, str, str]:
    """(rc, payload or "", reason) for one entry, with the single-entry exit codes."""
    try:
        return 0, str(resolve(entry, workspace)), ""
    except NoPayload as e:
        return NO_PAYLOAD_RC, "", str(e)
    except (ValueError, pd.NotDelivered) as e:
        return 1, "", str(e)


def batch(entries, workspace, out=sys.stdout) -> None:
    """One line per entry, `<rc>\t<payload>\t<entry>`, after BATCH_HEADER: the
    same verdicts as N single calls, for one process instead of N."""
    print(BATCH_HEADER, file=out)
    for entry in entries:
        rc, payload, _reason = _one(entry, workspace)
        print(f"{rc}\t{payload}\t{entry}", file=out)


def main(argv: list[str] | None = None) -> int:
    """`--workspace` is the tree the CALLER is serving, and it wins. The watcher
    treats its own assignment as authoritative, so a resolver that derived one
    could answer for a tree whose claims, state and results live elsewhere."""
    args = list(sys.argv[1:] if argv is None else argv)
    workspace = None
    if "--workspace" in args:
        i = args.index("--workspace")
        if i + 1 >= len(args):
            print("--workspace needs a directory", file=sys.stderr)
            return 2
        workspace = args[i + 1].strip() or None
        args = args[:i] + args[i + 2:]
    if args == ["--batch"]:
        batch([ln for ln in sys.stdin.read().split("\n") if ln], workspace)
        return 0
    if len(args) != 1 or not args[0]:
        print(f"usage: {Path(sys.argv[0]).name} <sentinel-path>|--batch [--workspace <dir>]",
              file=sys.stderr)
        return 2
    rc, payload, reason = _one(args[0], workspace)
    if rc:
        print(f"resolve_inbox_entry: {reason}", file=sys.stderr)
        return rc
    print(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
