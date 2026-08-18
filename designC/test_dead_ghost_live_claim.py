#!/usr/bin/env python3
"""Dead ghost + live claim is a LEGAL state (found by 001's 800x60 base arm).

publish -> stale dead-incarnation token still in inflight/ (recover not yet
run) -> claim consumes ready/. Two tokens, one dead: recover's live-holder leg
exists for exactly this. The invariant must count LIVE holders, not tokens.

Falsifier: two LIVE same-incarnation holders must still raise.
"""
import importlib.util
import os
import shutil
import sys
import tempfile

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(_HERE), "src"))
spec = importlib.util.spec_from_file_location("designC", os.path.join(_HERE, "designC.py"))
C = importlib.util.module_from_spec(spec)
sys.modules["designC"] = C
spec.loader.exec_module(C)
import outbox as ob  # noqa: E402

fails = []


def check(name, cond, detail=""):
    print(("  ok: " if cond else "  FAIL: ") + name + (f" {detail}" if not cond and detail else ""))
    if not cond:
        fails.append(name)


# --- the legal state: dead ghost beside a live claim ------------------------
root = tempfile.mkdtemp(prefix="ghost-")
try:
    C.init(root)
    check("publish", C.publish(root, "item-1", "body") is True)
    key = C.safe_key("item-1")
    ghost = C._d(root, C.INFLIGHT) / C.SEP.join((key, "ghost", "56044", "1", "0"))
    ghost.write_text("stale", encoding="utf-8")
    tok = C.claim(root, "item-1", "drainer-A")
    check("claim wins beside the dead ghost", bool(tok))
    try:
        st = C._state_of(root, key)
        check("state readable (no InvariantError)", True)
        check("live holder outranks the dead ghost", st.worker == "drainer-A", f"got {st.worker}")
    except C.InvariantError as e:
        check("state readable (no InvariantError)", False, str(e)[:100])
    # and recover still quarantines/handles the ghost rather than eating the claim
    moved = C.recover(root)
    check("recover leaves the live claim alone", C._state_of(root, key).worker == "drainer-A")
finally:
    shutil.rmtree(root, ignore_errors=True)

# --- falsifier: two LIVE holders must still raise ---------------------------
root = tempfile.mkdtemp(prefix="ghost-fal-")
try:
    C.init(root)
    key = C.safe_key("item-1")
    me, birth = os.getpid(), ob.process_identity(os.getpid()).start_usec
    for w in ("A", "B"):
        (C._d(root, C.INFLIGHT) / C.SEP.join((key, w, str(me), str(birth), "g" + w))).write_text("x")
    try:
        C._state_of(root, key)
        check("falsifier: 2 LIVE holders raise", False, "no raise")
    except C.InvariantError:
        check("falsifier: 2 LIVE holders raise", True)
finally:
    shutil.rmtree(root, ignore_errors=True)

print(("\nFAIL: " + "; ".join(fails)) if fails else "\nPASS — dead-ghost/live-claim legality (6 checks)")
sys.exit(1 if fails else 0)
