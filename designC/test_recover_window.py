#!/usr/bin/env python3
"""Deterministic multi-step-transition schedules for Design C (no search).

Two schedules (RW_SCHEDULE=recover|publish), each run LOCKED and NEUTERED
(RW_NEUTER=1 makes flock a no-op), with instrument liveness asserted in every
arm: the run fails loudly if the paused thread never reaches its window.

SCHEDULE recover — pause recover() between its live-holder check and its
_move link; drive publish + claim at the window.
  LOCKED: window closed (publish/claim block on the item lock).
  NEUTER: window OPEN but HARMLESS BY CONSTRUCTION — publish's inflight probe
  sees the dead ghost (still linked until _move's unlink) and refuses, so no
  ready copy can appear inside the window; claim has nothing to eat. End
  state legal in both arms. The flock is redundant on this leg, and the
  schedule proves WHY, not merely that.

SCHEDULE publish — pause publish() between its inflight probe and its
create-if-absent link (the docstring's own claim: "the probe is not a
compare-then-act: no claim can intervene between them"); drive claim at the
window against an already-published item.
  LOCKED: claim blocks; link sees ready still present -> EEXIST -> publish
  correctly returns False. Legal.
  NEUTER: claim consumes ready inside the window; publish's link then
  re-arms ready beside the live believer and returns True — the CE-1
  believer+ready-copy precursor, i.e. a re-publish of an item mid-delivery.
  THIS is the flock's unique load-bearing surface in Design C."""
import importlib.util
import os as real_os
import sys
import tempfile
import threading
from pathlib import Path

SCHEDULE = real_os.environ.get("RW_SCHEDULE", "recover")
NEUTER = real_os.environ.get("RW_NEUTER") == "1"
DEAD_PID = 99999

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))
spec = importlib.util.spec_from_file_location("designB", str(_HERE / "designC.py"))
C = importlib.util.module_from_spec(spec)
sys.modules["designB"] = C
spec.loader.exec_module(C)

try:
    real_os.kill(DEAD_PID, 0)
    sys.exit(f"DEAD_PID {DEAD_PID} is alive on this host; pick another")
except ProcessLookupError:
    pass

if NEUTER:
    import fcntl as _real_fcntl

    class _NoFlock:
        def __getattr__(self, name):
            return getattr(_real_fcntl, name)
        def flock(self, fd, op):
            return None
    C.ob.fcntl = _NoFlock()

PAUSE_THREAD = "recover-thread" if SCHEDULE == "recover" else "publish-thread"
at_window = threading.Event()
release = threading.Event()
_orig_os = C.os

class _PauseLinkOs:
    """Pause exactly the target thread's link call (probe/holder-check done)."""
    def __getattr__(self, name):
        val = getattr(_orig_os, name)
        if name != "link":
            return val
        def link(src, dst, **kw):
            if threading.current_thread().name == PAUSE_THREAD:
                at_window.set()
                if not release.wait(timeout=20):
                    raise RuntimeError("release never came")
            return val(src, dst, **kw)
        return link
C.os = _PauseLinkOs()

root = tempfile.mkdtemp()
getattr(C, "init", lambda _r: None)(root)   # constructor-time activation (unified module)
for d in (C.READY, C.INFLIGHT, C.ARCHIVE, C.PARKED, C.TMP):
    C._d(root, d).mkdir(parents=True, exist_ok=True)
C.ob.activate_lock_striping(Path(root))

ITEM = "item-1"
key = C.safe_key(ITEM)

paused_result = {}
if SCHEDULE == "recover":
    ghost = C._d(root, C.INFLIGHT) / C.SEP.join((key, "ghost", str(DEAD_PID), "1", "0"))
    ghost.write_text('{"body": "payload"}', encoding="utf-8")

    def _paused():
        paused_result["out"] = C.recover(root)
else:
    assert C.publish(root, ITEM, '{"body": "v1"}') is True, "setup publish failed"

    def _paused():
        paused_result["out"] = C.publish(root, ITEM, '{"body": "v2"}')

P = threading.Thread(target=_paused, name=PAUSE_THREAD, daemon=True)
P.start()
assert at_window.wait(timeout=10), \
    f"INSTRUMENT DEAD: {PAUSE_THREAD} never reached its link — schedule measures nothing"

side = {"published": None, "token": None, "done": threading.Event()}
def _side():
    if SCHEDULE == "recover":
        side["published"] = C.publish(root, ITEM, '{"body": "fresh"}')
    side["token"] = C.claim(root, ITEM, "A")
    side["done"].set()

S = threading.Thread(target=_side, name="side", daemon=True)
S.start()

entered = side["done"].wait(timeout=2.0)
if NEUTER:
    assert entered, "neutered flock should let the side thread through the window"
else:
    assert not entered, "LOCKED arm: side thread entered the window despite the item lock"

release.set()
P.join(timeout=20)
assert not P.is_alive(), f"{PAUSE_THREAD} wedged after release"
assert side["done"].wait(timeout=10), "side thread never finished after release"

ready = C._d(root, C.READY) / key
hold = C.holder(root, ITEM)
violation = hold is not None and ready.exists()
print(f"schedule={SCHEDULE} arm={'NEUTER' if NEUTER else 'LOCKED'} "
      f"paused_out={paused_result.get('out')!r} side_published={side['published']} "
      f"side_token={bool(side['token'])} holder={hold!r} "
      f"ready_exists={ready.exists()} violation={violation}")

if SCHEDULE == "recover":
    # Both arms legal; the neutered window is harmless by construction —
    # the ghost's own inflight presence blocks re-publish until the move ends.
    assert not violation, "recover window produced the CE-1 precursor — new finding, investigate"
    if NEUTER:
        assert side["published"] is False and side["token"] is None, \
            "publish/claim entered the recover window — ghost no longer blocks re-entry?"
        print("RECOVER/NEUTER: window open but harmless — ghost blocks publish, claim starves")
    else:
        print("RECOVER/LOCKED: window closed by the item lock; end state legal")
else:
    if NEUTER:
        assert violation and paused_result.get("out") is True, \
            "expected re-publish-beside-believer with the flock neutered"
        print("PUBLISH/NEUTER: publish returned True beside a live believer — "
              "believer + ready copy COEXIST; flock's unique surface demonstrated")
    else:
        assert not violation and paused_result.get("out") is False, \
            "LOCKED publish arm: expected EEXIST refusal and a legal end state"
        print("PUBLISH/LOCKED: probe-to-link window closed; publish correctly refused")
