#!/usr/bin/env python3
"""A sentinel owes work only while its task is still in tasks/ and unanswered.

The router's sentinel outlives its task: a room task answered elsewhere, or archived
without a result, left a zero-byte `task-*.txt` in the worker's inbox, so the sweep
read an idle worker as owing work. `task_disposition` settles it: pending or claimed
is owed; answered, archived or missing is not. The sweep then moves spent sentinels
older than an hour out of the inbox, and leaves owed or young ones in place.
"""
import contextlib
import importlib.util
import io
import json
import os
import sys
import tempfile
import time
from pathlib import Path
from unittest import mock

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
check("retired: the two old spent sentinels", sorted(moved), sorted([
    "task-s-space~task-aaaaaaaaaaaaaaaaaa.txt", "task-s-space~task-bbbbbbbbbbbbbbbbbb.txt"]))
check("kept: an answered task still claimed in tasks/ with no done flag (the bridge's hold)",
      (inbox / "task-cccccccccccccccccc.txt").exists(), True)
check("kept: the young one, still in the inbox", young.exists(), True)
check("kept: the owed .accepted one", (inbox / "task-eeeeeeeeeeeeeeeeee.accepted").exists(), True)
arch = inbox / pd.ARCHIVE_DIR
check("moved, not deleted: archived under deliveries/<worker>/archive/",
      sorted(p.name for p in arch.glob("*")), sorted(moved))
check("the inbox's only subfolder is archive/", [p.name for p in inbox.iterdir() if p.is_dir()], ["archive"])
check("still owed after retirement", sup.work_outstanding(ws, WID), True)
check("a second pass moves nothing", sup.retire_spent_sentinels(ws, WID, time.time()), [])

# A symlink at a sentinel name is malformed state, never moved, even to a live regular file.
live = ws / "elsewhere.txt"
live.write_text("x")
os.utime(live, (1.0, 1.0))
(inbox / "task-ffffffffffffffffff.txt").symlink_to(live)
check("fixture: the symlink resolves to a regular file", (inbox / "task-ffffffffffffffffff.txt").is_file(), True)
check("a symlinked sentinel is not moved", "task-ffffffffffffffffff.txt"
      in sup.retire_spent_sentinels(ws, WID, time.time() + 10 * 3600), False)
check("the symlink is still in the inbox", (inbox / "task-ffffffffffffffffff.txt").is_symlink(), True)

# An earlier archive of the same name is never overwritten.
again = sentinel("task-s-space~task-aaaaaaaaaaaaaaaaaa")
sup.retire_spent_sentinels(ws, WID, time.time())
check("a re-delivered, re-retired name sits beside the first, not over it",
      sorted(p.name for p in arch.glob("task-s-space~task-aaaaaaaaaaaaaaaaaa*")),
      ["task-s-space~task-aaaaaaaaaaaaaaaaaa.txt", "task-s-space~task-aaaaaaaaaaaaaaaaaa.txt.1"])

# A sentinel whose name is no task id is in neither set: the worker stays decidable.
sentinel("task-bad id")
check("a malformed id does not make the worker undecidable", sup.work_outstanding(ws, WID), True)
check("a malformed id is never retired", "task-bad id.txt" in sup.retire_spent_sentinels(ws, WID, time.time()), False)

# One rule: the boot prune (pool_delivery.prune_spent) and the sweep agree on every shape.
def shaped():
    w = Path(tempfile.mkdtemp(prefix="pool-agree-"))
    (w / "state").mkdir()
    box = pd.deliveries_dir(w, WID)
    box.mkdir(parents=True)
    t = w / "tasks"
    (t / "archive" / "2026-11").mkdir(parents=True)
    res = pd.results_dir(w)
    (res / "archive").mkdir(parents=True)
    for tid in ("task-pending", "task-claimed-core", "task-answered-noflag", "task-answered-flag",
                "task-archived", "task-missing", "task-live-and-archived"):
        (box / f"{tid}.txt").write_bytes(b"")
        os.utime(box / f"{tid}.txt", (1.0, 1.0))
    (t / "task-pending.txt").write_text("id: task-pending\n")
    (t / "task-claimed-core.claimed-core-1.txt").write_text("id: task-claimed-core\n")
    (t / "task-answered-noflag.txt").write_text("id: task-answered-noflag\n")
    (res / "task-answered-noflag.txt").write_text("answered\n")
    (res / "archive" / "task-answered-flag.txt").write_text("answered\n")
    pd.mark_done(w, WID, "task-answered-flag", published=True)
    (t / "archive" / "2026-11" / "task-archived.txt").write_text("id: task-archived\n")
    (t / "task-live-and-archived.txt").write_text("id: task-live-and-archived\n")
    pd.archived_payload(w, "task-live-and-archived").write_text("archived")
    return w

boot, sweep = shaped(), shaped()
pruned = pd.prune_spent(boot, WID)
swept = {n[:-4] for n in sup.retire_spent_sentinels(sweep, WID, time.time())}
check("boot prune and sweep retire the same tasks",
      sorted(pruned["archived"] + pruned["retired"] + pruned["stale"]), sorted(swept))
check("kept: pending, a lane's claim, live-in-tasks/ despite an archived copy, and an answered task "
      "still in tasks/ with no done flag (the bridge still reads whose reply it is)",
      sorted(pruned["kept"]), ["task-answered-noflag", "task-claimed-core", "task-live-and-archived", "task-pending"])
check("owed is kept minus that answered one: it owes no work, it only keeps its hold",
      sorted(sup.owed_task_ids(sweep, WID)), ["task-claimed-core", "task-live-and-archived", "task-pending"])

# The boot prune keeps a sentinel whose name is a sentinel but no lookup-able task id.
odd = Path(tempfile.mkdtemp(prefix="pool-odd-id-"))
(odd / "state").mkdir()
long_id = "task-" + "a" * 130
(pd.deliveries_dir(odd, WID)).mkdir(parents=True)
(pd.deliveries_dir(odd, WID) / f"{long_id}.txt").write_bytes(b"")
check("prune_spent keeps a sentinel whose id task_disposition refuses",
      pd.prune_spent(odd, WID)["kept"], [long_id])

# sentinel_retirable is the shared predicate itself.
sentinel_retirable = pd.sentinel_retirable
spent_ws = shaped()
check("sentinel_retirable: missing and answered-with-payload-gone leave; pending and claimed stay",
      [sentinel_retirable(spent_ws, WID, t) for t in ("task-missing", "task-answered-flag", "task-pending",
                                             "task-claimed-core")], [True, True, False, False])

# The sweep's text report names an archive failure instead of dropping it.
buf = io.StringIO()
with mock.patch.object(sup, "tick", return_value={
        "decisions": {}, "observations": {}, "not_supervised": [], "auth_expired": [], "resumed": False,
        "routing": {"alarm": None}, "sentinel_retire_errors": {WID: "OSError: read-only"}}), \
        contextlib.redirect_stdout(buf):
    sup.main(["--workspace", str(ws), "--sweep"])
check("the text report names the archive failure",
      f"{WID[:8]}  could not archive spent sentinels: OSError: read-only" in buf.getvalue(), True)

print(f"\n{'ALL PASS' if not fails else str(len(fails)) + ' FAILURE(S)'} — settled sentinels owe nothing")
for f in fails:
    print("   ", f)
sys.exit(1 if fails else 0)
