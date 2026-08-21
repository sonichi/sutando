#!/usr/bin/env python3
"""The canonical-checkout guard must cover the STALE auto-restart path.

`fix_down_bridges()` has always guarded the DOWN path, but the stale path is
the one that actually kills and relaunches — and it booted whatever was checked
out. This pins the decision unit behaviourally (the restart glue in main() is
un-importable, so the decision is extracted rather than regex-asserted).
"""
import importlib.util
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def _load():
    spec = importlib.util.spec_from_file_location("hc", REPO / "src" / "health-check.py")
    mod = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)
    except SystemExit:
        pass
    return mod


def main() -> int:
    hc = _load()
    failures = []

    def check(cond, label):
        print(("PASS: " if cond else "FAIL: ") + label)
        if not cond:
            failures.append(label)

    # A non-canonical checkout must refuse the restart and carry the reason.
    ok, why = hc.stale_restart_allowed(
        REPO, guard=lambda _d: (False, "checkout on 'feat/x', not main"))
    check(ok is False, "non-canonical checkout refuses a stale auto-restart")
    check("feat/x" in why, "the refusal carries the guard's reason, not a generic string")

    # A canonical checkout must still allow it — a guard that never permits is
    # as useless as one that never refuses.
    ok2, why2 = hc.stale_restart_allowed(REPO, guard=lambda _d: (True, ""))
    check(ok2 is True, "canonical checkout still permits the stale restart")
    check(why2 == "", "no reason is reported when the restart is permitted")

    # The default guard must BE the down path's guard, not a private copy that
    # can drift. Compare against the real symbol rather than asserting on prose.
    import inspect
    src = inspect.getsource(hc.stale_restart_allowed)
    check("_checkout_is_canonical" in src,
          "defaults to the same _checkout_is_canonical the down path uses")
    sentinel = []
    hc.stale_restart_allowed(REPO, guard=lambda d: (sentinel.append(d), (True, ""))[1])
    check(sentinel == [REPO],
          "the guard is called with the repo dir it was handed, not a hardcoded path")

    print(f"\n{'FAILED' if failures else 'OK'} — {len(failures)} failure(s)")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
