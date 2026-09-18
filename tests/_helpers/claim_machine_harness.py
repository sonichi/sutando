"""Shared harness for the outbox ClaimMachine: gated-boundary concurrency
driver over the REAL src/outbox.py. No hypothesis dependency — the explorer
(outbox-claim-machine.test.py) and the deterministic regression replays
(outbox-claim-regressions.test.py) both build on this.

Concurrency contract the oracle encodes (owner-ratified 2026-08-17):
- force-release administratively destroys whatever claim instance occupies
  the slot at its own unlink instant — a live claim may be revoked by design;
  it must never remove a claim created after it completes. Oracle mapping: a
  successful force clears every believer.
- ownership release and reclaim may only remove a claim instance they
  verified as their own/observed, verified at the same serialization point as
  the removal. Destroying an unverified successor's claim (the cb2f59f1
  counterexample: reclaim's compare-then-act tail landing on a fresh claim
  after force+acquire turned the slot over) is a protocol defect.
"""
from __future__ import annotations

import importlib.util
import inspect
import json
import os
import queue
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

# CI runs tests from the repo root; derive it the way the sibling suites do.
_REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SRC = Path(_REPO) / "src" / "outbox.py"
sys.path.insert(0, str(SRC.parent))
spec = importlib.util.spec_from_file_location("outbox_claim_machine_sut", SRC)
ob = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = ob                      # dataclasses need the registry
spec.loader.exec_module(ob)

ITEM = "room-evt-1"
ACTORS = ("drainer-A", "drainer-B")

_REL_SIG = inspect.signature(ob.release_delivery_claim)
_HAS_OWNERSHIP = "drainer_id" in _REL_SIG.parameters
_HAS_FORCE = "force" in _REL_SIG.parameters


class Gate:
    """Boundary scheduler with a synchronous handshake: a worker announces
    at_gate and blocks; the driver clears it, grants one permit, and waits
    for the worker to reach its next observable state. Interleavings are
    then chosen by the op sequence, not by OS thread timing."""

    def __init__(self):
        self.open = False
        self.go = {a: threading.Semaphore(0) for a in ACTORS}
        self.at_gate = {a: threading.Event() for a in ACTORS}
        self.actor_of = {}                       # thread ident -> actor name

    def pause(self):
        if self.open:
            return
        actor = self.actor_of.get(threading.get_ident())
        if actor is None:                        # main thread: never gated
            return
        self.at_gate[actor].set()
        while not self.open:
            if self.go[actor].acquire(timeout=0.01):
                return


class OsProxy:
    """os facade for the SUT module: identical, except calls that touch the
    test root pause at the gate first. This is the preemption point."""

    _GATED = {"link", "unlink", "stat", "open", "replace", "rename", "fsync"}

    def __init__(self, real, gate, root_holder):
        self._real, self._gate, self._root = real, gate, root_holder

    def __getattr__(self, name):
        val = getattr(self._real, name)
        if name not in self._GATED or not callable(val):
            return val

        def wrapped(*a, **k):
            root = self._root.get("root")
            if root and any(isinstance(x, (str, Path)) and str(x).startswith(root)
                            for x in a):
                self._gate.pause()
            return val(*a, **k)
        return wrapped


class GatedPath(type(Path())):
    """Path whose mutating/reading methods are boundaries too — p.unlink()
    and p.read_text() are where several historical windows lived."""
    _gate = None
    _root = None

    def _maybe_pause(self):
        g = GatedPath._gate
        if g is not None and GatedPath._root and \
                str(self).startswith(GatedPath._root):
            g.pause()

    def unlink(self, *a, **k):
        self._maybe_pause()
        return super().unlink(*a, **k)

    def read_text(self, *a, **k):
        self._maybe_pause()
        return super().read_text(*a, **k)

    def write_text(self, *a, **k):
        self._maybe_pause()
        return super().write_text(*a, **k)

    def stat(self, *a, **k):
        self._maybe_pause()
        return super().stat(*a, **k)


