#!/usr/bin/env python3
"""The status-ack runner: off by default, configured by its manifest, routed and receipted by
the gateway's real proactive outbox.

  a) disabled by default: the shipped manifest is off; a pass and the CLI read and write nothing
  b) config precedence CLI > env > manifest; a bad or non-positive deadline falls back
  c) first_ack_deadline_s from config: no line before the deadline, one by deadline + one poll
  d) thread routing through the real outbox writer: an in-thread ask keeps its root, a
     top-level ask is rooted at its message; the POST carries a dedupe key
  e) counted as sent only on a receipt: claimed before the drain, confirmed with the event id
     and room after; an uncertain send stays unknown and is never spoken again
  CONTROL (a): enabled, the same pass publishes   CONTROL (d): without [channel:] the same
  line would land in the owner DM, so the room check bites   CONTROL (e): DELIVERED confirms

Run: python3 tests/status-ack-runner.test.py
"""
from __future__ import annotations

import contextlib
import io
import json
import socket
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _helpers.hermetic_gateway import isolate_then_import  # noqa: E402

gb, _IMPORT_READS = isolate_then_import(env={"REMOTE_PROACTIVE_ROOM": ""})
from _helpers import proactive_lane as pl  # noqa: E402
from _helpers import status_ack_env as sa  # noqa: E402

import ack_claim  # noqa: E402

ISO = pl.scratch("status-ack-runner-")
check = pl.Checks()
mod = sa.runner()
TID = "task-1761300000001"


def tree(ws: Path) -> list:
    return sorted(str(p.relative_to(ws)) for p in ws.rglob("*"))


