#!/usr/bin/env python3
"""The period scan must survive a NON-DST rollback and one that crosses midnight.

Two assumptions failed together. `_local_epochs` probed `tm_isdst` 0 and 1 to
enumerate a wall minute's real epochs, but `Antarctica/Casey` rolls back three
hours reporting `tm_isdst=0` on BOTH sides, so the pre-shift occurrence was
never produced. Separately the day walk started at today's local date and only
went backward, so Casey's 2010 rollback across midnight — which leaves ELAPSED
epochs on a lexically LATER date — hid a fire the scan needed. Each defect alone
inflates the measured period, and an inflated period buys a bigger lateness
budget, so a stale slot runs instead of being dropped.

Fixtures. Casey 2023-03-09: 02:59 +1100 -> 00:00 +0800, tm_isdst=0 on both
sides, `now` 01:48 +0800 with the latest slot 1080s late. Casey 2010-03-05:
01:59 +1100 -> 2010-03-04 23:00 +0800 (the date goes BACKWARD), `now` 23:50
+0800 with the latest slot 1200s late. Ordinary: 2024-06-15 11:06 +0800.

Run: python3 tests/cron-period-non-dst-rollback.test.py
"""
import importlib.util
import json
import os
import pathlib
import sys
import tempfile
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

    Independent of the scan under test: no day walk, no offset enumeration,
    no hour pre-filter, no early exit.
    """
    fires = []
    e = now_epoch - (now_epoch % 60)
    for _ in range(window // 60):
        if cr.cron_matches(expr, time.localtime(e)):
            fires.append(e)
            if len(fires) == 2:
                return fires[0] - fires[1]
        e -= 60
    return None


def emitted_for(expr, due_epoch, now_epoch):
    """Names `run()` emits for a single entry, with only emit_task stubbed."""
    names = []
    with tempfile.TemporaryDirectory() as td:
        crons = pathlib.Path(td) / "crons.json"
        state = pathlib.Path(td) / "state.json"
        crons.write_text(json.dumps(
            [{"name": "fold-job", "cron": expr, "launchd": True,
              "prompt": "x"}]))
        state.write_text(json.dumps({"fold-job": due_epoch - 60}))
        _c, _s = cr.CRONS_FILE, cr.STATE_FILE
        _emit, _alive = cr.emit_task, cr.local_core_alive
        cr.CRONS_FILE, cr.STATE_FILE = crons, state
        cr.emit_task = lambda name, entry: pathlib.Path(td) / name
        cr.local_core_alive = lambda *a, **k: True
        try:
            names = cr.run(now_epoch)
        finally:
            cr.CRONS_FILE, cr.STATE_FILE = _c, _s
            cr.emit_task, cr.local_core_alive = _emit, _alive
    return names


SAME_DATE_NOW = 1678297680
SAME_DATE_EXPR = "30 1,2 * * *"
CROSS_DATE_NOW = 1267717800
CROSS_DATE_EXPR = "30 1,2,23 * * *"
CROSS_DATE_TRANSITION = 1267714800
ORDINARY_NOW = 1718420800
ORDINARY_EXPR = "*/15 * * * *"


_tz_prev = os.environ.get("TZ")
try:
    os.environ["TZ"] = "Antarctica/Casey"
    time.tzset()

    # Without this the case is not the one the docstring claims: a DST-polarity
    # probe would have found the pre-shift epoch if isdst actually differed.
    _before = time.localtime(SAME_DATE_NOW - 4 * 3600)
    _after = time.localtime(SAME_DATE_NOW)
    ok("control: the 2023 rollback is three hours wide",
       _after.tm_gmtoff - _before.tm_gmtoff == -10800)
    ok("control: and it is NOT a DST change (isdst=0 on both sides)",
       (_before.tm_isdst, _after.tm_isdst) == (0, 0))

    _ref = reference_period(SAME_DATE_EXPR, SAME_DATE_NOW)
    _got = cr.cron_period_seconds(SAME_DATE_EXPR, SAME_DATE_NOW)
    ok(f"non-DST rollback: scan equals brute force ({_ref})", _ref == _got)
    # Discriminating: the pre-fix scan returned 93600 here, not 7200.
    ok("the non-DST case is one the defect actually changed", _ref == 7200)

    # The lateness DECISION, not just the number: this slot is inside the
    # inflated 3600s budget and outside the real 900s one.
    _due = cr.latest_due_since(SAME_DATE_EXPR, SAME_DATE_NOW - 86400, SAME_DATE_NOW)
    ok(f"control: the slot really is 1080s late ({SAME_DATE_NOW - _due}s)",
       SAME_DATE_NOW - _due == 1080)
    ok("run() drops the stale slot at the true two-hour period",
       emitted_for(SAME_DATE_EXPR, _due, SAME_DATE_NOW) == [])

    # A rollback across midnight leaves elapsed epochs on a LATER local date.
    ok("control: the 2010 rollback really crosses the local date boundary",
       time.localtime(CROSS_DATE_TRANSITION - 60).tm_mday == 5
       and time.localtime(CROSS_DATE_TRANSITION).tm_mday == 4)
    _refx = reference_period(CROSS_DATE_EXPR, CROSS_DATE_NOW)
    _gotx = cr.cron_period_seconds(CROSS_DATE_EXPR, CROSS_DATE_NOW)
    ok(f"cross-date rollback: scan equals brute force ({_refx})", _refx == _gotx)
    # Discriminating: the pre-fix scan returned 75600, and a brute-force
    # _local_epochs alone still did — the day walk never visited Mar 5 at all.
    ok("the cross-date case is one the defect actually changed", _refx == 3600)
    _duex = cr.latest_due_since(CROSS_DATE_EXPR, CROSS_DATE_NOW - 86400,
                                CROSS_DATE_NOW)
    ok(f"control: that slot really is 1200s late ({CROSS_DATE_NOW - _duex}s)",
       CROSS_DATE_NOW - _duex == 1200)
    ok("run() drops the stale slot across the date rollback",
       emitted_for(CROSS_DATE_EXPR, _duex, CROSS_DATE_NOW) == [])

    # Controls: an ordinary day in the same zone is unaffected, and a slot
    # inside the budget still runs — so the two above are not just "drop all".
    ok("control: an ordinary day in the same zone still agrees",
       reference_period(ORDINARY_EXPR, ORDINARY_NOW)
       == cr.cron_period_seconds(ORDINARY_EXPR, ORDINARY_NOW))
    _dueo = cr.latest_due_since(ORDINARY_EXPR, ORDINARY_NOW - 86400, ORDINARY_NOW)
    ok(f"control: a slot inside budget on an ordinary day still runs "
       f"({ORDINARY_NOW - _dueo}s late)",
       emitted_for(ORDINARY_EXPR, _dueo, ORDINARY_NOW) == ["fold-job"])

    # The offset enumeration is hoisted per day, so the dense-schedule call
    # bound the optimized scan promised still holds.
    calls = [0]
    _real = cr._local_epochs
    cr._local_epochs = lambda *a, **k: (calls.__setitem__(0, calls[0] + 1),
                                        _real(*a, **k))[1]
    try:
        cr.cron_period_seconds("* * * * *", ORDINARY_NOW)
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
    print(f"cron-period-non-dst-rollback: {FAILS} failure(s)")
    sys.exit(1)
print("cron-period-non-dst-rollback OK: both rollbacks measured, stale slots dropped")
