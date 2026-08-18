#!/usr/bin/env python3
"""A queued task held by a running worker is not a stalled queue.

A delegated team task stays in `tasks/` for its whole run — the worker removes
it only when it publishes a result — so an in-flight task and an abandoned one
are byte-identical on disk. The probe warned "watcher or core may be stuck" on
both, which fires on ordinary operation and trains the reader to ignore it.

The suppression is BOUNDED by the worker's own hard deadline. Past
SUTANDO_TIER_HARD_TIMEOUT (session-worker.py:249, default 900s) a live holder
has outlived the limit it enforces on itself, so "held" stops meaning "working"
and starts meaning "wedged" — and that is precisely when an unbounded version
of this probe would go quiet forever.
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
check("held_is_progress" in body,
      "suppression is gated on a single named predicate, not an inline comparison")
check("not stalled" in body, "the all-held case says so explicitly")

# BEHAVIOURAL: a reworded detail with status="warn" still alerts, which no
# wording assertion can see. These call the probe and read what the notifier does.
import os
import tempfile
import time as _time

ALERTABLE = ("down", "missing", "not_loaded", "fail", "stale", "warn")


def probe(n_tasks, n_held, age_sec, real_lookup=False):
    """Run the real check_task_queue against a temp workspace.

    real_lookup keeps the shipped worker lookup so a broken `ps` is exercised.
    """
    tmp = Path(tempfile.mkdtemp())
    (tmp / "tasks").mkdir()
    names = []
    for i in range(n_tasks):
        f = tmp / "tasks" / f"task-{i:03d}.txt"
        f.write_text("id: x\n")
        old_t = _time.time() - age_sec
        os.utime(f, (old_t, old_t))
        names.append(f.name)
    held = set(names[:n_held])
    orig_ws, orig_held = hc.WORKSPACE_DIR, hc._tasks_held_by_a_worker
    hc.WORKSPACE_DIR = tmp
    if not real_lookup:
        hc._tasks_held_by_a_worker = lambda ps_output=None: held
    try:
        return hc.check_task_queue()
    finally:
        hc.WORKSPACE_DIR, hc._tasks_held_by_a_worker = orig_ws, orig_held

# The ordinary case this PR exists for: held, and still inside the deadline.
r = probe(1, 1, 400)
check(r["status"] == "ok", f"held + UNDER deadline -> ok (got {r['status']!r})")
check(r["status"] not in ALERTABLE,
      "all-held does NOT reach emit_task_for_failures / notify_for_failures")
# No wording claim here: one task under both thresholds reaches neither branch,
# so it takes the plain fall-through. The reassuring wording is asserted below,
# on the count+age branch, which is the only place it is still reachable.

# THE BOUND. 1000s is past the worker's own hard deadline
# (SUTANDO_TIER_HARD_TIMEOUT, session-worker.py:249, default 900), so "a live
# worker holds it" has stopped being evidence that anything is progressing.
# Without this the suppression is unbounded and a wedged worker is invisible.
r = probe(1, 1, 1000)
check(r["status"] == "warn", f"held + PAST deadline -> warn (got {r['status']!r})")
check(r["status"] in ALERTABLE, "a wedged worker reaches the notifier")
check("WORKER is wedged" in r["detail"],
      "the detail points at the worker, not the watcher")
check("not stalled" not in r["detail"],
      "and it drops the reassurance it can no longer support")

# A genuinely stalled queue must be unchanged — this is the probe's real job.
r = probe(1, 0, 1000)
check(r["status"] == "warn", f"unheld + past stuck age -> warn (got {r['status']!r})")
check(r["status"] in ALERTABLE, "a real stall still alerts")

# Mixed: partial accounting must NOT buy silence.
r = probe(4, 2, 400)
check(r["status"] == "warn", f"mixed queue -> warn (got {r['status']!r})")
check("2 in flight with a worker" in r["detail"], "mixed reports how many are accounted for")

# Count+age branch, fully held, inside the deadline.
r = probe(4, 4, 400)
check(r["status"] == "ok", f"count+age branch, all held -> ok (got {r['status']!r})")
check("not stalled" in r["detail"], "the detail still explains why it is quiet")

# The count+age branch carried the SAME unbounded suppression and returns before
# the stuck_age_sec branch is reached, so bounding only the latter would leave a
# 4-task all-held pile quiet at any age.
r = probe(4, 4, 1000)
check(r["status"] == "warn",
      f"count+age branch, all held PAST deadline -> warn (got {r['status']!r})")
check("WORKER is wedged" in r["detail"],
      "count+age branch also names the worker once past the deadline")

# A broken `ps` is two questions, not one: does it stay quiet, and does the
# silence it buys disable the probe? Both are asserted; the second is the point.
def _raising_run(*_a, **_kw):
    raise OSError("ps unavailable")

_orig_run = hc.subprocess.run
hc.subprocess.run = _raising_run
try:
    check(hc._tasks_held_by_a_worker() == set(),
          "a `ps` that raises yields no held set instead of propagating")
    r = probe(1, 0, 1000, real_lookup=True)
    check(r["status"] == "warn",
          f"with `ps` broken a real stall STILL warns (got {r['status']!r})")
finally:
    hc.subprocess.run = _orig_run

print()
if failures:
    print(f"{len(failures)} FAILED: " + "; ".join(failures))
    sys.exit(1)
print("all task-queue in-flight assertions passed")
