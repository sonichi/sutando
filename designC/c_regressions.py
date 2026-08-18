#!/usr/bin/env python3
"""Frozen counterexample for the Design C foundation.

CE-6  republish ABA (found by review of designB.py's token, 2026-08-17):
      B's token is key~worker~pid~birth -- unique per claimer INCARNATION,
      which is not the same thing as unique per claim. complete -> republish
      the same item_id -> reclaim by the SAME live process reproduces the
      byte-identical name, so a finalize left over from the first claim epoch
      lands on the second epoch's claim. Reachable the moment a republishing
      state (retry/, parked/, dlq/) exists; not reachable through recover,
      which only re-arms DEAD or birth-mismatched holders.

      C adds a per-claim generation to the name, so no two claims share it and
      the stale finalize finds nothing to rename.

Falsifier mode (--prove-bite): blind C's generation to a constant and assert
the ABA REPRODUCES. A regression that cannot fail proves nothing -- without
this arm, C's arm would pass even if the assertion were testing the lock, the
worker name, or nothing at all.
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
sys.path.insert(0, os.path.join(os.path.dirname(_HERE), "designB-eval"))

import designB as B  # noqa: E402
import designC as C  # noqa: E402

ITEM = "room-evt-1"
WORKER = "drainer-A"


def _aba(mod) -> dict:
    """Run the republish schedule against one module; report what happened."""
    root = tempfile.mkdtemp(prefix=f"aba-{mod.__name__}-")
    getattr(mod, "init", lambda _r: None)(root)   # C activates striping up front
    try:
        assert mod.publish(root, ITEM, "payload") is True, "epoch-1 publish"
        tok1 = mod.claim(root, ITEM, WORKER)
        assert tok1, "epoch-1 claim"
        assert mod.complete(root, tok1) is True, "epoch-1 complete"

        assert mod.publish(root, ITEM, "payload") is True, "republish"
        tok2 = mod.claim(root, ITEM, WORKER)
        assert tok2, "epoch-2 claim"

        stale_won = mod.complete(root, tok1, terminal=mod.PARKED)
        owner_won = mod.complete(root, tok2)
        return {"same_name": tok1 == tok2, "stale_won": stale_won,
                "owner_won": owner_won, "tok1": tok1, "tok2": tok2}
    finally:
        shutil.rmtree(root, ignore_errors=True)


def ce6_republish_aba(mod) -> str | None:
    """None if the invariant held, else the violation message.

    Invariant: a token from a finished claim epoch has no authority over the
    claim that is live now. Its finalize must be refused, and the live
    owner's own finalize must still succeed."""
    r = _aba(mod)
    if r["stale_won"] or not r["owner_won"]:
        return (f"stale finalize from a dead claim epoch was honored: "
                f"same_name={r['same_name']} stale_won={r['stale_won']} "
                f"owner_won={r['owner_won']} token={r['tok1']}")
    return None


def main() -> int:
    prove_bite = "--prove-bite" in sys.argv
    failures = 0

    b = ce6_republish_aba(B)
    print(f"CE-6 vs Design B : {'BITES (expected)' if b else 'HELD'}")
    if b:
        print(f"                   {b}")
    else:
        print("  UNEXPECTED: B did not reproduce the ABA -- the counterexample "
              "no longer models the defect it was written for.")
        failures += 1

    c = ce6_republish_aba(C)
    print(f"CE-6 vs Design C : {'VIOLATED' if c else 'HELD'}")
    if c:
        print(f"                   {c}")
        failures += 1

    if prove_bite:
        saved = C._generation
        C._generation = lambda: "blinded"
        try:
            blinded = ce6_republish_aba(C)
        finally:
            C._generation = saved
        print(f"CE-6 vs C(blind) : {'BITES (expected)' if blinded else 'HELD'}")
        if not blinded:
            print("  FALSIFIER FAILED: blinding the generation did not "
                  "reproduce the ABA, so C's arm is not testing it.")
            failures += 1

    print(f"\n{'FAIL' if failures else 'PASS'}: {failures} unexpected outcome(s)")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
