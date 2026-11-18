#!/usr/bin/env python3
"""The status-ack runner's durable first-seen record, and the bounded record stores.

  a) a task with no intake stamp is anchored at its first sighting; after a restart (a fresh
     runner module) it gets exactly one line at that anchor's deadline, and none after
     CONTROL: without the record the same task never gets a line (it would be missed)
  b) the record is create-only: a later pass or restart never moves it
  c) an unreadable record is never overwritten and never anchors: the task fails closed
  d) prune drops a record only once its task left the queue and retention passed; past the
     cap the oldest such records go; a queued task's or an unreadable record stays; an
     unreadable tasks/ prunes nothing
  e) the runner's prune bounds ack claims the same way: a queued task keeps its claims, the
     current outage keeps its alert, an ended outage's alert and an archived task's go

Run: python3 tests/status-ack-first-seen.test.py
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _helpers.hermetic_gateway import isolate_then_import  # noqa: E402

gb, _IMPORT_READS = isolate_then_import(env={"REMOTE_PROACTIVE_ROOM": ""})
from _helpers import proactive_lane as pl  # noqa: E402
from _helpers import status_ack_env as sa  # noqa: E402

import ack_claim  # noqa: E402

ISO = pl.scratch("status-ack-first-seen-")
check = pl.Checks()
HEX = "task-dd3a13e1514613581a"
DAY = 86400.0


def fs_module():
    spec = importlib.util.spec_from_file_location(
        "first_seen_under_test", sa.REPO / "skills" / "status-ack" / "scripts" / "first_seen.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def passes(lane, start, n, step=15.0, record=True):
    """`n` passes, each by a freshly loaded runner (a restart), draining the gateway after each."""
    for i in range(n):
        mod = sa.runner()
        if not record:
            mod.first_seen.record = lambda ws, tid, now: None
        mod.run_once(lane.ws, sa.on(mod), now=start + i * step, holders=sa.SESSION)
        lane.drain()


def main() -> int:
    fs = fs_module()
    deadline = sa.on(sa.runner()).deadlines["normal"]
    t1 = sa.T0 + 5000

    lane = pl.Lane(gb, ISO)
    sa.task(lane.ws, HEX, timestamp=False)
    passes(lane, t1, 1)
    check(fs.read(lane.ws, HEX) == t1 and lane.posts == [], "a) first sighting recorded, nothing said yet")
    passes(lane, t1 + deadline - 15, 1)
    check(lane.posts == [], "a) a restart before the deadline says nothing")
    passes(lane, t1 + deadline, 6)
    check(len(sa.room_posts(lane)) == 1, f"a) one line at the anchor's deadline across restarts ({len(lane.posts)})")
    check(fs.read(lane.ws, HEX) == t1, "b) restarts never move the first-seen stamp")

    ctl = pl.Lane(gb, ISO)
    sa.task(ctl.ws, HEX, timestamp=False)
    passes(ctl, t1, 1, record=False)
    passes(ctl, t1 + deadline, 6, record=False)
    check(ctl.posts == [], "CONTROL (a): without the record the unstamped task is never acknowledged")

    ws = ISO / "unit"
    (ws / "tasks").mkdir(parents=True)
    check(fs.record(ws, HEX, 100.0) == 100.0 and fs.record(ws, HEX, 900.0) == 100.0, "b) create-only: the first stamp wins")
    real_read, calls = fs.read, []
    fs.read = lambda w, t: calls.append(t) or (None if len(calls) == 1 else real_read(w, t))
    check(fs.record(ws, HEX, 900.0) == 100.0, "b) losing the create race returns the winner's stamp")
    fs.read = real_read
    (fs.store_dir(ws) / "task-other1.json").write_text(json.dumps({"task_id": "task-else", "first_seen": 1.0}))
    check(fs.read(ws, "task-other1") == fs.UNREADABLE, "c) a record naming another task is unreadable")
    try:
        fs.record(ws, "../escape", 1.0)
        refused = False
    except ValueError:
        refused = True
    check(refused, "b) a name that is not a task id is refused")
    bad = fs.store_dir(ws) / "task-unreadable1.json"
    bad.write_text("{oops", encoding="utf-8")
    check(fs.record(ws, "task-unreadable1", 5.0) == fs.UNREADABLE and bad.read_text() == "{oops",
          "c) an unreadable record is reported, never overwritten")

    lane = pl.Lane(gb, ISO)
    sa.task(lane.ws, HEX, timestamp=False)
    fs.store_dir(lane.ws).mkdir(parents=True, exist_ok=True)
    (fs.store_dir(lane.ws) / f"{HEX}.json").write_text("{oops", encoding="utf-8")
    passes(lane, t1 + 10 * deadline, 3)
    check(lane.posts == [], "c) an unreadable record and no other stamp: the task fails closed")

    ws = ISO / "prune"
    (ws / "tasks").mkdir(parents=True)
    now = 100 * DAY
    for i, tid in enumerate(["task-old-a", "task-old-b", "task-young-c", "task-young-d", "task-queued-e"]):
        fs.record(ws, tid, now - (30 * DAY if "old" in tid or "queued" in tid else (5 - i)))
    (ws / "tasks" / "task-queued-e.txt").write_text("id: task-queued-e\ntask: x\n")
    (fs.store_dir(ws) / "task-broken-f.json").write_text("{oops")
    (fs.store_dir(ws) / "task-bad..name.json").write_text(json.dumps({"task_id": "task-bad..name", "first_seen": 1.0}))
    queued = lambda tid: (ws / "tasks" / f"{tid}.txt").exists()  # noqa: E731
    gone = fs.prune(ws, queued, now)
    left = sorted(p.stem for p in fs.store_dir(ws).glob("*.json"))
    check(sorted(gone) == ["task-old-a", "task-old-b"] and "task-queued-e" in left and "task-broken-f" in left
          and "task-bad..name" in left,
          f"d) past retention and archived -> pruned; queued and unreadable kept ({gone}, {left})")
    gone = fs.prune(ws, queued, now, max_records=4)
    check(gone == ["task-young-c"], f"d) past the cap the oldest archived record goes first ({gone})")
    ws2 = ISO / "no-tasks"
    fs.record(ws2, "task-old-z", 0.0)
    check(sa.runner().prune(ws2, now)["first_seen"] == [], "d) an unreadable tasks/ prunes nothing")

    mod = sa.runner()
    ws = ISO / "claims"
    (ws / "tasks").mkdir(parents=True)
    (ws / "tasks" / "task-1761300000009.txt").write_text("id: task-1761300000009\ntask: x\n")
    old = now - 30 * DAY
    for key in ("ack:task-1761300000009:initial", "progress:task-1761300000009:first-notify",
                "ack:task-1761300000008:initial", "progress:task-1761300000008:first-notify",
                "alert:outage-1000:watcher-down", "alert:outage-2000:watcher-down"):
        ack_claim.claim(ws, key, "status-ack", now=old)
    ack_claim.update(ws, "ack:task-1761300000008:initial", "status-ack", "unknown")
    import os
    os.utime(ack_claim.claims_dir(ws) / "ack+task-1761300000008+initial.lock", (old, old))
    ack_claim.claim(ws, "note:task-1761300000008:other", "someone", now=old)
    (ack_claim.claims_dir(ws) / "ack+task-1761300000007+initial.json").write_text("{oops")
    (ack_claim.claims_dir(ws) / "ack+task-1761300000006+initial.json").write_text(
        json.dumps({"id": "ack+task-1761300000006+initial", "key": "ack:task-1761300000006:initial", "state": "claimed"}))
    (ws / "state" / "status-ack").mkdir(parents=True, exist_ok=True)
    (ws / "state" / "status-ack" / "watcher-outage.json").write_text(json.dumps({"since": 2000}))
    gone = sorted(mod.prune(ws, now)["claims"])
    left = sorted(p.name for p in ack_claim.claims_dir(ws).iterdir())
    check(gone == ["ack:task-1761300000008:initial", "alert:outage-1000:watcher-down",
                   "lock:ack+task-1761300000008+initial", "progress:task-1761300000008:first-notify"],
          f"e) archived task's and ended outage's claims pruned ({gone})")
    check(not any(n.endswith(".json") and (("task-1761300000008" in n and not n.startswith("note")) or "outage-1000" in n)
                  for n in left) and "ack+task-1761300000008+initial.lock" not in left
          and ack_claim.state(ws, "ack:task-1761300000009:initial") == "claimed"
          and ack_claim.state(ws, "alert:outage-2000:watcher-down") == "claimed"
          and ack_claim.state(ws, "note:task-1761300000008:other") == "claimed"
          and "ack+task-1761300000007+initial.json" in left and "ack+task-1761300000006+initial.json" in left,
          f"e) the records go, and an old orphaned lock; queued task, current outage, foreign, unreadable and undated stay ({left})")
    check(mod.prune(ws, old + 60)["claims"] == [], "e) nothing is pruned before retention")
    return check.exit_code()


if __name__ == "__main__":
    sys.exit(main())
