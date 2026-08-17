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


def ce2_holder_names_the_live_owner() -> str | None:
    """Found by full-budget BClaimMachine 2026-08-17 (run 1, second class):
    publish -> plant dead ghost -> claim(A) leaves a dead token and a live
    token coexisting (production analogue: recover's link+unlink crash
    window, then a claim of the ready copy). holder() must name the LIVE
    owner, never iteration order."""
    d = BClaimDriver()
    try:
        d.publish()
        d.plant_dead_claim()
        d.start_claim("drainer-A")
        _run_to_completion(d, "drainer-A")
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


def ce3_recover_to_claim_handoff_is_legal() -> str | None:
    """Oracle acceptance pin (full-budget false positive, 2026-08-17):
    recover moves a dead token to ready, ANOTHER actor claims it from that
    slot before recover returns. Completion order says "recover returned an
    owned key"; causal order says legitimate handoff. The machine must
    accept this schedule. (No prove-bite arm: its falsifier is the retired
    completion-order oracle itself.)"""
    d = BClaimDriver()
    try:
        d.plant_dead_claim()
        d.start_recover("drainer-A")
        d.step("drainer-A", 3)
        d.start_claim("drainer-B")
        _run_to_completion(d, "drainer-B")
        _run_to_completion(d, "drainer-A")
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


def ce2b_dead_only_holder_is_deterministic() -> str | None:
    """001 rider on CE-2: prefer-the-live-owner is half a fix — with ONLY
    dead tokens remaining, the answer must be deterministic and contract-
    stated (max (birth, name): the most recent dead incarnation), or the
    iteration-order defect class survives one state over. Direct fixture,
    no harness: determinism is not a concurrency property."""
    import tempfile
    from claim_machine_harness import DEAD_PID, ITEM
    with tempfile.TemporaryDirectory(prefix="ce2b-") as td:
        key = B.safe_key(ITEM)
        d = B.Path(td) / B.INFLIGHT
        d.mkdir(parents=True)
        (d / B.SEP.join((key, "zeta", str(DEAD_PID), "1"))).write_text("{}")
        (d / B.SEP.join((key, "alpha", str(DEAD_PID), "2"))).write_text("{}")
        got = B.holder(td, ITEM)
        if got != "alpha":
            return (f"dead-only holder returned {got!r}; contract says the "
                    "most recent incarnation (birth 2 = 'alpha')")
    return None


def _naive_holder_adversarial(root, item_id):
    """Pre-fix holder semantics: iterdir gives no ordering guarantee, so any
    order is a legal old behavior — this instantiates the adversarial one."""
    key = B.safe_key(item_id)
    for f in sorted(B._d(root, B.INFLIGHT).iterdir(), reverse=True):
        parts = f.name.split(B.SEP)
        if len(parts) == 4 and parts[0] == key:
            return parts[1]
    return None


def main() -> int:
    prove_bite = "--prove-bite" in sys.argv
    if prove_bite:
        saved_inflight, saved_holder = B._inflight_tokens, B.holder
        B._inflight_tokens = lambda root, key: []
        v1 = ce1_publish_while_inflight()
        B._inflight_tokens = saved_inflight
        if v1 is None:
            print("FAIL: CE-1 did not reproduce with the fix blinded")
            return 1
        print(f"PASS (falsifier): CE-1 reproduces blinded: {v1}")
        B.holder = _naive_holder_adversarial
        v2 = ce2_holder_names_the_live_owner()
        v2b = ce2b_dead_only_holder_is_deterministic()
        B.holder = saved_holder
        if v2 is None:
            print("FAIL: CE-2 did not reproduce with naive holder")
            return 1
        print(f"PASS (falsifier): CE-2 reproduces with naive holder: {v2}")
        if v2b is None:
            print("FAIL: CE-2b did not reproduce with naive holder")
            return 1
        print(f"PASS (falsifier): CE-2b reproduces with naive holder: {v2b}")
        return 0
    rc = 0
    for name, fn in (("CE-1 publish-while-inflight", ce1_publish_while_inflight),
                     ("CE-2 holder-names-live-owner", ce2_holder_names_the_live_owner),
                     ("CE-2b dead-only holder deterministic", ce2b_dead_only_holder_is_deterministic),
                     ("CE-3 recover-to-claim handoff accepted", ce3_recover_to_claim_handoff_is_legal)):
        v = fn()
        if v is not None:
            print(f"FAIL: {name}: {v}")
            rc = 1
        else:
            print(f"PASS: {name}")
    return rc


if __name__ == "__main__":
    sys.exit(main())
