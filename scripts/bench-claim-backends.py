#!/usr/bin/env python3
"""Durable A-vs-C claim-backend benchmark (owner ask, 2026-08-22).

Measures the dimensions a quick micro-bench misses, so numbers stay
comparable across heads and hosts:

  cycle     single-worker publish -> claim -> complete, per-item cost
  procs     cross-PROCESS contention (real bridges are processes, and the
            striped flock is a cross-process mechanism; threads understate it)
  unknown   the failure path: complete(OUTCOME_UNKNOWN) then recover()
  archive   complete cost as the archive grows (does history slow the hot path?)
  payload   1 KiB vs 64 KiB items

Emits a human table on stdout and, with --json PATH, a machine-readable
record {host, head, timestamp?, scenarios} for cross-run comparison.
Timestamps come from --stamp (callers pass `date -u`); the script itself
stays clock-free so runs are diffable.

Run:  python3 scripts/bench-claim-backends.py [--items 300] [--json out.json]
"""
from __future__ import annotations

import argparse
import json
import multiprocessing as mp
import shutil
import statistics
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "packages" / "ag2-sparrow"))

from ag2_sparrow.delivery_core.backend_a import DesignAClaimBackend  # noqa: E402
from ag2_sparrow.delivery_core.backend_c import DesignCClaimBackend  # noqa: E402

from ag2_sparrow.delivery_core.contract import DeliveryOutcome  # noqa: E402

BACKENDS = {
    "a": lambda root: DesignAClaimBackend(root),
    "c": lambda root: DesignCClaimBackend(root, activate=True),
}


def _timer():
    import time
    return time.perf_counter


def _fresh(label: str):
    td = Path(tempfile.mkdtemp(prefix=f"bench-{label}-"))
    return td, td / "root"


def bench_cycle(kind: str, n: int, payload: bytes, rounds: int = 3) -> dict:
    perf = _timer()
    runs = []
    for _ in range(rounds):
        td, root = _fresh(f"cycle-{kind}")
        try:
            b = BACKENDS[kind](root)
            t0 = perf()
            for i in range(n):
                b.publish(f"item-{i}", payload)
            t1 = perf()
            toks = [b.claim(f"item-{i}", "w0") for i in range(n)]
            t2 = perf()
            for t in toks:
                b.complete(t, DeliveryOutcome.CONFIRMED)
            t3 = perf()
            runs.append((t1 - t0, t2 - t1, t3 - t2))
        finally:
            shutil.rmtree(td, ignore_errors=True)
    med = [statistics.median(r[i] for r in runs) for i in range(3)]
    return {"publish_us": med[0] * 1e6 / n, "claim_us": med[1] * 1e6 / n,
            "complete_us": med[2] * 1e6 / n,
            "cycle_us": sum(med) * 1e6 / n, "items": n, "rounds": rounds}


def _proc_worker(kind: str, root: str, n: int, w: int, out_q) -> None:
    b = BACKENDS[kind](Path(root)) if kind == "a" else DesignCClaimBackend(Path(root))
    got = 0
    for i in range(n):
        t = b.claim(f"item-{i}", f"w{w}")
        if t:
            b.complete(t, DeliveryOutcome.CONFIRMED)
            got += 1
    out_q.put(got)


def bench_procs(kind: str, n: int, workers: int, payload: bytes) -> dict:
    perf = _timer()
    td, root = _fresh(f"procs-{kind}")
    try:
        b = BACKENDS[kind](root)  # creates + (for C) activates
        for i in range(n):
            b.publish(f"item-{i}", payload)
        q = mp.Queue()
        procs = [mp.Process(target=_proc_worker, args=(kind, str(root), n, w, q))
                 for w in range(workers)]
        t0 = perf()
        for p in procs:
            p.start()
        for p in procs:
            p.join(timeout=120)
        dt = perf() - t0
        wins = [q.get() for _ in range(workers)]
        return {"total_s": dt, "per_item_us": dt * 1e6 / n, "wins": sorted(wins),
                "exactly_once": sum(wins) == n, "workers": workers, "items": n}
    finally:
        shutil.rmtree(td, ignore_errors=True)


def bench_unknown_recover(kind: str, n: int, payload: bytes) -> dict:
    perf = _timer()
    td, root = _fresh(f"unk-{kind}")
    try:
        b = BACKENDS[kind](root)
        for i in range(n):
            b.publish(f"item-{i}", payload)
        toks = [b.claim(f"item-{i}", "w0") for i in range(n)]
        t0 = perf()
        for t in toks:
            b.complete(t, DeliveryOutcome.OUTCOME_UNKNOWN)
        t1 = perf()
        report = b.recover()
        t2 = perf()
        return {"unknown_us": (t1 - t0) * 1e6 / n,
                "recover_total_ms": (t2 - t1) * 1e3,
                "recover_report": str(report)[:120], "items": n}
    finally:
        shutil.rmtree(td, ignore_errors=True)


