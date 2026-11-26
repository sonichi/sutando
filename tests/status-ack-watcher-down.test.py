#!/usr/bin/env python3
"""Watcher down, end to end: only a standby holds the inbox -> one owner DM naming the remedy,
at most once per outage, asserted as delivered events at the fake gateway.

  a) standby only, under the threshold -> no alert
  b) at the threshold -> one owner-DM POST that names the remedy; its receipt is recorded
  c) many more passes in the same outage -> still one alert
  d) the room line for a task due after the alert's receipt says the owner was told; one due
     before it does not
  e) a session watcher returns, then a new standby-only outage -> exactly one more alert
  f) an unreadable holder table neither starts nor ends an outage
  CONTROL: a session watcher throughout -> no alert ever

Run: python3 tests/status-ack-watcher-down.test.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _helpers.hermetic_gateway import isolate_then_import  # noqa: E402

gb, _IMPORT_READS = isolate_then_import(env={"REMOTE_PROACTIVE_ROOM": ""})
from _helpers import proactive_lane as pl  # noqa: E402
from _helpers import status_ack_env as sa  # noqa: E402

import watcher_identity  # noqa: E402

ISO = pl.scratch("status-ack-watcher-down-")
check = pl.Checks()
mod = sa.runner()
UNREAD = watcher_identity.InboxHolders(False, [], 0)


def owner_posts(lane) -> list:
    return [p for p in lane.posts if p.get("room_id") == pl.OWNER_DM]


def tick(lane, now, holders):
    r = mod.run_once(lane.ws, sa.on(mod), now=now, holders=holders)
    lane.drain()
    return r


def main() -> int:
    lane = pl.Lane(gb, ISO)
    after = sa.on(mod).watcher_down_after_s
    sa.task(lane.ws, "task-1761300000001")
    t = sa.T0 + 10
    tick(lane, t, sa.STANDBY)
    tick(lane, t + after - 1, sa.STANDBY)
    check(owner_posts(lane) == [], f"a) under the threshold: no alert ({owner_posts(lane)})")

    r = tick(lane, t + after, sa.STANDBY)
    dm = owner_posts(lane)
    check(len(dm) == 1 and "restart the core" in dm[0].get("body", "") and "standby" in dm[0].get("body", ""),
          f"b) one owner-DM alert naming the remedy ({dm})")
    check(bool(dm) and "thread_root" not in dm[0], "b) the alert is a top-level DM line")
    tick(lane, t + after + 15, sa.STANDBY)
    check(mod.owner_alerted(lane.ws), "b) the alert's delivery receipt is recorded")

    for i in range(2, 30):
        tick(lane, t + after + 15 * i, sa.STANDBY)
    check(len(owner_posts(lane)) == 1, f"c) still one alert after 28 more passes ({len(owner_posts(lane))})")

    room = [p for p in lane.posts if p.get("room_id") == sa.ROOM]
    check(len(room) == 1 and "owner has been told" not in room[0]["body"] and "not reading" in room[0]["body"],
          f"d) the line sent with the alert still unconfirmed does not claim the owner was told ({room})")
    sa.task(lane.ws, "task-1761300000500")
    tick(lane, sa.T0 + 500 + 125, sa.STANDBY)
    room = [p for p in lane.posts if p.get("room_id") == sa.ROOM]
    check(len(room) == 2 and room[-1]["body"].endswith("The owner has been told."),
          f"d) a line due after the receipt says the owner was told ({room[-1:] if room else room})")

    t2 = sa.T0 + 2000
    tick(lane, t2, sa.SESSION)
    tick(lane, t2 + 15, UNREAD)
    tick(lane, t2 + 30, sa.STANDBY)
    tick(lane, t2 + 30 + after - 1, UNREAD)
    check(len(owner_posts(lane)) == 1, "f) an unreadable holder table neither ends nor advances an outage alone")
    tick(lane, t2 + 30 + after, sa.STANDBY)
    tick(lane, t2 + 30 + after + 600, sa.STANDBY)
    check(len(owner_posts(lane)) == 2, f"e) a new outage after the session returned: one more alert ({len(owner_posts(lane))})")

    ctl = pl.Lane(gb, ISO)
    sa.task(ctl.ws, "task-1761300000001")
    for i in range(40):
        tick(ctl, sa.T0 + 15 * i, sa.SESSION)
    check(owner_posts(ctl) == [], "CONTROL: a session watcher throughout: no alert")
    return check.exit_code()


if __name__ == "__main__":
    sys.exit(main())
