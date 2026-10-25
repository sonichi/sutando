#!/usr/bin/env python3
"""The proactive retry is BOUNDED, and it is the outbox's, not the bridge's.

The 2026-08-16 incident (one nudge, 12 posts) made an unbounded retry a duplicate
generator. The gateway's proactive send now keeps no attempt count of its own: the outbox
decides from the provider's outcome, and the count lives on the outbox item, so a restart
cannot reset it.

  1. a definite refusal (429) is retried on the outbox's backoff, then parked
     retry-window-exhausted; every POST is counted on the item
  2. a send that never connected (refused, DNS) is retried, not parked at attempt 1
  3. a timeout after connecting may have posted: parked at once, never resent
  4. a permanent 4xx parks on the first attempt
  5. the vendored resolve_failed_send still runs (other bridges use it)
"""
import socket
import sys
import tempfile
import time
import urllib.error
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _helpers.hermetic_gateway import isolate_then_import  # noqa: E402

gb, _IMPORT_READS = isolate_then_import(env={"REMOTE_PROACTIVE_ROOM": ""})
from _helpers import proactive_lane as pl  # noqa: E402

ISO = pl.scratch("proactive-retry-bound-")
check = pl.Checks()


def raising(exc):
    def respond(payload):
        raise exc
    return respond


def http(code):
    return urllib.error.HTTPError("https://gw.example/relay/v1/room", code, "err", {}, None)


def one(exc, stem):
    lane = pl.Lane(gb, ISO, raising(exc))
    lane.write(f"{stem}.txt", "nudge\n")
    lane.drain()
    items = lane.items()
    return lane, (items[0] if items else {})


def main() -> int:
    lane = pl.Lane(gb, ISO, raising(http(429)))
    lane.write("proactive-1.txt", "nudge\n")
    backend = lane.backend()
    now = [time.time()]
    backend.clock = lambda: now[0]
    for _ in range(200):
        lane.drain()
        if lane.where("proactive-1") != "pending":
            break
        now[0] += 31
    item = lane.items()[0]
    check(lane.where("proactive-1") == "undelivered" and item.get("reason") == "retry-window-exhausted",
          f"1. a refusal is retried on the backoff, then parked ({lane.where('proactive-1')}, {item.get('reason')})")
    check(5 <= len(lane.posts) <= 30 and item.get("attempts") == len(lane.posts),
          f"1. bounded, and every attempt is counted on the item ({len(lane.posts)} POSTs, {item.get('attempts')} counted)")

    for reason, label in ((ConnectionRefusedError(61, "refused"), "refused"),
                          (socket.gaierror(8, "nodename"), "DNS")):
        lane, item = one(urllib.error.URLError(reason), "proactive-net")
        check(lane.where("proactive-net") == "pending" and item.get("status") != "PARKED",
              f"2. {label}: never connected, so retried, not parked at attempt 1")

    lane, item = one(urllib.error.URLError(TimeoutError("t")), "proactive-to")
    lane.drain()
    check(item.get("reason") == "outcome-unknown" and len(lane.posts) == 1,
          f"3. a timeout parks at once and is never resent ({item.get('reason')}, {len(lane.posts)} POSTs)")

    lane, item = one(http(404), "proactive-4xx")
    check(item.get("reason") == "permanent-refusal" and lane.where("proactive-4xx") == "undelivered",
          f"4. a permanent 4xx parks on the first attempt ({item.get('reason')})")

    from ag2_sparrow.send_failure_policy import UnconfirmedDelivery, resolve_failed_send
    with tempfile.TemporaryDirectory() as tmp:
        f = Path(tmp) / "proactive-pkg.txt"
        f.write_text("pkg")
        c = Path(tmp) / "proactive-pkg.sending"
        f.rename(c)
        out = resolve_failed_send(c, UnconfirmedDelivery("x"), {})
        check(out in ("retried", "parked"),
              f"5. packaged resolve_failed_send runs without ModuleNotFoundError ({out})")
    return check.exit_code()


if __name__ == "__main__":
    raise SystemExit(main())
