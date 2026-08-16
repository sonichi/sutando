import sys, os, tempfile, multiprocessing as mp
# CI runs this with no arguments, so the repo root must be derived, not passed.
REPO = sys.argv[1] if len(sys.argv) > 1 else os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, 'packages/ag2-sparrow'))
from ag2_sparrow.outbox import acquire_delivery_claim   # PRODUCTION writer

def worker(args):
    root, item, i = args
    return 1 if acquire_delivery_claim(root, item, drainer_id=f"d{i}") else 0

if __name__ == "__main__":
    N, rounds = 24, 12
    totals = []
    for r in range(rounds):
        with tempfile.TemporaryDirectory() as tmp:
            os.makedirs(os.path.join(tmp, ".claims"), exist_ok=True)  # pre-make so mkdir isn't the serializer
            with mp.Pool(N) as pool:
                got = pool.map(worker, [(tmp, f"item-race-{r}", i) for i in range(N)])
        totals.append(sum(got))
    print(f"  {rounds} rounds x {N} concurrent PROCESSES racing one item")
    print(f"  winners per round: {totals}")
    bad = [t for t in totals if t != 1]
    print(f"  rounds with != 1 winner: {len(bad)}  {'<-- EXCLUSION BROKEN' if bad else '(exclusion holds)'}")
    if bad:
        print("FAILED — more than one drainer acquired the same item")
        raise SystemExit(1)
    print("PASS — delivery-claim exclusion holds under real concurrency")
