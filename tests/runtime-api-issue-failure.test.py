#!/usr/bin/env python3
"""Failure-injection regression for RuntimeServer._issue (review blocker).

_issue creates the durable request row and then mirrors it into a human-action
card. If the mirror (open_approval / open_elicitation) fails, the row must NOT
stay 'pending' — a pending row with no card is unanswerable and strands until
expiry. Contract pinned here, in-process (no daemon subprocess, no socket):

  1) mirror failure → ProtocolError surfaces to the caller
  2) …and the durable row is marked failed (with the error recorded)
  3) …never left pending; recover() after a restart re-links NOTHING for it
  4) success path unchanged: row pending + ha mapping recorded

Run: python3 tests/runtime-api-issue-failure.test.py   (stdlib only)
"""
from __future__ import annotations

import importlib.util
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src" / "runtime-api"))

_spec = importlib.util.spec_from_file_location(
    "rt_server", REPO / "src" / "runtime-api" / "server.py")
rt = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rt)

FAILS: list = []


def check(cond, msg):
    print(("  ok  " if cond else "  FAIL ") + msg)
    if not cond:
        FAILS.append(msg)


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="rt-issue-"))
    srv = rt.RuntimeServer(socket_path=str(tmp / "s.sock"),
                           db_path=str(tmp / "s.sqlite"),
                           ha_dir=str(tmp / "ha"))

    # 1+2+3: mirror failure → ProtocolError AND a durable 'failed' row
    def boom(_rec):
        raise OSError("ha dir gone")

    srv.ha.open_approval = boom
    err = None
    try:
        srv.dispatcher._issue("approval", "approval.request", {"action": "x.y"},
                   required=("action",))
    except rt.ProtocolError as e:
        err = e
    check(err is not None
          and "could not open the human-action card" in str(getattr(err, "message", err)),
          "mirror failure surfaces as a protocol error")
    rows = [r for r in map(srv.store.get, _all_ids(srv)) if r]
    check(len(rows) == 1 and rows[0]["status"] == "failed"
          and "ha dir gone" in (rows[0]["result"] or {}).get("error", ""),
          "the durable row is marked failed with the mirror error recorded")
    check(all(r["status"] != "pending" for r in rows),
          "no unanswerable pending row survives the failure")
    srv.dispatcher._ha_of.clear()
    srv.dispatcher.recover()
    check(srv.dispatcher._ha_of == {}, "restart recovery re-links nothing for the failed row")

    # 4: success path unchanged
    srv.ha.open_approval = lambda rec: "ha_" + rec["requestId"][-12:]
    out = srv.dispatcher._issue("approval", "approval.request", {"action": "x.y"},
                     required=("action",))
    got = srv.store.get(out["requestId"])
    check(out["status"] == "pending" and got["status"] == "pending"
          and srv.dispatcher._ha_of.get(out["requestId"]),
          "successful mirror: row pending + ha mapping recorded")

    print(f"\n{'PASS — issue failure-injection green' if not FAILS else f'FAILED ({len(FAILS)})'}")
    return 1 if FAILS else 0


def _all_ids(srv) -> list:
    cur = srv.store._db.execute("SELECT request_id FROM runtime_requests")
    return [r[0] for r in cur.fetchall()]


if __name__ == "__main__":
    raise SystemExit(main())
