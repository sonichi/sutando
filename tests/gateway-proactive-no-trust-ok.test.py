#!/usr/bin/env python3
"""A bare `{"ok": true}` never settles a proactive send, whatever the host configures.

REMOTE_PROACTIVE_TRUST_OK=1 used to count an accept with no event id as delivered: the file
was archived and a receipt written for a message nobody could show had landed. With the
setting on, at import and on the module:

  a) the accept is not DELIVERED, and no receipt carries a provider reference
  b) the file is parked, never archived as sent
  c) it is not sent again, across passes and a restart

Run: python3 tests/gateway-proactive-no-trust-ok.test.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _helpers.hermetic_gateway import isolate_then_import  # noqa: E402

gb, _IMPORT_READS = isolate_then_import(env={"REMOTE_PROACTIVE_ROOM": "", "REMOTE_PROACTIVE_TRUST_OK": "1"})
from _helpers import proactive_lane as pl  # noqa: E402

ISO = pl.scratch("gw-proactive-trust-ok-")
check = pl.Checks()


def main() -> int:
    if hasattr(gb, "PROACTIVE_TRUST_OK"):
        gb.PROACTIVE_TRUST_OK = True
    lane = pl.Lane(gb, ISO, lambda payload: {"ok": True})
    lane.write("proactive-1.txt", "status for the owner\n")
    for _ in range(4):
        lane.drain()
    lane.restart()
    lane.drain()
    items = lane.items()
    check(not any(i.get("status") == "DELIVERED" for i in items),
          f"a) a bare ok is never DELIVERED ({[i.get('status') for i in items]})")
    check(not any(i.get("provider_ref") for i in items), "a) no receipt carries a provider reference")
    check(lane.where("proactive-1") == "undelivered", f"b) the file is parked ({lane.where('proactive-1')})")
    check(len(lane.posts) == 1, f"c) sent once, not again ({len(lane.posts)} POSTs)")
    check(not hasattr(gb, "PROACTIVE_TRUST_OK") or gb.PROACTIVE_TRUST_OK is not True,
          "the bridge carries no trust-ok switch")
    return check.exit_code()


if __name__ == "__main__":
    sys.exit(main())
