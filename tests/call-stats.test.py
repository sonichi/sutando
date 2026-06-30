#!/usr/bin/env python3
"""Tests for pure functions in src/call-stats.py.

Covers the analysis pipeline that runs entirely in-process:
  - parse_ts(): ISO timestamp string → aware datetime or None
  - mask_phone(): full E.164 number → masked form (area code visible)
  - compute_stats(): list of call dicts → aggregated statistics dict
  - format_text(): stats dict + label → human-readable text

Run: python3 tests/call-stats.test.py
Exit 0 on pass, 1 on fail.
"""

from __future__ import annotations
import importlib.util
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("call_stats", REPO / "src" / "call-stats.py")
cs = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cs)


def check(label: str, cond: bool, fails: list) -> None:
    if not cond:
        fails.append(label)


# ---------------------------------------------------------------------------
# parse_ts
# ---------------------------------------------------------------------------

def test_parse_ts_iso_z() -> list[str]:
    """'2026-06-30T12:00:00Z' → aware datetime at noon UTC."""
    fails: list[str] = []
    from datetime import timezone
    dt = cs.parse_ts("2026-06-30T12:00:00Z")
    check("None returned for valid ISO Z", dt is not None, fails)
    if dt:
        check("year wrong", dt.year == 2026, fails)
        check("month wrong", dt.month == 6, fails)
        check("hour wrong", dt.hour == 12, fails)
        check("not UTC-aware", dt.tzinfo is not None, fails)
    return fails


def test_parse_ts_iso_offset() -> list[str]:
    """'+00:00' suffix is also accepted."""
    fails: list[str] = []
    dt = cs.parse_ts("2026-01-15T08:30:00+00:00")
    check("None returned for +00:00 timestamp", dt is not None, fails)
    return fails


def test_parse_ts_empty_string() -> list[str]:
    """Empty string → None (no crash)."""
    fails: list[str] = []
    check("'' should return None", cs.parse_ts("") is None, fails)
    return fails


def test_parse_ts_none() -> list[str]:
    """None → None (no crash)."""
    fails: list[str] = []
    check("None should return None", cs.parse_ts(None) is None, fails)
    return fails


def test_parse_ts_invalid_string() -> list[str]:
    """Garbage string → None (no crash)."""
    fails: list[str] = []
    check("garbage should return None", cs.parse_ts("not-a-date") is None, fails)
    return fails


# ---------------------------------------------------------------------------
# mask_phone
# ---------------------------------------------------------------------------

def test_mask_phone_us_eleven_digit() -> list[str]:
    """11-digit US number shows +1 + area code, masks local."""
    fails: list[str] = []
    result = cs.mask_phone("+14255551234")
    check(f"country code missing: {result!r}", "+1" in result, fails)
    check(f"area code missing: {result!r}", "425" in result, fails)
    check(f"local digits not masked: {result!r}", "5551234" not in result, fails)
    check(f"'XXX-XXXX' placeholder missing: {result!r}", "XXX-XXXX" in result, fails)
    return fails


def test_mask_phone_unknown_value() -> list[str]:
    """'unknown' sentinel passes through unchanged."""
    fails: list[str] = []
    result = cs.mask_phone("unknown")
    check(f"'unknown' was changed: {result!r}", result == "unknown", fails)
    return fails


def test_mask_phone_none() -> list[str]:
    """None → 'unknown'."""
    fails: list[str] = []
    result = cs.mask_phone(None)
    check(f"None → {result!r}, expected 'unknown'", result == "unknown", fails)
    return fails


def test_mask_phone_short_number() -> list[str]:
    """Short numbers (< 10 digits) pass through unchanged — can't extract area code."""
    fails: list[str] = []
    result = cs.mask_phone("1234")
    check(f"short number was mangled: {result!r}", result == "1234", fails)
    return fails


# ---------------------------------------------------------------------------
# compute_stats
# ---------------------------------------------------------------------------

def _make_calls(*entries):
    """Build a list of minimal call dicts from (start_time, duration_s, **extras)."""
    calls = []
    for e in entries:
        start, duration, *extras = e
        call = {"start_time": start, "duration_seconds": duration}
        if extras and isinstance(extras[0], dict):
            call.update(extras[0])
        calls.append(call)
    return calls


def test_compute_stats_empty() -> list[str]:
    """Empty call list → all-zero stats, no crash."""
    fails: list[str] = []
    stats = cs.compute_stats([])
    check("total not 0", stats["total"] == 0, fails)
    check("avg_duration not 0", stats["avg_duration_seconds"] == 0, fails)
    check("peak_hour not None", stats["peak_hour"] is None, fails)
    check("busiest_day not None", stats["busiest_day"] is None, fails)
    return fails


