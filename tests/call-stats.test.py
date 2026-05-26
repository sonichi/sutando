#!/usr/bin/env python3
"""Behavioral tests for `src/call-stats.py` utility functions.

Tests the pure/near-pure functions that process call data. No live
calls.jsonl file needed — call lists are constructed inline as fixtures.

Functions under test:
  parse_ts(s)                   — ISO timestamp → datetime | None
  mask_phone(num)               — phone number masking
  filter_by_window(calls, days) — date-range filter
  compute_stats(calls)          — aggregate stats dict
  format_text(stats, label)     — human-readable summary string

Run: python3 tests/call-stats.test.py
Exit: 0 on pass, non-zero on fail.
"""

from __future__ import annotations

import importlib.util
import os
import sys
import tempfile
from datetime import datetime, timezone, timedelta
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
_SRC = REPO / "src"

# -----------------------------------------------------------------------
# Module loader — module resolves CALLS_FILE at import time; redirect it
# to a non-existent temp path so load_calls() is a no-op in these tests.
# -----------------------------------------------------------------------
_WS = tempfile.mkdtemp(prefix="sutando-test-callstats-")
os.environ["SUTANDO_WORKSPACE"] = _WS
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

for _n in ("call_stats", "call-stats", "workspace_default", "util_paths"):
    sys.modules.pop(_n, None)

_spec = importlib.util.spec_from_file_location("call_stats", _SRC / "call-stats.py")
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

parse_ts = _mod.parse_ts
mask_phone = _mod.mask_phone
filter_by_window = _mod.filter_by_window
compute_stats = _mod.compute_stats
format_text = _mod.format_text

# -----------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------

PASS = 0
FAIL = 0


def ok(label: str) -> None:
    global PASS
    PASS += 1
    print(f"PASS  {label}")


def fail(label: str, detail: str = "") -> None:
    global FAIL
    FAIL += 1
    print(f"FAIL  {label}" + (f": {detail}" if detail else ""))


def _call(start_time=None, duration=None, caller=None, purpose=None, is_meeting=False, is_owner=False):
    """Build a minimal call-entry dict."""
    c: dict = {}
    if start_time:
        c["start_time"] = start_time
    if duration is not None:
        c["duration_seconds"] = duration
    if caller:
        c["caller"] = caller
    if purpose:
        c["purpose"] = purpose
    c["is_meeting"] = is_meeting
    c["is_owner"] = is_owner
    return c


def _now_iso(offset_days: int = 0) -> str:
    dt = datetime.now(timezone.utc) + timedelta(days=offset_days)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


# -----------------------------------------------------------------------
# parse_ts
# -----------------------------------------------------------------------

# T1 — ISO with Z suffix → aware datetime
r = parse_ts("2026-05-26T10:00:00Z")
if r is not None and r.year == 2026 and r.month == 5 and r.day == 26:
    ok("T1: ISO with Z suffix → datetime(2026-05-26 10:00:00 UTC)")
else:
    fail("T1: parse_ts Z suffix", f"got {r}")

# T2 — ISO with +00:00 offset → aware datetime
r = parse_ts("2026-05-26T10:00:00+00:00")
if r is not None and r.year == 2026 and r.hour == 10:
    ok("T2: ISO with +00:00 offset → datetime")
else:
    fail("T2: parse_ts +00:00 offset", f"got {r}")

# T3 — None → None
r = parse_ts(None)
if r is None:
    ok("T3: parse_ts(None) → None")
else:
    fail("T3: parse_ts None", f"got {r!r}")

# T4 — empty string → None
r = parse_ts("")
if r is None:
    ok("T4: parse_ts('') → None")
else:
    fail("T4: parse_ts empty string", f"got {r!r}")

# T5 — malformed string → None (no crash)
r = parse_ts("not-a-date")
if r is None:
    ok("T5: parse_ts('not-a-date') → None (no crash)")
else:
    fail("T5: parse_ts malformed", f"got {r!r}")

# T6 — parse_ts result is timezone-aware (has tzinfo) for UTC comparisons
r = parse_ts("2026-01-15T09:30:00Z")
if r is not None and r.tzinfo is not None:
    ok("T6: parse_ts result is timezone-aware (tzinfo present)")
else:
    fail("T6: parse_ts timezone-aware", f"tzinfo={r.tzinfo if r else 'None'}")

# -----------------------------------------------------------------------
# mask_phone
# -----------------------------------------------------------------------

# T7 — US 11-digit number → masked
masked = mask_phone("+14256716122")
if masked == "+1-425-XXX-XXXX":
    ok("T7: +14256716122 → +1-425-XXX-XXXX")
else:
    fail("T7: mask_phone 11-digit US", f"got {masked!r}")

# T8 — "unknown" → "unknown"
masked = mask_phone("unknown")
if masked == "unknown":
    ok("T8: mask_phone('unknown') → 'unknown'")
else:
    fail("T8: mask_phone unknown", f"got {masked!r}")

# T9 — None → "unknown"
masked = mask_phone(None)
if masked == "unknown":
    ok("T9: mask_phone(None) → 'unknown'")
else:
    fail("T9: mask_phone None", f"got {masked!r}")

# T10 — empty string → "unknown"
masked = mask_phone("")
if masked == "unknown":
    ok("T10: mask_phone('') → 'unknown'")
else:
    fail("T10: mask_phone empty", f"got {masked!r}")

# T11 — area code preserved, subscriber number hidden
masked = mask_phone("+12065551234")
if masked.startswith("+1-206") and "XXX-XXXX" in masked:
    ok("T11: area code preserved (+1-206-XXX-XXXX), subscriber hidden")
else:
    fail("T11: mask_phone area code preserved", f"got {masked!r}")

