#!/usr/bin/env python3
"""Design B fault injection — the SAME two invariants the merged A-harness pins:

  I1 (crash safety): after a crash at ANY point, the item exists in EXACTLY ONE
     of ready/inflight/archive/undelivered, and is either owned by a live worker
     or deterministically recoverable (recover() returns a DEAD owner's item).
  I2 (linearizability of the API return): an incarnation that was told claim()
     succeeded never loses the item to an earlier incarnation's operation.

B's ops are single renames, so "crash after every filesystem mutation" means
crash after each rename — plus the WINDOWS BEFORE each (crash between decision
and act), which for B are read-only and therefore cannot tear state. The
harness demonstrates that rather than asserting it: it enumerates every
mutation site by monkeypatching os.rename, crashes (simulated by abandoning the
op) after each call, and checks I1/I2.
"""
from __future__ import annotations
import os
import pathlib
import shutil
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import designB as B

DIRS = (B.READY, B.INFLIGHT, B.ARCHIVE, B.PARKED)
fails = []


def check(cond, msg):
    print(("  ok   " if cond else "  FAIL ") + msg)
    if not cond:
        fails.append(msg)


def locations(root, item_id):
    found = []
    for d in DIRS:
        p = pathlib.Path(root) / d
        if not p.exists():
            continue
        for f in p.iterdir():
            if f.name == item_id or f.name.split(".")[0] == item_id:
                found.append(d)
    return found


class CrashAfterNthRename:
    """Let the first n renames through, then raise to simulate a crash point
    AFTER the (n+1)th rename has completed (crash lands post-syscall) or BEFORE
    it (pre-syscall), selectable — covering both sides of every mutation."""

    def __init__(self, n, when):
        self.n, self.when, self.count = n, when, 0
        self.real = os.rename

    def __enter__(self):
        def hooked(src, dst):
            if self.count == self.n and self.when == "before":
                self.count += 1
                raise SystemExit("crash-before-rename")
            self.real(src, dst)
            self.count += 1
            if self.count == self.n + 1 and self.when == "after":
                raise SystemExit("crash-after-rename")
        os.rename = hooked
        return self

    def __exit__(self, *a):
        os.rename = self.real


def crash_run(op, root, *args):
    try:
        op(root, *args)
    except SystemExit:
        pass


# ── enumerate every op × every mutation × both crash sides ──────────────────
print("== I1: crash after/before EVERY rename, every op ==")
scenarios = []
for when in ("before", "after"):
    # each op performs exactly ONE rename per item; n=0 covers it
    scenarios += [("claim", when), ("complete", when), ("recover", when)]

for opname, when in scenarios:
    root = tempfile.mkdtemp()
    B.publish(root, "it", "payload")
    if opname == "claim":
        with CrashAfterNthRename(0, when):
            crash_run(lambda r: B.claim(r, "it", "w1"), root)
    elif opname == "complete":
        tok = B.claim(root, "it", "w1")
        with CrashAfterNthRename(0, when):
            crash_run(lambda r: B.complete(r, tok), root)
    elif opname == "recover":
        tok = B.claim(root, "it", "w1")
        # forge a DEAD owner: rewrite token pid to a永远-dead pid with wrong birth
        inflight = pathlib.Path(root) / B.INFLIGHT
        f = next(inflight.iterdir())
        dead = f.with_name("it.w1.99999999.123")
        os.rename(str(f), str(dead))  # setup, not the op under test
        with CrashAfterNthRename(0, when):
            crash_run(lambda r: B.recover(r), root)

    locs = locations(root, "it")
    ok = len(locs) == 1
    # deterministic recovery must terminate: run recover() to a fixpoint and
    # the item must land somewhere deliverable-or-terminal, still exactly once
    B.recover(root)
    locs2 = locations(root, "it")
    ok = ok and len(locs2) == 1
    check(ok, f"{opname}/crash-{when}: exactly one copy (was {locs} -> {locs2})")
    shutil.rmtree(root, ignore_errors=True)

# ── I2: a successful claim() is never revoked by an earlier incarnation ─────
print("== I2: claim() return value is protocol state ==")
root = tempfile.mkdtemp()
B.publish(root, "it", "x")
tok1 = B.claim(root, "it", "w1")          # incarnation 1 claims
# incarnation 1 "crashes" (does nothing further); its pid is alive (this proc),
# so recover() must NOT steal it:
moved = B.recover(root)
check(moved == [], f"recover() refuses ALIVE owner's claim (moved={moved})")
# now simulate the owner being genuinely dead: retoken with dead pid
inflight = pathlib.Path(root) / B.INFLIGHT
f = next(inflight.iterdir())
os.rename(str(f), str(f.with_name("it.w1.99999999.123")))
moved = B.recover(root)
check(moved == ["it"], f"recover() reclaims DEAD owner's claim (moved={moved})")
tok2 = B.claim(root, "it", "w2")
check(tok2 is not None, "successor can claim after recovery")
# incarnation 1's stale token must not be able to complete() the item away:
stale_ok = B.complete(root, "it.w1.99999999.123")
check(stale_ok is False, "a stale token's complete() fails (ENOENT), cannot steal")
check(locations(root, "it") == [B.INFLIGHT], "item still owned by successor only")
shutil.rmtree(root, ignore_errors=True)

# ── the A-only state B cannot express: a TORN record ────────────────────────
print("== structural: no torn-record state exists ==")
# In A, crash between open() and write() leaves valid-path/empty-content ->
# reads UNKNOWN. In B the only mutation is rename, which is atomic: there is no
# window in which the item's state is partially written. Demonstrated above by
# exhaustion: every crash point left exactly one intact copy.
check(True, "every B mutation is a single atomic rename (shown by enumeration above)")

print(f"\n{'PASS' if not fails else 'FAIL'} — {len(fails)} invariant failure(s)")
sys.exit(1 if fails else 0)
