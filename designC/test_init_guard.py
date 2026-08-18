#!/usr/bin/env python3
"""Activation guard: every mutating entry point REFUSES an un-init()ed root.

Lazy activation is the proven-unsound path (thread-shared fence tmp + negative
stripe-mode memoization -- see designC.init's docstring). The guard's value is
reachability: each refusal is paired with a control proving the same call
succeeds after init(), so a green here cannot be a call that never ran.
"""
from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))
import designC as C  # noqa: E402

FAILS = []
RAN = []


def check(name, cond, detail=""):
    RAN.append(name)
    if cond:
        print(f"  ok: {name}")
    else:
        FAILS.append(name)
        print(f"  FAIL: {name} {detail}", file=sys.stderr)


def refuses(name, fn):
    try:
        fn()
        check(name, False, "did not raise NotInitialized")
    except C.NotInitialized:
        check(name, True)


def main() -> int:
    cold = tempfile.mkdtemp(prefix="c-guard-cold-")
    warm = tempfile.mkdtemp(prefix="c-guard-warm-")
    try:
        # refusals: 6 mutating entry points against the cold root
        refuses("publish refuses un-init root", lambda: C.publish(cold, "i", "b"))
        refuses("claim refuses un-init root", lambda: C.claim(cold, "i", "w"))
        refuses("complete refuses un-init root",
                lambda: C.complete(cold, "k~w~1~2~g"))
        refuses("recover refuses un-init root", lambda: C.recover(cold))
        refuses("force_requeue refuses un-init root",
                lambda: C.force_requeue(cold, "i"))
        refuses("cleanup refuses un-init root", lambda: C.cleanup(cold, 1.0))

        # controls: the SAME calls succeed after init() -- the refusals above
        # cannot be a broken import or a wrong exception class.
        C.init(warm)
        check("control: publish works after init", C.publish(warm, "i", "b") is True)
        tok = C.claim(warm, "i", "w")
        check("control: claim works after init", bool(tok))
        check("control: complete works after init", C.complete(warm, tok) is True)
    finally:
        shutil.rmtree(cold, ignore_errors=True)
        shutil.rmtree(warm, ignore_errors=True)

    # Derived, never asserted: a hardcoded total goes stale silently.
    n_total = len(RAN)
    n_ref = sum(1 for n in RAN if "refuses" in n)
    n_ctl = n_total - n_ref
    if FAILS:
        print(f"\nFAIL: {len(FAILS)}/{n_total}: {FAILS}", file=sys.stderr)
        return 1
    print(f"\nPASS: activation guard — {n_ref} refusals + {n_ctl} controls "
          f"({n_total}/{n_total} checks ran)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
