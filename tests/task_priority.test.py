"""Tests for task_priority.py — is_valid_priority, default_priority_for_source,
parse_priority_from_text, parse_priority_from_file, sort_tasks_by_priority."""
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
from task_priority import (
    default_priority_for_source,
    is_valid_priority,
    parse_priority_from_file,
    parse_priority_from_text,
    sort_tasks_by_priority,
)


class TestIsValidPriority(unittest.TestCase):
    def test_urgent_valid(self):
        self.assertTrue(is_valid_priority("urgent"))

    def test_normal_valid(self):
        self.assertTrue(is_valid_priority("normal"))

    def test_low_valid(self):
        self.assertTrue(is_valid_priority("low"))

    def test_unknown_string_invalid(self):
        self.assertFalse(is_valid_priority("high"))

    def test_empty_string_invalid(self):
        self.assertFalse(is_valid_priority(""))

    def test_case_sensitive(self):
        self.assertFalse(is_valid_priority("URGENT"))


class TestDefaultPriorityForSource(unittest.TestCase):
    def test_voice_urgent(self):
        self.assertEqual(default_priority_for_source("voice"), "urgent")

    def test_phone_urgent(self):
        self.assertEqual(default_priority_for_source("phone"), "urgent")

    def test_chat_normal(self):
        self.assertEqual(default_priority_for_source("chat"), "normal")

    def test_context_drop_normal(self):
        self.assertEqual(default_priority_for_source("context-drop"), "normal")

    def test_discord_owner_tier_normal(self):
        self.assertEqual(default_priority_for_source("discord", "owner"), "normal")

    def test_discord_team_tier_low(self):
        self.assertEqual(default_priority_for_source("discord", "team"), "low")

    def test_discord_other_tier_low(self):
        self.assertEqual(default_priority_for_source("discord", "other"), "low")

    def test_discord_no_tier_defaults_owner_normal(self):
        self.assertEqual(default_priority_for_source("discord"), "normal")

    def test_telegram_owner_tier_normal(self):
        self.assertEqual(default_priority_for_source("telegram", "owner"), "normal")

    def test_telegram_team_tier_low(self):
        self.assertEqual(default_priority_for_source("telegram", "team"), "low")

    def test_health_check_low(self):
        self.assertEqual(default_priority_for_source("health-check"), "low")

    def test_cron_low(self):
        self.assertEqual(default_priority_for_source("cron"), "low")

    def test_sync_memory_low(self):
        self.assertEqual(default_priority_for_source("sync-memory"), "low")

    def test_unknown_source_normal(self):
        self.assertEqual(default_priority_for_source("unknown-source"), "normal")

    def test_empty_source_normal(self):
        self.assertEqual(default_priority_for_source(""), "normal")

    def test_case_insensitive_source(self):
        self.assertEqual(default_priority_for_source("VOICE"), "urgent")


class TestParsePriorityFromText(unittest.TestCase):
    def test_urgent_header(self):
        content = "id: task-1\npriority: urgent\ntask: do thing"
        self.assertEqual(parse_priority_from_text(content), "urgent")

    def test_normal_header(self):
        content = "id: task-1\npriority: normal\ntask: do thing"
        self.assertEqual(parse_priority_from_text(content), "normal")

    def test_low_header(self):
        content = "id: task-1\npriority: low\ntask: do thing"
        self.assertEqual(parse_priority_from_text(content), "low")

    def test_missing_header_defaults_normal(self):
        content = "id: task-1\ntask: do thing"
        self.assertEqual(parse_priority_from_text(content), "normal")

    def test_malformed_value_defaults_normal(self):
        content = "priority: SUPER-URGENT\ntask: do thing"
        self.assertEqual(parse_priority_from_text(content), "normal")

    def test_case_insensitive_header_key(self):
        content = "PRIORITY: urgent\ntask: do thing"
        self.assertEqual(parse_priority_from_text(content), "urgent")

    def test_case_insensitive_header_value(self):
        content = "priority: URGENT\ntask: do thing"
        self.assertEqual(parse_priority_from_text(content), "urgent")

    def test_stops_at_task_colon_body_injection_blocked(self):
        """priority: after task: must be ignored — body injection vector (PR #982)."""
        content = "id: task-1\ntask: do thing\npriority: urgent"
        self.assertEqual(parse_priority_from_text(content), "normal")

    def test_stops_at_triple_dash(self):
        content = "id: task-1\n---\npriority: urgent"
        self.assertEqual(parse_priority_from_text(content), "normal")

    def test_stops_at_blank_line(self):
        content = "id: task-1\n\npriority: urgent"
        self.assertEqual(parse_priority_from_text(content), "normal")

    def test_empty_content_defaults_normal(self):
        self.assertEqual(parse_priority_from_text(""), "normal")

    def test_leading_whitespace_on_header(self):
        content = "  priority: low\ntask: x"
        self.assertEqual(parse_priority_from_text(content), "low")


class TestParsePriorityFromFile(unittest.TestCase):
    def test_reads_priority_from_file(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "task-1.txt"
            p.write_text("id: task-1\npriority: urgent\ntask: go")
            self.assertEqual(parse_priority_from_file(p), "urgent")

    def test_missing_file_returns_normal(self):
        self.assertEqual(parse_priority_from_file(Path("/nonexistent/task.txt")), "normal")

    def test_file_without_priority_header_returns_normal(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "task-2.txt"
            p.write_text("id: task-2\ntask: no priority")
            self.assertEqual(parse_priority_from_file(p), "normal")


class TestSortTasksByPriority(unittest.TestCase):
    def test_urgent_before_normal_before_low(self):
        with tempfile.TemporaryDirectory() as d:
            low = Path(d) / "task-low.txt"
            normal = Path(d) / "task-normal.txt"
            urgent = Path(d) / "task-urgent.txt"
            low.write_text("priority: low\ntask: x")
            normal.write_text("priority: normal\ntask: x")
            urgent.write_text("priority: urgent\ntask: x")
            result = sort_tasks_by_priority([low, normal, urgent])
        self.assertEqual(result[0].name, "task-urgent.txt")
        self.assertEqual(result[1].name, "task-normal.txt")
        self.assertEqual(result[2].name, "task-low.txt")

    def test_fifo_tiebreak_within_same_priority(self):
        with tempfile.TemporaryDirectory() as d:
            older = Path(d) / "task-older.txt"
            newer = Path(d) / "task-newer.txt"
            older.write_text("priority: normal\ntask: x")
            time.sleep(0.05)
            newer.write_text("priority: normal\ntask: x")
            result = sort_tasks_by_priority([newer, older])
        self.assertEqual(result[0].name, "task-older.txt")
        self.assertEqual(result[1].name, "task-newer.txt")

    def test_empty_list_returns_empty(self):
        self.assertEqual(sort_tasks_by_priority([]), [])

    def test_single_item(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "task-1.txt"
            p.write_text("priority: low\ntask: x")
            result = sort_tasks_by_priority([p])
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].name, "task-1.txt")

    def test_missing_priority_header_sorts_as_normal(self):
        with tempfile.TemporaryDirectory() as d:
            urgent = Path(d) / "urgent.txt"
            no_prio = Path(d) / "noprio.txt"
            urgent.write_text("priority: urgent\ntask: x")
            no_prio.write_text("task: no priority header")
            result = sort_tasks_by_priority([no_prio, urgent])
        self.assertEqual(result[0].name, "urgent.txt")
        self.assertEqual(result[1].name, "noprio.txt")


if __name__ == "__main__":
    unittest.main()
