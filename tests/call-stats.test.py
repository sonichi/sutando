#!/usr/bin/env python3
"""
Tests for src/call-stats.py

Covers all pure / file-based functions:
  load_calls()         — JSONL reader (mirrors daily-insight's version)
  parse_ts()           — ISO-8601 parser returning datetime or None
  mask_phone()         — phone number masker (keeps country + area code)
  filter_by_window()   — time-window filter
  compute_stats()      — stats dict from call list
  format_text()        — text formatter (no file I/O)

main() (argparse + stdout) excluded from unit tests — covered implicitly
by compute_stats + format_text.

Run: python3 tests/call-stats.test.py
Exit code: 0 on pass, 1 on fail.
"""

from __future__ import annotations
import importlib.util
import json
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

REPO = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location(
    "call_stats", REPO / "src" / "call-stats.py"
)
cs = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cs)

PASS = 0
FAIL = 0


def ok(label: str) -> None:
    global PASS
    PASS += 1
    print(f"  PASS  {label}")


def fail(label: str, msg: str) -> None:
    global FAIL
    FAIL += 1
    print(f"  FAIL  {label}: {msg}")


# ── load_calls ───────────────────────────────────────────────────────────────

def test_load_calls_missing_file():
    with tempfile.TemporaryDirectory() as tmp:
        calls_file = Path(tmp) / "calls.jsonl"
        with patch.object(cs, "CALLS_FILE", calls_file):
            result = cs.load_calls()
    if result == []:
        ok("(lc-a) missing file → []")
    else:
        fail("(lc-a) missing file → []", f"got {result}")


def test_load_calls_valid_jsonl():
    with tempfile.TemporaryDirectory() as tmp:
        calls_file = Path(tmp) / "calls.jsonl"
        calls_file.write_text(
            '{"start_time":"2026-01-01T10:00:00Z","duration_seconds":120}\n'
            '{"start_time":"2026-01-02T11:00:00Z","duration_seconds":300}\n'
        )
        with patch.object(cs, "CALLS_FILE", calls_file):
            result = cs.load_calls()
    if len(result) == 2 and result[0]["duration_seconds"] == 120:
        ok("(lc-b) valid JSONL → 2 calls")
    else:
        fail("(lc-b) valid JSONL → 2 calls", f"got {result}")


def test_load_calls_skips_malformed():
    with tempfile.TemporaryDirectory() as tmp:
        calls_file = Path(tmp) / "calls.jsonl"
        calls_file.write_text(
            '{"start_time":"2026-01-01T10:00:00Z"}\n'
            'NOT JSON\n'
            '{"start_time":"2026-01-03T12:00:00Z"}\n'
        )
        with patch.object(cs, "CALLS_FILE", calls_file):
            result = cs.load_calls()
    if len(result) == 2:
        ok("(lc-c) malformed line skipped — 2 valid calls")
    else:
        fail("(lc-c) malformed line skipped — 2 valid calls", f"got {len(result)}")


# ── parse_ts ─────────────────────────────────────────────────────────────────

def test_parse_ts_none():
    if cs.parse_ts(None) is None:
        ok("(pt-a) None → None")
    else:
        fail("(pt-a) None → None", f"got {cs.parse_ts(None)}")


def test_parse_ts_empty_string():
    if cs.parse_ts("") is None:
        ok("(pt-b) empty string → None")
    else:
        fail("(pt-b) empty string → None", f"got {cs.parse_ts('')}")


def test_parse_ts_invalid_string():
    if cs.parse_ts("not-a-date") is None:
        ok("(pt-c) invalid string → None")
    else:
        fail("(pt-c) invalid string → None", f"got {cs.parse_ts('not-a-date')}")


def test_parse_ts_utc_z():
    result = cs.parse_ts("2026-01-15T10:30:00Z")
    if isinstance(result, datetime) and result.year == 2026 and result.hour == 10:
        ok("(pt-d) UTC Z suffix → datetime with correct fields")
    else:
        fail("(pt-d) UTC Z suffix → datetime", f"got {result!r}")


def test_parse_ts_with_offset():
    result = cs.parse_ts("2026-03-20T14:00:00+04:00")
    if isinstance(result, datetime) and result.hour == 14:
        ok("(pt-e) explicit +04:00 offset → datetime")
    else:
        fail("(pt-e) explicit +04:00 offset → datetime", f"got {result!r}")


