#!/usr/bin/env python3
"""Frozen counterexamples for Design B — deterministic replays of
machine-found defects (same discipline as tests/outbox-claim-regressions:
the explorer finds, the frozen schedule carries the regression weight).

CE-1  publish-while-inflight steal (found by BClaimMachine, 2026-08-17):
      plant_dead_claim; publish; claim(A) to completion; recover(B) to
      completion. Pre-fix, recover returned the item's key while drainer-A
      was the live believing holder — the dead ghost token's recovery
      re-opened a ready slot for an item that was owned ("recover STOLE
      room-evt-1 from live holder ['drainer-A']"). The fix quarantines
      dead tokens whose key has another live inflight token (reason
      "live-holder") and makes publish refuse while any inflight token
      for the key exists.

Falsifier mode (--prove-bite): blind the fix's sensor (_inflight_tokens
returns []) and assert the steal REPRODUCES — a regression that cannot
fail proves nothing.
"""
from __future__ import annotations

import os
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(_HERE), "tests", "_helpers"))
sys.path.insert(0, _HERE)

import designB as B  # noqa: E402
from claim_machine_b import BClaimDriver  # noqa: E402


def _run_to_completion(driver, actor):
    deadline = time.time() + 10.0
    while driver.busy[actor] and time.time() < deadline:
        driver.step(actor, 5)
    assert not driver.busy[actor], f"{actor} did not settle: wedged op"


def ce1_publish_while_inflight() -> str | None:
    """Returns None if the invariant held, else the violation message."""
    d = BClaimDriver()
    try:
        d.plant_dead_claim()
        d.publish()
        d.start_claim("drainer-A")
        _run_to_completion(d, "drainer-A")
        d.start_recover("drainer-B")
        _run_to_completion(d, "drainer-B")
        try:
            d.check_consistency()
            d.finish(check=True)
            return None
        except AssertionError as e:
            try:
                d.finish(check=False)
            except Exception:
                pass
            return str(e)
    except AssertionError as e:
        try:
            d.finish(check=False)
        except Exception:
            pass
        return str(e)


def main() -> int:
    prove_bite = "--prove-bite" in sys.argv
    if prove_bite:
        B._inflight_tokens = lambda root, key: []
        violation = ce1_publish_while_inflight()
        if violation is None:
            print("FAIL: fix sensor blinded and CE-1 did NOT reproduce — "
                  "the frozen schedule no longer bites")
            return 1
        print(f"PASS (falsifier): CE-1 reproduces with the fix blinded: {violation}")
        return 0
    violation = ce1_publish_while_inflight()
    if violation is not None:
        print(f"FAIL: CE-1 regressed: {violation}")
        return 1
    print("PASS: CE-1 publish-while-inflight — frozen schedule clean")
    return 0


if __name__ == "__main__":
    sys.exit(main())
