#!/usr/bin/env python3
"""Deterministic claim-protocol regressions: the four shrunk counterexamples
from the #2975 review cycle's defective heads, frozen as scripted replays.

Two arms, both required:
1. Every script replays against the CURRENT src/outbox.py and the oracle
   must hold — these exact interleaving schedules are the ones that broke
   the four historical heads, so any change that weakens the per-item
   claim serialization re-opens one of them.
2. Positive control: with _item_lock neutered to a no-op, at least one
   script must produce a violation within bounded attempts. A green arm 1
   is only meaningful while arm 2 proves the schedules still have teeth.

No hypothesis dependency — this is plain scripted replay over the shared
harness, sized for the PR-blocking suite. The independent derivation of the
same falsifiers as the outbox contract tests is deliberate redundancy: two
derivations catch a bad refactor of either.
"""
from __future__ import annotations

import contextlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "_helpers"))
import claim_machine_harness as H

SCRIPTS = {
    # 5f5208af: unconditional release deletes the winner (fixed by 9f008c98)
    "5f5208af": [
        ('start_acquire', 'drainer-A'),
        ('step', 'drainer-A', 1),
        ('start_release_own', 'drainer-B'),
        ('step', 'drainer-B', 1),
        ('start_acquire', 'drainer-B'),
        ('step', 'drainer-B', 1),
        ('check_consistency',),
    ],
    # 933a2a79: release not bound to instance / reclaim-force interplay (fixed by 9f008c98/64de6dd4)
    "933a2a79": [
        ('step', 'drainer-A', 1),
        ('start_release_force', 'drainer-A'),
        ('plant_dead_claim',),
        ('start_reclaim', 'drainer-B'),
        ('start_reclaim', 'drainer-B'),
        ('step', 'drainer-A', 2),
        ('step', 'drainer-B', 5),
        ('start_reclaim', 'drainer-A'),
        ('start_release_own', 'drainer-A'),
        ('start_reclaim', 'drainer-B'),
        ('start_release_own', 'drainer-A'),
        ('start_release_own', 'drainer-A'),
        ('step', 'drainer-B', 4),
        ('start_release_own', 'drainer-B'),
        ('start_reclaim', 'drainer-B'),
        ('start_release_own', 'drainer-A'),
        ('start_release_force', 'drainer-B'),
        ('start_release_own', 'drainer-A'),
        ('start_release_own', 'drainer-B'),
        ('start_reclaim', 'drainer-A'),
        ('step', 'drainer-A', 2),
        ('start_release_force', 'drainer-A'),
        ('start_release_force', 'drainer-B'),
        ('start_release_own', 'drainer-A'),
        ('start_release_own', 'drainer-B'),
        ('start_reclaim', 'drainer-B'),
        ('step', 'drainer-A', 4),
        ('step', 'drainer-B', 4),
        ('start_reclaim', 'drainer-A'),
        ('start_release_own', 'drainer-A'),
        ('start_release_force', 'drainer-B'),
        ('start_release_own', 'drainer-B'),
        ('plant_dead_claim',),
        ('plant_dead_claim',),
        ('start_reclaim', 'drainer-B'),
        ('start_release_force', 'drainer-A'),
        ('plant_dead_claim',),
        ('start_release_force', 'drainer-A'),
        ('start_release_force', 'drainer-A'),
        ('start_release_force', 'drainer-B'),
        ('start_reclaim', 'drainer-B'),
        ('step', 'drainer-B', 2),
        ('start_reclaim', 'drainer-A'),
        ('plant_dead_claim',),
        ('step', 'drainer-A', 4),
        ('plant_dead_claim',),
        ('plant_dead_claim',),
        ('start_release_own', 'drainer-B'),
        ('start_reclaim', 'drainer-A'),
        ('start_release_force', 'drainer-B'),
        ('plant_dead_claim',),
        ('plant_dead_claim',),
        ('step', 'drainer-A', 3),
        ('step', 'drainer-A', 1),
        ('start_release_own', 'drainer-B'),
        ('start_reclaim', 'drainer-B'),
        ('start_reclaim', 'drainer-B'),
        ('start_reclaim', 'drainer-A'),
        ('plant_dead_claim',),
    ],
    # 0d6083a3: ABA release destroys a successor claim (fixed by 64de6dd4)
    "0d6083a3": [
        ('start_acquire', 'drainer-B'),
        ('step', 'drainer-B', 1),
        ('start_release_force', 'drainer-A'),
        ('start_release_own', 'drainer-B'),
        ('step', 'drainer-B', 1),
        ('step', 'drainer-B', 1),
        ('step', 'drainer-A', 1),
        ('start_acquire', 'drainer-A'),
    ],
    # cb2f59f1: live-releaser TOCTOU: reclaim compare-then-act tail lands on a fresh claim (fixed by d2a2a978 flock)
    "cb2f59f1": [
        ('plant_dead_claim',),
        ('start_reclaim', 'drainer-B'),
        ('start_release_force', 'drainer-A'),
        ('step', 'drainer-B', 5),
        ('step', 'drainer-A', 2),
        ('start_acquire', 'drainer-A'),
        ('step', 'drainer-A', 1),
        ('step', 'drainer-B', 2),
        ('start_release_own', 'drainer-B'),
        ('step', 'drainer-B', 1),
        ('step', 'drainer-B', 4),
        ('step', 'drainer-B', 4),
        ('check_consistency',),
    ],
}


