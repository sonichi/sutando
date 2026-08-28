#!/usr/bin/env python3
"""The emit-lateness budget must scale with a job's own period.

A FLAT budget spends half a period on a 30-minute job and 1/96th of one on a
daily job, so the same delay costs the daily job an entire day. Measured on a
live host: `money-autopilot-scan` (43 8 * * *) was dropped at 2485s and 1988s
late on consecutive days while its sub-hourly peers recovered immediately.

Run: python3 tests/cron-lateness-scales-with-period.test.py
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


NOW = int(time.time())
b = lambda e: cr.emit_lateness_budget(e, NOW)

# No job may become STRICTER than the shipped constant.
for expr in ("*/5 * * * *", "*/30 * * * *", "0 * * * *", "43 8 * * *",
             "7 16 * * 5", "23 9 7 8 *"):
    ok(f"never below the old floor: {expr}", b(expr) >= cr.MAX_EMIT_LATENESS_SECONDS)

# Sub-hourly jobs keep EXACTLY the old budget — this change is not a loosening
# for the jobs the constant was tuned for.
ok("every 5 min unchanged", b("*/5 * * * *") == cr.MAX_EMIT_LATENESS_SECONDS)
ok("every 30 min unchanged", b("*/30 * * * *") == cr.MAX_EMIT_LATENESS_SECONDS)
ok("hourly unchanged", b("0 * * * *") == cr.MAX_EMIT_LATENESS_SECONDS)

# Low-frequency jobs get more, bounded by the cap so none runs "hours late".
ok("daily gets more than the floor", b("43 8 * * *") > cr.MAX_EMIT_LATENESS_SECONDS)
ok("weekly gets more than the floor", b("7 16 * * 5") > cr.MAX_EMIT_LATENESS_SECONDS)
for expr in ("43 8 * * *", "7 16 * * 5", "4 6 */3 * *"):
    ok(f"never above the cap: {expr}", b(expr) <= cr.MAX_EMIT_LATENESS_CAP_SECONDS)

# The live regression: both real drops fall inside the new daily budget.
ok("2485s-late daily slot would now run", 2485 <= b("43 8 * * *"))
ok("1988s-late daily slot would now run", 1988 <= b("43 8 * * *"))
# ...and the control: something genuinely stale still does NOT.
ok("a 6h-late daily slot is still dropped", 6 * 3600 > b("43 8 * * *"))

# Period detection: measured from the expression, not declared.
ok("daily period ~24h", cr.cron_period_seconds("43 8 * * *", NOW) == 24 * 3600)
ok("30-min period", cr.cron_period_seconds("*/30 * * * *", NOW) == 1800)
ok("weekly period ~7d", cr.cron_period_seconds("7 16 * * 5", NOW) == 7 * 24 * 3600)
# Unmeasurable period must fail SAFE (old constant), not open.
ok("unmeasurable period falls back to the floor",
   cr.cron_period_seconds("23 9 7 8 *", NOW) is None
   and b("23 9 7 8 *") == cr.MAX_EMIT_LATENESS_SECONDS)


# --- Bounded runtime + call-count controls ---

# The scan is O(minutes) by design; field parsing must not be. Count parses
# so slow hardware cannot flake and fast hardware cannot hide a regression.
_t0 = time.perf_counter()
cr._parse_field.cache_clear()
for _e in ("43 8 * * *", "7 16 * * 5", "0 10 * * 1", "23 9 7 8 *"):
    cr.cron_period_seconds(_e, NOW)
_info = cr._parse_field.cache_info()
_elapsed = time.perf_counter() - _t0

# 4 exprs x 5 fields = at most 20 distinct parses, however many minutes are scanned.
ok(f"parse count bounded by fields, not minutes ({_info.misses} misses)",
   _info.misses <= 20)
ok(f"the cache still serves the scan ({_info.hits} hits / {_info.misses} misses)",
   _info.hits > _info.misses)
# Wall clock is NOT the bound: libc time conversion costs 0.25us on one host and
# ~400us on another, so a seconds threshold flakes by machine, not by regression.
_lt, _mk = time.localtime, time.mktime
_calls = []


def _counted_localtime(*a):
    _calls.append(1)
    return _lt(*a)


def _counted_mktime(*a):
    _calls.append(1)
    return _mk(*a)


cr.time.localtime, cr.time.mktime = _counted_localtime, _counted_mktime
try:
    cr._parse_field.cache_clear()
    for _e in ("43 8 * * *", "7 16 * * 5", "0 10 * * 1", "23 9 7 8 *"):
        cr.cron_period_seconds(_e, NOW)
finally:
    cr.time.localtime, cr.time.mktime = _lt, _mk

# Day-stepping: ~1 date test per day over the window plus a few candidates.
# The minute-stepping scan this replaced needed 55,465 for the same four.
ok(f"period scan is O(days), not O(minutes) ({len(_calls)} libc time calls)",
   len(_calls) < 1000)
ok(f"4 worst-case period scans stay bounded ({_elapsed:.3f}s)", _elapsed < 60.0)

# Control: the call-count bound must track DAYS. Doubling the window doubles a
# day-stepping scan and multiplies a minute-stepping one by 1440x per day.
def _time_calls(window_s):
    calls = []
    lt, mk = time.localtime, time.mktime
    cr.time.localtime = lambda *a: (calls.append(1), lt(*a))[1]
    cr.time.mktime = lambda *a: (calls.append(1), mk(*a))[1]
    prev = cr.PERIOD_SCAN_MAX_SECONDS
    cr.PERIOD_SCAN_MAX_SECONDS = window_s
    try:
        cr.cron_period_seconds("23 9 7 8 *", NOW)
    finally:
        cr.PERIOD_SCAN_MAX_SECONDS = prev
        cr.time.localtime, cr.time.mktime = lt, mk
    return len(calls)


_c15 = _time_calls(15 * 24 * 3600)
_c30 = _time_calls(30 * 24 * 3600)
ok(f"control: doubling the window ~doubles the calls ({_c15} -> {_c30})",
   _c30 < _c15 * 4)
ok(f"control: the counter can move at all ({_c30} > {_c15})", _c30 > _c15)


# --- the three guard branches of the day-walk scan ---

# A malformed expression must RAISE, not silently score as unmeasurable: a
# 4-field entry is a config error, and None would quietly grant the floor budget.
try:
    cr.cron_period_seconds("0 9 * *", NOW)
    ok("malformed expression raises", False)
except ValueError as _e:
    ok(f"malformed expression raises ({str(_e)[:34]}...)", "5 fields" in str(_e))

# An inverted range expands to the EMPTY set, so the expression can never fire.
ok("empty field set -> unmeasurable, not a crash",
   cr.cron_period_seconds("5-3 9 * * *", NOW) is None)
ok("control: the inverted range really is empty", not cr._parse_field("5-3", 0, 59))

# Second fire outside the window -> None (the floor early-return), with the full
# window as the control so this proves the floor path, not a broken expression.
_prev = cr.PERIOD_SCAN_MAX_SECONDS
cr.PERIOD_SCAN_MAX_SECONDS = 72000
try:
    _short = cr.cron_period_seconds("43 8 * * *", NOW)
finally:
    cr.PERIOD_SCAN_MAX_SECONDS = _prev
_full = cr.cron_period_seconds("43 8 * * *", NOW)
ok(f"second fire below the floor -> None (got {_short})", _short is None)
ok(f"control: same expression measures {_full}s on the full window", _full == 86400)

# --- DST transition equivalence (the optimized scan vs cron_matches) ---

# The day walk reconstructs epochs from local wall-clock, so a repeated hour
# (fall-back) has TWO real epochs per minute and a skipped hour (spring) none.
_tz_prev = os.environ.get("TZ")
os.environ["TZ"] = "America/Los_Angeles"
time.tzset()
try:
    def _ref(expr, now, horizon=3 * 86400):
        """Two most recent fires by walking real epochs, the slow way."""
        seen = []
        for back in range(0, horizon, 60):
            e = now - back
            if cr.cron_matches(expr, time.localtime(e)):
                seen.append(e)
            if len(seen) == 2:
                return seen[0] - seen[1]
        return None

    _fb = int(time.mktime((2025, 11, 2, 3, 0, 0, 0, 0, -1)))
    # isdst=0 pins the SECOND pass through the repeated hour; a `now` after the
    # transition never scans the ambiguous minutes and cannot see their ordering.
    _fb_in = int(time.mktime((2025, 11, 2, 1, 30, 0, 0, 0, 0)))
    _sf = int(time.mktime((2025, 3, 9, 5, 0, 0, 0, 0, -1)))
    for _label, _now in (("fall-back", _fb), ("inside repeated hour", _fb_in),
                         ("spring-forward", _sf)):
        for _e in ("30 1 * * *", "*/15 * * * *", "45 1,2 * * *"):
            _got, _want = cr.cron_period_seconds(_e, _now), _ref(_e, _now)
            ok(f"{_label} {_e!r}: scan={_got} matches={_want}", _got == _want)

    # The bug this guards: isdst=-1 collapsed the repeated 01:30 to one epoch,
    # returning the 25-hour gap to the previous day instead of the real hour.
    ok("fall-back 01:30 measures one hour, not the 25-hour day",
       cr.cron_period_seconds("30 1 * * *", _fb) == 3600)
    ok("control: an ambiguous local minute really has two epochs",
       len(cr._local_epochs(2025, 11, 2, 1, 30)) == 2)
    ok("control: a skipped local minute has none",
       cr._local_epochs(2025, 3, 9, 2, 30) == [])
    ok("control: an ordinary local minute has exactly one",
       len(cr._local_epochs(2025, 6, 1, 1, 30)) == 1)

    # Asia/Kathmandu really raises OverflowError on the isdst=1 probe, but only
    # on some libc; substituted so the guard is exercised on every platform.
    os.environ["TZ"] = "Asia/Kathmandu"
    time.tzset()
    _real_mktime = cr.time.mktime
    _probes = []

    def _unrepresentable_dst(t):
        _probes.append(t[8])
        if t[8] == 1:
            raise OverflowError("mktime argument out of range")
        return _real_mktime(t)

    cr.time.mktime = _unrepresentable_dst
    _crash = None
    try:
        _kt = cr._local_epochs(2026, 8, 28, 10, 30)
    except Exception as exc:
        _kt, _crash = None, f"{type(exc).__name__}: {exc}"
    finally:
        cr.time.mktime = _real_mktime
    ok("an unrepresentable isdst probe does not escape _local_epochs",
       _crash is None)
    ok("and that minute still resolves to its one real epoch",
       _kt is not None and len(_kt) == 1)
    ok("control: the raising probe was actually reached (both isdst tried)",
       _probes == [0, 1])
finally:
    if _tz_prev is None:
        os.environ.pop("TZ", None)
    else:
        os.environ["TZ"] = _tz_prev
    time.tzset()


if FAILS:
    print(f"cron-lateness: {FAILS} failure(s)")
    sys.exit(1)
print("cron-lateness controls OK: floor held, cap held, the measured drops now run")
