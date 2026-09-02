"""Root derivation for a Signal Room worker: `<canonical results dir>/<task_id>`.

Nothing launches through this module. The core launches the worker from the task
body's in-band delegation block (`signal_room_tasks.delegation_lines`) as
`codex exec --sandbox workspace-write -C <root>`: codex's own seatbelt allows writes
under that working directory (plus its state dir and $TMPDIR) and nowhere else —
exactly "only the task root", granted by the launch itself. A nested profile could
never have done that: under codex's read-only mode a seatbelt only narrows. What
remains here is the derivation — the root for a task id, verified against the task
file, for a caller that holds nothing but the id.
"""

from __future__ import annotations

import os
import re
import stat
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import local_task_protocol  # noqa: E402
from signal_room_tasks import OUTPUT_ROOT_ENV, SIGNAL_TASK_PREFIX, worker_output_root  # noqa: E402

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
    root = worker_output_root(results_dir, task_id)
    try:
        if not stat.S_ISDIR(os.lstat(root).st_mode):
            raise LaunchRefused("task output dir is not a plain directory")
    except OSError:
        raise LaunchRefused("task output dir not found")
    return root


def worker_env(task_id: str, tasks_dir, results_dir, base_env=None) -> dict:
    """`base_env` plus the one variable the wrapper reads, for a caller holding only the id."""
    env = dict(os.environ if base_env is None else base_env)
    env[OUTPUT_ROOT_ENV] = output_root_for(task_id, tasks_dir, results_dir)
    return env