def test_compute_stats_total_count() -> list[str]:
    """total is exactly the number of calls passed in."""
    fails: list[str] = []
    calls = _make_calls(
        ("2026-06-01T10:00:00Z", 60),
        ("2026-06-01T11:00:00Z", 120),
        ("2026-06-01T12:00:00Z", 90),
    )
    stats = cs.compute_stats(calls)
    check(f"total wrong: {stats['total']}", stats["total"] == 3, fails)
    return fails


def test_compute_stats_duration_math() -> list[str]:
    """avg, longest, shortest are computed from duration_seconds."""
    fails: list[str] = []
    calls = _make_calls(
        ("2026-06-01T10:00:00Z", 60),
        ("2026-06-01T11:00:00Z", 180),
        ("2026-06-01T12:00:00Z", 120),
    )
    stats = cs.compute_stats(calls)
    check(f"avg wrong: {stats['avg_duration_seconds']}", stats["avg_duration_seconds"] == 120.0, fails)
    check(f"longest wrong: {stats['longest_seconds']}", stats["longest_seconds"] == 180, fails)
    check(f"shortest wrong: {stats['shortest_seconds']}", stats["shortest_seconds"] == 60, fails)
    check(f"total_minutes wrong: {stats['total_minutes']}", stats["total_minutes"] == 6.0, fails)
    return fails


def test_compute_stats_zero_duration_excluded() -> list[str]:
    """Calls with duration_seconds=0 are excluded from duration stats."""
    fails: list[str] = []
    calls = _make_calls(
        ("2026-06-01T10:00:00Z", 0),
        ("2026-06-01T11:00:00Z", 300),
    )
    stats = cs.compute_stats(calls)
    check("total should be 2", stats["total"] == 2, fails)
    check("with_duration should be 1 (exclude 0s)", stats["with_duration"] == 1, fails)
    check("avg should be 300", stats["avg_duration_seconds"] == 300.0, fails)
    return fails


def test_compute_stats_meetings_owner_flags() -> list[str]:
    """is_meeting and is_owner flags are tallied."""
    fails: list[str] = []
    calls = [
        {"start_time": "2026-06-01T10:00:00Z", "duration_seconds": 60, "is_meeting": True, "is_owner": True},
        {"start_time": "2026-06-01T11:00:00Z", "duration_seconds": 30, "is_meeting": False, "is_owner": False},
        {"start_time": "2026-06-01T12:00:00Z", "duration_seconds": 45, "is_meeting": True, "is_owner": False},
    ]
    stats = cs.compute_stats(calls)
    check(f"meetings wrong: {stats['meetings']}", stats["meetings"] == 2, fails)
    check(f"owner_calls wrong: {stats['owner_calls']}", stats["owner_calls"] == 1, fails)
    return fails


def test_compute_stats_peak_hour() -> list[str]:
    """peak_hour is the hour with the most calls."""
    fails: list[str] = []
    calls = [
        {"start_time": "2026-06-01T10:00:00Z", "duration_seconds": 60},
        {"start_time": "2026-06-01T10:30:00Z", "duration_seconds": 60},
        {"start_time": "2026-06-01T14:00:00Z", "duration_seconds": 60},
    ]
    stats = cs.compute_stats(calls)
    # 10am has 2 calls → peak
    check(f"peak_hour wrong: {stats['peak_hour']}", stats["peak_hour"][0] == 10, fails)
    check(f"peak_hour count wrong: {stats['peak_hour']}", stats["peak_hour"][1] == 2, fails)
    return fails


def test_compute_stats_top_callers_masked() -> list[str]:
    """top_callers contains masked phone numbers."""
    fails: list[str] = []
    calls = [
        {"start_time": "2026-06-01T10:00:00Z", "duration_seconds": 60, "caller": "+14255551234"},
        {"start_time": "2026-06-01T11:00:00Z", "duration_seconds": 60, "caller": "+14255551234"},
    ]
    stats = cs.compute_stats(calls)
    if stats["top_callers"]:
        num, count = stats["top_callers"][0]
        check("caller not masked (local digits visible)", "5551234" not in num, fails)
        check("XXX-XXXX not in masked caller", "XXX-XXXX" in num, fails)
        check(f"count wrong: {count}", count == 2, fails)
    return fails


