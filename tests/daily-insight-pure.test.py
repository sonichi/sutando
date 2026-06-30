#!/usr/bin/env python3
"""Tests for pure analysis functions in src/daily-insight.py.

Covers the three functions that take only in-memory data:
  - analyze_call_timing(): calls list → (hour_counts, day_counts)
  - analyze_call_duration(): calls list → stats dict or None
  - analyze_topics(): calls list → list of (word, count) tuples

These have no filesystem I/O and are safe to unit-test directly.
analyze_task_patterns() and analyze_note_activity() read workspace dirs
and are excluded here.

Run: python3 tests/daily-insight-pure.test.py
Exit 0 on pass, 1 on fail.
"""

from __future__ import annotations
import importlib.util
import os
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

# daily-insight.py resolves WORKSPACE at import time. Point it at a temp
# dir so the module loads cleanly without requiring real workspace state.
_tmp_ws = tempfile.mkdtemp()
os.environ.setdefault("SUTANDO_WORKSPACE", _tmp_ws)

spec = importlib.util.spec_from_file_location("daily_insight", REPO / "src" / "daily-insight.py")
di = importlib.util.module_from_spec(spec)
spec.loader.exec_module(di)


def check(label: str, cond: bool, fails: list) -> None:
    if not cond:
        fails.append(label)


# ---------------------------------------------------------------------------
# analyze_call_timing
# ---------------------------------------------------------------------------

def test_timing_empty_list() -> list[str]:
    """Empty list → both Counters are empty."""
    fails: list[str] = []
    hour_counts, day_counts = di.analyze_call_timing([])
    check("hour_counts not empty", len(hour_counts) == 0, fails)
    check("day_counts not empty", len(day_counts) == 0, fails)
    return fails


def test_timing_single_call_iso_z() -> list[str]:
    """ISO Z timestamp is parsed; hour and weekday are counted."""
    fails: list[str] = []
    calls = [{"start_time": "2026-06-29T10:30:00Z"}]  # Monday 10:30 UTC
    hour_counts, day_counts = di.analyze_call_timing(calls)
    check("hour 10 not counted", hour_counts.get(10, 0) == 1, fails)
    check("Monday not counted", day_counts.get("Monday", 0) == 1, fails)
    return fails


def test_timing_multiple_calls_same_hour() -> list[str]:
    """Multiple calls in the same hour accumulate the count."""
    fails: list[str] = []
    calls = [
        {"start_time": "2026-06-29T14:00:00Z"},
        {"start_time": "2026-06-29T14:45:00Z"},
        {"start_time": "2026-06-29T09:00:00Z"},
    ]
    hour_counts, _ = di.analyze_call_timing(calls)
    check(f"14:00 count wrong: {hour_counts.get(14)}", hour_counts.get(14, 0) == 2, fails)
    check(f"09:00 count wrong: {hour_counts.get(9)}", hour_counts.get(9, 0) == 1, fails)
    return fails


def test_timing_missing_timestamp_skipped() -> list[str]:
    """Calls with no timestamp field are silently skipped."""
    fails: list[str] = []
    calls = [
        {"start_time": None},
        {"start_time": ""},
        {},
        {"start_time": "2026-06-29T08:00:00Z"},
    ]
    hour_counts, day_counts = di.analyze_call_timing(calls)
    check("should count exactly 1 hour entry", sum(hour_counts.values()) == 1, fails)
    check("should count exactly 1 day entry", sum(day_counts.values()) == 1, fails)
    return fails


def test_timing_bad_timestamp_skipped() -> list[str]:
    """Unparseable timestamp strings are silently skipped (no crash)."""
    fails: list[str] = []
    calls = [
        {"start_time": "not-a-date"},
        {"start_time": "2026-06-29T10:00:00Z"},
    ]
    hour_counts, _ = di.analyze_call_timing(calls)
    check("bad ts should not be counted", sum(hour_counts.values()) == 1, fails)
    return fails


