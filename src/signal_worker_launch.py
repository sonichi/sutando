#!/usr/bin/env python3
"""Launch a Signal Room task's worker with its output root pinned server-side.

    python3 src/signal_worker_launch.py <task_id> -- <command...>

`<task_id>` must name a real Signal Room task — a `task-signal-*` file carrying
`source: signal-room` in the live, processed or archived task dir — whose
`<results>/<task_id>/` is a plain (non-symlink) directory. The command then runs
with SIGNAL_TASK_OUTPUT_ROOT set to that directory, which is the only way
`signal_image_gen.py` learns where it may write. The root is never taken from the
caller: the workspace and the task id decide it, and any other id is refused.
"""

from __future__ import annotations

import os
import re
import stat
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import local_task_protocol  # noqa: E402
from signal_image_gen import OUTPUT_ROOT_ENV  # noqa: E402

from signal_room_tasks import SIGNAL_TASK_PREFIX  # noqa: E402
from workspace_default import resolve_workspace  # noqa: E402

TASK_ID_RE = re.compile(r"^[A-Za-z0-9_\-.]+\Z")


class LaunchRefused(Exception):
    """The task id does not name a Signal Room task with a plain output dir."""


def output_root_for(task_id: str, tasks_dir, results_dir) -> str:
    """`<canonical results dir>/<task_id>` once the task and its dir are verified."""
    if (not isinstance(task_id, str) or not task_id.startswith(SIGNAL_TASK_PREFIX)
            or not TASK_ID_RE.match(task_id)):
        raise LaunchRefused("not a Signal Room task id")
    task_file = local_task_protocol.find_archived_task(Path(tasks_dir), task_id)
    if task_file is None:
        raise LaunchRefused("task not found")
    try:
        headers = local_task_protocol.parse_task_headers(
            task_file.read_text(encoding="utf-8", errors="replace")).headers
    except OSError:
        raise LaunchRefused("task unreadable")
    if headers.get("source") != "signal-room":
        raise LaunchRefused("task is not a Signal Room task")
    root = os.path.join(os.path.realpath(str(results_dir)), task_id)
    try:
        if not stat.S_ISDIR(os.lstat(root).st_mode):
            raise LaunchRefused("task output dir is not a plain directory")
    except OSError:
        raise LaunchRefused("task output dir not found")
    return root


def worker_env(task_id: str, tasks_dir, results_dir, base_env=None) -> dict:
    env = dict(os.environ if base_env is None else base_env)
    env[OUTPUT_ROOT_ENV] = output_root_for(task_id, tasks_dir, results_dir)
    return env


def main(argv) -> int:
    if len(argv) < 3 or argv[1] != "--":
        print(__doc__.strip().split("\n", 2)[2].strip(), file=sys.stderr)
        return 2
    workspace = resolve_workspace()
    try:
        env = worker_env(argv[0], workspace / "tasks", workspace / "results")
    except LaunchRefused as exc:
        print(f"signal_worker_launch: refused: {exc}", file=sys.stderr)
        return 2
    command = argv[2:]
    os.execvpe(command[0], command, env)
    return 1  # pragma: no cover — execvpe does not return


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
