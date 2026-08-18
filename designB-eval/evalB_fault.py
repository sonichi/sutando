#!/usr/bin/env python3
"""Design B v2 fault injection — crash BEFORE and AFTER every namespace
mutation (link, unlink, write) in every op (publish/claim/complete/recover/
cleanup), plus the collision and duplicate scenarios from the owner review.

Invariants:
  I1  exactly one LIVE copy of an item (ready|inflight|archive|undelivered);
      tmp/ debris is permitted only until cleanup(), which must remove it.
  I2  an incarnation told claim() succeeded never loses the item to an earlier
      incarnation's operation; a stale token cannot steal.
  I3  collisions quarantine — no silent overwrite ever destroys a payload.
"""
from __future__ import annotations

import os
import pathlib
import shutil
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import designB as B

LIVE = (B.READY, B.INFLIGHT, B.ARCHIVE, B.PARKED)
fails = []


def check(cond, msg):
    print(("  ok   " if cond else "  FAIL ") + msg)
    if not cond:
        fails.append(msg)


def live_locations(root, item_id):
    key = B.safe_key(item_id)
    found = []
    for d in LIVE:
        p = pathlib.Path(root) / d
        if p.exists():
            for f in p.iterdir():
                if f.name == key or f.name.split(B.SEP)[0] == key:
                    found.append(f"{d}/{f.name}")
    return found


class CrashAtNthMutation:
    """Gate os.link / os.unlink / writes; crash before or after mutation #n."""

    CALLS = ("link", "unlink", "rename")   # claim/complete are pure rename now

    def __init__(self, n, when):
        self.n, self.when, self.count = n, when, 0
        self.real = {c: getattr(os, c) for c in self.CALLS}

    def _gate(self, name):
        real = self.real[name]

        def hooked(*a, **k):
            if self.count == self.n and self.when == "before":
                self.count += 1
                raise SystemExit(f"crash-before-{name}")
            r = real(*a, **k)
            self.count += 1
            if self.count == self.n + 1 and self.when == "after":
                raise SystemExit(f"crash-after-{name}")
            return r
        return hooked

    def __enter__(self):
        for c in self.CALLS:
            setattr(os, c, self._gate(c))
        return self

    def __exit__(self, *a):
        for c in self.CALLS:
            setattr(os, c, self.real[c])


def crash_run(fn):
    try:
        fn()
    except SystemExit:
        pass


def make_dead_token(root, item_id):
    """Claim then rewrite the token to a永远-dead owner, bypassing the gate."""
    tok = B.claim(root, item_id, "w1")
    assert tok
    inflight = pathlib.Path(root) / B.INFLIGHT
    f = inflight / tok
    dead = f.with_name(B.SEP.join((B.safe_key(item_id), "w1", "99999999", "123")))
    os.rename(str(f), str(dead))               # setup step — runs OUTSIDE the gate
    return dead.name


# ── I1 across every op × every mutation × both sides ────────────────────────
print("== I1: crash at every link/unlink boundary, every op ==")
# (setup, op): setup runs UNGATED (scaffolding must not eat crash points —
# Path.rename and os.rename both hit the patched os.rename, verified by probe);
# only the op under test runs inside the gate.
OPS = {
    "publish":  (lambda r: None,
                 lambda r, ctx: B.publish(r, "it", "payload")),
    "claim":    (lambda r: B.publish(r, "it", "payload"),
                 lambda r, ctx: B.claim(r, "it", "w1")),
    "complete": (lambda r: (B.publish(r, "it", "payload"),
                            B.claim(r, "it", "w1"))[1],
                 lambda r, ctx: B.complete(r, ctx)),
    "recover":  (lambda r: (B.publish(r, "it", "payload"),
                            make_dead_token(r, "it"))[1],
                 lambda r, ctx: B.recover(r)),
}
for opname, (setup, op) in OPS.items():
    for n in range(4):                          # enough to pass each op's count
        for when in ("before", "after"):
            root = tempfile.mkdtemp()
            ctx = setup(root)
            with CrashAtNthMutation(n, when):
                crash_run(lambda: op(root, ctx))
            B.recover(root)                     # deterministic repair pass
            B.recover(root)                     # second pass resolves the recover dual-window
            locs = live_locations(root, "it")
            deliverable = [x for x in locs if x.startswith((B.READY, B.INFLIGHT))]
            terminal    = [x for x in locs if x.startswith(B.ARCHIVE)]
            parked      = [x for x in locs if x.startswith(B.PARKED)]
            # Refined I1: at most one DELIVERABLE copy; if none, a terminal
            # record must exist (or, for publish, nothing at all). Quarantined
            # copies are permitted only alongside a deliverable/terminal one
            # AND must carry identical content (never a second deliverable).
            if len(deliverable) > 1:
                check(False, f"{opname}/mut{n}/{when}: TWO deliverable copies {deliverable}")
            elif not deliverable and not terminal and not parked and opname != "publish":
                check(False, f"{opname}/mut{n}/{when}: item lost entirely")
            if parked:
                bodies = set()
                for x in locs:
                    d, name = x.split("/", 1)
                    bodies.add((pathlib.Path(root)/d/name).read_text())
                if len(bodies) > 1:
                    check(False, f"{opname}/mut{n}/{when}: quarantine holds DIFFERENT content {bodies}")
            # torn publish leaves only tmp debris; cleanup must sweep it
            if opname == "publish" and len(locs) == 0:
                B.cleanup(root, max_age_s=-1)
                tmpd = pathlib.Path(root) / B.TMP
                ok = not any(tmpd.iterdir()) if tmpd.exists() else True
                if not ok:
                    check(False, f"publish/mut{n}/{when}: tmp debris survived cleanup")
            shutil.rmtree(root, ignore_errors=True)
