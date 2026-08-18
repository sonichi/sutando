#!/usr/bin/env python3
"""Deterministic drives for the outbox's error and platform branches.

The happy paths live in the contract suites; these are the guards coverage
found silent — identity probes under fault, decision-table edges, and the
FileNotFound/OSError arms of the claim and item lifecycle. All mock-driven,
so both the Linux gate and a macOS dev machine execute every branch.
"""
import builtins
import sys
import tempfile
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))
import outbox  # noqa: E402
from outbox import (OwnerState, DeliveryOutcome, RetrySafety,  # noqa: E402
                    resolve_outcome, acquire_delivery_claim, read_delivery_claim,
                    may_reclaim_delivery, release_delivery_claim, ProcessIdentity)

FAILS = 0


def check(cond, msg):
    global FAILS
    print(("  ok  " if cond else "  FAIL ") + msg)
    if not cond:
        FAILS += 1


_real_open = builtins.open


def _open_raising(exc):
    def fake(path, *a, **kw):
        p = str(path)
        if p.startswith("/proc/") and p.endswith("/stat"):
            raise exc
        return _real_open(path, *a, **kw)
    return fake


# -- _linux_process_identity fault branches -----------------------------------

with mock.patch.object(builtins, "open", _open_raising(FileNotFoundError())), \
     mock.patch.object(outbox.os.path, "isdir", lambda p: True):
    ident = outbox._linux_process_identity(999999)
check(ident is not None and ident.state is OwnerState.DEAD,
      "missing /proc/<pid>/stat on a /proc system reads DEAD")

with mock.patch.object(builtins, "open", _open_raising(FileNotFoundError())), \
     mock.patch.object(outbox.os.path, "isdir", lambda p: False):
    ident = outbox._linux_process_identity(999999)
check(ident is None, "no /proc at all defers to the platform fallback (None)")

with mock.patch.object(builtins, "open", _open_raising(PermissionError())):
    ident = outbox._linux_process_identity(1)
check(ident is not None and ident.state is OwnerState.UNKNOWN,
      "EPERM on the stat file reads UNKNOWN, never DEAD")

with mock.patch.object(builtins, "open", _open_raising(OSError())):
    ident = outbox._linux_process_identity(1)
check(ident is not None and ident.state is OwnerState.UNKNOWN,
      "any other stat OSError reads UNKNOWN")


class _FakeStat:
    def __init__(self, body):
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return self._body


with mock.patch.object(builtins, "open",
                       lambda p, *a, **kw: _FakeStat(b"1 (x) garbage")
                       if str(p).startswith("/proc/") else _real_open(p, *a, **kw)):
    ident = outbox._linux_process_identity(1)
check(ident is not None and ident.state is OwnerState.ALIVE
      and ident.start_usec is None,
      "an unparseable stat line is ALIVE without a token, not UNKNOWN")

# -- resolve_outcome decision table -------------------------------------------

check(resolve_outcome(DeliveryOutcome.CONFIRMED, RetrySafety.UNSAFE) == "done",
      "CONFIRMED is done regardless of safety")
check(resolve_outcome(DeliveryOutcome.NOT_DELIVERED, RetrySafety.UNSAFE, 0) == "retry",
      "NOT_DELIVERED under the cap retries")
check(resolve_outcome(DeliveryOutcome.NOT_DELIVERED, RetrySafety.UNSAFE,
                      outbox.MAX_ATTEMPTS) == "park",
      "NOT_DELIVERED at the cap parks")
check(resolve_outcome(DeliveryOutcome.OUTCOME_UNKNOWN, RetrySafety.SAFE, 0) == "retry",
      "UNKNOWN + SAFE retries under the cap")
check(resolve_outcome(DeliveryOutcome.OUTCOME_UNKNOWN, RetrySafety.SAFE,
                      outbox.MAX_ATTEMPTS) == "park",
      "UNKNOWN + SAFE at the cap parks")
check(resolve_outcome(DeliveryOutcome.OUTCOME_UNKNOWN, RetrySafety.UNSAFE, 0) == "park",
      "UNKNOWN + UNSAFE parks at attempt 0")

