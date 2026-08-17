#!/usr/bin/env python3
"""A queued task held by a running worker is not a stalled queue.

A delegated team task stays in `tasks/` for its whole run — the worker removes
it only when it publishes a result — so an in-flight task and an abandoned one
are byte-identical on disk. The probe warned "watcher or core may be stuck" on
both, which fires on ordinary operation and trains the reader to ignore it.
"""

import importlib.util
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("hc", REPO / "src" / "health-check.py")
hc = importlib.util.module_from_spec(_spec)
try:
    _spec.loader.exec_module(hc)
except SystemExit:
    pass

failures = []


def check(cond, label):
    print(("PASS  " if cond else "FAIL  ") + label)
    if not cond:
        failures.append(label)


PS = """\
/bin/bash src/watch-tasks-stream.sh --handler-runner /r/skills/task-workstream-sessions/scripts/session-worker.py claude /w /w/tasks/task-aaa.txt /w/results /r /tmp/ev task-aaa.txt
/usr/bin/python3 /r/skills/task-workstream-sessions/scripts/session-worker.py --runtime claude --workspace /w --task-file /w/tasks/task-bbb.txt --results-dir /w/results --repo /r
/usr/bin/python3 src/health-check.py
/bin/bash src/watch-tasks-stream.sh
"""

held = hc._tasks_held_by_a_worker(PS)
check(held == {"task-aaa.txt", "task-bbb.txt"},
      f"both worker argv shapes are recognised (got {sorted(held)})")
check("task-aaa.txt" in held, "the --handler-runner wrapper form is seen")
check("task-bbb.txt" in held, "the --task-file form is seen")

# The plain watcher and the health-check itself must not register as workers.
check(hc._tasks_held_by_a_worker(
    "/bin/bash src/watch-tasks-stream.sh\n/usr/bin/python3 src/health-check.py\n") == set(),
    "a bare watcher and health-check itself hold nothing")

# Only paths under tasks/ count — a results path in the same argv is not a claim.
check(hc._tasks_held_by_a_worker(
    "python3 session-worker.py --task-file /w/results/task-ccc.txt\n") == set(),
    "a non-tasks/ path is not counted as a held task")

# Degrade to empty rather than raising: a probe must never fail the check.
check(hc._tasks_held_by_a_worker("") == set(), "empty ps output yields an empty set")
check(hc._tasks_held_by_a_worker("garbage\n\n   \n") == set(), "junk ps output does not raise")

# The wording is the point: "may be stuck" must not appear when every queued
# task is accounted for by a live worker.
src = (REPO / "src" / "health-check.py").read_text()
i = src.index("def check_task_queue")
body = src[i:i + 2600]
check("held_note" in body, "the probe threads the in-flight count into its detail")
check("inflight == len(files)" in body,
      "the stuck wording is suppressed only when EVERY queued task is held")
check("not stalled" in body, "the all-held case says so explicitly")

print()
if failures:
    print(f"{len(failures)} FAILED: " + "; ".join(failures))
    sys.exit(1)
print("all task-queue in-flight assertions passed")
