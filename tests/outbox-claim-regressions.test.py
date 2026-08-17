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


def replay(ops, check=True):
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


def main():
    failures = 0

    # Arm 1: on current code every frozen schedule must uphold the oracle.
    for name, ops in SCRIPTS.items():
        err = replay(ops)
        status = "ok  " if err is None else "FAIL"
        print(f"  {status} frozen schedule {name}" + (f"  -> {err}" if err else ""))
        failures += 0 if err is None else 1

    # Arm 2: neuter the lock; the schedules must still be able to bite.
    @contextlib.contextmanager
    def no_lock(root, item_id):
        yield
    saved = H.ob._item_lock
    H.ob._item_lock = no_lock
    try:
        bit = False
        for _attempt in range(10):
            for name, ops in SCRIPTS.items():
                if replay(ops) is not None:
                    print(f"  ok   positive control: {name} violates without the lock")
                    bit = True
                    break
            if bit:
                break
        if not bit:
            print("  FAIL positive control: no schedule violates with _item_lock "
                  "neutered — the harness has lost its teeth")
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