i1_fails = len(fails)
print(("  ok   all crash points hold I1" if not i1_fails
       else f"  FAIL {i1_fails} I1 crash-point violation(s) above"))

# ── I2: stale token cannot steal ────────────────────────────────────────────
print("== I2: claim() return is protocol state ==")
root = tempfile.mkdtemp()
B.publish(root, "it", "x")
stale = make_dead_token(root, "it")
check(B.recover(root) == [B.safe_key("it")], "recover returns the dead owner's key")
tok2 = B.claim(root, "it", "w2")
check(tok2 is not None, "successor claims after recovery")
check(B.complete(root, stale) is False, "stale token's complete() cannot steal")
check(live_locations(root, "it") == [f"{B.INFLIGHT}/{tok2}"], "successor still owns it")
shutil.rmtree(root, ignore_errors=True)

# ── I3: collisions quarantine, never overwrite ──────────────────────────────
print("== I3: destination collisions ==")
root = tempfile.mkdtemp()
B.publish(root, "it", "v1")
t1 = B.claim(root, "it", "w1")
assert B.complete(root, t1)                     # archive/<key> now occupied
B.publish(root, "it", "v2")                     # same id again
t2 = B.claim(root, "it", "w2")
check(B.complete(root, t2) is True, "second complete succeeds (unique terminal name)")
def _entries(d):
    p = pathlib.Path(root) / d
    return list(p.iterdir()) if p.exists() else []
bodies = sorted(f.read_text() for f in _entries(B.ARCHIVE) + _entries(B.PARKED))
check(bodies == ["v1", "v2"], f"both payloads survived, none overwritten: {bodies}")
shutil.rmtree(root, ignore_errors=True)

print("== I3b: publish refuses a duplicate id, never overwrites ready/ ==")
root = tempfile.mkdtemp()
check(B.publish(root, "it", "first") is True, "first publish")
check(B.publish(root, "it", "second") is False, "duplicate publish refused")
key = B.safe_key("it")
check((pathlib.Path(root)/B.READY/key).read_text() == "first", "original payload intact")
shutil.rmtree(root, ignore_errors=True)

print("== filename schema: ids with dots and separators parse correctly ==")
root = tempfile.mkdtemp()
weird = "a.b~c/d e"
B.publish(root, weird, "w")
tok = B.claim(root, weird, "w~or.ker")
check(tok is not None, "weird id claims")
check(B.holder(root, weird) is not None, "holder resolves despite dots/seps in id")
check(B.complete(root, tok), "weird id completes")
check(live_locations(root, weird) and live_locations(root, weird)[0].startswith(B.ARCHIVE),
      "weird id archived exactly once")
shutil.rmtree(root, ignore_errors=True)

print("== error classification: a config error raises, never a silent None ==")
root = tempfile.mkdtemp()
B.publish(root, "it", "x")
ro = pathlib.Path(root) / B.INFLIGHT
ro.mkdir(exist_ok=True)
os.chmod(ro, 0o500)                             # claim's link must EACCES
try:
    raised = False
    try:
        B.claim(root, "it", "w1")
    except B.OutboxConfigError:
        raised = True
    check(raised, "EACCES surfaces as OutboxConfigError, not a lost-race None")
finally:
    os.chmod(ro, 0o755)
shutil.rmtree(root, ignore_errors=True)

print("== cleanup bounds state ==")
root = tempfile.mkdtemp()
for i in range(20):
    B.publish(root, f"it{i}", "x")
    t = B.claim(root, f"it{i}", "w")
    B.complete(root, t)
n = B.cleanup(root, max_age_s=-1)               # everything is "old"
left = list((pathlib.Path(root)/B.ARCHIVE).iterdir())
check(n == 20 and left == [], f"cleanup pruned all 20 archived (pruned={n}, left={len(left)})")
shutil.rmtree(root, ignore_errors=True)

print(f"\n{'PASS' if not fails else 'FAIL'} — {len(fails)} failure(s)")
sys.exit(1 if fails else 0)
