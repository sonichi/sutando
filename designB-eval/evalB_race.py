#!/usr/bin/env python3
"""Design B race — same load shape as A's merged harness: warm mp.Pool,
24 workers racing to claim ONE item per round, 24 rounds + 300 amplified.
Invariant: exactly one winner per round; losers see None; the winner's
token names a file that exists; no round leaves 0 or 2+ owners."""
from __future__ import annotations
import multiprocessing as mp
import os
import pathlib
import shutil
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import designB as B


def racer(args):
    root, item, w = args
    return (w, B.claim(root, item, f"w{w}"))


def one_round(pool, nproc, rnd):
    root = tempfile.mkdtemp()
    item = f"it{rnd}"
    B.publish(root, item, "x")
    results = pool.map(racer, [(root, item, w) for w in range(nproc)])
    winners = [(w, t) for w, t in results if t]
    inflight = list((pathlib.Path(root) / B.INFLIGHT).iterdir())
    ready_left = list((pathlib.Path(root) / B.READY).iterdir())
    ok = len(winners) == 1 and len(inflight) == 1 and len(ready_left) == 0 \
        and inflight[0].name == winners[0][1]
    shutil.rmtree(root, ignore_errors=True)
    return ok, len(winners)


def main():
    nproc, rounds, amplified = 24, 24, 300
    with mp.Pool(nproc) as pool:          # warm pool — the lesson from A's harness
        bad = []
        for r in range(rounds):
            ok, nw = one_round(pool, nproc, r)
            if not ok:
                bad.append((r, nw))
        print(f"  {rounds} rounds x {nproc} procs: {len(bad)} bad rounds {bad or ''}")
        badA = 0
        for r in range(amplified):
            ok, nw = one_round(pool, nproc, 1000 + r)
            if not ok:
                badA += 1
        print(f"  amplified {amplified} rounds: {badA} bad")
    sys.exit(1 if (bad or badA) else 0)


if __name__ == "__main__":
    main()
