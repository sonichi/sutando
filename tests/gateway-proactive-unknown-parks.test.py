#!/usr/bin/env python3
"""A proactive send that may have landed is parked UNKNOWN and never sent again.

The room-message provider declares neither an idempotent send nor reconciliation, so a
resend after an uncertain outcome can only post twice. Each case accepts the POST (it is
recorded as landed), loses the confirmation, then restarts the process:

  a) the POST times out                 -> parked outcome-unknown, no second POST
  b) a 2xx with no event id             -> the same
  c) a 5xx after the POST               -> the same
  d) the file put back in results/      -> still no second POST, parked again
  e) control: connection refused        -> never connected, so retried and delivered once

Run: python3 tests/gateway-proactive-unknown-parks.test.py
"""
from __future__ import annotations

import socket
import sys
import time
import urllib.error
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _helpers.hermetic_gateway import isolate_then_import  # noqa: E402

gb, _IMPORT_READS = isolate_then_import(env={"REMOTE_PROACTIVE_ROOM": ""})
from _helpers import proactive_lane as pl  # noqa: E402

ISO = pl.scratch("gw-proactive-unknown-")
check = pl.Checks()


def lost(exc):
    def respond(payload):
        raise exc
    return respond


def http(code):
    return urllib.error.HTTPError("https://gw.example/relay/v1/room", code, "err", {}, None)


def scenario(label: str, respond) -> None:
    lane = pl.Lane(gb, ISO, respond)
    lane.write("proactive-1.txt", "status for the owner\n")
    lane.drain()
    check(len(lane.posts) == 1, f"{label}: the first send is posted once ({len(lane.posts)})")
    for _ in range(3):
        lane.drain()
    lane.restart()
    lane.respond = lambda payload: {"ok": True, "event_id": "$late"}
    for _ in range(3):
        lane.drain()
    items = lane.items()
    check(len(lane.posts) == 1, f"{label}: no second POST after the restart ({len(lane.posts)})")
    check(len(items) == 1 and items[0].get("status") == "PARKED"
          and items[0].get("reason") == "outcome-unknown",
          f"{label}: the outbox item is parked outcome-unknown ({[(i.get('status'), i.get('reason')) for i in items]})")
    check(lane.where("proactive-1") == "undelivered", f"{label}: the file is parked, not archived ({lane.where('proactive-1')})")
    if label.startswith("a)"):
        parked = next((lane.results / "undelivered").glob("proactive-1*"))
        lane.write("proactive-1.txt", parked.read_text(encoding="utf-8"))
        lane.drain()
        check(len(lane.posts) == 1 and lane.where("proactive-1") == "undelivered",
              f"d) the same file put back is not sent again ({len(lane.posts)} POSTs, {lane.where('proactive-1')})")
        check(any("not sent again" in m and "requeue" in m for m in lane.logs),
              "a) the park is logged with the operator's requeue command")


def main() -> int:
    scenario("a) timeout", lost(urllib.error.URLError(TimeoutError("timed out"))))
    scenario("b) no event id", lambda payload: {"ok": True})
    scenario("c) HTTP 502", lost(http(502)))

    lane = pl.Lane(gb, ISO, lost(urllib.error.URLError(ConnectionRefusedError(61, "refused"))))
    lane.write("proactive-2.txt", "status two\n")
    lane.drain()
    check(lane.where("proactive-2") == "pending", f"e) refused: handed back for a retry ({lane.where('proactive-2')})")
    lane.respond = lambda payload: {"ok": True, "event_id": "$ok"}
    backend = lane.backend()
    if backend is not None:
        backend.clock = lambda: time.time() + 60
    lane.drain()
    check(lane.where("proactive-2") == "archived" and len(lane.posts) == 2,
          f"e) refused, then delivered on the retry ({lane.where('proactive-2')}, {len(lane.posts)} attempts)")
    lane = pl.Lane(gb, ISO, lost(urllib.error.URLError(socket.gaierror(8, "nodename"))))
    lane.write("proactive-3.txt", "status three\n")
    lane.drain()
    check(lane.where("proactive-3") == "pending", f"e) DNS failure: handed back for a retry ({lane.where('proactive-3')})")
    return check.exit_code()


if __name__ == "__main__":
    sys.exit(main())
