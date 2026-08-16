#!/usr/bin/env python3
"""The outbox's recovery and bookkeeping paths — the branches a happy path skips.

The contract suite covers the protocol's guarantees. These cover the mechanics
those guarantees rest on: what an absent claim reads as, what a corrupt one reads
as, what a dead owner past its TTL permits, and that the attempt counter is a
counter. Each is a branch that only runs when something has already gone wrong,
which is exactly when nobody is watching.

Run: python3 tests/outbox-recovery-paths.test.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

import outbox as ob  # noqa: E402

FAILS: list[str] = []


def check(title: str, ok: bool, detail: str = "") -> None:
    if ok:
        print(f"  ok   {title}")
    else:
        FAILS.append(title)
        print(f"  FAIL {title}\n         {detail}")


def main() -> int:
    O, S = ob.DeliveryOutcome, ob.RetrySafety

    # resolve_outcome — every arm
    check("CONFIRMED is done", ob.resolve_outcome(O.CONFIRMED, S.UNSAFE, 0) == "done")
    check("NOT_DELIVERED retries within budget",
          ob.resolve_outcome(O.NOT_DELIVERED, S.UNSAFE, 0) == "retry")
    check("NOT_DELIVERED parks at the budget",
          ob.resolve_outcome(O.NOT_DELIVERED, S.UNSAFE, 99) == "park")
    check("UNKNOWN + SAFE may retry (idempotent destination)",
          ob.resolve_outcome(O.OUTCOME_UNKNOWN, S.SAFE, 0) == "retry")
    check("UNKNOWN + SAFE still parks at the budget",
          ob.resolve_outcome(O.OUTCOME_UNKNOWN, S.SAFE, 99) == "park")

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)

        # read_delivery_claim — absent vs corrupt
        check("absent claim reads None (free), not UNKNOWN",
              ob.read_delivery_claim(root, "nope") is None,
              "absent must mean free; UNKNOWN there would freeze the item forever")
        ob.acquire_delivery_claim(root, "corrupt", drainer_id="d")
        p = next((root / ob.CLAIMS_DIR).glob("corrupt*"))
        p.write_text("{not json", encoding="utf-8")
        rec = ob.read_delivery_claim(root, "corrupt")
        check("unparseable claim reads UNKNOWN, never free",
              rec is not None and rec.state == "UNKNOWN",
              f"got {rec!r}; treating a corrupt claim as absent re-sends a live item")

        # may_reclaim — the three gates
        check("nothing held -> reclaimable", ob.may_reclaim_delivery(root, "nope", 0) is True)
        check("torn claim is NOT reclaimable",
              ob.may_reclaim_delivery(root, "corrupt", 0) is False,
              "we do not know who holds it; stealing here duplicates")
        ob.acquire_delivery_claim(root, "dead", drainer_id="d")
        dp = next((root / ob.CLAIMS_DIR).glob("dead*"))
        d = json.loads(dp.read_text())
        d["pid"] = 999_999            # a pid that cannot exist
        d["claimed_at"] = 0.0         # long past any TTL
        dp.write_text(json.dumps(d), encoding="utf-8")
        check("dead owner past TTL IS reclaimable",
              ob.may_reclaim_delivery(root, "dead", 1.0) is True,
              "a genuinely dead owner must release, or the queue stalls forever")

        # attempts + release
        ob.acquire_delivery_claim(root, "i", drainer_id="d")
        check("attempts start at zero", ob.attempts_for(root, "i") == 0)
        ob.note_attempt(root, "i"); ob.note_attempt(root, "i")
        check("note_attempt counts", ob.attempts_for(root, "i") == 2)
        ob.park_item(root, "i", reason="unconfirmed")
        ob.requeue_item(root, "i")
        check("requeue clears the budget", ob.attempts_for(root, "i") == 0)
        check("requeue also releases the claim",
              ob.read_delivery_claim(root, "i") is None,
              "a re-queued item still holding its claim can never be picked up")
        ob.release_delivery_claim(root, "not-there", "d1")   # must not raise
        check("releasing an absent claim is a no-op", True)

        # force= skips the ownership read, so it reaches the unlink and takes the
        # FileNotFoundError branch. The ownership path returns before that.
        check("a forced release of an absent claim reports False, not True",
              ob.release_delivery_claim(root, "not-there", force=True) is False,
              "returning True would tell a caller it released something that was never there")

        # Swap tokens are CAS scratch, not state: a release must sweep them, or a
        # crashed reclaim leaves a name that blocks every future swap on that item.
        ob.acquire_delivery_claim(root, "sweep", "d1")
        claim = ob._claim_path(root, "sweep")
        for suffix in ("aaa", "bbb"):
            (claim.parent / f"{claim.name}.reclaim-{suffix}").write_text("x", encoding="utf-8")
        check("release sweeps leftover swap tokens",
              ob.release_delivery_claim(root, "sweep", "d1") is True
              and not list(claim.parent.glob(f"{claim.name}.reclaim-*")),
              "a stale token makes os.link fail forever, so the item can never be reclaimed")

        # A token that vanishes mid-sweep (a peer swept concurrently) must not
        # abort the release — that would strand the claim it just unlinked.
        ob.acquire_delivery_claim(root, "vanish", "d1")
        vclaim = ob._claim_path(root, "vanish")
        (vclaim.parent / f"{vclaim.name}.reclaim-zzz").write_text("x", encoding="utf-8")
        real_unlink = Path.unlink

        def vanishing(self, *a, **kw):
            if ".reclaim-" in self.name:
                raise FileNotFoundError(str(self))
            return real_unlink(self, *a, **kw)

        Path.unlink = vanishing
        try:
            released = ob.release_delivery_claim(root, "vanish", "d1")
        finally:
            Path.unlink = real_unlink
        check("a swap token deleted by a peer mid-sweep does not abort the release",
              released is True,
              "raising here would leave the claim released but the caller believing it failed")

        # --- adapter: the paths the contract suite skipped -------------------
        import outbox_adapter as oa

        # send() SUCCESS path. The contract suite only drove the raising one, so
        # the ordinary return was never executed by any test.
        class Fake(oa.DeliveryAdapter):
            def _transmit(self, item):
                return 200, {"ok": True, "event_id": "$id"}

        r = Fake().send({"item_id": "x", "body": "b"})
        check("adapter send() returns a receipt on the success path",
              r.outcome is O.CONFIRMED and r.receipt_id == "$id",
              f"got {r!r}")

        # the base class must refuse to be used directly
        try:
            oa.DeliveryAdapter()._transmit({})
            base_raised = False
        except NotImplementedError:
            base_raised = True
        check("base DeliveryAdapter._transmit refuses to run",
              base_raised, "a base adapter that silently returns None would look like a timeout")

        # AG2SpaceAdapter wires room + poster and passes the body through
        seen = {}
        def poster(room, body):
            seen["room"], seen["body"] = room, body
            return 200, {"ok": True, "event_id": "$e"}
        r2 = oa.AG2SpaceAdapter(poster, "!r:ag2.space").send({"item_id": "y", "body": "hello"})
        check("AG2SpaceAdapter posts body to its room and reports CONFIRMED",
              r2.outcome is O.CONFIRMED and seen == {"room": "!r:ag2.space", "body": "hello"},
              f"got {r2!r} / {seen!r}")

        # --- a crash mid-claim must LEAVE the torn file ----------------------
        real_fdopen = os.fdopen
        def boom(*a, **k):
            raise OSError("disk went away mid-write")
        os.fdopen = boom
        try:
            ob.acquire_delivery_claim(root, "torn", drainer_id="d")
            crashed = False
        except OSError:
            crashed = True
        finally:
            os.fdopen = real_fdopen
        check("a crash during claim creation propagates", crashed)
        check("and leaves the claim file behind (readable as UNKNOWN, not absent)",
              ob.read_delivery_claim(root, "torn") is not None,
              "removing it on crash makes a half-done delivery look like it never started")

    print(f"\n  {len(FAILS)} failure(s)")
    if FAILS:
        print("\nFAILED")
        return 1
    print("\nPASS — recovery and bookkeeping paths hold")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
