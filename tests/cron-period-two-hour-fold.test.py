#!/usr/bin/env python3
"""The period scan must stay correct across a rollback wider than one hour.

The per-hour batching assumed a fold reorders epochs only inside its own hour,
so day zero was filtered by wall-clock hour. `Antarctica/Troll` rolls back TWO
hours (+0200 -> +0000), which moves an already-elapsed 02:30 occurrence into a
wall-clock hour the filter had discarded. The one-hour `America/Los_Angeles`
controls cannot catch this: they are drawn from the same assumption.

Run: python3 tests/cron-period-two-hour-fold.test.py
"""
import importlib.util
import os
import pathlib
import sys
import time

_SRC = pathlib.Path(__file__).resolve().parents[1] / "src" / "cron-runner.py"
_spec = importlib.util.spec_from_file_location("cron_runner", _SRC)
cr = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cr)

FAILS = 0


def ok(name, cond):
    global FAILS
    if cond:
        print(f"  ok   {name}")
    else:
        FAILS += 1
        print(f"  FAIL {name}")


def reference_period(expr, now_epoch, window=3 * 86400):
    """Brute force every minute backwards, matched in local time.

    Independent of the scan under test: no hour pre-filter, no early exit.
    """
    mi_f, h_f, dom_f, mon_f, dow_f = expr.split()
    mins = cr._parse_field(mi_f, 0, 59)
    hrs = cr._parse_field(h_f, 0, 23)
    doms = cr._parse_field(dom_f, 1, 31)
    mons = cr._parse_field(mon_f, 1, 12)
    dows = cr._parse_field(dow_f, 0, 6)
    fires = []
    e = now_epoch - (now_epoch % 60)
    for _ in range(window // 60):
        lt = time.localtime(e)
        if (lt.tm_min in mins and lt.tm_hour in hrs and lt.tm_mday in doms
                and lt.tm_mon in mons and (lt.tm_wday + 1) % 7 in dows):
            fires.append(e)
            if len(fires) == 2:
                return fires[0] - fires[1]
        e -= 60
    return None


EXPR = "30 0,2 * * *"          # non-uniform: dropping a fire CHANGES the period
FOLD_NOW = 1761442200          # 2025-10-26 01:30 +0000, after Troll's 2h rollback
ORDINARY_NOW = 1763170200      # 2025-11-15 01:30 +0000, same zone, no transition

_tz_prev = os.environ.get("TZ")
try:
    os.environ["TZ"] = "Antarctica/Troll"
    time.tzset()

    # The zone really does roll back two hours here; without this the case is
    # not the one the docstring claims.
    ok("control: the transition is wider than one hour",
       time.localtime(FOLD_NOW).tm_gmtoff
       - time.localtime(FOLD_NOW - 4 * 3600).tm_gmtoff == -7200)

    ref_fold = reference_period(EXPR, FOLD_NOW)
    got_fold = cr.cron_period_seconds(EXPR, FOLD_NOW)
    ok(f"two-hour fold: scan equals brute force ({ref_fold})", ref_fold == got_fold)

    # Discriminating: the pre-fix scan returned 79200 here, not 7200. If this
    # ever passes trivially the case has stopped exercising the defect.
    ok("the fold case is one the defect actually changed", ref_fold == 7200)
    ok("control: an ordinary day in the same zone still agrees",
       reference_period(EXPR, ORDINARY_NOW)
       == cr.cron_period_seconds(EXPR, ORDINARY_NOW))

    # A uniform schedule CANNOT discriminate: dropping one fire leaves the
    # period unchanged, so it would pass against the defect.
    ok("control: a uniform schedule is not discriminating here",
       reference_period("30 */2 * * *", FOLD_NOW) == 7200)

    # The dense-schedule call bound survives away from a transition.
    calls = [0]
    _real = cr._local_epochs
    cr._local_epochs = lambda *a, **k: (calls.__setitem__(0, calls[0] + 1),
                                        _real(*a, **k))[1]
    try:
        os.environ["TZ"] = "America/Los_Angeles"
        time.tzset()
        cr.cron_period_seconds("* * * * *", int(time.mktime((2025, 11, 15, 13, 30, 0, 0, 0, -1))))
    finally:
        cr._local_epochs = _real
    ok(f"dense schedule off-transition stays one hour of expansion ({calls[0]})",
       calls[0] <= 60)
finally:
    if _tz_prev is None:
        os.environ.pop("TZ", None)
    else:
        os.environ["TZ"] = _tz_prev
    time.tzset()

if FAILS:
    print(f"cron-period-two-hour-fold: {FAILS} failure(s)")
    sys.exit(1)
print("two-hour-fold controls OK: scan matches brute force, call bound retained")