class LockProxy:
    """Lock facade: a worker announces entry into a lock wait so the
    scheduler can skip its steps instead of burning the arrival window."""

    def __init__(self, real, driver):
        self._real, self._driver = real, driver

    def __call__(self, fd, **kwargs):
        d = self._driver
        actor = d.gate.actor_of.get(threading.get_ident())
        if actor is not None:
            d.in_flock[actor] = True
        try:
            return self._real(fd, **kwargs)
        finally:
            if actor is not None:
                d.in_flock[actor] = False


def _dead_pid():
    """A pid that is genuinely dead: spawn-and-reap a child."""
    p = subprocess.Popen([sys.executable, "-c", "pass"])
    p.wait()
    return p.pid


DEAD_PID = _dead_pid()


class ClaimDriver:
    """Two gated drainer threads over the real outbox; op names match the
    explorer's rules so shrunk counterexamples replay verbatim."""

    def __init__(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="claim-machine-")
        self.root = Path(self.tmp.name)
        self.gate = Gate()
        self._root_holder = {"root": str(self.root)}
        self._saved_os = ob.os
        ob.os = OsProxy(os, self.gate, self._root_holder)
        self.in_flock = {a: False for a in ACTORS}
        self._saved_lock_fd = ob.lock_fd
        ob.lock_fd = LockProxy(self._saved_lock_fd, self)
        self._saved_path = ob.Path
        GatedPath._gate = self.gate
        GatedPath._root = str(self.root)
        ob.Path = GatedPath
        self._saved_read = getattr(ob, "_read_claim_at", None)
        if self._saved_read is not None:
            def gated_read(p, item_id, _orig=self._saved_read):
                self.gate.pause()
                return _orig(p, item_id)
            ob._read_claim_at = gated_read
        self.stats = {"granted": 0, "arrival_timeout": 0, "flock_skip": 0,
                      "idle_skip": 0}
        self.done_log = []                       # (seq, actor, op, result)
        self._seq_lock = threading.Lock()
        self._seq = 0
        self.inbox = {a: queue.Queue() for a in ACTORS}
        self.busy = {a: False for a in ACTORS}
        self.threads = {}
        for a in ACTORS:
            t = threading.Thread(target=self._worker, args=(a,), daemon=True)
            self.threads[a] = t
            t.start()
            self.gate.actor_of[t.ident] = a

    # ── worker plumbing ─────────────────────────────────────────────────────
    def _worker(self, actor):
        while True:
            job = self.inbox[actor].get()
            if job is None:
                return
            op, fn = job
            try:
                res = fn()
            except Exception as e:                # a protocol crash is a finding
                res = e
            with self._seq_lock:
                self._seq += 1
                self.done_log.append((self._seq, actor, op, res))
            self.busy[actor] = False

    def _submit(self, actor, op, fn):
        self.busy[actor] = True
        self.inbox[actor].put((op, fn))

    # ── ops (signature-adapted, logic is always the SUT's) ─────────────────
    def _op_acquire(self, actor):
        return lambda: ob.acquire_delivery_claim(self.root, ITEM, actor)

    def _op_reclaim(self, actor):
        return lambda: ob.reclaim_delivery_claim(self.root, ITEM, 0.0, actor)

    def _op_release_own(self, actor):
        if _HAS_OWNERSHIP:
            return lambda: ob.release_delivery_claim(self.root, ITEM, actor)
        return lambda: ob.release_delivery_claim(self.root, ITEM)

    def _op_release_force(self):
        if _HAS_FORCE:
            return lambda: ob.release_delivery_claim(self.root, ITEM, force=True)
        return lambda: ob.release_delivery_claim(self.root, ITEM)

    # ── op surface (names match the explorer's rules) ───────────────────────
    def start_acquire(self, actor):
        if not self.busy[actor]:
            self._submit(actor, "acquire", self._op_acquire(actor))

    def start_reclaim(self, actor):
        if not self.busy[actor]:
            self._submit(actor, "reclaim", self._op_reclaim(actor))

    def start_release_own(self, actor):
        if not self.busy[actor]:
            self._submit(actor, "release_own", self._op_release_own(actor))

    def start_release_force(self, actor):
        if not self.busy[actor]:
            self._submit(actor, "release_force", self._op_release_force())

    # Instrumented/loaded runners (coverage trace, 2-core CI) can delay gate
    # arrival past 1s; the bound only burns time when arrival is genuinely slow.
    ARRIVAL_BOUND = float(os.environ.get("CLAIM_MACHINE_ARRIVAL_BOUND", "5.0"))

    def step(self, actor, n):
        for _ in range(n):
            if not self.busy[actor]:
                self.stats["idle_skip"] += 1
                return
            # 50ms fast path, then skip instantly if the worker announced a
            # flock wait, else poll up to ARRIVAL_BOUND with early exits.
            if not self.gate.at_gate[actor].wait(timeout=0.05):
                if self.in_flock.get(actor):
                    self.stats["flock_skip"] += 1
                    return
                deadline = time.time() + self.ARRIVAL_BOUND
                while True:
                    if self.gate.at_gate[actor].wait(timeout=0.1):
                        break
                    if not self.busy[actor]:
                        self.stats["idle_skip"] += 1
                        return
                    if self.in_flock.get(actor):
                        self.stats["flock_skip"] += 1
                        return
                    if time.time() >= deadline:
                        self.stats["arrival_timeout"] += 1
                        return
            self.stats["granted"] += 1
            self.gate.at_gate[actor].clear()
            self.gate.go[actor].release()
            deadline = time.time() + self.ARRIVAL_BOUND
            while (time.time() < deadline and self.busy[actor]
                   and not self.gate.at_gate[actor].is_set()
                   and not self.in_flock.get(actor)):
                time.sleep(0.0005)

    def plant_dead_claim(self):
        """A drainer that died mid-delivery: stale claim, dead owner."""
        p = ob._claim_path(self.root, ITEM)
        if p.exists() or self._believers():
            return
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({
            "item_id": ITEM, "drainer_id": "ghost", "pid": DEAD_PID,
            "start_usec": 1, "claimed_at": time.time() - 3600.0,
        }, sort_keys=True), encoding="utf-8")

    def check_consistency(self):
        # Opportunistic: only when no op is mid-flight (a half-finished
        # mutation is not a violation, the state it leaves behind is).
        if any(self.busy.values()):
            return
        self._check()

    # ── settle + oracle ─────────────────────────────────────────────────────
    def _settle(self):
        self.gate.open = True
        deadline = time.time() + 30.0
        while any(self.busy.values()) and time.time() < deadline:
            time.sleep(0.005)
        assert not any(self.busy.values()), "op wedged: protocol deadlock"
        self.gate.open = False
        for a in ACTORS:                         # drain stale grants
            while self.gate.go[a].acquire(blocking=False):
                pass
            self.gate.at_gate[a].clear()

    def _believers(self):
        held = set()
        for _seq, actor, op, res in sorted(self.done_log):
            if isinstance(res, Exception):
                raise AssertionError(f"{actor} {op} raised {res!r}")
            ok = (res is True) or (not _HAS_OWNERSHIP and op.startswith("release"))
            if op in ("acquire", "reclaim") and ok:
                held.add(actor)
            elif op == "release_own" and ok:
                held.discard(actor)
            elif op == "release_force" and ok:
                held.clear()
        return held

    def _check(self):
        held = self._believers()
        assert len(held) <= 1, f"TWO live claim holders: {sorted(held)}"
        if held:
            rec = ob.read_delivery_claim(self.root, ITEM)
            assert rec is not None, \
                f"{next(iter(held))} holds the claim but the claim file is gone"
            assert rec.drainer_id in held, \
                f"claim file names {rec.drainer_id!r}, holder is {sorted(held)}"

    def finish(self, check=True):
        """Settle, optionally run the oracle, then tear the driver down."""
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
            ob.os = self._saved_os
            ob.lock_fd = self._saved_lock_fd
            ob.Path = self._saved_path
            GatedPath._gate = None
            if self._saved_read is not None:
                ob._read_claim_at = self._saved_read
            self.tmp.cleanup()
