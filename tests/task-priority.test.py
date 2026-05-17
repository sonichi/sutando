#!/usr/bin/env python3
"""Tests for src/task_priority.py — task priority taxonomy + readers.

Run: python3 tests/task-priority.test.py
Exit: 0 on pass, 1 on fail.
"""
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from task_priority import (  # noqa: E402
    default_priority_for_source,
    is_valid_priority,
    parse_priority_from_file,
    parse_priority_from_text,
    sort_tasks_by_priority,
)


class TestEnumAndDefaults(unittest.TestCase):
    def test_valid_priority_enum(self):
        for v in ("urgent", "normal", "low"):
            self.assertTrue(is_valid_priority(v))
        for v in ("Urgent", "URGENT", "high", "", "lowest", "1"):
            self.assertFalse(is_valid_priority(v))

    def test_voice_phone_default_urgent(self):
        self.assertEqual(default_priority_for_source("voice"), "urgent")
        self.assertEqual(default_priority_for_source("phone"), "urgent")
        # Mixed case + whitespace tolerated.
        self.assertEqual(default_priority_for_source(" Voice "), "urgent")

    def test_chat_context_drop_default_normal(self):
        self.assertEqual(default_priority_for_source("chat"), "normal")
        self.assertEqual(default_priority_for_source("context-drop"), "normal")

    def test_discord_owner_tier_normal_other_tier_low(self):
        self.assertEqual(default_priority_for_source("discord", "owner"), "normal")
        self.assertEqual(default_priority_for_source("discord", "team"), "low")
        self.assertEqual(default_priority_for_source("discord", "other"), "low")
        # Missing access_tier defaults to owner per the helper.
        self.assertEqual(default_priority_for_source("discord", None), "normal")

    def test_telegram_owner_tier_normal_other_tier_low(self):
        self.assertEqual(default_priority_for_source("telegram", "owner"), "normal")
        self.assertEqual(default_priority_for_source("telegram", "team"), "low")

    def test_health_check_and_cron_default_low(self):
        self.assertEqual(default_priority_for_source("health-check"), "low")
        self.assertEqual(default_priority_for_source("sync-memory"), "low")
        self.assertEqual(default_priority_for_source("cron"), "low")

    def test_unknown_source_falls_back_to_normal(self):
        self.assertEqual(default_priority_for_source("future-channel-x"), "normal")
        self.assertEqual(default_priority_for_source(""), "normal")
        self.assertEqual(default_priority_for_source(None), "normal")


class TestParsing(unittest.TestCase):
    def test_parse_priority_from_text_finds_header(self):
        body = (
            "id: task-1\n"
            "timestamp: 2026-05-16T00:00:00Z\n"
            "task: do something\n"
            "source: voice\n"
            "priority: urgent\n"
        )
        self.assertEqual(parse_priority_from_text(body), "urgent")

    def test_parse_priority_missing_returns_normal(self):
        body = "id: task-1\nsource: chat\n"
        self.assertEqual(parse_priority_from_text(body), "normal")

    def test_parse_priority_malformed_value_returns_normal(self):
        body = "id: task-1\npriority: super-urgent\n"
        self.assertEqual(parse_priority_from_text(body), "normal")

    def test_parse_priority_case_insensitive_value(self):
        body = "id: task-1\npriority: URGENT\n"
        self.assertEqual(parse_priority_from_text(body), "urgent")

    def test_parse_priority_stops_at_blank_line(self):
        # Body text containing "priority:" after a blank line MUST be ignored.
        body = (
            "id: task-1\n"
            "task: short\n"
            "\n"
            "...somewhere in the body it mentions priority: urgent...\n"
        )
        # No priority header in the header block — falls back to normal.
        self.assertEqual(parse_priority_from_text(body), "normal")


class TestSorting(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="task-pri-"))

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write(self, name: str, priority: str | None, mtime_offset: float = 0):
        p = self.tmp / name
        if priority is None:
            p.write_text("id: x\ntask: y\n")
        else:
            p.write_text(f"id: x\npriority: {priority}\ntask: y\n")
        if mtime_offset:
            now = time.time() + mtime_offset
            os.utime(p, (now, now))
        return p

    def test_urgent_before_normal_before_low(self):
        low = self._write("a-low.txt", "low")
        urgent = self._write("b-urgent.txt", "urgent")
        normal = self._write("c-normal.txt", "normal")
        out = sort_tasks_by_priority([low, urgent, normal])
        self.assertEqual([p.name for p in out], ["b-urgent.txt", "c-normal.txt", "a-low.txt"])

    def test_missing_priority_treated_as_normal(self):
        no_pri = self._write("a-no-prio.txt", None)
        urgent = self._write("b-urgent.txt", "urgent")
        low = self._write("c-low.txt", "low")
        out = sort_tasks_by_priority([no_pri, urgent, low])
        # urgent first, then no-prio (== normal), then low
        self.assertEqual([p.name for p in out], ["b-urgent.txt", "a-no-prio.txt", "c-low.txt"])

    def test_tiebreak_by_mtime_within_same_priority(self):
        # Both urgent — older mtime should come first (FIFO within tier).
        new_one = self._write("newer.txt", "urgent", mtime_offset=+10)
        old_one = self._write("older.txt", "urgent", mtime_offset=-10)
        out = sort_tasks_by_priority([new_one, old_one])
        self.assertEqual([p.name for p in out], ["older.txt", "newer.txt"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
