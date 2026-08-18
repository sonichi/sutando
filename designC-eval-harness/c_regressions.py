#!/usr/bin/env python3
"""Acceptance matrix for Design C — the whole owner-specified set in one file.

  arm 1   the four frozen A counterexamples, replayed against C
  arm 1b  the cb2f59f1 steal sweep (offset-swept, version-robust)
  arm 2   B's CE-1 publish-while-inflight and CE-3 handoff, in C's vocabulary
  arm 3   the counterexamples C makes UNREPRESENTABLE (B's CE-2/CE-2b/CE-4),
          each with the detector that fires if the state is forced anyway
  arm 4   positive control (--prove-bite): with the ownership mutex neutered,
          at least one arm above must violate, and the run names which

Provenance note, and it matters for how much arm 1 proves: the four schedules
below are copied verbatim (git show origin/exp/claim-machine:tests/
outbox-claim-regressions.test.py) from #3006, where they were SHRUNK against
A's syscall-boundary sequence. Replayed against a different SUT they are
schedule-SHAPE replays, not boundary-aligned ones — a green arm 1 is necessary,
not sufficient. Arm 1b's sweep is the arm with teeth, exactly as #3006 says of
itself, which is why both run here.
"""
from __future__ import annotations

import contextlib
import os
import pathlib
import sys
import tempfile
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(_HERE), "tests", "_helpers"))
sys.path.insert(0, _HERE)

import designC as C  # noqa: E402
from claim_machine_c import CClaimDriver  # noqa: E402
from claim_machine_harness import DEAD_PID, ITEM  # noqa: E402

# ── the four frozen A counterexamples, verbatim from #3006 ──────────────────
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
    """Returns None if the oracle held, else the violation message."""
    d = CClaimDriver()
    try:
        for op in ops:
            getattr(d, op[0])(*op[1:])
        d.finish(check=check)
        return None
    except AssertionError as e:
        with contextlib.suppress(BaseException):
            d.finish(check=False)
        return str(e)


def _drain(d, actor, rounds=30):
    for _ in range(rounds):
        if not d.busy[actor]:
            return
        d.step(actor, 1)


def steal_sweep(max_k=12):
    """cb2f59f1 class, swept over every pause offset: reclaim parks after its
    k-th boundary while force+acquire turn the slot over, then resumes its
    tail. Returns (k, err) for the first violating offset, else None."""
    for k in range(1, max_k + 1):
        d = CClaimDriver()
        err = None
        try:
            d.plant_dead_claim()
            d.start_reclaim("drainer-B")
            d.step("drainer-B", k)
            d.start_release_force("drainer-A")
            _drain(d, "drainer-A")
            d.start_acquire("drainer-A")
            _drain(d, "drainer-A")
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


def publish_sweep(max_k=14):
    """C-specific, and the sweep that covers B's CE-1 class in C's vocabulary:
    acquire folds publish-if-absent into claim, so one acquire parked at offset
    k while another runs to completion is where a publish can land on an item
    that has become HELD. Starts from ABSENT — the window needs a publish that
    OBSERVED absence, so planting a ghost first makes every offset unreachable
    (measured: with a ghost planted the whole sweep is toothless)."""
    for k in range(1, max_k + 1):
        d = CClaimDriver()
        err = None
        try:
            d.start_acquire("drainer-A")          # from ABSENT: A's publish
            d.step("drainer-A", k)                # parks mid-publish
            d.start_acquire("drainer-B")
            _drain(d, "drainer-B", 40)
            for _ in range(80):
                if not (d.busy["drainer-A"] or d.busy["drainer-B"]):
                    break
                d.step("drainer-A", 1)
                d.step("drainer-B", 1)
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


def ce1_publish_while_inflight():
    """B's CE-1 schedule in C's vocabulary: a dead ghost is planted, one actor
    acquires (publish-if-absent + claim), the other recovers. Pre-fix B
    re-opened a ready slot for an owned item."""
    return replay([('plant_dead_claim',),
                   ('start_acquire', 'drainer-A'),
                   ('step', 'drainer-A', 12),
                   ('start_reclaim', 'drainer-B'),
                   ('step', 'drainer-B', 12),
                   ('check_consistency',)])


