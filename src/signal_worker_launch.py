#!/usr/bin/env python3
"""Launch a Signal Room task's worker inside a kernel sandbox that can write ONLY its task root.

    python3 src/signal_worker_launch.py <task_id> -- <command...>

`<task_id>` must name a real Signal Room task — a `task-signal-*` file carrying
`source: signal-room` in the live, processed or archived task dir — whose
`<results>/<task_id>/` is a plain (non-symlink) directory. The launcher derives
that root itself (never from the caller's environment or arguments) and runs the
command under `sandbox-exec` with a seatbelt profile that denies every
`file-write*` except `(subpath "<root>")`. A worker that forges
SIGNAL_TASK_OUTPUT_ROOT, names another task id, or writes anywhere else fails at
the kernel — the wrapper's own checks are a second line, and the environment
variable is a convenience for it, not the boundary. Seatbelt profiles only ever
intersect, so a nested launch can narrow this allowance but never widen it.

There is no unsandboxed mode: a host without `sandbox-exec` refuses to launch.
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
SANDBOX_EXEC = "/usr/bin/sandbox-exec"


class LaunchRefused(Exception):
    """The task id does not name a Signal Room task with a plain output dir, or no sandbox."""


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


def sandbox_profile(root: str) -> str:
    """Seatbelt profile: everything as inherited, except writes — denied outside exactly `root`."""
    quoted = root.replace("\\", "\\\\").replace('"', '\\"')
    return ("(version 1)\n"
            "(allow default)\n"
            "(deny file-write*)\n"
            f'(allow file-write* (subpath "{quoted}"))\n')


def sandbox_argv(root: str, command) -> list:
    return [SANDBOX_EXEC, "-p", sandbox_profile(root), *command]


def worker_env(task_id: str, tasks_dir, results_dir, base_env=None) -> dict:
    env = dict(os.environ if base_env is None else base_env)
    env[OUTPUT_ROOT_ENV] = output_root_for(task_id, tasks_dir, results_dir)
    return env


def launch_argv(task_id: str, tasks_dir, results_dir, command, base_env=None) -> tuple:
    """(argv, env) for the sandboxed worker; refused when the host has no kernel sandbox."""
    env = worker_env(task_id, tasks_dir, results_dir, base_env)
    if not os.access(SANDBOX_EXEC, os.X_OK):
        raise LaunchRefused(f"{SANDBOX_EXEC} is unavailable; a Signal Room worker never runs unsandboxed")
    return sandbox_argv(env[OUTPUT_ROOT_ENV], command), env


def main(argv) -> int:
    if len(argv) < 3 or argv[1] != "--":
        print(__doc__.strip().split("\n", 2)[2].strip(), file=sys.stderr)
        return 2
    workspace = resolve_workspace()
    try:
        cmd, env = launch_argv(argv[0], workspace / "tasks", workspace / "results", argv[2:])
    except LaunchRefused as exc:
        print(f"signal_worker_launch: refused: {exc}", file=sys.stderr)
        return 2
    os.execve(cmd[0], cmd, env)
    return 1  # pragma: no cover — execve does not return


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
