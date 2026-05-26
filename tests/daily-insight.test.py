#!/usr/bin/env python3
"""
Tests for src/daily-insight.py

Covers pure / file-based functions:
  load_calls()              — JSONL reader with malformed-line skipping
  analyze_call_timing()     — Counter-based hour/day extractor
  analyze_call_duration()   — stats dict, None when no durations
  analyze_topics()          — keyword extraction + stopword filtering
  analyze_task_patterns()   — RESULTS_DIR scanner, source classifier
  analyze_note_activity()   — NOTES_DIR scanner, tag parser

generate_insight() and main() (sentinel, file writes, subprocess) are
excluded — they require live filesystem state and aren't pure.

Run: python3 tests/daily-insight.test.py
Exit code: 0 on pass, 1 on fail.
"""

from __future__ import annotations
import importlib.util
import os
import sys
import tempfile
import time
from collections import Counter
from pathlib import Path
from unittest.mock import patch

REPO = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location(
    "daily_insight", REPO / "src" / "daily-insight.py"
)
di = importlib.util.module_from_spec(spec)
spec.loader.exec_module(di)

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
        with patch.object(di, "CALLS_FILE", calls_file):
            result = di.load_calls()
    if result == []:
        ok("(lc-a) missing file → []")
    else:
        fail("(lc-a) missing file → []", f"got {result}")


def test_load_calls_empty_file():
    with tempfile.TemporaryDirectory() as tmp:
        calls_file = Path(tmp) / "calls.jsonl"
        calls_file.write_text("")
        with patch.object(di, "CALLS_FILE", calls_file):
            result = di.load_calls()
    if result == []:
        ok("(lc-b) empty file → []")
    else:
        fail("(lc-b) empty file → []", f"got {result}")


def test_load_calls_valid_lines():
    with tempfile.TemporaryDirectory() as tmp:
        calls_file = Path(tmp) / "calls.jsonl"
        calls_file.write_text(
            '{"start_time": "2026-01-01T10:00:00Z", "duration_seconds": 120}\n'
            '{"start_time": "2026-01-02T11:00:00Z", "duration_seconds": 300}\n'
        )
        with patch.object(di, "CALLS_FILE", calls_file):
            result = di.load_calls()
    if len(result) == 2 and result[0]["duration_seconds"] == 120:
        ok("(lc-c) valid JSONL → 2 calls parsed")
    else:
        fail("(lc-c) valid JSONL → 2 calls parsed", f"got {result}")


def test_load_calls_skips_malformed():
    with tempfile.TemporaryDirectory() as tmp:
        calls_file = Path(tmp) / "calls.jsonl"
        calls_file.write_text(
            '{"start_time": "2026-01-01T10:00:00Z"}\n'
            'NOT JSON AT ALL\n'
            '{"start_time": "2026-01-03T12:00:00Z"}\n'
        )
        with patch.object(di, "CALLS_FILE", calls_file):
            result = di.load_calls()
    if len(result) == 2:
        ok("(lc-d) malformed line skipped — 2 valid calls returned")
    else:
        fail("(lc-d) malformed line skipped — 2 valid calls returned", f"got {result}")


def test_load_calls_blank_lines_ignored():
    with tempfile.TemporaryDirectory() as tmp:
        calls_file = Path(tmp) / "calls.jsonl"
        calls_file.write_text(
            '\n'
            '{"start_time": "2026-01-01T10:00:00Z"}\n'
            '\n'
        )
        with patch.object(di, "CALLS_FILE", calls_file):
            result = di.load_calls()
    if len(result) == 1:
        ok("(lc-e) blank lines ignored — 1 call returned")
    else:
        fail("(lc-e) blank lines ignored — 1 call returned", f"got {result}")


# ── analyze_call_timing ──────────────────────────────────────────────────────

def test_timing_empty_calls():
    hours, days = di.analyze_call_timing([])
    if hours == Counter() and days == Counter():
        ok("(ct-a) empty calls → empty counters")
    else:
        fail("(ct-a) empty calls → empty counters", f"hours={hours} days={days}")