# T12 — non-numeric characters stripped from digit count; short number returned as-is
masked = mask_phone("123")  # only 3 digits — below 10-digit threshold
if masked == "123":
    ok("T12: sub-10-digit number returned unchanged")
else:
    fail("T12: short number passthrough", f"got {masked!r}")

# -----------------------------------------------------------------------
# filter_by_window
# -----------------------------------------------------------------------

_recent_call = _call(start_time=_now_iso(-1))   # 1 day ago — within any reasonable window
_old_call = _call(start_time=_now_iso(-40))     # 40 days ago — outside 30-day window
_calls = [_recent_call, _old_call]

# T13 — days=None returns all calls (no filter)
result = filter_by_window(_calls, None)
if len(result) == 2:
    ok("T13: filter_by_window(calls, None) returns all calls")
else:
    fail("T13: filter all", f"got {len(result)} calls")

# T14 — days=30 keeps recent, drops 40-day-old
result = filter_by_window(_calls, 30)
if len(result) == 1 and result[0] is _recent_call:
    ok("T14: filter_by_window(calls, 30) keeps recent, drops 40-day-old")
else:
    fail("T14: filter 30 days", f"got {len(result)} calls")

# T15 — days=1 with recent call 1 day ago — boundary: 1-day call may be on or just inside the cutoff
result_1d = filter_by_window([_recent_call], 7)
if len(result_1d) == 1:
    ok("T15: call from 1 day ago kept by 7-day window")
else:
    fail("T15: filter 7-day boundary", f"got {len(result_1d)}")

# T16 — empty list → empty result regardless of days
result = filter_by_window([], 7)
if result == []:
    ok("T16: filter_by_window([], 7) → empty list")
else:
    fail("T16: filter empty list", f"got {result}")

# -----------------------------------------------------------------------
# compute_stats
# -----------------------------------------------------------------------

# T17 — empty call list → all zeros/Nones
stats = compute_stats([])
if stats["total"] == 0 and stats["avg_duration_seconds"] == 0 and stats["peak_hour"] is None:
    ok("T17: compute_stats([]) → total=0, avg=0, peak_hour=None")
else:
    fail("T17: compute_stats empty", f"stats={stats}")

# T18 — single call with known duration → correct avg/longest/shortest
calls = [_call(start_time="2026-05-26T14:00:00Z", duration=120, caller="+14255550000", purpose="scheduling")]
stats = compute_stats(calls)
if stats["total"] == 1 and stats["avg_duration_seconds"] == 120.0 and stats["longest_seconds"] == 120:
    ok("T18: single call → total=1, avg=120s, longest=120s")
else:
    fail("T18: compute_stats single call", f"stats={stats}")

# T19 — purpose counter: top_purposes includes the call's purpose
stats = compute_stats([_call(purpose="scheduling"), _call(purpose="scheduling"), _call(purpose="check-in")])
top_purpose_names = [p for p, _ in stats["top_purposes"]]
if "scheduling" in top_purpose_names and stats["top_purposes"][0] == ("scheduling", 2):
    ok("T19: compute_stats top_purposes: scheduling×2 ranks first")
else:
    fail("T19: top_purposes", f"top_purposes={stats['top_purposes']}")

# T20 — is_meeting counter
calls = [_call(is_meeting=True), _call(is_meeting=True), _call(is_meeting=False)]
stats = compute_stats(calls)
if stats["meetings"] == 2 and stats["total"] == 3:
    ok("T20: compute_stats meetings count (2/3 are meetings)")
else:
    fail("T20: meetings count", f"meetings={stats['meetings']} total={stats['total']}")

# T21 — top_callers masks phone numbers
calls = [_call(caller="+14255550000"), _call(caller="+14255550000")]
stats = compute_stats(calls)
caller_numbers = [num for num, _ in stats["top_callers"]]
if all("XXX" in num for num in caller_numbers if num != "unknown"):
    ok("T21: compute_stats top_callers phone numbers are masked")
else:
    fail("T21: top_callers masked", f"callers={caller_numbers}")

# T22 — total_minutes is sum of durations in minutes, rounded to 1 decimal
calls = [_call(duration=60), _call(duration=90), _call(duration=90)]  # 240s total = 4.0 min
stats = compute_stats(calls)
if stats["total_minutes"] == 4.0:
    ok("T22: compute_stats total_minutes = 4.0 (240s / 60)")
else:
    fail("T22: total_minutes", f"got {stats['total_minutes']}")

# -----------------------------------------------------------------------
# format_text
# -----------------------------------------------------------------------

# T23 — format_text returns a string containing the window label
stats = compute_stats([_call(duration=120, purpose="test")])
text = format_text(stats, "last 7 days")
if isinstance(text, str) and "last 7 days" in text:
    ok("T23: format_text includes window label in output")
else:
    fail("T23: format_text label", f"text[:80]={text[:80]!r}")

# T24 — format_text includes total count
stats = compute_stats([_call(duration=60), _call(duration=60), _call(duration=60)])
text = format_text(stats, "all time")
if "3" in text and ("call" in text.lower()):
    ok("T24: format_text includes total call count (3)")
else:
    fail("T24: format_text total count", f"text[:120]={text[:120]!r}")

# T25 — format_text on empty stats doesn't crash
stats = compute_stats([])
text = format_text(stats, "empty window")
if isinstance(text, str) and "0" in text:
    ok("T25: format_text on empty stats returns valid string without crash")
else:
    fail("T25: format_text empty", f"text={text!r}")

# -----------------------------------------------------------------------
# Summary
# -----------------------------------------------------------------------
print(f"\n{PASS}/{PASS + FAIL} tests passed")
if FAIL:
    sys.exit(1)