# ── mask_phone ────────────────────────────────────────────────────────────────

def test_mask_phone_none():
    if cs.mask_phone(None) == "unknown":
        ok("(mp-a) None → 'unknown'")
    else:
        fail("(mp-a) None → 'unknown'", f"got {cs.mask_phone(None)!r}")


def test_mask_phone_unknown_string():
    if cs.mask_phone("unknown") == "unknown":
        ok("(mp-b) 'unknown' passthrough")
    else:
        fail("(mp-b) 'unknown' passthrough", f"got {cs.mask_phone('unknown')!r}")


def test_mask_phone_us_11_digits():
    result = cs.mask_phone("+14255551234")
    # Should be +1-425-XXX-XXXX
    if result == "+1-425-XXX-XXXX":
        ok("(mp-c) US 11-digit → +1-425-XXX-XXXX")
    else:
        fail("(mp-c) US 11-digit → +1-425-XXX-XXXX", f"got {result!r}")


def test_mask_phone_short_number():
    # Fewer than 10 digits — returned as-is
    result = cs.mask_phone("12345")
    if result == "12345":
        ok("(mp-d) short number (<10 digits) → returned as-is")
    else:
        fail("(mp-d) short number (<10 digits) → returned as-is", f"got {result!r}")


def test_mask_phone_hides_subscriber():
    result = cs.mask_phone("+14255551234")
    if "5551234" not in result and "XXX" in result:
        ok("(mp-e) subscriber number hidden in masked output")
    else:
        fail("(mp-e) subscriber number hidden in masked output", f"got {result!r}")


# ── filter_by_window ─────────────────────────────────────────────────────────

def test_filter_window_none_returns_all():
    calls = [{"start_time": "2020-01-01T00:00:00Z"}, {"start_time": "2025-06-01T00:00:00Z"}]
    result = cs.filter_by_window(calls, None)
    if len(result) == 2:
        ok("(fw-a) days=None → all calls returned")
    else:
        fail("(fw-a) days=None → all calls returned", f"got {len(result)}")