def test_timing_counts_hour_and_day():
    calls = [
        {"start_time": "2026-01-05T09:00:00Z"},  # Monday, hour 9
        {"start_time": "2026-01-05T09:30:00Z"},  # Monday, hour 9
        {"start_time": "2026-01-06T14:00:00Z"},  # Tuesday, hour 14
    ]
    hours, days = di.analyze_call_timing(calls)
    if hours[9] == 2 and hours[14] == 1 and days["Monday"] == 2 and days["Tuesday"] == 1:
        ok("(ct-b) timestamps → correct hour and day counts")
    else:
        fail("(ct-b) timestamps → correct hour and day counts",
             f"hours={dict(hours)} days={dict(days)}")


def test_timing_skips_missing_timestamp():
    calls = [
        {"duration_seconds": 120},  # no timestamp
        {"start_time": "2026-01-05T10:00:00Z"},
    ]
    hours, days = di.analyze_call_timing(calls)
    if sum(hours.values()) == 1:
        ok("(ct-c) call with no timestamp skipped")
    else:
        fail("(ct-c) call with no timestamp skipped", f"total hour count={sum(hours.values())}")


def test_timing_uses_timestamp_fallback():
    calls = [{"timestamp": "2026-01-07T08:00:00Z"}]  # uses 'timestamp' not 'start_time'
    hours, days = di.analyze_call_timing(calls)
    if hours[8] == 1:
        ok("(ct-d) 'timestamp' field used as fallback")
    else:
        fail("(ct-d) 'timestamp' field used as fallback", f"hours={dict(hours)}")


# ── analyze_call_duration ────────────────────────────────────────────────────

def test_duration_no_duration_fields():
    calls = [{"start_time": "2026-01-01T10:00:00Z"}]
    result = di.analyze_call_duration(calls)
    if result is None:
        ok("(cd-a) no duration fields → None")
    else:
        fail("(cd-a) no duration fields → None", f"got {result}")


def test_duration_empty_calls():
    result = di.analyze_call_duration([])
    if result is None:
        ok("(cd-b) empty calls → None")
    else:
        fail("(cd-b) empty calls → None", f"got {result}")


def test_duration_stats_correct():
    calls = [
        {"duration_seconds": 60},   # 1 min
        {"duration_seconds": 120},  # 2 min
        {"duration_seconds": 540},  # 9 min — well above 2× avg (60)
    ]
    result = di.analyze_call_duration(calls)
    if (result is not None
            and result["count"] == 3
            and result["avg_minutes"] == 4.0
            and result["longest_minutes"] == 9.0):
        ok("(cd-c) duration stats: count, avg, longest correct")
    else:
        fail("(cd-c) duration stats: count, avg, longest correct", f"got {result}")


def test_duration_long_call_pct():
    # avg = (60+60+600)/3 = 240s; 2×avg = 480s; 600 > 480 → 1 long call → 33.3%
    calls = [
        {"duration_seconds": 60},
        {"duration_seconds": 60},
        {"duration_seconds": 600},
    ]
    result = di.analyze_call_duration(calls)
    if result is not None and result["long_call_pct"] == 33.3:
        ok("(cd-d) long_call_pct calculated correctly (33.3%)")
    else:
        fail("(cd-d) long_call_pct calculated correctly", f"got {result}")


# ── analyze_topics ───────────────────────────────────────────────────────────

def test_topics_empty_calls():
    result = di.analyze_topics([])
    if result == []:
        ok("(tp-a) empty calls → []")
    else:
        fail("(tp-a) empty calls → []", f"got {result}")


def test_topics_extracts_keywords():
    calls = [
        {"summary": "discussed project planning and budget"},
        {"summary": "planning session about project timelines"},
    ]
    result = di.analyze_topics(calls)
    words = [w for w, _ in result]
    if "planning" in words and "project" in words:
        ok("(tp-b) repeated keywords appear in top results")
    else:
        fail("(tp-b) repeated keywords appear in top results", f"got words={words}")


def test_topics_stopwords_excluded():
    calls = [{"summary": "about their there would could which where these"}]
    result = di.analyze_topics(calls)
    words = [w for w, _ in result]
    stopwords = {"about", "their", "there", "would", "could", "which", "where", "these"}
    overlap = stopwords & set(words)
    if not overlap:
        ok("(tp-c) stopwords excluded from results")
    else:
        fail("(tp-c) stopwords excluded from results", f"stopwords found: {overlap}")


