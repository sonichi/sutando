#!/usr/bin/env python3
"""The activation guard: a mutating call on an un-init()ed root must RAISE.

Lazy activation on the claim path is unsound and unfixably so -- `_stripe_mode`
negatively memoizes per process, so a thread that reads the fence first caches
"unstriped" and locks per-item files while a sibling locks stripes: no mutual
exclusion, silently, with every test still green. This pins the loud failure
that replaces it.

Falsifier: the same call after init() must SUCCEED. Without that arm the test
would pass against a module that raised unconditionally.
"""
import importlib.util
import os
import shutil
import sys
import tempfile

_HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location("designC", os.path.join(_HERE, "designC.py"))
C = importlib.util.module_from_spec(spec)
sys.modules["designC"] = C
spec.loader.exec_module(C)

fails = []
checks = []   # counted, never asserted: a hardcoded total goes stale silently

# --- every mutating entry point must refuse an uninitialized root -----------
for name, call in (
    ("publish",       lambda r: C.publish(r, "i", "b")),
    ("claim",         lambda r: C.claim(r, "i", "w")),
    ("complete",      lambda r: C.complete(r, C.SEP.join(("k", "w", "1", "1", "g")))),
    ("force_requeue", lambda r: C.force_requeue(r, "i")),
    # Both reached _item_lock only inside a loop, so an EMPTY root skipped the
    # guard entirely and they returned [] / 0 — a no-op that reads as success.
    ("recover",       lambda r: C.recover(r)),
    ("cleanup",       lambda r: C.cleanup(r, 0)),
):
    root = tempfile.mkdtemp(prefix="init-guard-")
    try:
        call(root)
        fails.append(f"{name}: silently accepted an uninitialized root")
    except C.NotInitialized:
        checks.append(name); print(f"  ok: {name} refuses an uninitialized root")
    except Exception as e:
        fails.append(f"{name}: wrong error {type(e).__name__}: {e}")
    finally:
        shutil.rmtree(root, ignore_errors=True)

# --- falsifier: after init() the same calls must WORK -----------------------
root = tempfile.mkdtemp(prefix="init-guard-ok-")
try:
    C.init(root)
    if C.publish(root, "i", "b") is not True:
        fails.append("control: publish failed after init() — the guard is unconditional")
    else:
        tok = C.claim(root, "i", "w")
        if not tok:
            fails.append("control: claim failed after init()")
        elif C.complete(root, tok) is not True:
            fails.append("control: complete failed after init()")
        else:
            checks.append("control"); print("  ok: control — publish/claim/complete all succeed after init()")
    C.init(root)  # idempotent
    checks.append("idempotent"); print("  ok: init() is idempotent")
finally:
    shutil.rmtree(root, ignore_errors=True)

print("\nFAIL: " + "; ".join(fails) if fails else f"\nPASS — activation guard ({len(checks)} checks: {', '.join(checks)})")
sys.exit(1 if fails else 0)