def test_timing_uses_timestamp_field_as_fallback() -> list[str]:
    """'timestamp' key is accepted when 'start_time' is absent."""
    fails: list[str] = []
    calls = [{"timestamp": "2026-06-29T15:00:00Z"}]
    hour_counts, _ = di.analyze_call_timing(calls)
    check("hour 15 not counted from 'timestamp' key", hour_counts.get(15, 0) == 1, fails)
    return fails


# ---------------------------------------------------------------------------
# analyze_call_duration
# ---------------------------------------------------------------------------

def test_duration_empty_list() -> list[str]:
    """Empty list → None (no data)."""
    fails: list[str] = []
    result = di.analyze_call_duration([])
    check("empty list should return None", result is None, fails)
    return fails


def test_duration_all_zero_skipped() -> list[str]:
    """Calls with duration_seconds=0 produce no stats (None)."""
    fails: list[str] = []
    calls = [{"duration_seconds": 0}, {"duration_seconds": 0}]
    result = di.analyze_call_duration(calls)
    check("all-zero durations should return None", result is None, fails)
    return fails


def test_duration_avg_and_longest() -> list[str]:
    """avg_minutes, longest_minutes are computed correctly."""
    fails: list[str] = []
    calls = [
        {"duration_seconds": 60},   # 1 min
        {"duration_seconds": 180},  # 3 min
        {"duration_seconds": 120},  # 2 min
    ]
    result = di.analyze_call_duration(calls)
    check("result should not be None", result is not None, fails)
    if result:
        check(f"avg_minutes wrong: {result['avg_minutes']}", result["avg_minutes"] == 2.0, fails)
        check(f"longest_minutes wrong: {result['longest_minutes']}", result["longest_minutes"] == 3.0, fails)
        check(f"count wrong: {result['count']}", result["count"] == 3, fails)
    return fails


def test_duration_long_call_pct() -> list[str]:
    """long_call_pct: calls with duration > 2× average are flagged."""
    fails: list[str] = []
    # avg = (60 + 60 + 600) / 3 = 240; 2x = 480; 600 > 480 → 1/3 = 33.3%
    calls = [
        {"duration_seconds": 60},
        {"duration_seconds": 60},
        {"duration_seconds": 600},
    ]
    result = di.analyze_call_duration(calls)
    check("result should not be None", result is not None, fails)
    if result:
        check(
            f"long_call_pct wrong: {result['long_call_pct']}",
            abs(result["long_call_pct"] - 33.3) < 1.0,
            fails,
        )
    return fails


def test_duration_accepts_duration_key() -> list[str]:
    """'duration' key (alias) is accepted when 'duration_seconds' is absent."""
    fails: list[str] = []
    calls = [{"duration": 120}]
    result = di.analyze_call_duration(calls)
    check("'duration' key should be accepted", result is not None, fails)
    return fails


def test_duration_non_numeric_skipped() -> list[str]:
    """Non-numeric duration values are skipped (no crash)."""
    fails: list[str] = []
    calls = [
        {"duration_seconds": "not-a-number"},
        {"duration_seconds": 300},
    ]
    result = di.analyze_call_duration(calls)
    check("numeric call counted", result is not None, fails)
    if result:
        check("only numeric duration counted", result["count"] == 1, fails)
    return fails


# ---------------------------------------------------------------------------
# analyze_topics
# ---------------------------------------------------------------------------

def test_topics_empty_list() -> list[str]:
    """Empty list → empty topics."""
    fails: list[str] = []
    result = di.analyze_topics([])
    check("empty list → empty topics", result == [], fails)
    return fails


def test_topics_counts_words() -> list[str]:
    """Words longer than 4 chars are counted from 'summary' field."""
    fails: list[str] = []
    calls = [
        {"summary": "performance review discussion"},
        {"summary": "performance analysis report"},
    ]
    result = di.analyze_topics(calls)
    words = [w for w, _ in result]
    check("'performance' should be top topic", "performance" in words, fails)
    # 'performance' appears twice — should be first or near top
    if result and result[0][0] == "performance":
        check("'performance' count == 2", result[0][1] == 2, fails)
    return fails


