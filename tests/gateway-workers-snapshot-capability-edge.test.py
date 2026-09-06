#!/usr/bin/env python3
"""Workers-snapshot push across the worker-routing capability EDGE.

The payload's shape is half capability, so a snapshot sent in legacy mode is
not "already sent" once routing is advertised, and an endpoint backoff earned
before the advertisement is evidence about a broker that no longer exists.

Every transition here runs with NO snapshot rewrite, NO utime bump and NO
internal reset — those are exactly what let the earlier suites pass while the
transition itself was broken.

Run: python3 tests/gateway-workers-snapshot-capability-edge.test.py  (stdlib only)
"""
import importlib.util
import json
import os
import sys
import tempfile
import time
import urllib.error
from pathlib import Path

FAILS = []
MOD = Path(__file__).resolve().parent.parent / "src" / "remote-gateway-bridge.py"


def check(cond, msg):
    print(("ok  " if cond else "FAIL") + " " + msg)
    if not cond:
        FAILS.append(msg)


def fresh(tmp):
    """A newly executed module = a fresh process's globals. Between SCENARIOS
    that is legitimate; within one it would be the reset that hid this bug."""
    spec = importlib.util.spec_from_file_location("rtc_edge", MOD)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    snap = Path(tmp) / "state" / "pool-status.json"
    snap.parent.mkdir(parents=True, exist_ok=True)
    snap.write_text(json.dumps(BLOB))
    return m, snap


BLOB = {"ts": 1, "writer": "pool-lead", "live_cores": ["core-1"],
        "bindings": {"!r:x": {"instance": "core-1", "pinned": True,
                              "dedicated": False}}}
ADVERTISE = {"capabilities": ["worker-routing"]}
ROUTED = {**BLOB, "worker_id": "home", "location": "local"}


def recorder(calls, exc=None):
    def _req(method, path, payload=None, timeout=35):
        calls.append((method, path, payload))
        if exc is not None:
            raise exc(path)
        return {}
    return _req


def h404(path):
    return urllib.error.HTTPError(path, 404, "nf", {}, None)


def main() -> int:
    tmp = tempfile.mkdtemp(prefix="wsedge-test-")
    os.environ["SUTANDO_TEST_MODE"] = "1"
    os.environ["SUTANDO_WORKSPACE"] = tmp
    Path(tmp, ".notes-migrated").touch()
    Path(tmp, ".build_log-migrated").touch()
    os.environ["REMOTE_TASK_URL"] = "http://127.0.0.1:9"   # never contacted
    os.environ["REMOTE_TASK_TOKEN"] = "testtoken"
    os.environ["REMOTE_TASK_PROVIDER"] = "remote-gateway"
    for k in ("SUTANDO_WORKER_ID", "SUTANDO_WORKER_SEAT", "SUTANDO_CORE_ID",
              "SUTANDO_WORKER_LOCATION"):
        os.environ.pop(k, None)

    # --- 1. successful legacy push -> advertise -> the routed snapshot goes out
    m, _ = fresh(tmp)
    calls = []
    m._req = recorder(calls)
    check(m._maybe_push_workers_snapshot() is True and calls[-1][2] == BLOB,
          "legacy push goes out as the exact parent envelope")
    m._note_broker_capabilities(ADVERTISE)
    pushed = m._maybe_push_workers_snapshot()          # no rewrite, no reset
    check(pushed is True and len(calls) == 2,
          "advertising routing re-pushes the SAME file with no rewrite")
    check(calls[-1] == ("POST", "/v1/workers", ROUTED),
          "the re-push carries the seat the worker picker reads")

    # control: a heartbeat that changes nothing must NOT re-push, or every
    # beat would repost and the on-change relay would become a per-beat one.
    m._note_broker_capabilities(ADVERTISE)
    check(m._maybe_push_workers_snapshot() is False and len(calls) == 2,
          "control: re-advertising the same mode does not re-push")

    # --- 2. legacy 404 -> advertise -> the hour-long backoff is reconsidered
    m, _ = fresh(tmp)
    calls = []
    m._req = recorder(calls, exc=h404)
    check(m._maybe_push_workers_snapshot() is False and len(calls) == 1,
          "404 on the legacy endpoint defers the push")
    m._req = recorder(calls)
    check(m._maybe_push_workers_snapshot() is False and len(calls) == 1,
          "positive control: the backoff really is suppressing (a healthy "
          "broker still gets no request)")
    check(m._workers_push_retry_at > time.time() + 3000,
          "positive control: the backoff is the hour-long one")
    m._note_broker_capabilities(ADVERTISE)
    pushed = m._maybe_push_workers_snapshot()          # no rewrite, no reset
    check(pushed is True and calls[-1] == ("POST", "/v1/workers", ROUTED),
          "a new advertisement retires a backoff earned before it")

    # --- 2b. the RECIPROCAL transition: routed rejection -> withdrawal -> legacy push
    m, snap = fresh(tmp)
    calls = []
    m._note_broker_capabilities(ADVERTISE)
    m._req = recorder(calls)
    check(m._maybe_push_workers_snapshot() is True and calls[-1][2] == ROUTED,
          "routed snapshot goes out while routing is advertised")
    snap.write_text(json.dumps({**BLOB, "ts": 2}))
    os.utime(snap, (time.time() + 1, time.time() + 1))
    m._req = recorder(calls, exc=lambda p: urllib.error.HTTPError(p, 422, "unprocessable", {}, None))
    check(m._maybe_push_workers_snapshot() is False
          and m._workers_push_retry_at > time.time() + 3000,
          "a rejected ROUTED update earns the hour-long backoff")
    m._revoke_broker_capabilities()          # no rewrite, no reset
    m._req = recorder(calls)
    pushed = m._maybe_push_workers_snapshot()
    check(pushed is True and calls[-1][2] == {**BLOB, "ts": 2},
          "withdrawing routing retires that backoff and the EXACT legacy payload goes out")

    # --- 3. the edge is an EDGE: a repeated advertisement never clears backoff
    m, _ = fresh(tmp)
    calls = []
    m._note_broker_capabilities(ADVERTISE)             # rising edge, no backoff yet
    m._req = recorder(calls, exc=h404)
    check(m._maybe_push_workers_snapshot() is False and len(calls) == 1,
          "routed push can 404 too")
    m._note_broker_capabilities(ADVERTISE)             # same state: NOT an edge
    m._req = recorder(calls)
    check(m._maybe_push_workers_snapshot() is False and len(calls) == 1,
          "control: an unchanged advertisement leaves the backoff intact "
          "(level-triggered clearing would hammer a 404 broker every beat)")

    # --- 4. revocation is an EDGE too: a repeated withdrawal never clears backoff
    m, _ = fresh(tmp)
    calls = []
    m._req = recorder(calls, exc=h404)          # routing already off: a LEGACY 404
    check(m._maybe_push_workers_snapshot() is False
          and m._workers_push_retry_at > time.time() + 3000,
          "legacy 404 earns the backoff with routing already off")
    m._revoke_broker_capabilities()             # SAME state — not a transition
    m._req = recorder(calls)
    check(m._maybe_push_workers_snapshot() is False and len(calls) == 1,
          "control: revoking when routing was ALREADY off leaves the backoff intact "
          "(level-triggered clearing would hammer an unchanged broker every beat)")

    print()
    print(("FAILED: " + "; ".join(FAILS)) if FAILS else "all green")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
