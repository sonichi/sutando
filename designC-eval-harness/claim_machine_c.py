#!/usr/bin/env python3
"""ClaimMachine for Design C — SAME instrument, third SUT.

Reuses the shared harness verbatim for everything that defines the instrument:
Gate (synchronous boundary handshake), OsProxy (gated os facade), GatedPath
(gated Path methods), FcntlProxy (flock-wait announcement, so a blocked actor
is skipped instead of burning its arrival window), DEAD_PID, the two-actor
model, the step semantics, and the oracle SHAPE (at most one believing holder;
the believer's record exists and names them).

Op surface — deliberately a SUPERSET of B's, so the four frozen A schedules are
expressible verbatim and C's proof surface is not quietly smaller than A's:

    acquire        publish-if-absent, then claim   (A: acquire)
    release_own    finalize(my token)              (A: release_own)
    release_force  force_requeue                   (A: release_force)
    reclaim        recover                         (A: reclaim)

`acquire` folds publish-if-absent into the claim because A's claim record is
free-standing (nothing to publish) while C's item must exist as an object. The
fold is not a shortcut: publish refuses while the item is live in ANY state, so
the fold is precisely where publish-while-held pressure enters the machine —
and it is the arm that bites when the mutex is neutered.

Extra oracle clauses beyond A's:
  - single live object per item: designC._locate raises InvariantError, which
    surfaces as a finding (a raised op result is a failure by construction).
  - token+generation currency: the believer's token must still BE the live
    object's name, not merely name the right worker.
  - no-steal (B's I2, scoped): recover may not re-arm an item whose believer
    claimed it before the recover started.
"""
from __future__ import annotations

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
    ACTORS, DEAD_PID, FcntlProxy, Gate, GatedPath, ITEM, OsProxy)
import designC as C  # noqa: E402

if os.environ.get("C_NEUTER") == "1":
    # POSITIVE CONTROL: remove the ownership mutex and change NOTHING else —
    # every transition is still one atomic rename. Whatever the machine then
    # finds is precisely what the mutex contributes; if it finds nothing, a
    # green run of the real protocol proves nothing about the mutex.
    import contextlib

    @contextlib.contextmanager
    def _no_lock(root, key):
        yield
    C.key_lock = _no_lock