def test_topics_short_words_excluded():
    calls = [{"summary": "do it go up ok now"}]
    result = di.analyze_topics(calls)
    if all(len(w) > 4 for w, _ in result):
        ok("(tp-d) words ≤4 chars excluded")
    else:
        fail("(tp-d) words ≤4 chars excluded", f"got {result}")


def test_topics_top_10_limit():
    # 15 unique long words — should return at most 10
    words = ["alpha", "bravo", "charlie", "delta", "echo",
             "foxtrot", "golf123", "hotel12", "india12", "juliet1",
             "kilo123", "lima123", "mike123", "november", "oscar12"]
    calls = [{"summary": " ".join(words)}]
    result = di.analyze_topics(calls)
    if len(result) <= 10:
        ok("(tp-e) result capped at 10 topics")
    else:
        fail("(tp-e) result capped at 10 topics", f"got {len(result)} topics")


# ── analyze_task_patterns ────────────────────────────────────────────────────

def test_task_patterns_no_results_dir():
    with tempfile.TemporaryDirectory() as tmp:
        results_dir = Path(tmp) / "results"
        # results/ does not exist
        with patch.object(di, "RESULTS_DIR", results_dir):
            try:
                result = di.analyze_task_patterns()
                is_empty = (not result) or (sum(result.values()) == 0)
            except Exception:
                is_empty = True
    if is_empty:
        ok("(ap-a) no results dir → empty (or graceful)")
    else:
        fail("(ap-a) no results dir → empty (or graceful)", f"got {result}")


def test_task_patterns_classifies_discord():
    with tempfile.TemporaryDirectory() as tmp:
        results = Path(tmp) / "results"
        results.mkdir()
        (results / "task-001.txt").write_text("Task from discord channel message.")
        with patch.object(di, "RESULTS_DIR", results):
            result = di.analyze_task_patterns()
    if result["Discord"] == 1:
        ok("(ap-b) discord keyword → classified as Discord")
    else:
        fail("(ap-b) discord keyword → classified as Discord", f"got {dict(result)}")


def test_task_patterns_classifies_telegram():
    with tempfile.TemporaryDirectory() as tmp:
        results = Path(tmp) / "results"
        results.mkdir()
        (results / "task-002.txt").write_text("Telegram message: do this.")
        with patch.object(di, "RESULTS_DIR", results):
            result = di.analyze_task_patterns()
    if result["Telegram"] == 1:
        ok("(ap-c) telegram keyword → classified as Telegram")
    else:
        fail("(ap-c) telegram keyword → classified as Telegram", f"got {dict(result)}")


def test_task_patterns_classifies_voice():
    with tempfile.TemporaryDirectory() as tmp:
        results = Path(tmp) / "results"
        results.mkdir()
        (results / "task-003.txt").write_text("voice command received.")
        with patch.object(di, "RESULTS_DIR", results):
            result = di.analyze_task_patterns()
    if result["Voice"] == 1:
        ok("(ap-d) voice keyword → classified as Voice")
    else:
        fail("(ap-d) voice keyword → classified as Voice", f"got {dict(result)}")


def test_task_patterns_fallthrough_other():
    with tempfile.TemporaryDirectory() as tmp:
        results = Path(tmp) / "results"
        results.mkdir()
        (results / "task-004.txt").write_text("Scheduled maintenance task.")
        with patch.object(di, "RESULTS_DIR", results):
            result = di.analyze_task_patterns()
    if result["Other"] == 1:
        ok("(ap-e) no keyword match → classified as Other")
    else:
        fail("(ap-e) no keyword match → classified as Other", f"got {dict(result)}")


# ── analyze_note_activity ────────────────────────────────────────────────────

def test_notes_no_notes_dir():
    with tempfile.TemporaryDirectory() as tmp:
        notes_dir = Path(tmp) / "notes"
        # notes/ does not exist
        with patch.object(di, "NOTES_DIR", notes_dir):
            try:
                result = di.analyze_note_activity()
                is_empty = result["total"] == 0 and result["recent_7d"] == 0
            except Exception:
                is_empty = True
    if is_empty:
        ok("(na-a) no notes dir → total=0, recent_7d=0 (or graceful)")
    else:
        fail("(na-a) no notes dir → total=0, recent_7d=0", f"got {result}")


