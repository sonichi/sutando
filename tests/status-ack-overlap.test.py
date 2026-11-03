#!/usr/bin/env python3
"""status-ack speaks only for an unpicked task: never beside an activity card or a progress notice.

Each case runs the real runner pass and the gateway's real proactive drain; the assertion is at
the fake room endpoint.
  a) a task whose card shows "queued" gets no line          CONTROL: the same task with no card does
  b) a task a session picked up (card RUNNING) gets no line
  c) a task with a sent progress notice (task-progress's sent marker) gets no line
  d) a card that appears between the decision and the publish withdraws the line and frees the claim
  e) watcher down with a queued card: the owner DM still goes out, the room gets no line
  f) an activity snapshot that cannot be read counts as a card

Run: python3 tests/status-ack-overlap.test.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _helpers.hermetic_gateway import isolate_then_import  # noqa: E402

gb, _IMPORT_READS = isolate_then_import(env={"REMOTE_PROACTIVE_ROOM": ""})
from _helpers import proactive_lane as pl  # noqa: E402
from _helpers import status_ack_env as sa  # noqa: E402

import ack_claim  # noqa: E402

ISO = pl.scratch("status-ack-overlap-")
check = pl.Checks()
mod = sa.runner()
TID = "task-1761300000001"
DUE = sa.T0 + 10_000


def lane_with(*phases, holders=sa.SESSION, hook=None):
    lane = pl.Lane(gb, ISO)
    sa.task(lane.ws, TID)
    if phases:
        sa.activity(lane.ws, TID, *phases)
    r = mod.run_once(lane.ws, sa.on(mod), now=DUE, holders=holders, before_publish=hook)
    lane.drain()
    return lane, r


def main() -> int:
    lane, r = lane_with()
    check(len(sa.room_posts(lane)) == 1 and r.spoken == [TID], f"CONTROL: no card, no notice -> one line ({lane.posts})")

    lane, r = lane_with("QUEUED")
    check(sa.room_posts(lane) == [] and r.spoken == [] and "card" in r.skipped.get(TID, ""),
          f"a) a queued card -> no line ({r.skipped})")

    lane, r = lane_with("QUEUED", "RUNNING")
    check(sa.room_posts(lane) == [] and r.spoken == [] and "picked up" in r.skipped.get(TID, ""),
          f"b) picked up -> no line ({r.skipped})")

    lane = pl.Lane(gb, ISO)
    sa.task(lane.ws, TID)
    ack_claim.claim(lane.ws, f"progress:{TID}:sent", "task-progress")
    r = mod.run_once(lane.ws, sa.on(mod), now=DUE, holders=sa.SESSION)
    lane.drain()
    check(sa.room_posts(lane) == [] and "progress" in r.skipped.get(TID, ""),
          f"c) a progress notice -> no line ({r.skipped})")

    def card_lands(tid):
        sa.activity(lane_box[0].ws, tid, "QUEUED")

    lane_box = [pl.Lane(gb, ISO)]
    sa.task(lane_box[0].ws, TID)
    r = mod.run_once(lane_box[0].ws, sa.on(mod), now=DUE, holders=sa.SESSION, before_publish=card_lands)
    lane_box[0].drain()
    check(sa.room_posts(lane_box[0]) == [] and "card" in r.withdrawn.get(TID, "")
          and ack_claim.state(lane_box[0].ws, f"ack:{TID}:initial") == "none",
          f"d) a card landing before the publish withdraws it and frees the claim ({r.withdrawn})")

    lane = pl.Lane(gb, ISO)
    sa.task(lane.ws, TID)
    sa.activity(lane.ws, TID, "QUEUED")
    after = sa.on(mod).watcher_down_after_s
    for t in (DUE, DUE + after, DUE + after + 15):
        mod.run_once(lane.ws, sa.on(mod), now=t, holders=sa.STANDBY)
        lane.drain()
    owner = [p for p in lane.posts if p.get("room_id") == pl.OWNER_DM]
    check(len(owner) == 1 and sa.room_posts(lane) == [],
          f"e) watcher down beside a queued card: one owner DM, no room line ({lane.posts})")

    lane = pl.Lane(gb, ISO)
    sa.task(lane.ws, TID)
    (lane.ws / "state" / "activity").mkdir(parents=True, exist_ok=True)
    (lane.ws / "state" / "activity" / f"{TID}.json").write_text("{not json", encoding="utf-8")
    r = mod.run_once(lane.ws, sa.on(mod), now=DUE, holders=sa.SESSION)
    lane.drain()
    check(sa.room_posts(lane) == [], f"f) an unreadable activity snapshot counts as a card ({r.skipped})")
    return check.exit_code()


if __name__ == "__main__":
    sys.exit(main())