def ce3_recover_to_claim_handoff():
    """B's CE-3 acceptance pin: recover re-arms a dead owner's item and ANOTHER
    actor claims it from that slot before recover returns. Causally legal; the
    oracle must ACCEPT it (its falsifier is the retired completion-order
    oracle, which false-positived here)."""
    return replay([('plant_dead_claim',),
                   ('start_reclaim', 'drainer-A'),
                   ('step', 'drainer-A', 3),
                   ('start_acquire', 'drainer-B'),
                   ('step', 'drainer-B', 12),
                   ('step', 'drainer-A', 12),
                   ('check_consistency',)])


# ── arm 3: states C makes unrepresentable, each with its detector ───────────
def _force_two_tokens(root):
    """Write the CE-2/CE-4 precondition DIRECTLY to the filesystem, bypassing
    every protocol edge: one dead token and one live token for the same item.
    No sequence of C's ops can produce this — each edge is one rename out of a
    verified single-object state — so the question is whether C DETECTS it or
    silently acts on it (B's defect was acting: re-arming a live-held item)."""
    key = C.safe_key(ITEM)
    d = C._d(root, C.INFLIGHT)
    (d / C.SEP.join((key, "0", "ghost", str(DEAD_PID), "1"))).write_text("{}")
    (d / C.SEP.join((key, "0", "live", str(os.getpid()), "9"))).write_text("{}")
    return key


def ce2_holder_is_unambiguous():
    """B's CE-2/CE-2b class (holder() must not answer by iteration order).
    C has no set to order — but if the two-token state is forced, holder()
    must say so rather than pick."""
    with tempfile.TemporaryDirectory(prefix="c-ce2-") as td:
        _force_two_tokens(td)
        try:
            got = C.holder(td, ITEM)
        except C.InvariantError:
            return None
        return (f"holder() answered {got!r} for a two-object item instead of "
                "raising: the iteration-order defect class is reachable")


def ce4_recover_cannot_rearm_owned():
    """B's CE-4 (recover re-arms a slot for an owned item) needs the same
    forced state. C's recover must refuse to transition it — loudly."""
    with tempfile.TemporaryDirectory(prefix="c-ce4-") as td:
        key = _force_two_tokens(td)
        try:
            moved = C.recover(td)
        except C.InvariantError:
            ready = pathlib.Path(td) / C.READY
            leaked = [f.name for f in ready.iterdir()] if ready.exists() else []
            if leaked:
                return f"recover raised but still re-armed {leaked}"
            return None
        return (f"recover returned {moved!r} for a two-object item: it re-armed "
                f"a slot for {key} while a live token held it")


def ce4_window_probe():
    """CE-4's pattern — a decision invalidated between observe and act — as a
    live probe. A second ownership mutator (force_requeue) is fired from
    another thread while recover sits INSIDE its transition. Under the mutex it
    blocks, re-observes READY afterwards and does nothing; the item ends with
    exactly one object. (A racing CLAIM cannot test this: inside the window the
    item is still HELD, so the claim loses whether or not a mutex exists — that
    version of the probe is toothless, measured.)"""
    import threading
    with tempfile.TemporaryDirectory(prefix="c-ce4b-") as td:
        C.publish(td, ITEM, "payload")
        key = C.safe_key(ITEM)
        st = C._state_of(td, key)
        ghost = C.SEP.join((key, "0", "ghost", str(DEAD_PID), "1"))
        os.rename(str(st.path), str(C._d(td, C.INFLIGHT) / ghost))

        real_rename, out = C._rename, {}

        def racing_rename(src, dst):
            if C.READY in str(dst) and "thread" not in out:
                t = threading.Thread(
                    target=lambda: out.setdefault(
                        "forced", C.force_requeue(td, ITEM)))
                out["thread"] = t
                t.start()
                time.sleep(0.15)          # the window, if there is one
            return real_rename(src, dst)

        C._rename = racing_rename
        try:
            C.recover(td)
        except C.InvariantError as e:
            return f"recover's transition was invalidated under it: {e}"
        finally:
            C._rename = real_rename
        if "thread" not in out:
            return "probe never reached recover's transition"
        out["thread"].join(timeout=5)
        try:
            final = C.observe(td, ITEM)
        except C.InvariantError as e:
            return f"two live objects after the probe: {e}"
        if final.state is not C.AVAILABLE:
            return f"item ended {final.state}, not READY(recovered)"
        return None


