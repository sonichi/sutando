#!/usr/bin/env python3
"""A sentinel owes work only while its task is still in tasks/ and unanswered.

The router's sentinel outlives its task: a room task answered elsewhere, or archived
without a result, left a zero-byte `task-*.txt` in the worker's inbox, so the sweep
read an idle worker as owing work. `task_disposition` settles it: pending or claimed
is owed; answered, archived or missing is not. The sweep then moves spent sentinels
older than an hour out of the inbox, and leaves owed or young ones in place.
"""
import importlib.util
import json
import os
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "skills" / "worker-pool" / "scripts"
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(REPO / "src"))

spec = importlib.util.spec_from_file_location("pool_supervise", SCRIPTS / "pool_supervise.py")
sup = importlib.util.module_from_spec(spec)
sys.modules["pool_supervise"] = sup
spec.loader.exec_module(sup)
pd = sup.pd

fails = []


def check(name, got, want):
    ok = got == want
    print(f"  {'ok ' if ok else 'BAD'} {name}: {got!r}")
    if not ok:
        fails.append(f"{name}: got {got!r}, want {want!r}")


WID = "39041ce6"
ws = Path(tempfile.mkdtemp(prefix="pool-settled-sentinels-"))
(ws / "state").mkdir()
(ws / "state" / "roster.json").write_text(json.dumps({"workers": {WID: {"state": "live"}}}))
inbox = pd.deliveries_dir(ws, WID)
inbox.mkdir(parents=True)
tasks = ws / "tasks"
(tasks / "archive" / "2026-11").mkdir(parents=True)
(pd.results_dir(ws) / "archive").mkdir(parents=True)


def sentinel(tid, suffix=".txt", age_s=7200):
    p = inbox / f"{tid}{suffix}"
    p.write_bytes(b"")
    t = time.time() - age_s
    os.utime(p, (t, t))
    return p


# (a) archived without a result (a room task the core retired): not owed.
sentinel("task-s-space~task-aaaaaaaaaaaaaaaaaa")
(tasks / "archive" / "2026-11" / "task-s-space~task-aaaaaaaaaaaaaaaaaa.txt").write_text("task: x\n")
check("(a) task archived, no result: not owed", sup.work_outstanding(ws, WID), False)

# (b) the task file is gone everywhere: nothing a worker could answer, not owed.
sentinel("task-s-space~task-bbbbbbbbbbbbbbbbbb")
check("(b) task missing everywhere: not owed", sup.work_outstanding(ws, WID), False)

# (c) still pending in tasks/: owed, whatever its neighbours say.
sentinel("task-cccccccccccccccccc")
(tasks / "task-cccccccccccccccccc.txt").write_text("task: x\n")
check("(c) task pending in tasks/: owed", sup.work_outstanding(ws, WID), True)
check("(c) owed_task_ids names only it", sup.owed_task_ids(ws, WID), ["task-cccccccccccccccccc"])

# (d) claimed by the worker (tasks/<id>.txt renamed by a lane): still owed.
(tasks / "task-cccccccccccccccccc.txt").unlink()
(tasks / "task-cccccccccccccccccc.claimed-lane1").write_text("id: task-cccccccccccccccccc\ntask: x\n")
check("(d) task claimed: owed", sup.work_outstanding(ws, WID), True)

# (e) answered: the reply drained into results/archive/ settles it.
(pd.results_dir(ws) / "archive" / "task-cccccccccccccccccc.txt").write_text("answered\n")
check("(e) claimed, then answered: not owed", sup.work_outstanding(ws, WID), False)

# The bulk caller's hint skips only the result lookup; the task file still decides.
check("unanswered=True on an answered, claimed task reads its file: claimed",
      sup.task_disposition(ws, "task-cccccccccccccccccc", unanswered=True), "claimed")
check("without the hint the result settles it: answered",
      sup.task_disposition(ws, "task-cccccccccccccccccc"), "answered")

# Retirement: a young spent sentinel stays, an old spent one moves, an owed one stays.
young = sentinel("task-s-space~task-dddddddddddddddddd", age_s=60)
sentinel("task-eeeeeeeeeeeeeeeeee", suffix=".accepted")
(tasks / "task-eeeeeeeeeeeeeeeeee.txt").write_text("task: x\n")
moved = sup.retire_spent_sentinels(ws, WID, time.time())
check("retired: the three old spent sentinels", sorted(moved), sorted([
    "task-s-space~task-aaaaaaaaaaaaaaaaaa.txt", "task-s-space~task-bbbbbbbbbbbbbbbbbb.txt",
    "task-cccccccccccccccccc.txt"]))
check("kept: the young one, still in the inbox", young.exists(), True)
check("kept: the owed .accepted one", (inbox / "task-eeeeeeeeeeeeeeeeee.accepted").exists(), True)
arch = ws / "state" / "delivery-archive" / WID
check("moved, not deleted: archived under state/delivery-archive/<worker>/<month>/",
      sorted(p.name for p in arch.glob("*/*")), sorted(moved))
check("the inbox gained no subfolder", [p.name for p in inbox.iterdir() if p.is_dir()], [])
check("still owed after retirement", sup.work_outstanding(ws, WID), True)
check("a second pass moves nothing", sup.retire_spent_sentinels(ws, WID, time.time()), [])

# A symlink at a sentinel name is malformed state, never moved.
(inbox / "task-ffffffffffffffffff.txt").symlink_to(tasks / "nowhere")
check("a symlinked sentinel is not moved", "task-ffffffffffffffffff.txt"
      in sup.retire_spent_sentinels(ws, WID, time.time() + 10 * 3600), False)

print(f"\n{'ALL PASS' if not fails else str(len(fails)) + ' FAILURE(S)'} — settled sentinels owe nothing")
for f in fails:
    print("   ", f)
sys.exit(1 if fails else 0)
