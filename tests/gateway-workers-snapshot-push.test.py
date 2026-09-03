#!/usr/bin/env python3
"""Gateway workers-snapshot push: on-change relay of state/pool-status.json
to POST /v1/workers — no file is a no-op, unchanged content never re-posts,
a change re-posts, and a 404 broker backs off instead of hammering.

Run: python3 tests/gateway-workers-snapshot-push.test.py   (stdlib only)
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


def check(cond, msg):
    print(("ok  " if cond else "FAIL") + " " + msg)
    if not cond:
        FAILS.append(msg)


def main() -> int:
    tmp = tempfile.mkdtemp(prefix="wspush-test-")
    os.environ["SUTANDO_TEST_MODE"] = "1"
    os.environ["SUTANDO_WORKSPACE"] = tmp
    Path(tmp, ".notes-migrated").touch()
    Path(tmp, ".build_log-migrated").touch()
    os.environ["REMOTE_TASK_URL"] = "http://127.0.0.1:9"  # never contacted
    os.environ["REMOTE_TASK_TOKEN"] = "testtoken"
    os.environ["REMOTE_TASK_PROVIDER"] = "remote-gateway"
    for k in ("SUTANDO_WORKER_ID", "SUTANDO_WORKER_SEAT", "SUTANDO_CORE_ID",
              "SUTANDO_WORKER_LOCATION"):
        os.environ.pop(k, None)  # hermetic: the host's seat must not leak in

    spec = importlib.util.spec_from_file_location(
        "rtc_push", Path(__file__).resolve().parent.parent / "src" / "remote-gateway-bridge.py")
    rtc = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(rtc)

    calls = []

    def fake_req(method, path, payload=None, timeout=35):
        calls.append((method, path, payload))
        return {}

    rtc._req = fake_req

    check(rtc._maybe_push_workers_snapshot() is False and not calls,
          "no snapshot file: no-op, no request")

    snap_path = Path(tmp) / "state" / "pool-status.json"
    snap_path.parent.mkdir(parents=True, exist_ok=True)
    blob = {"ts": 1, "writer": "pool-lead", "live_cores": ["core-1"],
            "bindings": {"!r:x": {"instance": "core-1", "pinned": True,
                                  "dedicated": False}}}
    snap_path.write_text(json.dumps(blob))
    check(rtc._maybe_push_workers_snapshot() is True,
          "new snapshot pushes")
    # The pushing seat stamps itself onto the lead's file (worker-aware brokers
    # key workers by seat); an unconfigured process is the home seat.
    check(calls == [("POST", "/v1/workers",
                     {**blob, "worker_id": "home", "location": "local"})],
          "pushed the blob + this seat's worker_id/location to POST /v1/workers")
    check(rtc._maybe_push_workers_snapshot() is False and len(calls) == 1,
          "unchanged snapshot never re-posts")

    blob["ts"] = 2
    snap_path.write_text(json.dumps(blob))
    os.utime(snap_path, (time.time() + 2, time.time() + 2))
    check(rtc._maybe_push_workers_snapshot() is True and len(calls) == 2,
          "changed snapshot pushes again")

    def req_404(method, path, payload=None, timeout=35):
        calls.append((method, path, payload))
        raise urllib.error.HTTPError(path, 404, "nf", {}, None)

    rtc._req = req_404
    blob["ts"] = 3
    snap_path.write_text(json.dumps(blob))
    os.utime(snap_path, (time.time() + 4, time.time() + 4))
    check(rtc._maybe_push_workers_snapshot() is False and len(calls) == 3,
          "404 broker: push attempted once, reported deferred")
    check(rtc._maybe_push_workers_snapshot() is False and len(calls) == 3,
          "inside the 404 backoff no further request is made")
    check(rtc._workers_push_retry_at > time.time() + 3000,
          "404 backoff is about an hour")

    snap_path.write_text("{ mid-write garbage")
    rtc._workers_push_retry_at = 0.0
    rtc._req = fake_req
    os.utime(snap_path, (time.time() + 6, time.time() + 6))
    check(rtc._maybe_push_workers_snapshot() is False and len(calls) == 3,
          "malformed (mid-write) snapshot is skipped, not posted")

    print(f"\n{len(FAILS)} failure(s)")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
