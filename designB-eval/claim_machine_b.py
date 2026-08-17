#!/usr/bin/env python3
"""ClaimMachine for Design B — SAME instrument, different SUT.

Reuses 001's harness verbatim for everything that defines the instrument:
Gate (synchronous boundary handshake), OsProxy (gated os facade), GatedPath
(gated Path methods), DEAD_PID, the two-actor model, the sequencing/step
semantics, and the oracle SHAPE (at most one believing holder; a believer's
claim record exists and names them). Only the op surface is B's:

    publish   tmp write -> link into ready/     (B-only: items exist as files)
    claim     rename ready/<key> -> inflight/<token>   (A: acquire)
    complete  rename inflight/<token> -> archive/      (A: release_own)
    recover   DEAD owners' tokens -> ready/            (A: reclaim)

Deliberate surface differences, for the parity review:
- B has NO force-release op: administrative destruction is not part of B's
  local ownership protocol (it lives at the requeue layer). The machine
  therefore has no release_force rule — a smaller proof surface, which is a
  finding for the table, not an omission of the instrument.
- B needs publish (location-as-state: no file, nothing to claim). A's claim
  record is free-standing so its machine has no publish equivalent.

Extra oracle clause (B's I2 in machine form): recover() must NEVER return an
item whose believing holder is a live actor — both actors run in this live
process, so any nonempty recover of a believed item is a steal.
"""
from __future__ import annotations

import json
import os
import queue
import sys
import tempfile
import threading
import time
from pathlib import Path

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(_HERE), "tests", "_helpers"))
sys.path.insert(0, _HERE)

from claim_machine_harness import (  # noqa: E402
    ACTORS, DEAD_PID, Gate, GatedPath, ITEM, OsProxy)
import designB as B  # noqa: E402

if os.environ.get("B_NEUTER") == "1":
    # POSITIVE CONTROL: replace claim's atomic rename with check-then-act
    # (read, write-copy, unlink) — the machine MUST find the double-claim,
    # or a green run proves nothing about the real protocol.
    def _broken_claim(root, item_id, worker):
        key = B.safe_key(item_id)
        src = B.Path(root) / B.READY / key
        try:
            body = src.read_text()               # gated boundary
        except FileNotFoundError:
            return None
        ident = B.ob.process_identity(os.getpid())
        token = B.SEP.join((key, worker, str(os.getpid()), str(ident.start_usec)))
        d = B.Path(root) / B.INFLIGHT
        d.mkdir(parents=True, exist_ok=True)
        (d / token).write_text(body)             # gated boundary
        try:
            src.unlink()
        except FileNotFoundError:
            pass
        return token
    B.claim = _broken_claim


