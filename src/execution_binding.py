#!/usr/bin/env python3
"""The one writer of ``<workspace>/state/bindings/active-execution.json``.

The record names the task a core session is running (``{"task_id": ...}``). Its
readers decide whose task that is from the task file itself: the native-app
consent gate (``skills/macos-tools/scripts/native_pim_consent.py``), the
``native-pim-guard`` hook and the activity emitter. A notifier binds the task it
dispatches and unbinds it when that task finishes. The id comes from the task
file's name, which the notifier claimed; it is never taken from prompt text.

CLI, for bash callers with only an interpreter path:

    execution_binding.py bind <workspace> <task filename>     # on failure no record is left behind
    execution_binding.py unbind <workspace> <task filename>   # only while it still names that task
    execution_binding.py clear <workspace>                    # whatever it names

A failed ``bind`` must not leave an earlier task's record standing: a reader cannot
tell that it belongs to another task. The CLI removes it, and exits 1 either way.
"""
from __future__ import annotations

import json
import os
import re
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

RELATIVE = Path("state") / "bindings" / "active-execution.json"
_TASK_FILE = re.compile(r"(task-[A-Za-z0-9_.~-]+)\.txt")
USAGE = "usage: execution_binding.py bind|unbind <workspace> <task filename> | clear <workspace>"


def path(workspace) -> Path:
    return Path(workspace) / RELATIVE


def task_id(filename: str) -> str:
    """The id a task file's name carries; ValueError for anything that is not one plain name."""
    m = _TASK_FILE.fullmatch(filename or "")
    if not m or ".." in filename:
        raise ValueError(f"not a task filename: {filename!r}")
    return m.group(1)


def bind(workspace, filename: str) -> Path:
    """Atomically name ``filename``'s task as the one the core session is running."""
    record = {"task_id": task_id(filename),
              "bound_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    target = path(workspace)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent)
    try:
        with os.fdopen(fd, "w") as fh:
            json.dump(record, fh)
        os.replace(tmp, target)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return target


def unbind(workspace, filename: str) -> bool:
    """Remove the record while it still names ``filename``'s task; a newer binding is kept."""
    target = path(workspace)
    try:
        bound = json.loads(target.read_text()).get("task_id")
    except (OSError, ValueError, AttributeError):
        return False
    if bound != task_id(filename):
        return False
    target.unlink(missing_ok=True)
    return True


def clear(workspace) -> None:
    """Remove the record whatever it names; OSError when it exists and cannot be removed."""
    path(workspace).unlink(missing_ok=True)


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv[:1] == ["clear"] and len(argv) == 2:
        try:
            clear(argv[1])
        except OSError as e:
            print(f"execution_binding: {e}", file=sys.stderr)
            return 1
        return 0
    if len(argv) != 3 or argv[0] not in ("bind", "unbind"):
        print(USAGE, file=sys.stderr)
        return 2
    try:
        (bind if argv[0] == "bind" else unbind)(argv[1], argv[2])
    except (ValueError, OSError) as e:
        print(f"execution_binding: {e}", file=sys.stderr)
        if argv[0] == "bind":
            try:
                clear(argv[1])
            except OSError as stale:
                print(f"execution_binding: the earlier record could not be removed: {stale}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