def main() -> int:
    prove_bite = "--prove-bite" in sys.argv
    prove_detector = "--prove-detector" in sys.argv
    if prove_bite:
        @contextlib.contextmanager
        def no_lock(root, key):
            yield
        C.key_lock = no_lock
    if prove_detector:
        # SECOND positive control, for the arms the mutex control cannot reach:
        # the forced-state arms (CE-2/CE-2b/CE-4) are about the multiplicity
        # DETECTOR, so their falsifier is removing the detector, not the lock.
        # Blinded, _locate answers with an arbitrary one of the two objects —
        # exactly B's pre-fix iteration-order semantics.
        def _first_hit(root, key):
            hits = []
            for d in (C.READY, C.INFLIGHT):
                q = C.Path(root) / d
                if not q.exists():
                    continue
                for f in sorted(q.iterdir()):
                    if f.name.split(C.SEP)[0] == key:
                        hits.append((d, f))
            return hits[0] if hits else None
        C._locate = _first_hit

    failures, bites = 0, []

    for name, ops in SCRIPTS.items():
        err = replay(ops)
        print(("  ok   " if err is None else "  FAIL ")
              + f"frozen A schedule {name}" + (f"  -> {err}" if err else ""))
        if err:
            bites.append(f"A schedule {name}")
        failures += 0 if err is None else 1

    for label, fn in (("steal sweep (cb2f59f1 class)", steal_sweep),
                      ("publish sweep (B CE-1 class)", publish_sweep)):
        hit = fn()
        if hit is None:
            print(f"  ok   {label} holds at every offset")
        else:
            print(f"  FAIL {label} violates at k={hit[0]} -> {hit[1]}")
            bites.append(f"{label} k={hit[0]}")
            failures += 1

    for label, fn in (("B CE-1 publish-while-inflight", ce1_publish_while_inflight),
                      ("B CE-3 recover->claim handoff accepted", ce3_recover_to_claim_handoff),
                      ("B CE-2/2b holder unambiguous (forced state detected)", ce2_holder_is_unambiguous),
                      ("B CE-4 recover refuses an owned item (forced state)", ce4_recover_cannot_rearm_owned),
                      ("B CE-4 window probe: no window inside the transition", ce4_window_probe)):
        err = fn()
        print(("  ok   " if err is None else "  FAIL ") + label
              + (f"  -> {err}" if err else ""))
        if err:
            bites.append(label)
        failures += 0 if err is None else 1

    if prove_bite or prove_detector:
        if bites:
            what = "ownership mutex" if prove_bite else "multiplicity detector"
            print(f"\nPASS (positive control): {len(bites)} arm(s) violate with "
                  f"the {what} neutered: {bites}")
            print("  NOTE every arm not listed above has NO TEETH against C: "
                  "it holds with the mutex removed, so its green run in the "
                  "normal mode is not evidence about the mutex.")
            return 0
        print("\nFAIL (positive control): nothing violates with that mechanism "
              "removed — the matrix has no teeth and its green run means nothing")
        return 1
    if failures:
        print(f"\nFAILED ({failures})")
        return 1
    print("\nPASS: Design C acceptance matrix — 4 frozen A schedules, 2 sweeps, "
          "5 B-derived cases")
    return 0


if __name__ == "__main__":
    sys.exit(main())