class BClaimDriver:
    """Two gated actors over designB; op names mirror the explorer's rules."""

    def __init__(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="claim-machine-b-")
        self.root = Path(self.tmp.name)
        self.gate = Gate()
        self._root_holder = {"root": str(self.root)}
        self._saved_os = B.os
        B.os = OsProxy(os, self.gate, self._root_holder)
        self._saved_path = B.Path
        GatedPath._gate = self.gate
        GatedPath._root = str(self.root)
        B.Path = GatedPath
        self.done_log = []                       # (seq, actor, op, result)
        self._seq_lock = threading.Lock()
        self._seq = 0
        self.tokens = {a: None for a in ACTORS}  # actor -> live token
        self.inbox = {a: queue.Queue() for a in ACTORS}
        self.busy = {a: False for a in ACTORS}
        self.threads = {}
        for a in ACTORS:
            t = threading.Thread(target=self._worker, args=(a,), daemon=True)
            self.threads[a] = t
            t.start()
            self.gate.actor_of[t.ident] = a

    # ── worker plumbing (identical semantics to the A driver) ───────────────
    def _worker(self, actor):
        while True:
            job = self.inbox[actor].get()
            if job is None:
                return
            op, fn, start_id = job
            try:
                res = fn()
            except Exception as e:                # a protocol crash is a finding
                res = e
            with self._seq_lock:
                self._seq += 1
                self.done_log.append((self._seq, actor, op, res, start_id))
            self.busy[actor] = False

    def _submit(self, actor, op, fn):
        self.busy[actor] = True
        with self._seq_lock:
            self._seq += 1
            start_id = self._seq
        self.inbox[actor].put((op, fn, start_id))

    # ── op surface ──────────────────────────────────────────────────────────
    def publish(self):
        """Main-thread setup op (ungated, like plant_dead_claim): make the
        item claimable if it is not already anywhere in the namespace."""
        if any(self.busy.values()):
            return
        B.publish(self.root, ITEM, "payload")    # False if already present: fine

    def start_claim(self, actor):
        if not self.busy[actor]:
            self._submit(actor, "claim",
                         lambda: B.claim(self.root, ITEM, actor))

    def start_complete(self, actor):
        if not self.busy[actor]:
            tok = self.tokens.get(actor)
            if tok is None:
                return                            # nothing to complete; no-op
            self._submit(actor, "complete",
                         lambda: B.complete(self.root, tok))

    def start_recover(self, actor):
        if not self.busy[actor]:
            self._submit(actor, "recover", lambda: B.recover(self.root))

    def step(self, actor, n):
        for _ in range(n):
            if not self.busy[actor]:
                return
            if not self.gate.at_gate[actor].wait(timeout=0.05):
                if not self.gate.at_gate[actor].wait(timeout=0.95):
                    return
            self.gate.at_gate[actor].clear()
            self.gate.go[actor].release()
            deadline = time.time() + 1.0
            while (time.time() < deadline and self.busy[actor]
                   and not self.gate.at_gate[actor].is_set()):
                time.sleep(0.0005)

    def plant_dead_claim(self):
        """A drainer that died mid-delivery: inflight token, dead owner."""
        if any(self.busy.values()) or self._believers():
            return
        key = B.safe_key(ITEM)
        d = Path(self.root) / B.INFLIGHT
        if any(f.name.split(B.SEP)[0] == key for f in d.iterdir()) if d.exists() else False:
            return
        d.mkdir(parents=True, exist_ok=True)
        ghost = d / B.SEP.join((key, "ghost", str(DEAD_PID), "1"))
        ghost.write_text(json.dumps({"body": "payload"}), encoding="utf-8")

    def check_consistency(self):
        if any(self.busy.values()):
            return
        self._replay_tokens()
        self._check()

    # ── settle + oracle ─────────────────────────────────────────────────────
    def _settle(self):
        self.gate.open = True
        deadline = time.time() + 30.0
        while any(self.busy.values()) and time.time() < deadline:
            time.sleep(0.005)
        assert not any(self.busy.values()), "op wedged: protocol deadlock"
        self.gate.open = False
        for a in ACTORS:
            while self.gate.go[a].acquire(blocking=False):
                pass
            self.gate.at_gate[a].clear()

    def _replay_tokens(self):
        """Derive believers + live tokens from the log (same believer
        semantics as the A driver: success grants, own-completion releases).

        Two oracles cover B's I2 (001 review, 2026-08-17):
        - SCOPED order assert here: recover returning a key is a steal only
          if a believer's claim COMPLETED before the recover STARTED — a
          claim completing inside the recover's window consumed the
          recovery's own ready slot (legitimate handoff, CE-3), which the
          unscoped completion-order assert false-positived on.
        - STATE assert in _check: believer + ready copy never coexist
          (covers the steal-enabling state between ops, CE-1)."""
        held = {}
        held_done = {}                            # actor -> claim done_id
        for done_id, actor, op, res, start_id in sorted(self.done_log):
            if isinstance(res, Exception):
                raise AssertionError(f"{actor} {op} raised {res!r}")
            if op == "claim" and isinstance(res, str):
                held[actor] = res
                held_done[actor] = done_id
            elif op == "complete" and res is True:
                held.pop(actor, None)
                held_done.pop(actor, None)
            elif op == "recover" and res:
                key = B.safe_key(ITEM)
                pre = [a for a, d in held_done.items() if d < start_id]
                for k in res:
                    assert not (k == key and pre), \
                        (f"recover (started {start_id}) STOLE {k} from "
                         f"pre-existing holder {sorted(pre)}")
        self.tokens = {a: held.get(a) for a in ACTORS}
        return held

    def _believers(self):
        return set(self._replay_tokens().keys())

    def _check(self):
        held = self._believers()
        assert len(held) <= 1, f"TWO live claim holders: {sorted(held)}"
        if held:
            actor = next(iter(held))
            rec = B.holder(self.root, ITEM)
            assert rec is not None, \
                f"{actor} holds the claim but the inflight record is gone"
            assert rec == actor, \
                f"inflight record names {rec!r}, holder is {sorted(held)}"
            # B's I2 as a STATE invariant (replaces the completion-order
            # no-steal replay assert): an owned item must not also be
            # claimable — a ready copy alongside a believer is the
            # double-delivery precursor CE-1 demonstrated.
            ready = Path(self.root) / B.READY / B.safe_key(ITEM)
            assert not ready.exists(), \
                f"{actor} owns the item AND a ready copy is claimable"

    def finish(self, check=True):
        try:
            self._settle()
            if check:
                self._replay_tokens()
                self._check()
        finally:
            self.gate.open = True
            for a in ACTORS:
                self.inbox[a].put(None)
            for t in self.threads.values():
                t.join(timeout=2)
            B.os = self._saved_os
            B.Path = self._saved_path
            GatedPath._gate = None
            self.tmp.cleanup()


# ── explorer (mirrors outbox-claim-machine.test.py rule-for-rule) ───────────
def main():
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

    class BClaimMachine(RuleBasedStateMachine):
        def __init__(self):
            super().__init__()
            self.driver = BClaimDriver()

        actors = st.sampled_from(ACTORS)

        @rule()
        def publish(self):
            self.driver.publish()

        @rule(actor=actors)
        def start_claim(self, actor):
            self.driver.start_claim(actor)

        @rule(actor=actors)
        def start_complete(self, actor):
            self.driver.start_complete(actor)

        @rule(actor=actors)
        def start_recover(self, actor):
            self.driver.start_recover(actor)

        @rule(actor=actors, n=st.integers(min_value=1, max_value=5))
        def step(self, actor, n):
            self.driver.step(actor, n)

        @rule()
        def plant_dead_claim(self):
            self.driver.plant_dead_claim()

        @rule()
        def check_consistency(self):
            self.driver.check_consistency()

        def teardown(self):
            self.driver.finish(check=True)

    examples = int(os.environ.get("CLAIM_MACHINE_EXAMPLES", "60"))
    steps = int(os.environ.get("CLAIM_MACHINE_STEPS", "40"))
    cfg = settings(max_examples=examples, stateful_step_count=steps,
                   deadline=None, database=None,
                   suppress_health_check=list(HealthCheck))
    run_state_machine_as_test(BClaimMachine, settings=cfg)
    print(f"PASS: BClaimMachine — {examples} examples x {steps} steps, "
          "single-owner + record-consistency + no-steal invariants held")


if __name__ == "__main__":
    main()
