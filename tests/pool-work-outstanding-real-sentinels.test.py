#!/usr/bin/env python3
"""A worker's "owes work" reads the live inbox layout, not a glob of it.

On a real pool the router's sentinels are zero-byte `task-*.txt` files that the
session watcher never renames to `.accepted`, and no done flag is written: an
inbox holds every task it was ever handed (148 on one measured host). Counting
them made every live worker owe work forever, which unguarded the wedge rung
and the fast resume. The worker's own question is task_dispatch's `owned-by`
less the tasks with a ready result, live or archived: the same contract
`check-pending-tasks.sh` uses for a worker session.
"""
import importlib.util
import json
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "skills" / "worker-pool" / "scripts"
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(REPO / "src"))

spec = importlib.util.spec_from_file_location("pool_supervise", SCRIPTS / "pool_supervise.py")
sup = importlib.util.module_from_spec(spec)
sys.modules["pool_supervise"] = sup
spec.loader.exec_module(sup)
pd, wi = sup.pd, sup.wi
from delivery import task_dispatch as td  # noqa: E402

fails = []


def check(name, got, want):
    ok = got == want
    print(f"  {'ok ' if ok else 'BAD'} {name}: {got!r}")
    if not ok:
        fails.append(f"{name}: got {got!r}, want {want!r}")


def worker_owes(ws, wid):
    """The PR's reader, under whichever signature this checkout has."""
    try:
        return sup.work_outstanding(ws, wid)
    except TypeError:
        return sup.work_outstanding(ws, wid, 1000.0)


WID = "17c6c322"
ws = Path(tempfile.mkdtemp(prefix="pool-real-sentinels-"))
(ws / "state").mkdir()
(ws / "state" / "roster.json").write_text(json.dumps({"workers": {WID: {"state": "live"}}}))
inbox = pd.deliveries_dir(ws, WID)
inbox.mkdir(parents=True)
archive = pd.results_dir(ws) / "archive"
archive.mkdir(parents=True)
# The measured shape: many zero-byte .txt sentinels, no .accepted, no done flags,
# every one answered, its reply already drained into the archive.
for i in range(148):
    tid = f"task-17{i:08d}"
    (inbox / f"{tid}.txt").write_bytes(b"")
    (archive / f"{tid}.txt").write_text(f"reply {i}\n")

check("fixture: a delivered reply is ready to task_dispatch",
      td.has_ready_result(pd.results_dir(ws), "task-1700000000.txt"), True)
check("fixture: no sentinel was ever accepted", list(inbox.glob("*.accepted")), [])
check("148 answered zero-byte sentinels owe nothing", worker_owes(ws, WID), False)

# (a) a zero-byte sentinel, never accepted, no result anywhere: owed.
(inbox / "task-1799999999.txt").write_bytes(b"")
check("(a) zero-byte .txt, no .accepted, no result: owed", worker_owes(ws, WID), True)

(pd.results_dir(ws) / "task-1799999999.txt").write_text("")
check("(a) an EMPTY live result delivers nothing: still owed", worker_owes(ws, WID), True)

# (b) the same task once answered, live in results/ or drained to results/archive/.
(pd.results_dir(ws) / "task-1799999999.txt").write_text("answered\n")
check("(b) its reply live in results/: not owed", worker_owes(ws, WID), False)
(pd.results_dir(ws) / "task-1799999999.txt").unlink()
(archive / "task-1799999999.txt").write_text("answered\n")
check("(b) its reply in results/archive/: not owed", worker_owes(ws, WID), False)

# (c) a task the worker really accepted and has not answered: owed.
(inbox / "task-1800000000.accepted").write_bytes(b"")
check("(c) accepted, no result: owed", worker_owes(ws, WID), True)
(archive / "task-1800000000.txt").write_text("answered\n")
check("(c) accepted, then answered: not owed", worker_owes(ws, WID), False)

print(f"\n{'ALL PASS' if not fails else str(len(fails)) + ' FAILURE(S)'} — worker work signal on real sentinels")
for f in fails:
    print("   ", f)
sys.exit(1 if fails else 0)
