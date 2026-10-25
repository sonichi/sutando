#!/usr/bin/env python3
"""The initial ack is withdrawn at dispatch when the task is picked up, or its card or a progress
notice appears, after the runner published the ack and before the gateway drains it.

The runner publishes; the change lands; then the gateway's real proactive drain runs with the
host's real dispatch, ack and route gates. Assertions are at the fake room endpoint.
  a) picked up (the task file renamed to a claimed state) -> no POST, the claim is released, and
     the next pass says nothing either
  b) an activity card appears -> no POST       c) a progress notice goes out -> no POST
  d) the ack gate is ack-specific: after pickup, a later progress line about the same task is
     still delivered while the ack is withdrawn
  CONTROL: nothing changes before the drain -> the ack is delivered

Run: python3 tests/status-ack-dispatch-overlap.test.py
"""
from __future__ import annotations

import functools
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _helpers.hermetic_gateway import isolate_then_import  # noqa: E402

gb, _IMPORT_READS = isolate_then_import(env={"REMOTE_PROACTIVE_ROOM": ""})
from _helpers import proactive_lane as pl  # noqa: E402
from _helpers import status_ack_env as sa  # noqa: E402

import ack_claim  # noqa: E402
import proactive_dispatch_gate as gate  # noqa: E402

import proactive_routing  # noqa: E402

ISO = pl.scratch("status-ack-dispatch-overlap-")
check = pl.Checks()
mod = sa.runner()
TID = "task-1761300000001"
KEY = f"ack:{TID}:initial"
DUE = sa.T0 + 10_000


def drain(lane):
    ack = getattr(gate, "initial_ack_veto", None)
    lane.drain(gate=functools.partial(gate.dispatch_veto, workspace=lane.ws),
               ack_gate=functools.partial(ack, workspace=lane.ws) if ack else None,
               route_gate=functools.partial(gate.route_veto, workspace=lane.ws))


def pick_up(lane):
    p = lane.tasks / f"{TID}.txt"
    p.rename(p.with_name(f"{TID}.claimed-core-1.txt"))


def card(lane):
    sa.activity(lane.ws, TID, "QUEUED")


def progress(lane):
    ack_claim.claim(lane.ws, f"progress:{TID}:first-notify", "task-progress")


def published_then(change):
    lane = pl.Lane(gb, ISO)
    sa.task(lane.ws, TID)
    r = mod.run_once(lane.ws, sa.on(mod), now=DUE, holders=sa.SESSION)
    assert r.spoken == [TID], r
    if change is not None:
        change(lane)
    drain(lane)
    return lane


def main() -> int:
    lane = published_then(None)
    check(len(sa.room_posts(lane)) == 1, f"CONTROL: no change before the drain -> the ack is delivered ({lane.posts})")

    for label, change in (("a) picked up", pick_up), ("b) a card appears", card), ("c) a progress notice", progress)):
        lane = published_then(change)
        check(lane.posts == [], f"{label} after publication -> no POST ({lane.posts})")
        mod.run_once(lane.ws, sa.on(mod), now=DUE + 15, holders=sa.SESSION)
        drain(lane)
        mod.run_once(lane.ws, sa.on(mod), now=DUE + 30, holders=sa.SESSION)
        drain(lane)
        check(lane.posts == [] and ack_claim.state(lane.ws, KEY) == "none",
              f"{label}: the withdrawn ack is released and never re-sent ({ack_claim.read(lane.ws, KEY)})")

    lane = pl.Lane(gb, ISO)
    sa.task(lane.ws, TID)
    mod.run_once(lane.ws, sa.on(mod), now=DUE, holders=sa.SESSION)
    pick_up(lane)
    name = proactive_routing.about_task_filename(TID, int((DUE + 5) * 1000), channel="ag2space")
    lane.write(name, f"[channel: {sa.ROOM}]\n[thread: {sa.MSG_EVT}]\nStill on it: reading the logs.\n")
    drain(lane)
    bodies = [p.get("body") for p in sa.room_posts(lane)]
    check(bodies == ["Still on it: reading the logs."],
          f"d) a progress line after pickup is delivered; only the ack is withdrawn ({bodies})")
    return check.exit_code()


if __name__ == "__main__":
    sys.exit(main())
