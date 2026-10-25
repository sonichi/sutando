#!/usr/bin/env python3
"""Startup recovery of the hosted status-ack loop: a restart never re-announces and never releases
an uncertain send. Each restart is a fresh runner module running `serve()`; assertions are at the
fake room endpoint of the gateway's real proactive drain.

  a) a task acked (confirmed) before the restart stays acked: no second line
  b) a claim the previous runner took but never published becomes unknown, never released;
     the task gets no line      CONTROL: releasing it instead would announce the task again
  c) published but the claim was not yet stamped with its file (a crash between the two): the
     one line goes out and the claim turns unknown, so it is never sent a second time
  d) an unknown claim is left as it is and never re-announced
  e) a claim held by a live publisher (another process) is left untouched
  f) tasks that arrived while the runner was down get at most one line each
  g) a second runner on the same workspace runs no pass while the first holds it

Run: python3 tests/status-ack-recovery.test.py
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _helpers.hermetic_gateway import isolate_then_import  # noqa: E402

gb, _IMPORT_READS = isolate_then_import(env={"REMOTE_PROACTIVE_ROOM": ""})
from _helpers import proactive_lane as pl  # noqa: E402
from _helpers import status_ack_env as sa  # noqa: E402

import ack_claim  # noqa: E402

ISO = pl.scratch("status-ack-recovery-")
check = pl.Checks()
TID = "task-1761300000001"
KEY = f"ack:{TID}:initial"
DUE = sa.T0 + 10_000
DEAD_PID = 2 ** 22 + 12345


def restart(lane, start, n=4, mod=None):
    """A fresh runner process: `serve()` for `n` passes, the gateway draining after each."""
    mod = mod or sa.runner()
    clock = iter(start + 15 * i for i in range(n))
    reports = []
    mod.serve(lane.ws, lambda: sa.on(mod), passes=n, clock=lambda: next(clock),
              sleep=lambda s: lane.drain(), holders=sa.SESSION, emit=reports.append)
    return reports


def dead_claim(ws, **fields):
    return ack_claim.claim(ws, KEY, "status-ack", now=DUE - 60, pid=DEAD_PID, **fields)


def main() -> int:
    lane = pl.Lane(gb, ISO, lambda p: {"ok": True, "event_id": "$Posted_1"})
    sa.task(lane.ws, TID)
    restart(lane, DUE)
    check(len(sa.room_posts(lane)) == 1 and ack_claim.state(lane.ws, KEY) == "confirmed", "a) first run: one line, confirmed")
    restart(lane, DUE + 600)
    check(len(sa.room_posts(lane)) == 1, f"a) after a restart the acked task stays acked ({len(lane.posts)} POSTs)")

    lane = pl.Lane(gb, ISO)
    sa.task(lane.ws, TID)
    dead_claim(lane.ws)
    reports = restart(lane, DUE)
    check(ack_claim.state(lane.ws, KEY) == "unknown" and lane.posts == [],
          f"b) a dead runner's unpublished claim turns unknown; no line ({ack_claim.read(lane.ws, KEY)})")
    check(reports[0].get("recovered") == [(KEY, "unknown")], f"b) the first pass reports it ({reports[0].get('recovered')})")

    ctl = pl.Lane(gb, ISO)
    sa.task(ctl.ws, TID)
    dead_claim(ctl.ws)
    mod = sa.runner()
    mod.recover = lambda ws: [(KEY, "released")] if ack_claim.release(ws, KEY, "status-ack") else []
    restart(ctl, DUE, mod=mod)
    check(len(sa.room_posts(ctl)) == 1, "CONTROL (b): releasing on restart would announce the task again")

    lane = pl.Lane(gb, ISO)
    sa.task(lane.ws, TID)
    mod = sa.runner()
    mod.run_once(lane.ws, sa.on(mod), now=DUE, holders=sa.SESSION)
    rec = ack_claim.read(lane.ws, KEY)
    ack_claim.update(lane.ws, KEY, "status-ack", gen=rec["gen"], pid=DEAD_PID)
    stripped = {k: v for k, v in ack_claim.read(lane.ws, KEY).items() if k != "via"}
    ack_claim.local_record.write_text_whole(ack_claim._path(lane.ws, KEY), json.dumps(stripped))
    restart(lane, DUE + 15, n=6)
    check(len(sa.room_posts(lane)) == 1 and ack_claim.state(lane.ws, KEY) == "unknown",
          f"c) published before the crash: one line, then unknown, never again ({len(lane.posts)})")

    lane = pl.Lane(gb, ISO)
    sa.task(lane.ws, TID)
    dead_claim(lane.ws)
    ack_claim.update(lane.ws, KEY, "status-ack", "unknown")
    restart(lane, DUE)
    check(lane.posts == [] and ack_claim.state(lane.ws, KEY) == "unknown", "d) unknown stays unknown, no line")

    lane = pl.Lane(gb, ISO)
    sa.task(lane.ws, TID)
    live = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        ack_claim.claim(lane.ws, KEY, "status-ack", now=DUE - 5, pid=live.pid)
        restart(lane, DUE)
        check(ack_claim.state(lane.ws, KEY) == "claimed" and lane.posts == [],
              "e) a live publisher's claim is left as it is")
    finally:
        live.kill()
        live.wait()

    lane = pl.Lane(gb, ISO)
    tids = [f"task-17613000000{i}1" for i in range(3)]
    for tid in tids:
        sa.task(lane.ws, tid, msg=f"$Ask_{tid}")
    restart(lane, DUE, n=8)
    restart(lane, DUE + 900, n=8)
    roots = sorted(p.get("thread_root") for p in sa.room_posts(lane))
    check(roots == sorted(f"$Ask_{t}" for t in tids), f"f) arrived while down: exactly one line each ({roots})")

    mod = sa.runner()
    check(not mod._pid_alive(None) and not mod._pid_alive(True) and not mod._pid_alive(DEAD_PID)
          and mod._pid_alive(1) and mod._pid_alive(sa.os.getpid()),
          "e) liveness: no pid or a gone pid is dead; a pid we may not signal is alive")

    lane = pl.Lane(gb, ISO)
    sa.task(lane.ws, TID)
    held = mod.runner_lock(lane.ws)
    try:
        reports = restart(lane, DUE)
    finally:
        held.close()
    check(lane.posts == [] and all("another runner" in str(r.get("skipped")) for r in reports),
          f"g) a second runner runs no pass ({reports[:1]})")
    restart(lane, DUE)
    check(len(sa.room_posts(lane)) == 1, "g) once the first lets go, the next runner takes over")
    return check.exit_code()


if __name__ == "__main__":
    sys.exit(main())