def test_topics_short_words_excluded() -> list[str]:
    """Words <= 4 chars are excluded from topics."""
    fails: list[str] = []
    calls = [{"summary": "do the big task"}]
    result = di.analyze_topics(calls)
    words = [w for w, _ in result]
    for short in ["do", "the", "big"]:
        check(f"short word '{short}' should be excluded", short not in words, fails)
    return fails


def test_topics_stopwords_excluded() -> list[str]:
    """Common stopwords ('about', 'would', etc.) are excluded."""
    fails: list[str] = []
    calls = [{"summary": "about their project which would should help"},
             {"summary": "about their project which would should help"}]
    result = di.analyze_topics(calls)
    words = [w for w, _ in result]
    for sw in ["about", "their", "which", "would", "should"]:
        check(f"stopword '{sw}' should be excluded", sw not in words, fails)
    return fails


def test_topics_strips_punctuation() -> list[str]:
    """Punctuation around words is stripped before counting."""
    fails: list[str] = []
    calls = [{"summary": "strategy, performance! review:"}]
    result = di.analyze_topics(calls)
    words = [w for w, _ in result]
    check("'strategy' without comma counted", "strategy" in words, fails)
    check("'performance' without exclamation counted", "performance" in words, fails)
    return fails


def test_topics_uses_topic_key_as_fallback() -> list[str]:
    """'topic' key is accepted when 'summary' is absent."""
    fails: list[str] = []
    calls = [{"topic": "performance review analysis"}]
    result = di.analyze_topics(calls)
    words = [w for w, _ in result]
    check("'performance' found via 'topic' key", "performance" in words, fails)
    return fails


def test_topics_returns_at_most_ten() -> list[str]:
    """Results are capped at 10 entries."""
    fails: list[str] = []
    words = ["alpha", "bravo", "charlie", "delta", "epsilon",
             "foxtrot", "gamma", "hotel", "india", "juliet", "kilo"]
    calls = [{"summary": " ".join(words)}]
    result = di.analyze_topics(calls)
    check(f"topics should be <= 10, got {len(result)}", len(result) <= 10, fails)
    return fails


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

def main() -> int:
    cases = [
        ("timing: empty list → empty counters", test_timing_empty_list),
        ("timing: ISO Z parsed to hour + weekday", test_timing_single_call_iso_z),
        ("timing: same-hour calls accumulate", test_timing_multiple_calls_same_hour),
        ("timing: missing timestamp skipped", test_timing_missing_timestamp_skipped),
        ("timing: bad timestamp skipped", test_timing_bad_timestamp_skipped),
        ("timing: 'timestamp' key fallback", test_timing_uses_timestamp_field_as_fallback),
        ("duration: empty list → None", test_duration_empty_list),
        ("duration: all-zero → None", test_duration_all_zero_skipped),
        ("duration: avg + longest computed", test_duration_avg_and_longest),
        ("duration: long_call_pct", test_duration_long_call_pct),
        ("duration: 'duration' key accepted", test_duration_accepts_duration_key),
        ("duration: non-numeric skipped", test_duration_non_numeric_skipped),
        ("topics: empty list → []", test_topics_empty_list),
        ("topics: words counted from summary", test_topics_counts_words),
        ("topics: short words excluded", test_topics_short_words_excluded),
        ("topics: stopwords excluded", test_topics_stopwords_excluded),
        ("topics: punctuation stripped", test_topics_strips_punctuation),
        ("topics: 'topic' key fallback", test_topics_uses_topic_key_as_fallback),
        ("topics: capped at 10 entries", test_topics_returns_at_most_ten),
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
    print(f"\ndaily-insight pure functions: {total}/{total} passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
