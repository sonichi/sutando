#!/usr/bin/env python3
"""A message about a task is re-checked at the moment of its send, after the claim.

Each case lets the outbox claim a `proactive-about-<task>--<ts>.txt` file and then lands
one change before the send, through the host's dispatch gate:

  a) the task's result is written   -> no POST, withdrawn
  b) a CANCEL_INSTRUCTION arrives   -> no POST, withdrawn
  c) the task goes WAITING          -> no POST, withdrawn
  d) the task ends (COMPLETED)      -> no POST, withdrawn
  e) a gate that raises             -> no POST: an unchecked task is never messaged
  f) control: nothing lands         -> posted once
  g) the task is archived/removed after publication -> no POST, withdrawn

Run: python3 tests/gateway-proactive-dispatch-revalidate.test.py
"""
from __future__ import annotations

import functools
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from _helpers.hermetic_gateway import isolate_then_import  # noqa: E402

gb, _IMPORT_READS = isolate_then_import(env={"REMOTE_PROACTIVE_ROOM": ""})
from _helpers import proactive_lane as pl  # noqa: E402

ISO = pl.scratch("gw-proactive-revalidate-")
import proactive_dispatch_gate  # noqa: E402

check = pl.Checks()
TID = "task-1761300000000"


def land_result(lane):
    (lane.results / f"{TID}.txt").write_text("the answer\n", encoding="utf-8")


def land_cancel(lane):
    (lane.tasks / "task-1761300000999.txt").write_text(
        f"id: task-1761300000999\ntask: CANCEL_INSTRUCTION: stop processing {TID}\n", encoding="utf-8")


def land_phase(phase):
    def land(lane):
        d = lane.ws / "state" / "activity"
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{TID}.json").write_text(json.dumps({"task_id": TID, "phase": phase}), encoding="utf-8")
    return land


def land_archived(lane):
    for f in lane.tasks.glob("task-*"):
        f.unlink()


def scenario(label: str, land, gate=None, want_post: bool = False) -> None:
    lane = pl.Lane(gb, ISO)
    (lane.tasks / f"{TID}.claimed-core-2.txt").write_text(f"id: {TID}\ntask: hi\n", encoding="utf-8")
    lane.write(f"proactive-about-{TID}--1761300000500.txt", "received, being worked on\n")
    if gate is None:
        gate = functools.partial(proactive_dispatch_gate.dispatch_veto, workspace=lane.ws)
    backend = lane.backend()
    if backend is not None and land is not None:
        real = backend.claim

        def claim_then_land(item_id, worker):
            token = real(item_id, worker)
            if token is not None:
                land(lane)
            return token
        backend.claim = claim_then_land
    elif land is not None:
        land(lane)  # a bridge with no dispatch-time check: the change is there all along
    lane.drain(gate=gate)
    stem = f"proactive-about-{TID}--1761300000500"
    if want_post:
        check(len(lane.posts) == 1 and lane.where(stem) == "archived",
              f"{label}: posted once ({len(lane.posts)}, {lane.where(stem)})")
        return
    items = lane.items()
    reason = str((items[0] if items else {}).get("reason") or "")
    check(lane.posts == [], f"{label}: no POST ({len(lane.posts)})")
    check(lane.where(stem) == "archived" and reason.startswith("withdrawn:")
          and items[0].get("status") == "PARKED" and "provider_ref" not in items[0],
          f"{label}: withdrawn, its outbox item parked with no receipt ({lane.where(stem)}, {reason!r})")
    lane.drain(gate=gate)
    check(lane.posts == [], f"{label}: a later pass does not send it either")


def broken(task_id):
    raise OSError("activity store unreadable")


def main() -> int:
    scenario("a) result", land_result)
    scenario("b) cancel", land_cancel)
    scenario("c) waiting", land_phase("WAITING"))
    scenario("d) completed", land_phase("COMPLETED"))
    scenario("e) gate raises", lambda lane: None, gate=broken)
    scenario("f) control", None, want_post=True)
    scenario("g) archived after publication", land_archived)
    check("left the queue" in str(proactive_dispatch_gate.dispatch_veto(TID, ISO / "nowhere")),
          "the gate withdraws a message about a task that is not in the queue")
    return check.exit_code()


if __name__ == "__main__":
    sys.exit(main())
