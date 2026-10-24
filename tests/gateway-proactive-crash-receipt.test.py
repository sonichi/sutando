#!/usr/bin/env python3
"""A process that dies around a proactive send never makes it post twice.

A child process drains one proactive file and is killed (os._exit) at one point:

  a) before-receipt: the POST left, the process died before reading the answer
     -> after the restart the item is parked outcome-unknown, nothing is sent again
  b) after-receipt: the event id was recorded, the process died before completing
     -> after the restart the item is delivered with that event id, nothing is sent again

  c) before-receipt, and the owner DM changes before the restart
     -> still the one parked item: no POST to the new DM, no second item left READY

The claim of the dead child is honoured until its reclaim window ends: a drain inside the
window leaves the file pending, never lost and never sent.

Run: python3 tests/gateway-proactive-crash-receipt.test.py
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _helpers.hermetic_gateway import isolate_then_import  # noqa: E402

gb, _IMPORT_READS = isolate_then_import(env={"REMOTE_PROACTIVE_ROOM": ""})
HERE = Path(__file__).resolve().parent
from _helpers import proactive_lane as pl  # noqa: E402
from _helpers.hermetic_env import isolated_child_env  # noqa: E402

ISO = pl.scratch("gw-proactive-crash-")
check = pl.Checks()

CHILD = r"""
import os, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from _helpers.hermetic_gateway import isolate_then_import
gb, _reads = isolate_then_import(env={"REMOTE_PROACTIVE_ROOM": ""})
from _helpers import proactive_lane as pl
iso = pl.scratch("gw-proactive-crash-child-")
stage, ws, posts = sys.argv[2], Path(sys.argv[3]), Path(sys.argv[4])

def respond(payload):
    posts.write_text(posts.read_text() + "x" if posts.exists() else "x")
    if stage == "before-receipt":
        os._exit(9)
    return {"ok": True, "event_id": "$landed"}

if stage == "after-receipt":
    real = gb._record_proactive_receipt
    def record_then_die(*a):
        real(*a)
        os._exit(9)
    gb._record_proactive_receipt = record_then_die
pl.Lane(gb, iso, respond, ws=ws).drain()
os._exit(0)
"""


def scenario(stage: str, want_status: str, want_where: str, new_dm: "str | None" = None) -> None:
    lane = pl.Lane(gb, ISO)
    lane.write("proactive-1.txt", "status for the owner\n")
    posts = lane.ws / "child-posts"
    env = isolated_child_env()
    rc = subprocess.run([sys.executable, "-c", CHILD, str(HERE), stage, str(lane.ws), str(posts)],
                        env=env, capture_output=True, text=True, timeout=120)
    check(rc.returncode == 9, f"{stage}: the child died mid-drain (exit {rc.returncode}) {rc.stderr[-300:]}")
    check(posts.exists() and posts.read_text() == "x", f"{stage}: the child posted once")
    lane.restart()
    if new_dm:
        stage, lane.owner_dm = f"{stage} + DM change", new_dm
    lane.drain()
    check(lane.posts == [] and lane.where("proactive-1") == "pending",
          f"{stage}: inside the dead claim's window nothing is sent and the file waits "
          f"({len(lane.posts)} POSTs, {lane.where('proactive-1')})")
    backend = lane.backend()
    if backend is not None:
        backend.reclaim_ttl_s = 0.0
    for _ in range(3):
        lane.drain()
    items = lane.items()
    check(len(items) == 1, f"{stage}: one outbox item, none left READY ({[(i.get('item_id'), i.get('status')) for i in items]})")
    item = items[0] if len(items) == 1 else {}
    check(lane.posts == [], f"{stage}: no second POST after the restart ({len(lane.posts)})")
    check(item.get("status") == want_status, f"{stage}: the item is {want_status} ({item.get('status')}, {item.get('reason')})")
    check(lane.where("proactive-1") == want_where, f"{stage}: the file is {want_where} ({lane.where('proactive-1')})")
    if want_status == "DELIVERED":
        check(item.get("provider_ref") == "$landed" and item.get("destination") == pl.OWNER_DM,
              f"{stage}: the receipt keeps the event id and the room ({item.get('provider_ref')}, {item.get('destination')})")
    else:
        check(item.get("reason") == "outcome-unknown", f"{stage}: parked outcome-unknown ({item.get('reason')})")


def main() -> int:
    scenario("before-receipt", "PARKED", "undelivered")
    scenario("after-receipt", "DELIVERED", "archived")
    scenario("before-receipt", "PARKED", "undelivered", new_dm="!RepointedOwnerDm:ag2.space")
    return check.exit_code()


if __name__ == "__main__":
    sys.exit(main())
