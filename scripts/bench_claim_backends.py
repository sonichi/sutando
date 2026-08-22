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

REPO = Path(__file__).resolve().parent.parent  # lint-workspace-resolution: allow-repo-root (package path + git -C, no per-user state)
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


def _pctl(xs_us: list) -> dict:
    xs = sorted(xs_us)
    ix = lambda q: xs[min(len(xs) - 1, int(q * len(xs)))]
    return {"p50": ix(0.50), "p95": ix(0.95), "p99": ix(0.99), "max": xs[-1]}


def bench_cycle(kind: str, n: int, payload: bytes) -> dict:
    """Per-op latency distributions (owner: medians hide the tail)."""
    perf = _timer()
    td, root = _fresh(f"cycle-{kind}")
    try:
        b = BACKENDS[kind](root)
        lat = {"publish": [], "claim": [], "complete": []}
        toks = []
        for i in range(n):
            t0 = perf(); b.publish(f"item-{i}", payload)
            lat["publish"].append((perf() - t0) * 1e6)
        for i in range(n):
            t0 = perf(); toks.append(b.claim(f"item-{i}", "w0"))
            lat["claim"].append((perf() - t0) * 1e6)
        t_all = perf()
        for t in toks:
            t0 = perf(); b.complete(t, DeliveryOutcome.CONFIRMED)
            lat["complete"].append((perf() - t0) * 1e6)
        wall = perf() - t_all
        out = {op: _pctl(v) for op, v in lat.items()}
        out["items"] = n
        out["complete_throughput_per_s"] = n / wall if wall else 0
        return out
    finally:
        shutil.rmtree(td, ignore_errors=True)


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
        # A dead/hung worker never put()s; an untimed get would hang forever.
        wins = []
        import queue as _q
        for _ in range(workers):
            try:
                wins.append(q.get(timeout=10))
            except _q.Empty:
                break
        for p in procs:
            if p.is_alive():
                p.terminate()
                p.join(timeout=10)
        if len(wins) != workers:
            raise RuntimeError(
                f"{kind}: only {len(wins)}/{workers} workers reported — "
                f"worker crash or hang; exitcodes={[p.exitcode for p in procs]}")
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


def _claim_and_hang(kind, root, item, ev_claimed):
    b = BACKENDS[kind](Path(root)) if kind == "a" else DesignCClaimBackend(Path(root))
    t = b.claim(item, "victim")
    assert t is not None
    ev_claimed.set()
    import time
    time.sleep(300)  # killed long before this returns


def bench_crash_injection(kind: str, payload: bytes) -> dict:
    """Gate 2: kill -9 a claim holder; recover() must free the DEAD claim,
    must NOT touch a LIVE holder, and redelivery must be exactly-once."""
    td, root = _fresh(f"crash-{kind}")
    try:
        # A's dead-claim reclaim is TTL-gated by design; a short TTL
        # measures the policy instead of waiting out the 300 s default.
        b = DesignAClaimBackend(root, reclaim_ttl_s=1.0) if kind == "a" \
            else BACKENDS[kind](root)
        for name in ("victim-item", "live-item"):
            b.publish(name, payload)
        ev = mp.Event()
        p = mp.Process(target=_claim_and_hang, args=(kind, str(root), "victim-item", ev))
        p.start()
        assert ev.wait(timeout=30), "victim never claimed"
        live_tok = b.claim("live-item", "survivor")
        p.kill(); p.join(timeout=30)
        import time
        time.sleep(1.2)  # past A's (shortened) reclaim TTL
        r1 = b.recover()
        reclaim = b.claim("victim-item", "survivor2")
        dup = b.claim("victim-item", "survivor3")
        live_still_held = b.claim("live-item", "thief") is None
        ok = (reclaim is not None) and (dup is None) and live_still_held \
            and (live_tok is not None)
        for t in (reclaim, live_tok):
            if t is not None:
                b.complete(t, DeliveryOutcome.CONFIRMED)
        return {"dead_claim_recovered": reclaim is not None,
                "no_double_owner": dup is None,
                "live_holder_untouched": live_still_held,
                "recover_report": str(r1)[:100], "ok": ok}
    finally:
        shutil.rmtree(td, ignore_errors=True)


def bench_publish_inflight_conflict(kind: str, payload: bytes) -> dict:
    """Re-publishing an id that is CLAIMED must not clobber or double it."""
    td, root = _fresh(f"conflict-{kind}")
    try:
        b = BACKENDS[kind](root)
        b.publish("dup", payload)
        tok = b.claim("dup", "w0")
        second = b.publish("dup", payload)
        thief = b.claim("dup", "w1")
        b.complete(tok, DeliveryOutcome.CONFIRMED)
        return {"republish_while_inflight_accepted": bool(second),
                "double_owner_created": thief is not None,
                "ok": thief is None}
    finally:
        shutil.rmtree(td, ignore_errors=True)


