#!/usr/bin/env python3
"""The runner re-checks a task right before it publishes, and speaks only for an attested origin.

Publish-time (each lands after the policy said speak and the claim was won, before publish):
  a) a result      b) a no-send result      c) WAITING      d) a cancel      e) the task archived
  each -> no proactive file, and the claim is released so nothing is recorded as sent
  CONTROL: nothing lands -> exactly one file
After publish, before the gateway's send:
  f) a result lands -> the outbox withdraws it, no POST, and the runner releases its claim
Origin (no fallback room, thread or DM):
  g) an unstamped task, and a stamped task whose room was edited afterwards -> nothing
  h) a stamped Discord / Telegram task -> nothing (no attested Matrix room)
  i) a stamped task whose room is not a Matrix room id -> nothing, and no DM either
  CONTROL: the same stamped AG2 Space task -> one line in its own room

Run: python3 tests/status-ack-suppression.test.py
"""
from __future__ import annotations

import functools
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _helpers.hermetic_gateway import isolate_then_import  # noqa: E402

gb, _IMPORT_READS = isolate_then_import(env={"REMOTE_PROACTIVE_ROOM": ""})
from _helpers import proactive_lane as pl  # noqa: E402
from _helpers import status_ack_env as sa  # noqa: E402

import ack_claim  # noqa: E402
from proactive_dispatch_gate import dispatch_veto  # noqa: E402

ISO = pl.scratch("status-ack-suppression-")
check = pl.Checks()
mod = sa.runner()
NOW = sa.T0 + 130


def phase(ws: Path, tid: str, value: str) -> None:
    d = ws / "state" / "activity"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{tid}.json").write_text(json.dumps({"task_id": tid, "phase": value}), encoding="utf-8")


LANDINGS = {
    "a) a result": lambda ws, tid: (ws / "results" / f"{tid}.txt").write_text("the answer\n"),
    "b) a no-send result": lambda ws, tid: (ws / "results" / f"{tid}.txt").write_text("[no-send]\n"),
    "c) WAITING": lambda ws, tid: phase(ws, tid, "WAITING"),
    "d) a cancel": lambda ws, tid: (ws / "tasks" / "task-1761300009999.txt").write_text(
        f"id: task-1761300009999\ntask: CANCEL_INSTRUCTION: stop processing {tid}\n"),
    "e) the task archived": lambda ws, tid: (ws / "tasks" / f"{tid}.txt").unlink(),
}


def publish_time(landing) -> tuple:
    lane = pl.Lane(gb, ISO)
    tid = "task-1761300000001"
    sa.task(lane.ws, tid, thread_root=sa.ROOT_EVT)
    hook = (lambda t: landing(lane.ws, t)) if landing else None
    r = mod.run_once(lane.ws, sa.on(mod), now=NOW, holders=sa.SESSION, before_publish=hook)
    return lane, r, sa.proactive_files(lane.ws), ack_claim.state(lane.ws, f"ack:{tid}:initial")


def origin(**kw) -> tuple:
    lane = pl.Lane(gb, ISO)
    tf = sa.task(lane.ws, "task-1761300000001", **kw)
    return lane, tf


def spoken(lane) -> list:
    mod.run_once(lane.ws, sa.on(mod), now=NOW, holders=sa.SESSION)
    lane.drain()
    return lane.posts


def main() -> int:
    for label, landing in LANDINGS.items():
        _, r, files, state = publish_time(landing)
        check(files == [] and state == "none" and r.withdrawn,
              f"{label} between decide and publish -> nothing published, claim released "
              f"({files}, {state}, {r.withdrawn})")
    _, r, files, state = publish_time(None)
    check(len(files) == 1 and state == "claimed", f"CONTROL: nothing lands -> one file, claim held ({files}, {state})")

    lane = pl.Lane(gb, ISO)
    tid = "task-1761300000001"
    sa.task(lane.ws, tid, thread_root=sa.ROOT_EVT)
    mod.run_once(lane.ws, sa.on(mod), now=NOW, holders=sa.SESSION)
    (lane.results / f"{tid}.txt").write_text("the answer\n", encoding="utf-8")
    lane.drain(gate=functools.partial(dispatch_veto, workspace=lane.ws))
    r = mod.run_once(lane.ws, sa.on(mod), now=NOW + 15, holders=sa.SESSION)
    check(lane.posts == [] and ("ack:%s:initial" % tid, "released") in r.settled
          and ack_claim.state(lane.ws, f"ack:{tid}:initial") == "none",
          f"f) answered after publish -> withdrawn at dispatch, no POST, claim released ({lane.posts}, {r.settled})")

    lane, _ = origin(stamped=False)
    check(spoken(lane) == [], "g) an unstamped task gets nothing")
    lane, tf = origin()
    tf.write_text(tf.read_text().replace(sa.ROOM, "!OtherRoomQq:ag2.space"), encoding="utf-8")
    check(spoken(lane) == [], "g) a stamped task whose room was edited afterwards gets nothing")
    for src in ("discord", "telegram"):
        lane, _ = origin(source=src)
        check(spoken(lane) == [], f"h) a stamped {src} task gets nothing")
    lane, _ = origin(room="123456789012345678")
    check(spoken(lane) == [], "i) a stamped task with a non-Matrix room gets nothing, not even a DM")
    lane, _ = origin()
    posts = spoken(lane)
    check(len(posts) == 1 and posts[0].get("room_id") == sa.ROOM,
          f"CONTROL: the stamped AG2 Space task gets one line in its own room ({posts})")
    return check.exit_code()


if __name__ == "__main__":
    sys.exit(main())