# -- claim read edges ---------------------------------------------------------

with tempfile.TemporaryDirectory() as tmp:
    root = Path(tmp)

    check(read_delivery_claim(root, "absent") is None, "absent claim reads None")
    check(may_reclaim_delivery(root, "absent", 1.0) is True,
          "nothing holds it: reclaimable")

    outbox._claims_dir(root).mkdir(parents=True, exist_ok=True)
    outbox._claim_path(root, "torn").write_text("")
    rec = read_delivery_claim(root, "torn")
    check(rec is not None and rec.state == "UNKNOWN", "empty claim reads UNKNOWN")
    check(may_reclaim_delivery(root, "torn", 0.0) is False,
          "torn claim is never stolen")

    outbox._claim_path(root, "badjson").write_text("{not json")
    rec = read_delivery_claim(root, "badjson")
    check(rec is not None and rec.state == "UNKNOWN", "malformed JSON reads UNKNOWN")

    # live holder with a matching identity: never reclaimable
    assert acquire_delivery_claim(root, "held", "drainer-A")
    check(may_reclaim_delivery(root, "held", 0.0) is False,
          "a live same-identity holder is never displaced")

    # release guards: ownership-checked; absent claim is False, not a raise
    check(release_delivery_claim(root, "never-existed", drainer_id="drainer-A")
          is False, "releasing an absent claim returns False")
    check(release_delivery_claim(root, "held", drainer_id="not-the-holder")
          is False, "a non-holder cannot release a live claim")
    check(read_delivery_claim(root, "held") is not None,
          "the live claim survives the foreign release attempt")
    check(release_delivery_claim(root, "held", drainer_id="drainer-A") is True,
          "the holder releases its own claim")

    # non-dict JSON is a claim we cannot read: UNKNOWN, not a raise
    outbox._claim_path(root, "notdict").write_text("42")
    rec = read_delivery_claim(root, "notdict")
    check(rec is not None and rec.state == "UNKNOWN",
          "valid-but-non-dict JSON reads UNKNOWN")

    # an UNKNOWN owner (EPERM-class) is never displaced, regardless of TTL
    assert acquire_delivery_claim(root, "opaque", "drainer-B")
    with mock.patch.object(outbox, "process_identity",
                           lambda pid: ProcessIdentity(pid, OwnerState.UNKNOWN)):
        check(may_reclaim_delivery(root, "opaque", 0.0) is False,
              "an opaque (UNKNOWN) owner is never displaced")

    # a claim with no birth token cannot prove reuse: treated as the same owner
    assert acquire_delivery_claim(root, "untokened", "drainer-C")
    p = outbox._claim_path(root, "untokened")
    import json as _json
    d3 = _json.loads(p.read_text())
    d3["start_usec"] = None
    p.write_text(_json.dumps(d3))
    with mock.patch.object(outbox, "process_identity",
                           lambda pid: ProcessIdentity(pid, OwnerState.ALIVE, 12345)):
        check(may_reclaim_delivery(root, "untokened", 0.0) is False,
              "without a birth token, an ALIVE pid is treated as the holder")

    # reclaiming an unheld item is just an acquire
    check(outbox.reclaim_delivery_claim(root, "unheld", "drainer-D", 1.0) is True,
          "reclaim of an absent claim degrades to a plain acquire")

    # item lifecycle guards
    check(outbox.attempts_for(root, "no-item") == 0,
          "attempts for an unknown item default to 0")
    outbox.park_item(root, "p1", reason="test")
    check(outbox._read_item(root, "p1")["status"] == "PARKED", "park writes status")
    outbox.requeue_item(root, "p1")
    d2 = outbox._read_item(root, "p1")
    check(d2["status"] == "QUEUED" and d2["attempts"] == 0,
          "requeue resets the budget")

if FAILS:
    print(f"FAILED ({FAILS})")
    raise SystemExit(1)
print("PASS — outbox error and platform branches are all driven")
