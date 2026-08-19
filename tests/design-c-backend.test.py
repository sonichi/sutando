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
import json
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

    # Assert-don't-perform (yixuan, #3104): constructing on an unfenced root
    # must refuse with the migration instruction, never write the fence.
    with tempfile.TemporaryDirectory() as td:
        try:
            DesignCClaimBackend(Path(td))
            check("unfenced root refused without activate=True", False)
        except RuntimeError as e:
            check("unfenced root refused without activate=True",
                  "not stripe-fenced" in str(e))
        check("refusal did not write the fence",
              not backend_c.outbox._fence_path(Path(td)).exists())
        b = DesignCClaimBackend(Path(td), activate=True)
        check("deploy-path activation fences the root",
              backend_c.outbox._fence_path(Path(td)).exists())
        check("fenced root then constructs without activate",
              DesignCClaimBackend(Path(td)) is not None)

    # Fence VALIDITY, not existence (Codex, #3104): a corrupt fence and a
    # wrong-stripe-count fence must both fail construction closed.
    with tempfile.TemporaryDirectory() as td:
        fp = backend_c.outbox._fence_path(Path(td))
        fp.parent.mkdir(parents=True, exist_ok=True)
        fp.write_text("not-json")
        try:
            DesignCClaimBackend(Path(td))
            check("corrupt fence refused at construction", False)
        except RuntimeError as e:
            check("corrupt fence refused at construction",
                  "unreadable stripes fence" in str(e))
    with tempfile.TemporaryDirectory() as td:
        fp = backend_c.outbox._fence_path(Path(td))
        fp.parent.mkdir(parents=True, exist_ok=True)
        fp.write_text(json.dumps(
            {"stripes": backend_c.outbox.LOCK_STRIPES + 1}))
        try:
            DesignCClaimBackend(Path(td))
            check("mismatched stripe-count fence refused", False)
        except RuntimeError as e:
            check("mismatched stripe-count fence refused",
                  "migration required" in str(e))

    # CE-6: complete -> republish -> reclaim; the stale token must be inert.
    with tempfile.TemporaryDirectory() as td:
        b = DesignCClaimBackend(Path(td), activate=True)
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
        b = DesignCClaimBackend(Path(td), activate=True)
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
        b = DesignCClaimBackend(Path(td), activate=True)
        plant_dead_ghost(b, ITEM)
        rep = b.recover()
        check("ghost-2: lone dead token re-arms",
              backend_c._safe_key(ITEM) in rep.recovered)
        check("ghost-2: re-armed item is claimable",
              b.claim(ITEM, "drainer-B") is not None)

    # SLOT: publish refused while the id is live in either namespace.
    with tempfile.TemporaryDirectory() as td:
        b = DesignCClaimBackend(Path(td), activate=True)
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
        b = DesignCClaimBackend(Path(td), activate=True)
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



    # PID-REUSE (Codex blocking finding): an ALIVE pid whose birth mismatches
    # the token is a reused pid — the claimant is dead; recover must re-arm.
    with tempfile.TemporaryDirectory() as td:
        b = DesignCClaimBackend(Path(td), activate=True)
        key = backend_c._safe_key(ITEM)
        reused = b.root / "inflight" / SEP.join(
            (key, "ghost", str(os.getpid()), "1", "0"))   # alive pid, wrong birth
        reused.write_text("{}", encoding="utf-8")
        rep = b.recover()
        check("pid-reuse: reused-pid token re-arms", key in rep.recovered)
        check("pid-reuse: item claimable again",
              b.claim(ITEM, "drainer-B") is not None)
        # control: a token carrying THIS process's REAL birth is a live
        # holder and must never be recovered.
        ident = backend_c.outbox.process_identity(os.getpid())
        live = b.root / "inflight" / SEP.join(
            (key + "x", "w", str(os.getpid()), str(ident.start_usec), "g1"))
        live.write_text("{}", encoding="utf-8")
        rep = b.recover()
        check("pid-reuse control: genuine live holder untouched",
              (key + "x") not in rep.recovered and live.exists())

    # cleanup must not reset a LIVE item's park ceiling (air's #3104 nit).
    with tempfile.TemporaryDirectory() as td:
        b = DesignCClaimBackend(Path(td), activate=True)
        b.publish(ITEM, b"x")
        t = b.claim(ITEM, "w")
        b.complete(t, DeliveryOutcome.NOT_DELIVERED, park_at_attempts=5)
        rep = b.cleanup(max_age_s=0.0)   # everything is "old" at age 0
        check("cleanup: live item's attempts survive age-based prune",
              b.attempts(ITEM) == 1)
        b.claim(ITEM, "w")   # consume ready; then archive it via CONFIRMED
        t = b.claim(ITEM, "w") or t
        # item now inflight-or-archived; drop the live object entirely:
        for f in (b.root / "inflight").iterdir():
            f.unlink()
        b.cleanup(max_age_s=0.0)
        check("cleanup: dead key's attempts are pruned",
              b.attempts(ITEM) == 0)

    if FAILS:
        print(f"\nFAILED {len(FAILS)}: {FAILS}", file=sys.stderr)
        return 1
    print("\nPASS: DesignCClaimBackend — CE-6 inert stale epoch, anticipated "
          "ghost states, structural one-slot, park ceiling")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