def _select_matrix(quick: bool, items, deep: bool):
    """(scales, proc_counts, proc_items) for the three CLI modes."""
    if quick:
        return [40], (1, 2), 30
    if items is not None:
        return [items], (1, 4, 16), max(20, items // 2)
    return [100, 10_000] + ([100_000] if deep else []), (1, 4, 16), 400


def _verdict(a: dict, c: dict, proc_counts) -> bool:
    """True iff every correctness invariant held for both backends."""
    return all(k[f"procs_{w}"]["exactly_once"]
               for k in (a, c) for w in proc_counts) \
        and a["crash"]["ok"] and c["crash"]["ok"] \
        and a["conflict"]["ok"] and c["conflict"]["ok"]


def head_sha() -> str:
    """Via src/git_binary's resolver — a bare `git` can hit the Xcode-CLT stub."""
    try:
        sys.path.insert(0, str(REPO / "src"))  # git_binary lives in src/
        from git_binary import git_argv
        return subprocess.run([*git_argv("-C", str(REPO), "rev-parse", "--short", "HEAD")],
                              capture_output=True, text=True, timeout=10).stdout.strip()
    except Exception:
        return "unknown"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--items", type=int, default=None,
                    help="override the scale list with ONE custom size")
    ap.add_argument("--deep", action="store_true",
                    help="add the 100k-item scale (minutes, not seconds)")
    ap.add_argument("--quick", action="store_true",
                    help="CI smoke: tiny scales, 1/2 procs — exercises every "
                         "scenario and correctness assert in seconds")
    ap.add_argument("--json", type=Path, default=None)
    ap.add_argument("--stamp", default=None,
                    help="caller-supplied ISO timestamp for the JSON record")
    args = ap.parse_args(argv)

    import platform
    import resource
    result = {"host": platform.node(), "head": head_sha(), "scenarios": {},
              "fsync_note": ("neither backend calls fsync on the hot path today; "
                             "durability is rename-atomicity + page cache (warm)")}
    if args.stamp:
        result["timestamp"] = args.stamp
    k1, k64 = b"x" * 1024, b"x" * 65536
    scales, proc_counts, proc_items = _select_matrix(args.quick, args.items, args.deep)

    for kind in ("a", "c"):
        s = result["scenarios"].setdefault(kind, {})
        cpu0 = resource.getrusage(resource.RUSAGE_SELF)
        for n in scales:
            s[f"cycle_1k_{n}"] = bench_cycle(kind, n, k1)
        s["cycle_64k"] = bench_cycle(kind, 100, k64)
        for w in proc_counts:
            s[f"procs_{w}"] = bench_procs(kind, proc_items, w, k1)
        s["unknown_recover"] = bench_unknown_recover(kind, 100, k1)
        s["archive_0"] = bench_archive_scale(kind, 0, 50, k1)
        s["archive_2k"] = bench_archive_scale(kind, 2000, 50, k1)
        s["crash"] = bench_crash_injection(kind, k1)
        s["conflict"] = bench_publish_inflight_conflict(kind, k1)
        cpu1 = resource.getrusage(resource.RUSAGE_SELF)
        s["cpu_s_selfproc"] = round((cpu1.ru_utime - cpu0.ru_utime)
                                    + (cpu1.ru_stime - cpu0.ru_stime), 2)

    a, c = result["scenarios"]["a"], result["scenarios"]["c"]
    print(f"claim-backend benchmark @ {result['head']} on {result['host']}")
    print(result["fsync_note"])
    print(f"{'scenario':34} {'A':>26} {'C':>26}")

    def fmt(d):
        return f"{d['p50']:6.0f}/{d['p95']:6.0f}/{d['p99']:7.0f}/{d['max']:8.0f}"

    for n in scales:
        ka, kc = a[f"cycle_1k_{n}"], c[f"cycle_1k_{n}"]
        print(f"-- {n} items, 1KiB (p50/p95/p99/max us) --")
        for op in ("publish", "claim", "complete"):
            print(f"{op:34} {fmt(ka[op]):>26} {fmt(kc[op]):>26}")
        print(f"{'complete throughput (items/s)':34} "
              f"{ka['complete_throughput_per_s']:26.0f} {kc['complete_throughput_per_s']:26.0f}")
    print(f"{'64KiB complete p99 (us)':34} {a['cycle_64k']['complete']['p99']:26.0f} "
          f"{c['cycle_64k']['complete']['p99']:26.0f}")
    for w in proc_counts:
        pa, pc = a[f"procs_{w}"], c[f"procs_{w}"]
        print(f"{f'{w}-proc contention (us/item)':34} {pa['per_item_us']:26.0f} "
              f"{pc['per_item_us']:26.0f}   exactly-once A={pa['exactly_once']} C={pc['exactly_once']}")
    print(f"{'UNKNOWN complete (us/item)':34} {a['unknown_recover']['unknown_us']:26.0f} "
          f"{c['unknown_recover']['unknown_us']:26.0f}")
    print(f"{'complete @ 2k archive (us/item)':34} "
          f"{a['archive_2k']['complete_us_at_history']:26.0f} "
          f"{c['archive_2k']['complete_us_at_history']:26.0f}")
    for label in ("crash", "conflict"):
        print(f"{label:34} A={a[label]} ")
        print(f"{'':34} C={c[label]}")
    print(f"{'CPU s (self, whole matrix)':34} {a['cpu_s_selfproc']:26} {c['cpu_s_selfproc']:26}")

    if not _verdict(a, c, proc_counts):
        print("FAIL: correctness invariant violated (exactly-once/crash/conflict)",
              file=sys.stderr)
        return 1
    if args.json:
        args.json.write_text(json.dumps(result, indent=2))
        print(f"json -> {args.json}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
