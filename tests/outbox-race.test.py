import json
import multiprocessing as mp
import os
import sys
import tempfile
# CI runs this with no arguments, so the repo root must be derived, not passed.
REPO = sys.argv[1] if len(sys.argv) > 1 else os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, 'src'))
from outbox import (                        # PRODUCTION writers
    acquire_delivery_claim,
    reclaim_delivery_claim,
    read_delivery_claim,
    _claim_path,
)

def worker(args):
    root, item, i = args
    return 1 if acquire_delivery_claim(root, item, drainer_id=f"d{i}") else 0


def reclaim_worker(args):
    root, item, i = args
    return f"d{i}" if reclaim_delivery_claim(root, item, 1.0, f"d{i}") else None


def plant_stale_claim(root, item):
    """A claim held by a pid that is not running, long past any TTL."""
    os.makedirs(os.path.join(root, ".claims"), exist_ok=True)
    _claim_path(root, item).write_text(json.dumps({
        "item_id": item, "drainer_id": "dead-owner", "pid": 999999,
        "start_usec": 1, "claimed_at": 0.0,
    }, sort_keys=True), encoding="utf-8")

if __name__ == "__main__":
    N, rounds = 24, 12
    totals = []
    # ONE warm pool here too, for the reason phase 2 already states: a fresh pool
    # per round pays process startup inside the window under test.
    with mp.Pool(N) as pool:
        for r in range(rounds):
            with tempfile.TemporaryDirectory() as tmp:
                os.makedirs(os.path.join(tmp, ".claims"), exist_ok=True)  # pre-make so mkdir isn't the serializer
                got = pool.map(worker, [(tmp, f"item-race-{r}", i) for i in range(N)])
            totals.append(sum(got))
    print(f"  {rounds} rounds x {N} concurrent PROCESSES racing one item")
    print(f"  winners per round: {totals}")
    bad = [t for t in totals if t != 1]
    print(f"  rounds with != 1 winner: {len(bad)}  {'<-- EXCLUSION BROKEN' if bad else '(exclusion holds)'}")
    if bad:
        print("FAILED — more than one drainer acquired the same item")
        raise SystemExit(1)

    # Phase 2 — RECLAIM of a dead owner's claim. Exclusion on acquire says
    # nothing about two drainers acting on one stale observation.
    rc_totals, orphaned = [], 0
    # ONE warm pool for every round. A fresh pool per round pays process startup
    # inside the window under test, which staggers the workers and hides the race.
    with mp.Pool(N) as pool:
        for r in range(rounds * 2):
            with tempfile.TemporaryDirectory() as tmp:
                item = f"item-reclaim-{r}"
                plant_stale_claim(tmp, item)
                got = pool.map(reclaim_worker, [(tmp, item, i) for i in range(N)], chunksize=1)
                winners = [g for g in got if g]
                rc_totals.append(len(winners))
                held = read_delivery_claim(tmp, item)
                # The winner must still hold it: a loser that unlinks unconditionally
                # destroys the winner's claim and the item is delivered twice.
                if len(winners) == 1 and (held is None or held.drainer_id != winners[0]):
                    orphaned += 1
    print(f"\n  {rounds} rounds x {N} concurrent PROCESSES reclaiming one dead owner's claim")
    print(f"  winners per round: {rc_totals}")
    bad_rc = [t for t in rc_totals if t != 1]
    print(f"  rounds with != 1 winner: {len(bad_rc)}  "
          f"{'<-- RECLAIM EXCLUSION BROKEN' if bad_rc else '(exclusion holds)'}")
    print(f"  rounds where the winner's claim was destroyed by a loser: {orphaned}")
    if bad_rc or orphaned:
        print("FAILED — reclaim is not atomic; two drainers can hold one item")
        raise SystemExit(1)
    print("PASS — delivery-claim exclusion holds under real concurrency, on acquire AND reclaim")
