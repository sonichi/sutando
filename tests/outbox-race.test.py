import json
import multiprocessing as mp
import os
import sys
import tempfile
import time
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


def run_rounds(per_phase_s, min_rounds, max_rounds, one_round):
    """Repeat one_round until max_rounds or the phase budget runs out.

    A broken lock fails on nearly every round, so min_rounds keeps the power;
    the extra rounds only chase rare interleavings and are the first thing a
    thrashing 2-core CI runner can no longer afford inside its 120s file cap.
    The budget gates round STARTS: worst case is min_rounds + one budget's
    worth of starts, each running to completion.
    """
    results, t0 = [], time.monotonic()
    for r in range(max_rounds):
        if r >= min_rounds and time.monotonic() - t0 > per_phase_s:
            break
        results.append(one_round(r))
    return results

if __name__ == "__main__":
    # Contention is workers-per-core, not an absolute: a fixed 24 is 12x
    # oversubscribed on a 2-core runner, and the thrash is what timed out.
    # Under the coverage gate every pool child pays instrumented startup, and
    # the 240s file cap fired mid-spawn on a thrashed lane (2026-09-02, x3 PRs).
    INSTRUMENTED = os.environ.get("SUTANDO_TEST_SUBPROCESS_COVERAGE") == "1"
    N = max(4, (os.cpu_count() or 2)) if INSTRUMENTED else max(8, (os.cpu_count() or 2) * 3)
    # Instrumented floor is halved to 2 (phase 2 runs MIN_ROUNDS*2 -> 6 guaranteed
    # rounds, not 9) with MAX == floor, so the 240s coverage cap holds. Why: PR body.
    MIN_ROUNDS = 2 if INSTRUMENTED else 3
    MAX_ROUNDS = MIN_ROUNDS if INSTRUMENTED else 12
    # Bounds round STARTS only (moot when MAX == floor, i.e. instrumented): worst
    # case off-instrumentation is floor + budget + in-flight round durations.
    PHASE_BUDGET_S = float(os.environ.get("OUTBOX_RACE_PHASE_BUDGET_S", "35"))
    totals = []
    # ONE warm pool here too, for the reason phase 2 already states: a fresh pool
    # per round pays process startup inside the window under test.
    with mp.Pool(N) as pool:
        def acquire_round(r):
            with tempfile.TemporaryDirectory() as tmp:
                os.makedirs(os.path.join(tmp, ".claims"), exist_ok=True)  # pre-make so mkdir isn't the serializer
                got = pool.map(worker, [(tmp, f"item-race-{r}", i) for i in range(N)])
            return sum(got)
        totals = run_rounds(PHASE_BUDGET_S, MIN_ROUNDS, MAX_ROUNDS, acquire_round)
    print(f"  {len(totals)} of {MAX_ROUNDS} rounds x {N} concurrent PROCESSES racing one item"
          + (" (stopped at phase budget)" if len(totals) < MAX_ROUNDS else ""))
    print(f"  winners per round: {totals}")
    bad = [t for t in totals if t != 1]
    print(f"  rounds with != 1 winner: {len(bad)}  {'<-- EXCLUSION BROKEN' if bad else '(exclusion holds)'}")
    if bad:
        print("FAILED — more than one drainer acquired the same item")
        raise SystemExit(1)

    # Phase 2 — RECLAIM of a dead owner's claim. Exclusion on acquire says
    # nothing about two drainers acting on one stale observation.
    orphan_flags = []
    # ONE warm pool for every round. A fresh pool per round pays process startup
    # inside the window under test, which staggers the workers and hides the race.
    with mp.Pool(N) as pool:
        def reclaim_round(r):
            with tempfile.TemporaryDirectory() as tmp:
                item = f"item-reclaim-{r}"
                plant_stale_claim(tmp, item)
                got = pool.map(reclaim_worker, [(tmp, item, i) for i in range(N)], chunksize=1)
                winners = [g for g in got if g]
                held = read_delivery_claim(tmp, item)
                # The winner must still hold it: a loser that unlinks unconditionally
                # destroys the winner's claim and the item is delivered twice.
                orphan_flags.append(
                    len(winners) == 1 and (held is None or held.drainer_id != winners[0]))
                return len(winners)
        rc_totals = run_rounds(PHASE_BUDGET_S, MIN_ROUNDS * 2, MAX_ROUNDS * 2, reclaim_round)  # floor 6 either way
    orphaned = sum(orphan_flags)
    print(f"\n  {len(rc_totals)} of {MAX_ROUNDS * 2} rounds x {N} concurrent PROCESSES reclaiming one dead owner's claim"
          + (" (stopped at phase budget)" if len(rc_totals) < MAX_ROUNDS * 2 else ""))
    print(f"  winners per round: {rc_totals}")
    bad_rc = [t for t in rc_totals if t != 1]
    print(f"  rounds with != 1 winner: {len(bad_rc)}  "
          f"{'<-- RECLAIM EXCLUSION BROKEN' if bad_rc else '(exclusion holds)'}")
    print(f"  rounds where the winner's claim was destroyed by a loser: {orphaned}")
    if bad_rc or orphaned:
        print("FAILED — reclaim is not atomic; two drainers can hold one item")
        raise SystemExit(1)
    print("PASS — delivery-claim exclusion holds under real concurrency, on acquire AND reclaim")