def main() -> int:
    manifest = json.loads((sa.REPO / "skills" / "status-ack" / "manifest.json").read_text())
    check(manifest["config"]["STATUS_ACK_ENABLED"] == "0", "a) the shipped manifest is off")
    cfg = mod.settings(environ={})
    check(not cfg.enabled, "a) with no CLI or env, the runner is off")
    lane = pl.Lane(gb, ISO)
    sa.task(lane.ws, TID)
    before = tree(lane.ws)
    saved = mod.probe_snapshot
    mod.probe_snapshot = lambda *a, **k: (_ for _ in ()).throw(AssertionError("probed while off"))
    try:
        r = mod.run_once(lane.ws, cfg, now=sa.T0 + 10_000, holders=sa.STANDBY)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = mod.main(["--workspace", str(lane.ws)])
    finally:
        mod.probe_snapshot = saved
    lane.drain()
    check(not r.spoken and r.alert is None and tree(lane.ws) == before and lane.posts == [],
          "a) off: a pass reads nothing, writes nothing, sends nothing")
    check(rc == 0 and json.loads(out.getvalue())["enabled"] is False and tree(lane.ws) == before,
          "a) off: the CLI reports disabled and changes nothing")
    r = mod.run_once(lane.ws, sa.on(mod), now=sa.T0 + 10_000, holders=sa.SESSION)
    check(r.spoken == [TID], "CONTROL (a): enabled, the same pass publishes")

    m = {"STATUS_ACK_ENABLED": "0", "STATUS_ACK_FIRST_ACK_DEADLINE_NORMAL_S": "90"}
    check(mod.settings(environ={"STATUS_ACK_ENABLED": "1"}, manifest_cfg=m).enabled, "b) env beats the manifest")
    check(not mod.settings(enabled="0", environ={"STATUS_ACK_ENABLED": "1"}, manifest_cfg=m).enabled,
          "b) the CLI beats env")
    check(mod.settings(environ={}, manifest_cfg=m).deadlines["normal"] == 90.0, "b) the manifest sets a deadline")
    check(mod.settings(environ={"STATUS_ACK_FIRST_ACK_DEADLINE_NORMAL_S": "45"}, manifest_cfg=m)
          .deadlines["normal"] == 45.0, "b) env overrides the manifest deadline")
    bad = mod.settings(environ={"STATUS_ACK_FIRST_ACK_DEADLINE_URGENT_S": "-1",
                                "STATUS_ACK_FIRST_ACK_DEADLINE_LOW_S": "soon"}, manifest_cfg={})
    check(bad.deadlines == {"urgent": 30.0, "normal": 120.0, "low": 600.0}, f"b) bad values fall back ({bad.deadlines})")
    shipped = mod.settings(environ={})
    check(shipped.deadlines == {"urgent": 30.0, "normal": 120.0, "low": 600.0} and shipped.poll_s == 15.0,
          f"b) shipped defaults ({shipped.deadlines}, poll {shipped.poll_s})")

    poll = shipped.poll_s
    for deadline in (45.0, 120.0):
        lane = pl.Lane(gb, ISO)
        sa.task(lane.ws, TID)
        cfg = sa.on(mod, normal=deadline)
        first = None
        t = sa.T0 + 3.0
        while t < sa.T0 + deadline + 3 * poll and first is None:
            if mod.run_once(lane.ws, cfg, now=t, holders=sa.SESSION).spoken:
                first = t
            t += poll
        check(first is not None and sa.T0 + deadline <= first <= sa.T0 + deadline + poll,
              f"c) deadline {deadline:.0f}s: first line at +{(first or 0) - sa.T0:.0f}s, within one poll")

    lane = pl.Lane(gb, ISO, lambda payload: {"ok": True, "event_id": "$Posted_9"})
    sa.task(lane.ws, TID, thread_root=sa.ROOT_EVT)
    sa.task(lane.ws, "task-1761300000002")
    mod.run_once(lane.ws, sa.on(mod), now=sa.T0 + 130, holders=sa.SESSION)
    key = f"ack:{TID}:initial"
    check(ack_claim.state(lane.ws, key) == "claimed", "e) published but not yet delivered: claimed, not sent")
    lane.drain()
    by_root = {p.get("thread_root"): p for p in lane.posts}
    check(set(by_root) == {sa.ROOT_EVT, sa.MSG_EVT} and all(p["room_id"] == sa.ROOM for p in lane.posts),
          f"d) in-thread keeps its root, top-level is rooted at its message, both in the task's room ({lane.posts})")
    check(all(p.get("dedupe_key") for p in lane.posts), "d) every POST carries the outbox's dedupe key")
    mod.run_once(lane.ws, sa.on(mod), now=sa.T0 + 145, holders=sa.SESSION)
    rec = ack_claim.read(lane.ws, key) or {}
    check(rec.get("state") == "confirmed" and rec.get("provider_ref") == "$Posted_9" and rec.get("room") == sa.ROOM,
          f"CONTROL (e): DELIVERED settles it confirmed with event id and room ({rec})")

    ctl = pl.Lane(gb, ISO)
    ctl.write("proactive-about-task-1761300000003--1761300130000.txt", "Received. Not picked up yet.\n")
    ctl.drain()
    check(ctl.posts and ctl.posts[0]["room_id"] == pl.OWNER_DM,
          "CONTROL (d): the same line without [channel:] goes to the owner DM")

    def timeout(_payload):
        raise socket.timeout("timed out")

    lane = pl.Lane(gb, ISO, timeout)
    sa.task(lane.ws, TID)
    mod.run_once(lane.ws, sa.on(mod), now=sa.T0 + 130, holders=sa.SESSION)
    lane.drain()
    for i in range(1, 8):
        mod.run_once(lane.ws, sa.on(mod), now=sa.T0 + 130 + 15 * i, holders=sa.SESSION)
        lane.drain()
    check(ack_claim.state(lane.ws, key) == "unknown" and len(lane.posts) == 1,
          f"e) an uncertain send stays unknown and is never spoken again ({ack_claim.state(lane.ws, key)}, "
          f"{len(lane.posts)} POST)")
    return check.exit_code()


if __name__ == "__main__":
    sys.exit(main())
