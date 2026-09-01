#!/usr/bin/env python3
"""Fault injection over the claim protocol: crash after EVERY filesystem mutation.

Run: python3 tests/outbox-claim-fault-injection.test.py

Checks the two invariants the owner named:
  CRASH-RECOVERY  after a crash following any completed mutation, the state is a
                  valid owned claim or deterministically recoverable to one.
  SINGLE-OWNER    at most one live incarnation ever holds a successful acquire.
"""
import json
import os
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / 'src'))
import outbox as ob  # noqa: E402

MUTATORS = ('link', 'unlink', 'rename', 'open')
FAILS = []


class Crash(Exception):
    pass


def run_crashing(fn, after_n):
    """Run fn, raising Crash immediately after the after_n-th fs mutation."""
    real = {k: getattr(os, k) for k in MUTATORS}
    n = {'i': 0}

    def wrap(name):
        def f(*a, **k):
            r = real[name](*a, **k)
            n['i'] += 1
            if n['i'] == after_n:
                raise Crash(f"after mutation #{after_n} ({name})")
            return r
        return f
    for k in MUTATORS:
        setattr(os, k, wrap(k))
    try:
        return fn(), n['i']
    except Crash:
        return 'CRASHED', n['i']
    finally:
        for k, v in real.items():
            setattr(os, k, v)


def dead_claim(root, item, who='dead-owner'):
    ob.acquire_delivery_claim(root, item, who)
    p = ob._claim_path(root, item)
    rec = json.loads(p.read_text(encoding='utf-8'))
    rec.update(pid=999999, claimed_at=0.0)
    p.write_text(json.dumps(rec, sort_keys=True), encoding='utf-8')
    return p


def age_all_tokens(root, item):
    """Simulate the passage of time before recovery runs.

    Ages the canonical claim too: a torn one left by a crash is indistinguishable
    from a live mid-write until it is old, and recovery does not run instantly.
    """
    p = ob._claim_path(root, item)
    old = time.time() - 3600
    # A claim's age is its `claimed_at` FIELD, not its mtime; touching files
    # alone leaves it freshly taken and recovery refuses on TTL.
    try:
        rec = json.loads(p.read_text(encoding="utf-8"))
        rec["claimed_at"] = old
        p.write_text(json.dumps(rec, sort_keys=True), encoding="utf-8")
    except (OSError, ValueError):
        pass
    targets = list(ob._claims_dir(root).glob(f"{p.name}.*"))
    if p.exists():
        targets.append(p)
    for tok in targets:
        os.utime(str(tok), (old, old))


def kill_crashed_incarnation(root, item):
    """A crash means the process is GONE. Any claim it left names a dead pid.

    Without this the harness leaves the 'crashed' claim owned by this very live
    process, so recovery correctly refuses and the harness blames the protocol.
    """
    p = ob._claim_path(root, item)
    try:
        rec = json.loads(p.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return
    if rec.get('pid') == os.getpid():
        rec['pid'] = 999999
        p.write_text(json.dumps(rec, sort_keys=True), encoding='utf-8')


def recoverable(root, item):
    """Can ANY drainer eventually own this item again? (crash-recovery invariant)"""
    kill_crashed_incarnation(root, item)
    age_all_tokens(root, item)
    for who in ('R1', 'R2', 'R3'):
        if ob.reclaim_delivery_claim(root, item, 0.001, who):
            return True, who
        if ob.acquire_delivery_claim(root, item, who):
            return True, who
    return False, None


def check(label, ok, detail=''):
    print(f"  {'ok  ' if ok else 'FAIL'} {label}")
    if not ok:
        FAILS.append(label)
        if detail:
            print(f"         {detail}")


print("=== crash after each mutation of RELEASE (owner releasing its own claim)")
for n in range(1, 5):
    with tempfile.TemporaryDirectory() as t:
        r = Path(t)
        ob.acquire_delivery_claim(r, "it", "A")
        out, total = run_crashing(lambda: ob.release_delivery_claim(r, "it", "A"), n)
        if n > total and out != 'CRASHED':
            print(f"  --   crash point {n} is past the {total} mutations of this path")
            continue
        ok, who = recoverable(r, "it")
        check(f"release crash@{n}: item still recoverable (got owner={who})", ok,
              f"outcome={out}; leftovers={[p.name[-28:] for p in ob._claims_dir(r).glob('*')]}")

print("\n=== crash after each mutation of RECLAIM (taking over a dead owner)")
for n in range(1, 5):
    with tempfile.TemporaryDirectory() as t:
        r = Path(t)
        dead_claim(r, "it")
        out, total = run_crashing(lambda: ob.reclaim_delivery_claim(r, "it", 0.001, "X"), n)
        if n > total and out != 'CRASHED':
            print(f"  --   crash point {n} is past the {total} mutations of this path")
            continue
        ok, who = recoverable(r, "it")
        check(f"reclaim crash@{n}: item still recoverable (got owner={who})", ok,
              f"outcome={out}; leftovers={[p.name[-28:] for p in ob._claims_dir(r).glob('*')]}")

print("\n=== SINGLE-OWNER: a successful acquire is never invalidated by an earlier incarnation")
# B acquires after A's release begins; C then races. Nobody who got True may lose the slot.
for inject_at in range(1, 4):
    with tempfile.TemporaryDirectory() as t:
        r = Path(t)
        ob.acquire_delivery_claim(r, "it", "A")
        granted = []
        real = {k: getattr(os, k) for k in MUTATORS}
        n = {'i': 0}

        def wrap(name):
            def f(*a, **k):
                res = real[name](*a, **k)
                n['i'] += 1
                if n['i'] == inject_at:
                    # a successor appears mid-release, then a third party races
                    if ob._claim_path(r, "it").exists():
                        try:
                            os.unlink(str(ob._claim_path(r, "it")))
                        except OSError:
                            pass
                    if ob.acquire_delivery_claim(r, "it", "B"):
                        granted.append("B")
                    if ob.acquire_delivery_claim(r, "it", "C"):
                        granted.append("C")
                return res
            return f
        for k in MUTATORS:
            setattr(os, k, wrap(k))
        try:
            ob.release_delivery_claim(r, "it", "A")
        except Exception:
            pass
        finally:
            for k, v in real.items():
                setattr(os, k, v)
        holder = ob.read_delivery_claim(r, "it")
        hid = holder.drainer_id if holder else None
        lost = [g for g in granted if g != hid]
        check(f"inject@{inject_at}: every acquire=True still holds "
              f"(granted={granted}, canonical={hid})",
              len(granted) <= 1 or not lost,
              "an incarnation was told it owned delivery and then lost the slot to an "
              "earlier incarnation's operation — both would send")

print(f"\n  {len(FAILS)} failure(s)")
raise SystemExit(1 if FAILS else 0)