# ---------------------------------------------------------------------------
# format_text
# ---------------------------------------------------------------------------

def _minimal_stats(**overrides):
    """Minimal stats dict that satisfies format_text()."""
    base = {
        "total": 0,
        "with_duration": 0,
        "avg_duration_seconds": 0,
        "longest_seconds": 0,
        "shortest_seconds": 0,
        "total_minutes": 0,
        "meetings": 0,
        "owner_calls": 0,
        "peak_hour": None,
        "quiet_hours": [],
        "busiest_day": None,
        "top_purposes": [],
        "top_callers": [],
    }
    base.update(overrides)
    return base


def test_format_text_header() -> list[str]:
    """Output starts with '📞 Call stats —' and the window label."""
    fails: list[str] = []
    text = cs.format_text(_minimal_stats(total=5), "last 7 days")
    check("header missing", "Call stats" in text, fails)
    check("window label missing", "last 7 days" in text, fails)
    return fails


def test_format_text_total_count() -> list[str]:
    """Total call count is always in output."""
    fails: list[str] = []
    text = cs.format_text(_minimal_stats(total=42), "all time")
    check("'42 calls' missing", "42 calls" in text, fails)
    return fails


def test_format_text_no_duration_placeholder() -> list[str]:
    """When with_duration=0, a 'no duration data' placeholder appears."""
    fails: list[str] = []
    text = cs.format_text(_minimal_stats(total=3, with_duration=0), "week")
    check("no-duration placeholder missing", "no duration data" in text, fails)
    return fails


def test_format_text_duration_lines() -> list[str]:
    """When with_duration>0, avg/longest/shortest are shown."""
    fails: list[str] = []
    stats = _minimal_stats(
        total=2, with_duration=2,
        avg_duration_seconds=90.0, longest_seconds=120, shortest_seconds=60,
        total_minutes=3.0,
    )
    text = cs.format_text(stats, "week")
    check("'Avg' missing", "Avg" in text, fails)
    check("'Longest' missing", "Longest" in text, fails)
    check("'90.0s' missing", "90.0s" in text, fails)
    return fails


def test_format_text_peak_hour_formatted() -> list[str]:
    """Peak hour is formatted as HH:00."""
    fails: list[str] = []
    text = cs.format_text(_minimal_stats(total=3, peak_hour=(9, 3)), "week")
    check("'09:00' missing", "09:00" in text, fails)
    return fails


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

def main() -> int:
    cases = [
        ("parse_ts: ISO Z string", test_parse_ts_iso_z),
        ("parse_ts: +00:00 offset", test_parse_ts_iso_offset),
        ("parse_ts: '' → None", test_parse_ts_empty_string),
        ("parse_ts: None → None", test_parse_ts_none),
        ("parse_ts: garbage → None", test_parse_ts_invalid_string),
        ("mask_phone: 11-digit US masked", test_mask_phone_us_eleven_digit),
        ("mask_phone: 'unknown' pass-through", test_mask_phone_unknown_value),
        ("mask_phone: None → 'unknown'", test_mask_phone_none),
        ("mask_phone: short number pass-through", test_mask_phone_short_number),
        ("compute_stats: empty list", test_compute_stats_empty),
        ("compute_stats: total count", test_compute_stats_total_count),
        ("compute_stats: duration math", test_compute_stats_duration_math),
        ("compute_stats: zero-duration excluded", test_compute_stats_zero_duration_excluded),
        ("compute_stats: meetings + owner flags", test_compute_stats_meetings_owner_flags),
        ("compute_stats: peak hour", test_compute_stats_peak_hour),
        ("compute_stats: top callers masked", test_compute_stats_top_callers_masked),
        ("format_text: header + window label", test_format_text_header),
        ("format_text: total count shown", test_format_text_total_count),
        ("format_text: no-duration placeholder", test_format_text_no_duration_placeholder),
        ("format_text: duration lines shown", test_format_text_duration_lines),
        ("format_text: peak hour HH:00", test_format_text_peak_hour_formatted),
    ]
    all_failures: list[str] = []
    for label, fn in cases:
        try:
            fails = fn()
        except Exception as exc:
            fails = [f"raised {type(exc).__name__}: {exc}"]
        if fails:
            print(f"  ✗ {label}")
            for f in fails:
                print(f"      {f}")
            all_failures.extend(fails)
        else:
            print(f"  ✓ {label}")

    if all_failures:
        print(f"\n{len(all_failures)} failure(s)")
        return 1
    total = len(cases)
    print(f"\ncall-stats: {total}/{total} passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
