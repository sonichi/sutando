#!/usr/bin/env python3
"""The emit-lateness budget must scale with a job's own period.

A FLAT budget spends half a period on a 30-minute job and 1/96th of one on a
daily job, so the same delay costs the daily job an entire day. Measured on a
live host: `money-autopilot-scan` (43 8 * * *) was dropped at 2485s and 1988s
late on consecutive days while its sub-hourly peers recovered immediately.

Run: python3 tests/cron-lateness-scales-with-period.test.py
"""
import importlib.util
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
ok(f"cache actually serves the scan ({_info.hits} hits)", _info.hits > 1000)
# Generous bound: measured ~0.03s locally; 2s still catches an O(n) parse regression,
# which costs seconds per call, without flaking on a loaded CI box.
ok(f"4 worst-case period scans stay bounded ({_elapsed:.3f}s)", _elapsed < 2.0)

# Control: the parse-count assertion must be able to FAIL. Bypassing the cache
# restores the O(minutes) behaviour the assertion exists to forbid.
_uncached = cr._parse_field.__wrapped__
_misses_uncached = 0
_orig_pf = cr._parse_field
try:
    def _counting(field, lo, hi):
        global _misses_uncached
        _misses_uncached += 1
        return _uncached(field, lo, hi)
    cr._parse_field = _counting
    cr.cron_period_seconds("7 16 * * 5", NOW)
finally:
    cr._parse_field = _orig_pf
ok(f"control: uncached parsing IS O(minutes) ({_misses_uncached} calls)",
   _misses_uncached > 1000)

if FAILS:
    print(f"cron-lateness: {FAILS} failure(s)")
    sys.exit(1)
print("cron-lateness controls OK: floor held, cap held, the measured drops now run")
