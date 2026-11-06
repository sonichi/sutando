#!/usr/bin/env python3
"""`scripts/unanswered-tasks.py --json`: per-task and overall claim lag, and whether only a standby
holds the inbox. Text mode and the exit codes are pinned by tests/unanswered-tasks.test.py.
Run: python3 tests/unanswered-tasks-json.test.py
"""
import contextlib
import importlib.util
import io
import json
import os
import sys
import tempfile
from collections import namedtuple
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
_s = importlib.util.spec_from_file_location("uat", str(_REPO / "scripts" / "unanswered-tasks.py"))
uat = importlib.util.module_from_spec(_s)
_s.loader.exec_module(uat)

Holders = namedtuple("Holders", "observed holders undecided")
SESSION = Holders(True, [(101, "session"), (102, "untagged")], 0)
STANDBY = Holders(True, [(102, "untagged")], 0)
NOW = 1_792_000_000.0
PASS = FAIL = 0


def check(cond, name):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name}")


def task(root, task_id, *, intake_ago, mtime_ago=None, phase=None, result=None):
    (root / "tasks").mkdir(parents=True, exist_ok=True)
    (root / "results").mkdir(parents=True, exist_ok=True)
    stamp = NOW - intake_ago
    from datetime import datetime, timezone
    iso = datetime.fromtimestamp(stamp, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    f = root / "tasks" / f"{task_id}.txt"
    f.write_text(f"id: {task_id}\ntimestamp: {iso}\nsource: ag2space\ntask: hello\n")
    m = NOW - (intake_ago if mtime_ago is None else mtime_ago)
    os.utime(f, (m, m))
    if phase:
        (root / "state" / "activity").mkdir(parents=True, exist_ok=True)
        (root / "state" / "activity" / f"{task_id}.json").write_text(json.dumps({"phase": phase}))
    if result:
        (root / "results" / f"{task_id}.txt").write_text(result)


def by_id(snap):
    return {t["task_id"]: t for t in snap["tasks"]}


# --- Oldest unclaimed age, from the immutable intake time -----------------------------------------
with tempfile.TemporaryDirectory() as d:
    root = Path(d)
    task(root, "task-chat-a", intake_ago=600, mtime_ago=30)  # rewritten 30 s ago
    snap = uat.snapshot(root, 120, now=NOW, holders=SESSION)
    check(snap["oldest_unclaimed_task"] == "task-chat-a" and abs(snap["oldest_unclaimed_age_s"] - 600) < 1,
          "oldest unclaimed age is measured from intake, not the file's mtime")
    check(abs(by_id(snap)["task-chat-a"]["mtime_age_s"] - 600) > 500,
          "CONTROL: an mtime-anchored age would have reported 30 s for the same task")

with tempfile.TemporaryDirectory() as d:
    root = Path(d)
    task(root, "task-chat-old", intake_ago=900)
    task(root, "task-chat-new", intake_ago=200)
    snap = uat.snapshot(root, 120, now=NOW, holders=SESSION)
    check(snap["oldest_unclaimed_task"] == "task-chat-old", "the overall figure is the OLDEST unclaimed task")
    check(all(t["unclaimed_age_s"] is not None for t in snap["tasks"]), "each task reports its own unclaimed age")

# --- Claimed is not unclaimed; an unrelated answer resets nothing ---------------------------------
with tempfile.TemporaryDirectory() as d:
    root = Path(d)
    task(root, "task-chat-run", intake_ago=900, phase="RUNNING")
    task(root, "task-chat-q", intake_ago=300, phase="QUEUED")
    snap = uat.snapshot(root, 120, now=NOW, holders=SESSION)
    t = by_id(snap)
    check(t["task-chat-run"]["claimed"] and t["task-chat-run"]["unclaimed_age_s"] is None,
          "a RUNNING task is claimed and has no unclaimed age")
    check(snap["oldest_unclaimed_task"] == "task-chat-q", "a QUEUED task still counts as unclaimed")
    check(snap["unanswered_count"] == 2, "a claimed task with no result is still unanswered (same as text mode)")

with tempfile.TemporaryDirectory() as d:
    root = Path(d)
    task(root, "task-chat-a", intake_ago=600)
    before = uat.snapshot(root, 120, now=NOW, holders=SESSION)["oldest_unclaimed_age_s"]
    task(root, "task-chat-b", intake_ago=10, result="the reply to b\n")
    snap = uat.snapshot(root, 120, now=NOW, holders=SESSION)
    check(snap["oldest_unclaimed_age_s"] == before and snap["oldest_unclaimed_task"] == "task-chat-a",
          "an unrelated task's reply never resets another task's pending age")
    check(by_id(snap)["task-chat-b"]["answered"] and by_id(snap)["task-chat-b"]["unclaimed_age_s"] is None,
          "the answered task itself drops out of the unclaimed figure")

with tempfile.TemporaryDirectory() as d:
    root = Path(d)
    task(root, "task-chat-w", intake_ago=600)
    real = uat._holder
    uat._holder = lambda ws, tid: "worker-2"
    try:
        snap = uat.snapshot(root, 120, now=NOW, holders=SESSION)
    finally:
        uat._holder = real
    t = by_id(snap)["task-chat-w"]
    check(t["claimed"] and t["holder"] == "worker-2" and snap["oldest_unclaimed_task"] is None,
          "a task a worker holds is claimed, as in text mode it is not the core's")

# --- A claimed or assigned file is looked up under its canonical id ---------------------------------
CANON = "task-1792000000000"


def renamed(root, name, *, intake_ago=600, declare=True, **kw):
    task(root, CANON, intake_ago=intake_ago, **kw)
    src = root / "tasks" / f"{CANON}.txt"
    if not declare:
        src.write_text(src.read_text().replace(f"id: {CANON}\n", ""))
    src.rename(root / "tasks" / name)


for name in (f"{CANON}.claimed-core-2.txt", f"{CANON}.assigned-inst-a.txt"):
    for declare in (True, False):
        how = f"{name.split('.')[1]}, id {'declared' if declare else 'from the filename'}"
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            renamed(root, name, declare=declare, phase="RUNNING")
            snap = uat.snapshot(root, 120, now=NOW, holders=SESSION)
            t = snap["tasks"][0]
            check(t["task_id"] == CANON and t["phase"] == "RUNNING" and t["claimed"]
                  and snap["oldest_unclaimed_task"] is None,
                  f"{how}: RUNNING activity stored under the canonical id is found; not oldest-unclaimed")
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            renamed(root, name, declare=declare, result="the reply\n")
            snap = uat.snapshot(root, 0, now=NOW, holders=SESSION)
            t = snap["tasks"][0]
            check(t["answered"] and not t["unanswered"] and snap["unanswered_count"] == 0
                  and snap["oldest_unclaimed_task"] is None,
                  f"{how}: a result under the canonical id answers it")
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            renamed(root, name, declare=declare)
            seen, real = [], uat._holder
            uat._holder = lambda ws, tid: (seen.append(tid), "worker-2" if tid == CANON else None)[1]
            try:
                snap = uat.snapshot(root, 120, now=NOW, holders=SESSION)
            finally:
                uat._holder = real
            check(seen == [CANON] and snap["tasks"][0]["claimed"] and snap["oldest_unclaimed_task"] is None,
                  f"{how}: the worker lookup uses the canonical id")

with tempfile.TemporaryDirectory() as d:
    root = Path(d)
    renamed(root, f"{CANON}.claimed-core-2.txt", phase="RUNNING")
    stem = Path(f"{CANON}.claimed-core-2.txt").stem
    check(uat._phase(root, stem) is None and uat._phase(root, CANON) == "RUNNING",
          "CONTROL: a Path.stem lookup misses the RUNNING activity the canonical id finds")

# --- A slug id with no timestamp has no immutable intake ---------------------------------------------
with tempfile.TemporaryDirectory() as d:
    root = Path(d)
    (root / "tasks").mkdir(); (root / "results").mkdir()
    f = root / "tasks" / "task-plain.txt"
    f.write_text("id: task-plain\nsource: ag2space\ntask: hello\n")
    os.utime(f, (NOW - 900, NOW - 900))
    before = uat.snapshot(root, 120, now=NOW, holders=SESSION)
    os.utime(f, (NOW, NOW))  # rewritten: the mtime is now
    after = uat.snapshot(root, 120, now=NOW, holders=SESSION)
    for label, snap in (("before a rewrite", before), ("after a rewrite", after)):
        t = snap["tasks"][0]
        check(t.get("intake_known") is False and t["age_s"] is None and t["unclaimed_age_s"] is None
              and snap.get("unclaimed_intake_unknown") == ["task-plain"],
              f"slug id, no timestamp, {label}: intake is reported unknown, never fresh")
    check(after["tasks"][0]["mtime_age_s"] == 0.0,
          "CONTROL: an mtime anchor would have reset the same task's age to 0 on rewrite")

# --- Who holds the inbox ---------------------------------------------------------------------------
with tempfile.TemporaryDirectory() as d:
    root = Path(d)
    task(root, "task-chat-a", intake_ago=600)
    for label, holders, want in [
        ("only the untagged standby", STANDBY, True),
        ("only a tagged standby", Holders(True, [(5, "standby")], 0), True),
        ("a session watcher beside the standby", SESSION, False),
        ("nobody", Holders(True, [], 0), False),
        ("an unreadable process table", Holders(False, [], 0), None),
        ("a standby plus an undecided line", Holders(True, [(5, "standby")], 1), None),
    ]:
        snap = uat.snapshot(root, 120, now=NOW, holders=holders)
        check(snap["standby_only"] is want and snap["inbox"]["standby_only"] is want,
              f"standby_only is {want} with {label}")
    check(uat.snapshot(root, 120, now=NOW, holders=STANDBY)["inbox"]["state"] == "standby_only",
          "the inbox state is the policy's own classification")


# --- CLI: --json prints one object, with the text mode's exit codes -------------------------------
def run_main(argv, holders=SESSION):
    buf, old_argv, real = io.StringIO(), sys.argv, uat._inbox_view
    sys.argv = ["unanswered-tasks.py"] + argv
    uat._inbox_view = lambda inbox, h=None: real(inbox, holders)
    try:
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(io.StringIO()):
            rc = uat.main()
    finally:
        sys.argv, uat._inbox_view = old_argv, real
    return rc, buf.getvalue()


with tempfile.TemporaryDirectory() as d:
    root = Path(d)
    task(root, "task-chat-a", intake_ago=600)
    os.utime(root / "tasks" / "task-chat-a.txt", None)
    rc_text, _ = run_main(["--workspace", str(root), "--min-age-sec", "0"])
    rc_json, out = run_main(["--workspace", str(root), "--min-age-sec", "0", "--json"])
    snap = json.loads(out)
    check(rc_text == rc_json == 1, "--json exits 1 on an unanswered task, exactly as text mode does")
    check(snap["unanswered_count"] == 1 and snap["tasks"][0]["unanswered"],
          "--json names the unanswered task and why")

with tempfile.TemporaryDirectory() as d:
    root = Path(d)
    task(root, "task-chat-a", intake_ago=600, result="done\n")
    rc_text, _ = run_main(["--workspace", str(root)])
    rc_json, out = run_main(["--workspace", str(root), "--json"])
    check(rc_text == rc_json == 0 and json.loads(out)["unanswered_count"] == 0,
          "--json exits 0 when every task is answered, exactly as text mode does")

with tempfile.TemporaryDirectory() as d:
    root = Path(d)
    task(root, "task-chat-a", intake_ago=600)
    real = uat._holder

    def unreadable(ws, tid):
        raise PermissionError("deliveries/")
    uat._holder = unreadable
    try:
        rc, out = run_main(["--workspace", str(root), "--json"])
    finally:
        uat._holder = real
    check(rc == 2 and "error" in json.loads(out), "--json exits 2 with an error object when holders are unreadable")

_saved = sys.modules.get("status_ack_policy")
sys.modules["status_ack_policy"] = None
try:
    with tempfile.TemporaryDirectory() as d:
        rc, out = run_main(["--workspace", d, "--json"])
        check(rc == 2 and "policy owner" in json.loads(out)["error"],
              "--json exits 2, not 1, when the policy owner is unimportable")
finally:
    if _saved is None:
        sys.modules.pop("status_ack_policy", None)
    else:
        sys.modules["status_ack_policy"] = _saved

# An empty queue must not hide a broken grammar owner: exit 2, never a healthy-looking 0.
_ds = uat.dedup_soundness
_saved_markers, _had = _ds._MARKERS, "result_markers" in sys.modules
_saved_rm = sys.modules.get("result_markers")
_ds._MARKERS = None
sys.modules["result_markers"] = None
try:
    with tempfile.TemporaryDirectory() as d:
        (Path(d) / "tasks").mkdir()
        rc, out = run_main(["--workspace", d, "--json"])
        check(rc == 2 and "result_markers" in json.loads(out)["error"],
              "--json on an EMPTY queue exits 2 when result_markers.py is unimportable")
finally:
    if _had:
        sys.modules["result_markers"] = _saved_rm
    else:
        sys.modules.pop("result_markers", None)
    _ds._MARKERS = _saved_markers
with tempfile.TemporaryDirectory() as d:
    (Path(d) / "tasks").mkdir()
    rc, out = run_main(["--workspace", d, "--json"])
    check(rc == 0 and json.loads(out)["unanswered_count"] == 0,
          "CONTROL: the same empty queue exits 0 once the grammar owner is importable again")

print(f"\nunanswered-tasks-json: {PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
