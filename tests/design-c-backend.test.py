#!/usr/bin/env python3
"""DesignCClaimBackend regressions — the properties Design C exists for,
beyond the backend-agnostic contract suite:

  CE-6   a token from a finished claim epoch has NO authority over the claim
         live now (per-claim generation; the republish ABA that bites B).
  GHOST  dead-ghost-beside-live-claim is an anticipated intermediate: no
         invariant raise (the 800x60 finding on the any-two-tokens variant),
         recover quarantines the ghost instead of re-arming a duplicate.
  SLOT   one live ready slot per item, structurally (EEXIST refusal).

Run: python3 tests/design-c-backend.test.py"""
# ruff: noqa: E402 — imports follow the sys.path insert below
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "packages" / "ag2-sparrow"))

import os

from ag2_sparrow.delivery_core import DeliveryOutcome
from ag2_sparrow.delivery_core import backend_c
from ag2_sparrow.delivery_core.backend_c import SEP, DesignCClaimBackend

FAILS = []
ITEM = "room-evt-1"


def check(name, cond, detail=""):
    if cond:
        print(f"  ok: {name}")
    else:
        FAILS.append(name)
        print(f"  FAIL: {name} {detail}", file=sys.stderr)


def plant_dead_ghost(b, item):
    """A crashed prior incarnation's token: dead pid, epoch birth."""
    key = backend_c._safe_key(item)
    ghost = b.root / "inflight" / SEP.join((key, "ghost", "99999", "1", "0"))
    ghost.write_text("{}", encoding="utf-8")
    return ghost


def main() -> int:
    try:
        os.kill(99999, 0)
        print("SKIP-INVALID: pid 99999 alive on this host", file=sys.stderr)
        return 1
    except ProcessLookupError:
        pass

    # CE-6: complete -> republish -> reclaim; the stale token must be inert.
    with tempfile.TemporaryDirectory() as td:
        b = DesignCClaimBackend(Path(td))
        check("publish", b.publish(ITEM, b"v1"))
        t1 = b.claim(ITEM, "drainer-A")
        check("claim epoch 1", t1 is not None)
        check("complete epoch 1", b.complete(t1, DeliveryOutcome.CONFIRMED))
        check("republish", b.publish(ITEM, b"v2"))
        t2 = b.claim(ITEM, "drainer-A")
        check("claim epoch 2 (same worker, same process)", t2 is not None)
        check("CE-6: tokens differ across epochs", t1.incarnation != t2.incarnation)
        check("CE-6: stale finalize refused",
              b.complete(t1, DeliveryOutcome.CONFIRMED) is False)
        check("CE-6: live owner's finalize still lands",
              b.complete(t2, DeliveryOutcome.CONFIRMED) is True)

    # GHOST: dead token beside a live claim — anticipated, never a raise.
    with tempfile.TemporaryDirectory() as td:
        b = DesignCClaimBackend(Path(td))
        b.publish(ITEM, b"x")
        plant_dead_ghost(b, ITEM)
        try:
            t = b.claim(ITEM, "drainer-A")
            check("ghost: claim beside dead ghost succeeds, no raise",
                  t is not None)
        except backend_c.InvariantError as e:
            check("ghost: claim beside dead ghost succeeds, no raise", False, str(e))
            t = None
        rep = b.recover()
        check("ghost: recover quarantines the ghost (live holder present)",
              backend_c._safe_key(ITEM) in rep.quarantined and not rep.recovered)
        if t:
            check("ghost: live claim completes normally",
                  b.complete(t, DeliveryOutcome.CONFIRMED))

    # GHOST-2: dead token with NO live holder re-arms to ready.
    with tempfile.TemporaryDirectory() as td:
        b = DesignCClaimBackend(Path(td))
        plant_dead_ghost(b, ITEM)
        rep = b.recover()
        check("ghost-2: lone dead token re-arms",
              backend_c._safe_key(ITEM) in rep.recovered)
        check("ghost-2: re-armed item is claimable",
              b.claim(ITEM, "drainer-B") is not None)

    # SLOT: publish refused while the id is live in either namespace.
    with tempfile.TemporaryDirectory() as td:
        b = DesignCClaimBackend(Path(td))
        check("slot: first publish", b.publish(ITEM, b"x"))
        check("slot: second publish refused (ready occupied)",
              b.publish(ITEM, b"x") is False)
        t = b.claim(ITEM, "w")
        check("slot: publish refused while in flight",
              b.publish(ITEM, b"x") is False)
        b.complete(t, DeliveryOutcome.NOT_DELIVERED)   # -> back to ready
        check("slot: retryable completion re-arms exactly one slot",
              (b.root / "ready").exists() and b.claim(ITEM, "w2") is not None)

    # attempts + park ceiling through the contract surface.
    with tempfile.TemporaryDirectory() as td:
        b = DesignCClaimBackend(Path(td))
        b.publish(ITEM, b"x")
        t = b.claim(ITEM, "w")
        b.complete(t, DeliveryOutcome.NOT_DELIVERED, park_at_attempts=2)
        check("attempts: first failure recorded", b.attempts(ITEM) == 1)
        t = b.claim(ITEM, "w")
        b.complete(t, DeliveryOutcome.NOT_DELIVERED, park_at_attempts=2)
        check("attempts: ceiling parks (item no longer claimable)",
              b.claim(ITEM, "w") is None)
        parked = list((b.root / "undelivered").iterdir())
        check("attempts: parked record names max-attempts",
              any("max-attempts" in p.name for p in parked))

    if FAILS:
        print(f"\nFAILED {len(FAILS)}: {FAILS}", file=sys.stderr)
        return 1
    print("\nPASS: DesignCClaimBackend — CE-6 inert stale epoch, anticipated "
          "ghost states, structural one-slot, park ceiling")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