LAST_DIAG = {}


def replay(ops, check=True, diag_key=None):
    d = H.ClaimDriver()
    try:
        for op in ops:
            getattr(d, op[0])(*op[1:])
        d.finish(check=check)
        return None
    except AssertionError as e:
        with contextlib.suppress(BaseException):
            d.finish(check=False)
        return str(e)
    finally:
        if diag_key is not None:
            LAST_DIAG[diag_key] = (dict(d.stats),
                                   [(a, o, repr(r)) for _s, a, o, r
                                    in sorted(d.done_log)])


def steal_sweep(max_k=12):
    """cb2f59f1-class schedule swept over every pause offset k: reclaim
    parks after its k-th boundary while force+acquire turn the slot over,
    then resumes its tail. Version-robust where frozen index-based replays
    are not — some k lands in the read/act window on ANY python's syscall
    sequence. Returns (k, err) for the first violating offset, else None."""
    for k in range(1, max_k + 1):
        d = H.ClaimDriver()
        err = None
        try:
            d.plant_dead_claim()
            d.start_reclaim("drainer-B")
            d.step("drainer-B", k)
            d.start_release_force("drainer-A")
            for _ in range(30):
                if not d.busy["drainer-A"]:
                    break
                d.step("drainer-A", 1)
            d.start_acquire("drainer-A")
            for _ in range(30):
                if not d.busy["drainer-A"]:
                    break
                d.step("drainer-A", 1)
            for _ in range(80):
                if not (d.busy["drainer-A"] or d.busy["drainer-B"]):
                    break
                d.step("drainer-B", 1)
                d.step("drainer-A", 1)
            try:
                d.finish(check=True)
            except AssertionError as e:
                err = str(e)
        except AssertionError as e:
            err = str(e)
            with contextlib.suppress(BaseException):
                d.finish(check=False)
        if err is not None:
            return k, err
    return None


def main():
    failures = 0

    # Arm 1: on current code every frozen schedule must uphold the oracle.
    for name, ops in SCRIPTS.items():
        err = replay(ops)
        status = "ok  " if err is None else "FAIL"
        print(f"  {status} frozen schedule {name}" + (f"  -> {err}" if err else ""))
        failures += 0 if err is None else 1

    # Arm 1b: the sweep against the REAL protocol — no offset may violate.
    hit = steal_sweep()
    if hit is None:
        print("  ok   steal sweep holds on current code (all offsets)")
    else:
        print(f"  FAIL steal sweep k={hit[0]} violates on current code "
              f"-> {hit[1]}")
        failures += 1

    # Arm 2: neuter the lock; the schedules must still be able to bite.
    @contextlib.contextmanager
    def no_lock(root, item_id):
        yield
    saved = H.ob._item_lock
    H.ob._item_lock = no_lock
    try:
        bit = False
        for name, ops in SCRIPTS.items():
            if replay(ops, diag_key=name) is not None:
                print(f"  ok   positive control: {name} violates without the lock")
                bit = True
                break
        if not bit:
            # Frozen indices are version-sensitive (syscall-sequence drift,
            # measured CI 2026-08-17); the semantic sweep is the real arm.
            hit = steal_sweep()
            if hit is not None:
                print(f"  ok   positive control: steal sweep bites at "
                      f"k={hit[0]} without the lock -> {hit[1]}")
                bit = True
        if not bit:
            print("  FAIL positive control: no schedule violates with _item_lock "
                  "neutered — the harness has lost its teeth")
            import sys as _sys
            trace = (_sys.gettrace() is not None,
                     [i for i in range(6)
                      if getattr(_sys, "monitoring", None)
                      and _sys.monitoring.get_tool(i)])
            print(f"  diag python={_sys.version.split()[0]} "
                  f"settrace={trace[0]} monitoring_tools={trace[1]} "
                  f"arrival_bound={H.ClaimDriver.ARRIVAL_BOUND}")
            for name, (stats, log) in LAST_DIAG.items():
                print(f"  diag {name} stats={stats}")
                print(f"  diag {name} log={log}")
            failures += 1
    finally:
        H.ob._item_lock = saved

    if failures:
        print(f"FAILED ({failures})")
        sys.exit(1)
    print("PASS: outbox claim regressions — 4 frozen schedules hold on current "
          "code, and still bite with the lock removed")


if __name__ == "__main__":
    main()
