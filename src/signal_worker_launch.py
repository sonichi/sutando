"""Root derivation for a Signal Room task: `<canonical results dir>/<task_id>`.

Nothing launches through this module, and nothing here reaches the sandboxed
worker. The trusted core runs `signal_image_gen.py --task-id <id>` AFTER the
worker returns, and the wrapper derives its root here from the id alone: the
task file is found under the configured tasks dir — any live name first
(`task_archive.find_task_file` knows the `.claimed-*` / `.assigned-*` renames a
task carries while it is being processed), then the processed and archived
layouts — its `source:` header must say `signal-room`, and the root is created
0700 when absent and refused unless it is a plain (non-symlink) directory.
"""

from __future__ import annotations

import os
import re
import stat
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import local_task_protocol  # noqa: E402
import task_archive  # noqa: E402

from signal_room_tasks import SIGNAL_TASK_PREFIX, canonical_output_root  # noqa: E402

TASK_ID_RE = re.compile(r"^[A-Za-z0-9_\-.]+\Z")


class LaunchRefused(Exception):
    """The task id does not name a Signal Room task with a plain output dir."""


def find_task(task_id: str, tasks_dir) -> Path | None:
    """The task file under any name it carries: live (bare, claimed, assigned), then
    processed and archived. The id is validated first — both locators glob with it."""
    if (not isinstance(task_id, str) or not task_id.startswith(SIGNAL_TASK_PREFIX)
            or not TASK_ID_RE.match(task_id)
            or not local_task_protocol.valid_archive_lookup_id(task_id)):
        return None
    tasks_dir = Path(tasks_dir)
    return (task_archive.find_task_file(tasks_dir, task_id)
            or local_task_protocol.find_archived_task(tasks_dir, task_id))


def output_root_for(task_id: str, tasks_dir, results_dir) -> str:
    """`<canonical results dir>/<task_id>` once the task is verified; created 0700 if absent."""
    task_file = find_task(task_id, tasks_dir)
    if task_file is None:
        raise LaunchRefused("not a known Signal Room task id")
    try:
        headers = local_task_protocol.parse_task_headers(
            task_file.read_text(encoding="utf-8", errors="replace")).headers
    except OSError:
        raise LaunchRefused("task unreadable")
    if headers.get("source") != "signal-room":
        raise LaunchRefused("task is not a Signal Room task")
    root = canonical_output_root(results_dir, task_id)
    try:
        os.mkdir(root, 0o700)
    except FileExistsError:
        pass
    except OSError:
        raise LaunchRefused("results dir not found")
    try:
        if not stat.S_ISDIR(os.lstat(root).st_mode):
            raise LaunchRefused("task output dir is not a plain directory")
    except OSError:
        raise LaunchRefused("task output dir not found")
    return root