def test_notes_counts_total():
    with tempfile.TemporaryDirectory() as tmp:
        notes_dir = Path(tmp) / "notes"
        notes_dir.mkdir()
        (notes_dir / "a.md").write_text("# Note A\n")
        (notes_dir / "b.md").write_text("# Note B\n")
        (notes_dir / "c.md").write_text("# Note C\n")
        with patch.object(di, "NOTES_DIR", notes_dir):
            result = di.analyze_note_activity()
    if result["total"] == 3:
        ok("(na-b) 3 notes → total=3")
    else:
        fail("(na-b) 3 notes → total=3", f"got total={result.get('total')}")


def test_notes_recent_7d_count():
    with tempfile.TemporaryDirectory() as tmp:
        notes_dir = Path(tmp) / "notes"
        notes_dir.mkdir()
        # 2 recent notes (mtime = now)
        (notes_dir / "recent1.md").write_text("# Recent 1\n")
        (notes_dir / "recent2.md").write_text("# Recent 2\n")
        # 1 old note (mtime = 10d ago)
        old_note = notes_dir / "old.md"
        old_note.write_text("# Old\n")
        old_time = time.time() - (10 * 86400)
        os.utime(old_note, (old_time, old_time))
        with patch.object(di, "NOTES_DIR", notes_dir):
            result = di.analyze_note_activity()
    if result["total"] == 3 and result["recent_7d"] == 2:
        ok("(na-c) recent_7d counts only notes within 7 days")
    else:
        fail("(na-c) recent_7d counts only notes within 7 days",
             f"got total={result.get('total')} recent_7d={result.get('recent_7d')}")


def test_notes_extracts_tags():
    with tempfile.TemporaryDirectory() as tmp:
        notes_dir = Path(tmp) / "notes"
        notes_dir.mkdir()
        (notes_dir / "tagged.md").write_text(
            "---\ntitle: Test\ndate: 2026-01-01\ntags: [ai, projects, voice]\n---\nContent.\n"
        )
        with patch.object(di, "NOTES_DIR", notes_dir):
            result = di.analyze_note_activity()
    top_tags = [t for t, _ in result["top_tags"]]
    if "ai" in top_tags and "projects" in top_tags:
        ok("(na-d) YAML tags extracted correctly")
    else:
        fail("(na-d) YAML tags extracted correctly", f"got top_tags={top_tags}")


def test_notes_top_tags_limit_5():
    with tempfile.TemporaryDirectory() as tmp:
        notes_dir = Path(tmp) / "notes"
        notes_dir.mkdir()
        for i in range(8):
            (notes_dir / f"n{i}.md").write_text(
                f"---\ntitle: N{i}\ntags: [tag{i}]\n---\n"
            )
        with patch.object(di, "NOTES_DIR", notes_dir):
            result = di.analyze_note_activity()
    if len(result["top_tags"]) <= 5:
        ok("(na-e) top_tags capped at 5")
    else:
        fail("(na-e) top_tags capped at 5", f"got {len(result['top_tags'])} tags")


if __name__ == "__main__":
    print("daily-insight tests")
    print("=" * 40)
    test_load_calls_missing_file()
    test_load_calls_empty_file()
    test_load_calls_valid_lines()
    test_load_calls_skips_malformed()
    test_load_calls_blank_lines_ignored()
    test_timing_empty_calls()
    test_timing_counts_hour_and_day()
    test_timing_skips_missing_timestamp()
    test_timing_uses_timestamp_fallback()
    test_duration_no_duration_fields()
    test_duration_empty_calls()
    test_duration_stats_correct()
    test_duration_long_call_pct()
    test_topics_empty_calls()
    test_topics_extracts_keywords()
    test_topics_stopwords_excluded()
    test_topics_short_words_excluded()
    test_topics_top_10_limit()
    test_task_patterns_no_results_dir()
    test_task_patterns_classifies_discord()
    test_task_patterns_classifies_telegram()
    test_task_patterns_classifies_voice()
    test_task_patterns_fallthrough_other()
    test_notes_no_notes_dir()
    test_notes_counts_total()
    test_notes_recent_7d_count()
    test_notes_extracts_tags()
    test_notes_top_tags_limit_5()
    print("=" * 40)
    print(f"Results: {PASS} passed, {FAIL} failed")
    sys.exit(0 if FAIL == 0 else 1)