class CClaimDriver:
    """Two gated actors over designC; op names mirror the A explorer's rules."""

    def __init__(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="claim-machine-c-")
        self.root = Path(self.tmp.name)
        self.gate = Gate()
        self._root_holder = {"root": str(self.root)}
        self._saved_os = C.os
        C.os = OsProxy(os, self.gate, self._root_holder)
        self.in_flock = {a: False for a in ACTORS}
        self._saved_fcntl = C.fcntl
        C.fcntl = FcntlProxy(self._saved_fcntl, self)
        self._saved_path = C.Path
        GatedPath._gate = self.gate
        GatedPath._root = str(self.root)
        C.Path = GatedPath
        self.done_log = []                       # (done_id, actor, op, res, start_id)
        self._seq_lock = threading.Lock()
        self._seq = 0
        self.tokens = {a: None for a in ACTORS}
        self.inbox = {a: queue.Queue() for a in ACTORS}
        self.busy = {a: False for a in ACTORS}
        self.threads = {}
        for a in ACTORS:
            t = threading.Thread(target=self._worker, args=(a,), daemon=True)
            self.threads[a] = t
            t.start()
            self.gate.actor_of[t.ident] = a

    # ── worker plumbing (identical semantics to the A/B drivers) ────────────
    def _worker(self, actor):
        while True:
            job = self.inbox[actor].get()
            if job is None:
                return
            op, fn, start_id = job
            try:
                res = fn()
            except Exception as e:               # a protocol crash is a finding
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

    # ── ops ────────────────────────────────────────────────────────────────
    def publish(self):
        """Main-thread setup op (ungated, like plant_dead_claim)."""
        if any(self.busy.values()):
            return
        C.publish(self.root, ITEM, "payload")

    def _acquire(self, actor):
        def run():
            C.publish(self.root, ITEM, "payload")   # False when already live
            return C.claim(self.root, ITEM, actor)
        return run

    def _would_be_token(self, actor):
        """A's release_own is 'release it if it is mine'. An actor holding
        nothing still submits a token naming itself, so finalize's rejection
        path is exercised rather than skipped."""
        tok = self.tokens.get(actor)
        if tok is not None:
            return tok
        ident = C.ob.process_identity(os.getpid())
        return C.SEP.join((C.safe_key(ITEM), "0", C._safe_component(actor),
                           str(os.getpid()), str(ident.start_usec)))

    def start_acquire(self, actor):
        if not self.busy[actor]:
            self._submit(actor, "acquire", self._acquire(actor))

    def start_claim(self, actor):
        if not self.busy[actor]:
            self._submit(actor, "claim",
                         lambda: C.claim(self.root, ITEM, actor))

    def start_release_own(self, actor):
        if not self.busy[actor]:
            tok = self._would_be_token(actor)
            self._submit(actor, "release_own",
                         lambda: C.finalize(self.root, tok))

    def start_release_force(self, actor):
        if not self.busy[actor]:
            self._submit(actor, "release_force",
                         lambda: C.force_requeue(self.root, ITEM))

    def start_reclaim(self, actor):
        if not self.busy[actor]:
            self._submit(actor, "reclaim", lambda: C.recover(self.root))

    # C-native aliases
    start_complete = start_release_own
    start_recover = start_reclaim

    def step(self, actor, n):
        for _ in range(n):
            if not self.busy[actor]:
                return
            # Two-phase arrival wait: 50ms fast path, then skip instantly if the
            # worker announced a flock wait, else allow a slow-CI second.
            if not self.gate.at_gate[actor].wait(timeout=0.05):
                if self.in_flock.get(actor):
                    return
                if not self.gate.at_gate[actor].wait(timeout=0.95):
                    return
            self.gate.at_gate[actor].clear()
            self.gate.go[actor].release()
            deadline = time.time() + 1.0
            while (time.time() < deadline and self.busy[actor]
                   and not self.gate.at_gate[actor].is_set()):
                time.sleep(0.0005)

    def plant_dead_claim(self):
        """A worker that claimed and died: HELD by a dead owner. Planted as a
        LEGAL state (publish-if-absent, then move ready -> inflight), so the
        single-live-object invariant is never violated by the scaffolding —
        if the multiplicity detector fires, the protocol did it."""
        if any(self.busy.values()) or self._believers():
            return
        key = C.safe_key(ITEM)
        with C.key_lock(self.root, key):
            st = C._state_of(self.root, key)
            if st.state is C.HELD:
                return
            if st.state is C.ABSENT:
                tmp = C._d(self.root, C.TMP) / f"{key}~plant~{time.time_ns()}"
                tmp.write_text("payload", encoding="utf-8")
                os.rename(str(tmp), str(C._d(self.root, C.READY) / f"{key}~0"))
                st = C._state_of(self.root, key)
            ghost = C.SEP.join((key, str(st.gen), "ghost", str(DEAD_PID), "1"))
            os.rename(str(st.path), str(C._d(self.root, C.INFLIGHT) / ghost))

    def check_consistency(self):
        if any(self.busy.values()):
            return
        self._replay_tokens()
        self._check()

    # ── settle + oracle ────────────────────────────────────────────────────
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
        """Believers from the log: a successful claim grants, own-finalize
        releases, a successful force clears everyone (A's ratified contract:
        force administratively destroys whatever occupies the slot)."""
        held = {}
        held_done = {}                           # actor -> claim done_id
        for done_id, actor, op, res, start_id in sorted(self.done_log):
            if isinstance(res, Exception):
                raise AssertionError(f"{actor} {op} raised {res!r}")
            if op in ("acquire", "claim") and isinstance(res, str):
                held[actor] = res
                held_done[actor] = done_id
            elif op == "release_own" and res is True:
                held.pop(actor, None)
                held_done.pop(actor, None)
            elif op == "release_force" and res is True:
                held.clear()
                held_done.clear()
            elif op == "reclaim" and res:
                key = C.safe_key(ITEM)
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
        held = self._replay_tokens()
        assert len(held) <= 1, f"TWO live claim holders: {sorted(held)}"
        try:
            st = C.observe(self.root, ITEM)
        except C.InvariantError as e:
            raise AssertionError(f"single-live-object invariant: {e}") from e
        if held:
            actor, tok = next(iter(held.items()))
            assert st.state is C.HELD, \
                f"{actor} holds the claim but the item is {st.state}"
            assert st.worker == actor, \
                f"live object names {st.worker!r}, holder is {actor!r}"
            assert st.token == tok, \
                f"{actor}'s token is stale: live={st.token!r} believed={tok!r}"

    def finish(self, check=True):
        try:
            self._settle()
            if check:
                self._check()
        finally:
            self.gate.open = True
            for a in ACTORS:
                self.inbox[a].put(None)
            for t in self.threads.values():
                t.join(timeout=2)
            C.os = self._saved_os
            C.fcntl = self._saved_fcntl
            C.Path = self._saved_path
            GatedPath._gate = None
            self.tmp.cleanup()


# ── explorer (mirrors the A explorer rule-for-rule) ────────────────────────
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

    class CClaimMachine(RuleBasedStateMachine):
        def __init__(self):
            super().__init__()
            self.driver = CClaimDriver()

        actors = st.sampled_from(ACTORS)

        @rule()
        def publish(self):
            self.driver.publish()

        @rule(actor=actors)
        def start_acquire(self, actor):
            self.driver.start_acquire(actor)

        @rule(actor=actors)
        def start_release_own(self, actor):
            self.driver.start_release_own(actor)

        @rule(actor=actors)
        def start_release_force(self, actor):
            self.driver.start_release_force(actor)

        @rule(actor=actors)
        def start_reclaim(self, actor):
            self.driver.start_reclaim(actor)

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
    run_state_machine_as_test(CClaimMachine, settings=cfg)
    print(f"PASS: CClaimMachine — {examples} examples x {steps} steps, "
          "single-owner + token-currency + single-object + no-steal invariants held")


if __name__ == "__main__":
    main()
