#!/usr/bin/env python3
"""Tests for skills/ea-scheduling/scripts/handle-meeting-requests.py

Tests pure-function logic: request detection, slot formatting, reply drafting, dedup state.
No live Gmail or Calendar calls — all subprocess-stubbed.

Run: python3 tests/ea-scheduling.test.py
Exit: 0 on pass, 1 on fail.
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch, MagicMock

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "skills" / "ea-scheduling" / "scripts" / "handle-meeting-requests.py"

spec = importlib.util.spec_from_file_location("ea_sched", SCRIPT)
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


class TestRequestDetection(unittest.TestCase):
    def test_schedule_a_call(self):
        self.assertTrue(m._is_meeting_request("Would love to schedule a call with you"))

    def test_find_time_to_meet(self):
        self.assertTrue(m._is_meeting_request("Let's find a time to meet this week"))

    def test_are_you_available(self):
        self.assertTrue(m._is_meeting_request("Are you available for a 30 min chat?"))

    def test_calendly_link(self):
        self.assertTrue(m._is_meeting_request("Book here: calendly.com/bassil/30min"))

    def test_keen_to_connect(self):
        self.assertTrue(m._is_meeting_request("I'm keen to connect for a quick intro call"))

    def test_plain_non_request(self):
        self.assertFalse(m._is_meeting_request("Thanks for the deck, looks great!"))

    def test_empty_string(self):
        self.assertFalse(m._is_meeting_request(""))

    def test_unrelated_email(self):
        self.assertFalse(m._is_meeting_request("Please find attached the invoice #1234"))


class TestSenderEmailExtraction(unittest.TestCase):
    def test_name_angle_format(self):
        self.assertEqual(m.get_sender_email("Chi Wang <chi@ag2.ai>"), "chi@ag2.ai")

    def test_plain_email(self):
        self.assertEqual(m.get_sender_email("someone@example.com"), "someone@example.com")

    def test_trailing_whitespace(self):
        self.assertEqual(m.get_sender_email("  user@test.com  "), "user@test.com")


class TestSlotFormatting(unittest.TestCase):
    def test_format_slot_structure(self):
        # Tuesday 27 May 2026 10:00 UTC = 14:00 Dubai (+4)
        start = datetime(2026, 5, 26, 10, 0, tzinfo=timezone.utc)
        end = start + timedelta(minutes=30)
        result = m.format_slot(start, end)
        self.assertIn("Dubai", result)
        self.assertIn("UTC+4", result)
        self.assertIn("May", result)

    def test_format_slot_duration_visible(self):
        start = datetime(2026, 5, 26, 9, 0, tzinfo=timezone.utc)
        end = start + timedelta(minutes=30)
        result = m.format_slot(start, end)
        # Should show two times (start–end)
        self.assertIn("–", result)


class TestReplyDrafting(unittest.TestCase):
    def setUp(self):
        self.request = {
            "from": "Recruiter Name <recruiter@bigfour.com>",
            "subject": "Coffee chat?",
            "snippet": "Would love to find time to connect",
        }
        self.slots = [
            (datetime(2026, 5, 26, 6, 0, tzinfo=timezone.utc),
             datetime(2026, 5, 26, 6, 30, tzinfo=timezone.utc)),
        ]

    def test_reply_contains_sender_first_name(self):
        reply = m.draft_reply(self.request, self.slots)
        self.assertIn("Recruiter", reply)

    def test_reply_contains_slot(self):
        reply = m.draft_reply(self.request, self.slots)
        self.assertIn("May", reply)

    def test_reply_signed_bassil(self):
        reply = m.draft_reply(self.request, self.slots)
        self.assertIn("Bassil", reply)

    def test_reply_no_slots_fallback(self):
        reply = m.draft_reply(self.request, [])
        self.assertIn("propose a time", reply)


class TestDedupState(unittest.TestCase):
    def test_load_empty(self):
        with tempfile.TemporaryDirectory() as td:
            fake_path = Path(td) / "state" / "ea-handled-ids.json"
            with patch.object(m, "DEDUP_STATE", fake_path):
                result = m._load_handled()
            self.assertEqual(result, set())

    def test_save_and_reload(self):
        with tempfile.TemporaryDirectory() as td:
            fake_path = Path(td) / "state" / "ea-handled-ids.json"
            with patch.object(m, "DEDUP_STATE", fake_path):
                m._save_handled({"msg_001", "msg_002"})
                reloaded = m._load_handled()
            self.assertEqual(reloaded, {"msg_001", "msg_002"})

    def test_corrupt_file_returns_empty(self):
        with tempfile.TemporaryDirectory() as td:
            fake_path = Path(td) / "state" / "ea-handled-ids.json"
            fake_path.parent.mkdir(parents=True, exist_ok=True)
            fake_path.write_text("NOT JSON{{")
            with patch.object(m, "DEDUP_STATE", fake_path):
                result = m._load_handled()
            self.assertEqual(result, set())


class TestSkillMd(unittest.TestCase):
    def test_skill_md_exists(self):
        skill_md = ROOT / "skills" / "ea-scheduling" / "SKILL.md"
        self.assertTrue(skill_md.exists(), "SKILL.md missing")

    def test_skill_md_documents_send_flag(self):
        skill_md = ROOT / "skills" / "ea-scheduling" / "SKILL.md"
        content = skill_md.read_text()
        self.assertIn("--send", content)

    def test_skill_md_documents_dry_run(self):
        skill_md = ROOT / "skills" / "ea-scheduling" / "SKILL.md"
        content = skill_md.read_text()
        self.assertIn("dry-run", content.lower())


if __name__ == "__main__":
    unittest.main(verbosity=2)
