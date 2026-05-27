"""Tests for dashboard.py Outbox tab — Phase B-2 of issue #932.

Run: python3 tests/dashboard-outbox.test.py
"""
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import outbox_log  # noqa: E402
import dashboard   # noqa: E402


def _write_outbox(workspace: Path, entries: list[dict]) -> None:
    state = workspace / "state"
    state.mkdir(parents=True, exist_ok=True)
    (state / "outbox.log").write_text(
        "\n".join(json.dumps(e) for e in entries) + "\n",
        encoding="utf-8",
    )


class TestGetOutbox(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.workspace = Path(self.tmp.name)
        os.environ["SUTANDO_WORKSPACE"] = str(self.workspace)

    def tearDown(self):
        os.environ.pop("SUTANDO_WORKSPACE", None)
        self.tmp.cleanup()

    def test_returns_empty_list_when_no_log(self):
        self.assertEqual(dashboard.get_outbox(), [])

    def test_returns_newest_first(self):
        now = time.time()
        _write_outbox(self.workspace, [
            {"ts": now - 100, "iso_ts": "2026-05-27T01:00:00Z", "channel_type": "email", "recipient": "a@b.com", "body_preview": "older", "body_len": 5},
            {"ts": now, "iso_ts": "2026-05-27T02:00:00Z", "channel_type": "x_twitter", "recipient": "broadcast", "body_preview": "newer", "body_len": 5},
        ])
        entries = dashboard.get_outbox(limit=20)
        self.assertEqual(len(entries), 2)
        self.assertEqual(entries[0]["channel_type"], "x_twitter")  # newest first
        self.assertEqual(entries[1]["channel_type"], "email")

    def test_respects_limit(self):
        now = time.time()
        _write_outbox(self.workspace, [
            {"ts": now + i, "iso_ts": "2026-05-27T00:00:00Z", "channel_type": "discord_dm",
             "recipient": f"user{i}", "body_preview": "hi", "body_len": 2}
            for i in range(30)
        ])
        entries = dashboard.get_outbox(limit=5)
        self.assertLessEqual(len(entries), 5)


class TestRenderOutboxCard(unittest.TestCase):
    def test_empty_state_message(self):
        html = dashboard.render_outbox_card([])
        self.assertIn("No outbound messages logged yet", html)
        self.assertIn("card full", html)

    def test_renders_table_with_entries(self):
        entries = [
            {"ts": 1.0, "iso_ts": "2026-05-27T03:15:00Z", "channel_type": "x_twitter",
             "recipient": "broadcast", "body_preview": "Tweet content", "body_len": 13},
            {"ts": 0.9, "iso_ts": "2026-05-27T03:10:00Z", "channel_type": "email",
             "recipient": "alice@example.com", "body_preview": "Email body", "body_len": 10},
        ]
        html = dashboard.render_outbox_card(entries)
        self.assertIn("<table", html)
        self.assertIn("x/twitter", html)
        self.assertIn("email", html)
        self.assertIn("03:15", html)
        self.assertIn("Tweet content", html)

    def test_channel_type_label_mapping(self):
        for ch_type, expected_label in dashboard._CHANNEL_LABELS.items():
            html = dashboard.render_outbox_card([
                {"ts": 1.0, "iso_ts": "2026-05-27T00:00:00Z", "channel_type": ch_type,
                 "recipient": "r", "body_preview": "p", "body_len": 1}
            ])
            self.assertIn(expected_label, html, f"label missing for {ch_type}")

    def test_long_recipient_truncated(self):
        long_id = "1" * 35
        html = dashboard.render_outbox_card([
            {"ts": 1.0, "iso_ts": "2026-05-27T00:00:00Z", "channel_type": "discord_dm",
             "recipient": long_id, "body_preview": "hi", "body_len": 2}
        ])
        # Truncated form should appear, not the full 35-char string
        self.assertNotIn(long_id, html)
        self.assertIn("..", html)

    def test_uses_recipient_label_over_recipient_id(self):
        html = dashboard.render_outbox_card([
            {"ts": 1.0, "iso_ts": "2026-05-27T00:00:00Z", "channel_type": "discord_dm",
             "recipient": "9999999", "recipient_label": "Alice (owner) DM",
             "body_preview": "hi", "body_len": 2}
        ])
        self.assertIn("Alice (owner) DM", html)
        self.assertNotIn("9999999", html)

    def test_long_preview_truncated(self):
        long_preview = "A" * 80
        html = dashboard.render_outbox_card([
            {"ts": 1.0, "iso_ts": "2026-05-27T00:00:00Z", "channel_type": "email",
             "recipient": "bob@x.com", "body_preview": long_preview, "body_len": 80}
        ])
        self.assertNotIn(long_preview, html)
        self.assertIn("..", html)


if __name__ == "__main__":
    unittest.main()
