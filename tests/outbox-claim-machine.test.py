#!/usr/bin/env python3
"""Hypothesis ClaimMachine explorer: stateful search over the outbox
delivery-claim protocol's interleavings.

Drives the shared ClaimDriver (tests/_helpers/claim_machine_harness.py — the REAL
src/outbox.py under gated concurrent drainers) with Hypothesis choosing the
op sequence and per-boundary schedule. Invariants live in the driver's
oracle: at most one believing holder; a believer's claim file exists and
names them.

Dependency policy (owner-ratified 2026-08-17): exploration is a REQUIRED
nightly job — set CLAIM_MACHINE_REQUIRED=1 there so a missing hypothesis
FAILS the job instead of silently passing. Without the flag (ad-hoc/dev
runs) a missing hypothesis skips with exit 0.

Detection is probabilistic on the subtle defect classes (measured on the
four historical #2975 heads: the two blatant ones 2/2 runs, ABA 3/6,
live-releaser TOCTOU 1/6 at 800x60) — the deterministic replays in
tests/outbox-claim-regressions.test.py carry the PR-blocking regression
weight; this job's value is finding NEW interleavings and alarming if the
locking that keeps main green is ever weakened.
"""
from __future__ import annotations

import os
import sys

try:
    from hypothesis import settings, HealthCheck
    from hypothesis.stateful import (RuleBasedStateMachine, rule,
                                     run_state_machine_as_test)
    from hypothesis import strategies as st
except ImportError:
    if os.environ.get("CLAIM_MACHINE_REQUIRED") == "1":
        print("FAIL: hypothesis is required for this job and is not installed")
        sys.exit(1)
    print("SKIP: hypothesis not installed")
    sys.exit(0)

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "_helpers"))
from claim_machine_harness import ClaimDriver, ACTORS  # noqa: E402


class ClaimMachine(RuleBasedStateMachine):
    def __init__(self):
        super().__init__()
        self.driver = ClaimDriver()

    actors = st.sampled_from(ACTORS)

    @rule(actor=actors)
    def start_acquire(self, actor):
        self.driver.start_acquire(actor)

    @rule(actor=actors)
    def start_reclaim(self, actor):
        self.driver.start_reclaim(actor)

    @rule(actor=actors)
    def start_release_own(self, actor):
        self.driver.start_release_own(actor)

    @rule(actor=actors)
    def start_release_force(self, actor):
        self.driver.start_release_force(actor)

    @rule(actor=actors, n=st.integers(min_value=1, max_value=5))
    def step(self, actor, n):
        self.driver.step(actor, n)

    @rule()
    def plant_dead_claim(self):
        self.driver.plant_dead_claim()

    @rule()
    def check_consistency(self):
        # Deliberately NOT a precondition — rule availability must be
        # deterministic across replays, thread timing is not.
        self.driver.check_consistency()

    def teardown(self):
        self.driver.finish(check=True)


if __name__ == "__main__":
    examples = int(os.environ.get("CLAIM_MACHINE_EXAMPLES", "60"))
    steps = int(os.environ.get("CLAIM_MACHINE_STEPS", "40"))
    # Opt-in example DB (nightly): failure-trace artifacts; same-machine
    # replay aid only — the frozen schedules carry regression weight.
    db = None
    db_dir = os.environ.get("CLAIM_MACHINE_DB")
    if db_dir:
        from hypothesis.database import DirectoryBasedExampleDatabase
        db = DirectoryBasedExampleDatabase(db_dir)
    cfg = settings(max_examples=examples, stateful_step_count=steps,
                   deadline=None, database=db,
                   suppress_health_check=list(HealthCheck))
    run_state_machine_as_test(ClaimMachine, settings=cfg)
    print(f"PASS: ClaimMachine — {examples} examples x {steps} steps, "
          "single-owner + file-consistency invariants held")