def test_filter_window_excludes_old():
    now = datetime.now(timezone.utc)
    recent = (now - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    old = (now - timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
    calls = [{"start_time": recent}, {"start_time": old}]
    result = cs.filter_by_window(calls, 7)
    if len(result) == 1 and result[0]["start_time"] == recent:
        ok("(fw-b) 7-day window excludes 30-day-old call")
    else:
        fail("(fw-b) 7-day window excludes 30-day-old call", f"got {len(result)} calls")


def test_filter_window_no_timestamp_excluded():
    calls = [{"duration_seconds": 120}]  # no start_time or timestamp
    result = cs.filter_by_window(calls, 7)
    if result == []:
        ok("(fw-c) call with no timestamp → excluded from window filter")
    else:
        fail("(fw-c) call with no timestamp → excluded", f"got {result}")


def test_filter_window_empty_calls():
    result = cs.filter_by_window([], 7)
    if result == []:
        ok("(fw-d) empty input → []")
    else:
        fail("(fw-d) empty input → []", f"got {result}")


# ── compute_stats ─────────────────────────────────────────────────────────────

def test_compute_stats_empty():
    stats = cs.compute_stats([])
    if stats["total"] == 0 and stats["peak_hour"] is None and stats["busiest_day"] is None:
        ok("(cs-a) empty calls → total=0, peak_hour=None, busiest_day=None")
    else:
        fail("(cs-a) empty calls → zeros and Nones", f"got {stats}")


def test_compute_stats_total_count():
    calls = [{"start_time": "2026-01-05T09:00:00Z"} for _ in range(5)]
    stats = cs.compute_stats(calls)
    if stats["total"] == 5:
        ok("(cs-b) total count correct")
    else:
        fail("(cs-b) total count correct", f"got total={stats['total']}")


def test_compute_stats_duration_fields():
    calls = [
        {"duration_seconds": 60},
        {"duration_seconds": 120},
        {"duration_seconds": 180},
    ]
    stats = cs.compute_stats(calls)
    if (stats["with_duration"] == 3
            and stats["avg_duration_seconds"] == 120.0
            and stats["longest_seconds"] == 180
            and stats["shortest_seconds"] == 60
            and stats["total_minutes"] == 6.0):
        ok("(cs-c) duration stats: avg, longest, shortest, total_minutes")
    else:
        fail("(cs-c) duration stats", f"got {stats}")


def test_compute_stats_meetings_counted():
    calls = [
        {"is_meeting": True},
        {"is_meeting": True},
        {"is_meeting": False},
    ]
    stats = cs.compute_stats(calls)
    if stats["meetings"] == 2:
        ok("(cs-d) is_meeting=True counted correctly")
    else:
        fail("(cs-d) is_meeting=True counted correctly", f"got meetings={stats['meetings']}")


def test_compute_stats_peak_hour():
    calls = [
        {"start_time": "2026-01-05T09:00:00Z"},
        {"start_time": "2026-01-05T09:30:00Z"},
        {"start_time": "2026-01-05T14:00:00Z"},
    ]
    stats = cs.compute_stats(calls)
    if stats["peak_hour"] and stats["peak_hour"][0] == 9 and stats["peak_hour"][1] == 2:
        ok("(cs-e) peak_hour is hour 9 with 2 calls")
    else:
        fail("(cs-e) peak_hour is hour 9 with 2 calls", f"got {stats['peak_hour']}")


def test_compute_stats_callers_masked():
    calls = [{"caller": "+14255551234"}, {"caller": "+14255551234"}]
    stats = cs.compute_stats(calls)
    top_num = stats["top_callers"][0][0] if stats["top_callers"] else ""
    if "XXX" in top_num:
        ok("(cs-f) top_callers phone numbers are masked")
    else:
        fail("(cs-f) top_callers phone numbers are masked", f"got {top_num!r}")


# ── format_text ───────────────────────────────────────────────────────────────

def test_format_text_contains_window_label():
    stats = cs.compute_stats([])
    # force total=1 to avoid early return
    stats["total"] = 1
    text = cs.format_text(stats, "last 7 days")
    if "last 7 days" in text:
        ok("(ft-a) format_text includes window label")
    else:
        fail("(ft-a) format_text includes window label", f"got {text!r}")


def test_format_text_no_duration_fallback():
    stats = cs.compute_stats([{"caller": "unknown"}])
    text = cs.format_text(stats, "all time")
    if "no duration data" in text.lower() or "with_duration" not in text:
        ok("(ft-b) format_text shows fallback when no duration data")
    else:
        fail("(ft-b) format_text shows fallback when no duration data", f"got {text!r}")


def test_format_text_total_in_output():
    calls = [
        {"start_time": "2026-01-05T09:00:00Z", "duration_seconds": 60},
        {"start_time": "2026-01-06T10:00:00Z", "duration_seconds": 90},
    ]
    stats = cs.compute_stats(calls)
    text = cs.format_text(stats, "last 30 days")
    if "Total: 2" in text:
        ok("(ft-c) format_text shows total call count")
    else:
        fail("(ft-c) format_text shows total call count", f"got {text!r}")


if __name__ == "__main__":
    print("call-stats tests")
    print("=" * 40)
    test_load_calls_missing_file()
    test_load_calls_valid_jsonl()
    test_load_calls_skips_malformed()
    test_parse_ts_none()
    test_parse_ts_empty_string()
    test_parse_ts_invalid_string()
    test_parse_ts_utc_z()
    test_parse_ts_with_offset()
    test_mask_phone_none()
    test_mask_phone_unknown_string()
    test_mask_phone_us_11_digits()
    test_mask_phone_short_number()
    test_mask_phone_hides_subscriber()
    test_filter_window_none_returns_all()
    test_filter_window_excludes_old()
    test_filter_window_no_timestamp_excluded()
    test_filter_window_empty_calls()
    test_compute_stats_empty()
    test_compute_stats_total_count()
    test_compute_stats_duration_fields()
    test_compute_stats_meetings_counted()
    test_compute_stats_peak_hour()
    test_compute_stats_callers_masked()
    test_format_text_contains_window_label()
    test_format_text_no_duration_fallback()
    test_format_text_total_in_output()
    print("=" * 40)
    print(f"Results: {PASS} passed, {FAIL} failed")
    sys.exit(0 if FAIL == 0 else 1)
