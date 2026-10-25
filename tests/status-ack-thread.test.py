#!/usr/bin/env python3
"""The status ack carries the verified room and thread root to the provider, even when the task
exists only as a renamed live file the gateway's archive lookup cannot find.

  a) only task-<ms>.claimed-core-2.txt present, top-level ask -> POST rooted at its message
  b) only task-<ms>.claimed-core-2.txt present, in-thread ask -> POST keeps the thread root
  c) only an .assigned-<inst> file present -> the same
  d) an unrecoverable thread (markers do not round-trip) publishes nothing and releases the claim
  e) a published file edited after publication (thread or room) -> zero POSTs; untouched -> one
  f) a stale reconcile snapshot never releases or settles a successor claim of the same holder
  CONTROL: with a plain [channel:]-only body the gateway cannot recover the thread for such a file

Run: python3 tests/status-ack-thread.test.py
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
import proactive_dispatch_gate  # noqa: E402

ISO = pl.scratch("status-ack-thread-")
check = pl.Checks()
mod = sa.runner()
TID = "task-1761300000001"
NOW = sa.T0 + 130


def renamed_only(lane, suffix, **kw):
    p = sa.task(lane.ws, TID, **kw)
    q = p.with_name(f"{TID}{suffix}.txt")
    p.rename(q)
    return q


def main() -> int:
    for suffix, label in ((".claimed-core-2", "claimed"), (".assigned-worker1", "assigned")):
        for root, want, shape in ((None, sa.MSG_EVT, "top-level"), (sa.ROOT_EVT, sa.ROOT_EVT, "in-thread")):
            lane = pl.Lane(gb, ISO)
            renamed_only(lane, suffix, thread_root=root)
            check(not (lane.tasks / f"{TID}.txt").exists(), f"a) only the {label} file exists")
            r = mod.run_once(lane.ws, sa.on(mod), now=NOW, holders=sa.SESSION)
            lane.drain()
            check(r.spoken == [TID], f"a) {label} {shape}: the ack is published ({r.spoken}, {r.skipped})")
            check(len(lane.posts) == 1 and lane.posts[0].get("thread_root") == want
                  and lane.posts[0]["room_id"] == sa.ROOM,
                  f"a) {label} {shape}: the POST carries thread_root {want} in the task room ({lane.posts})")
            check("[thread" not in lane.posts[0]["body"] and "[channel" not in lane.posts[0]["body"],
                  "a) the markers are stripped from the posted body")

    ctl = pl.Lane(gb, ISO)
    renamed_only(ctl, ".claimed-core-2", thread_root=sa.ROOT_EVT)
    ctl.write(f"proactive-about-{TID}--1761300130000.txt", f"[channel: {sa.ROOM}]\nReceived.\n")
    ctl.drain()
    check(ctl.posts and ctl.posts[0].get("thread_root") is None,
          f"CONTROL: without the thread marker the same file is posted unthreaded ({ctl.posts})")

    lane = pl.Lane(gb, ISO)
    renamed_only(lane, ".claimed-core-2", thread_root=sa.ROOT_EVT)
    real = mod.parse_markers
    mod.parse_markers = lambda body, **k: real("[channel: " + sa.ROOM + "]\n" + "text\n", **k)
    try:
        r = mod.run_once(lane.ws, sa.on(mod), now=NOW, holders=sa.SESSION)
    finally:
        mod.parse_markers = real
    lane.drain()
    key = f"ack:{TID}:initial"
    check(not r.spoken and TID in r.withdrawn and lane.posts == [] and not sa.proactive_files(lane.ws),
          f"d) an unrecoverable thread publishes nothing ({r.spoken}, {r.withdrawn}, {lane.posts})")
    check(ack_claim.state(lane.ws, key) == "none", f"d) and releases the claim ({ack_claim.state(lane.ws, key)})")
    def gated(lane):
        return {"gate": functools.partial(proactive_dispatch_gate.dispatch_veto, workspace=lane.ws),
                "route_gate": functools.partial(proactive_dispatch_gate.route_veto, workspace=lane.ws)}

    for what, edit in (("the thread", lambda t: t.replace(f"[thread: {sa.MSG_EVT}]", "[thread: $ForgedRoot_9]")),
                       ("the room", lambda t: t.replace(sa.ROOM, "!OtherRoomXyZ:ag2.space")),
                       ("nothing (control)", lambda t: t)):
        lane = pl.Lane(gb, ISO)
        renamed_only(lane, ".claimed-core-2")
        r = mod.run_once(lane.ws, sa.on(mod), now=NOW, holders=sa.SESSION)
        (pub,) = sorted(lane.results.glob("proactive-*.txt"))
        pub.write_text(edit(pub.read_text(encoding="utf-8")), encoding="utf-8")
        lane.drain(**gated(lane))
        want = 1 if what.startswith("nothing") else 0
        check(r.spoken == [TID] and len(lane.posts) == want,
              f"e) after publication edits {what}: {len(lane.posts)} POST(s), want {want} ({lane.logs[-1:]})")

    lane = pl.Lane(gb, ISO)
    renamed_only(lane, ".claimed-core-2")
    mod.run_once(lane.ws, sa.on(mod), now=NOW, holders=sa.SESSION)
    stale = ack_claim.held_by(lane.ws, mod.HOLDER, "claimed")
    ack_claim.release(lane.ws, f"ack:{TID}:initial", mod.HOLDER, gen=stale[0]["gen"])
    successor = ack_claim.claim(lane.ws, f"ack:{TID}:initial", mod.HOLDER, via="proactive-about-successor.txt")
    withdrawn = {"status": "PARKED", "reason": "withdrawn: the task is archived"}
    saved = mod._receipt, ack_claim.held_by
    mod._receipt = lambda ws, name: withdrawn
    ack_claim.held_by = lambda *a, **k: stale
    try:
        settled = mod.reconcile(lane.ws)
        survived = (ack_claim.read(lane.ws, f"ack:{TID}:initial") or {}).get("gen") == successor
        ack_claim.held_by = lambda *a, **k: [{k_: v for k_, v in stale[0].items() if k_ != "gen"}]
        mod.reconcile(lane.ws)
        deleted = ack_claim.state(lane.ws, f"ack:{TID}:initial") == "none"
    finally:
        mod._receipt, ack_claim.held_by = saved
    check(settled == [] and survived, f"f) a stale snapshot leaves the successor claim alone ({settled})")
    check(deleted, "CONTROL: the same stale snapshot without a generation deletes the successor")
    return check.exit_code()


if __name__ == "__main__":
    sys.exit(main())