def bench_archive_scale(kind: str, history: int, probe: int, payload: bytes) -> dict:
    """Complete cost measured after `history` completed items."""
    perf = _timer()
    td, root = _fresh(f"arch-{kind}")
    try:
        b = BACKENDS[kind](root)
        for i in range(history):
            b.publish(f"h-{i}", payload)
            b.complete(b.claim(f"h-{i}", "w0"), DeliveryOutcome.CONFIRMED)
        for i in range(probe):
            b.publish(f"p-{i}", payload)
        toks = [b.claim(f"p-{i}", "w0") for i in range(probe)]
        t0 = perf()
        for t in toks:
            b.complete(t, DeliveryOutcome.CONFIRMED)
        dt = perf() - t0
        return {"history": history, "complete_us_at_history": dt * 1e6 / probe,
                "probe": probe}
    finally:
        shutil.rmtree(td, ignore_errors=True)


def head_sha() -> str:
    try:
        return subprocess.run(["git", "-C", str(REPO), "rev-parse", "--short", "HEAD"],
                              capture_output=True, text=True, timeout=10).stdout.strip()
    except Exception:
        return "unknown"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--items", type=int, default=300)
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--json", type=Path, default=None)
    ap.add_argument("--stamp", default=None,
                    help="caller-supplied ISO timestamp for the JSON record")
    args = ap.parse_args()

    import platform
    result = {"host": platform.node(), "head": head_sha(), "scenarios": {}}
    if args.stamp:
        result["timestamp"] = args.stamp
    k1, k64 = b"x" * 1024, b"x" * 65536

    for kind in ("a", "c"):
        s = result["scenarios"].setdefault(kind, {})
        s["cycle_1k"] = bench_cycle(kind, args.items, k1)
        s["cycle_64k"] = bench_cycle(kind, max(50, args.items // 6), k64)
        s["procs"] = bench_procs(kind, max(100, args.items // 2), args.workers, k1)
        s["unknown_recover"] = bench_unknown_recover(kind, max(100, args.items // 3), k1)
        s["archive_0"] = bench_archive_scale(kind, 0, 50, k1)
        s["archive_2k"] = bench_archive_scale(kind, 2000, 50, k1)

    a, c = result["scenarios"]["a"], result["scenarios"]["c"]
    print(f"claim-backend benchmark @ {result['head']} on {result['host']}")
    print(f"{'scenario':28} {'A':>12} {'C':>12}   note")
    rows = [
        ("cycle 1KiB (us/item)", a["cycle_1k"]["cycle_us"], c["cycle_1k"]["cycle_us"], ""),
        ("  publish", a["cycle_1k"]["publish_us"], c["cycle_1k"]["publish_us"], ""),
        ("  claim", a["cycle_1k"]["claim_us"], c["cycle_1k"]["claim_us"], ""),
        ("  complete", a["cycle_1k"]["complete_us"], c["cycle_1k"]["complete_us"], ""),
        ("cycle 64KiB (us/item)", a["cycle_64k"]["cycle_us"], c["cycle_64k"]["cycle_us"], ""),
        (f"{a['procs']['workers']}-proc contention (us/item)", a["procs"]["per_item_us"],
         c["procs"]["per_item_us"],
         f"exactly-once A={a['procs']['exactly_once']} C={c['procs']['exactly_once']}"),
        ("UNKNOWN complete (us/item)", a["unknown_recover"]["unknown_us"],
         c["unknown_recover"]["unknown_us"], ""),
        ("recover() total (ms)", a["unknown_recover"]["recover_total_ms"],
         c["unknown_recover"]["recover_total_ms"], ""),
        ("complete @ empty archive", a["archive_0"]["complete_us_at_history"],
         c["archive_0"]["complete_us_at_history"], "us/item"),
        ("complete @ 2k archive", a["archive_2k"]["complete_us_at_history"],
         c["archive_2k"]["complete_us_at_history"], "us/item"),
    ]
    for label, av, cv, note in rows:
        print(f"{label:28} {av:12.0f} {cv:12.0f}   {note}")

    if not (a["procs"]["exactly_once"] and c["procs"]["exactly_once"]):
        print("FAIL: exactly-once violated under contention", file=sys.stderr)
        return 1
    if args.json:
        args.json.write_text(json.dumps(result, indent=2))
        print(f"json -> {args.json}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
