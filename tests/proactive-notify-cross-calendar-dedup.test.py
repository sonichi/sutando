#!/usr/bin/env python3
"""
Regression guard: proactive-notify runner must dedup cross-calendar duplicates
before the match phase so the owner gets one notification per meeting, not one
per calendar subscription.

Background (sonichi/sutando#966): gws calendar +agenda lists across all
subscribed calendars. An owner with 7 team calendars gets 7 identical candidates
for "Daily Standup at 12:00 PDT" — one per calendar. Before the fix, each passed
the match phase independently and generated a separate delivery.

Guards:
  1. Identical dedup_key_suffix → one candidate survives, duplicates dropped
  2. Different dedup_key_suffix → both candidates survive (distinct events)
  3. Empty dedup_key_suffix → all pass through (can't dedup without a key)
  4. Mixed: titled + untitled → each group dedups independently
  5. Structural: dedup happens before match loop (order-of-operations)
  6. Structural: dedup comment mentions cross-calendar rationale

Run: python3 tests/proactive-notify-cross-calendar-dedup.test.py
Exit: 0 on pass, 1 on fail.
"""

from __future__ import annotations

import sys
import unittest
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parent.parent
SKILL_SCRIPTS = ROOT / "skills" / "proactive-notify" / "scripts"
RUNNER_SRC = ROOT / "skills" / "proactive-notify" / "scripts" / "runner.py"
sys.path.insert(0, str(SKILL_SCRIPTS))

from runner import _process_ping  # noqa: E402

SOURCE = RUNNER_SRC.read_text(encoding="utf-8")


def _event(title: str, minutes: int = 5, calendar: str = "primary") -> dict:
    start = (datetime.now(timezone.utc) + timedelta(minutes=minutes)).isoformat()
    suffix = f"{start}|{title}" if title else ""
    return {
        "title": title,
        "start_iso": start,
        "start_local": "12:00 PDT",
        "minutes_until": minutes,
        "calendar": calendar,
        "location": "",
        "description": "",
        "dedup_key_suffix": suffix,
    }


def _make_spec(name: str = "test") -> dict:
    return {
        "name": name,
        "source": "google_calendar",
        "body_template": "Meeting in {minutes_until} min: {title}",
        "urgency": "important",
    }


def _fake_policy() -> dict:
    return {
        "default_channel": {"critical": "call", "important": "queue", "fyi": "queue"},
        "overrides": [],
    }


class FakePresence:
    presenter_mode_active = False
    voice_client_connected = False
    owner_active_in_discord_within_min_5 = False
    in_quiet_hours = False


class TestCrossCalendarDedup(unittest.TestCase):
    def _run(self, candidates, spec=None, live=False):
        spec = spec or _make_spec()
        with patch("runner.importlib.import_module") as mock_import:
            mock_source = MagicMock()
            mock_source.fetch.return_value = candidates
            mock_import.return_value = mock_source
            return _process_ping(spec, {}, FakePresence(), _fake_policy(), live=live)

    def test_duplicate_events_collapsed_to_one(self):
        """7 calendar copies of the same event → exactly 1 notification candidate."""
        ev = _event("Daily Standup")
        duplicates = [dict(ev, calendar=f"cal-{i}") for i in range(7)]
        records = self._run(duplicates)
        self.assertEqual(len(records), 1, (
            f"Expected 1 record for 7 identical-suffix events, got {len(records)}. "
            "Cross-calendar dedup not working. See sonichi/sutando#966."
        ))

    def test_distinct_events_both_survive(self):
        """Two different events at different times → both produce candidates."""
        ev1 = _event("Standup", minutes=5)
        ev2 = _event("1:1 with boss", minutes=10)
        records = self._run([ev1, ev2])
        self.assertEqual(len(records), 2, "Two distinct events must each produce a record")

    def test_empty_dedup_key_passes_through(self):
        """Items with no dedup_key_suffix all pass through unchanged."""
        ev1 = dict(_event(""), dedup_key_suffix="")
        ev2 = dict(_event(""), dedup_key_suffix="")
        records = self._run([ev1, ev2])
        self.assertEqual(len(records), 2, "Items with empty suffix must all pass through")

    def test_mixed_titled_and_unkeyable_events(self):
        """One titled event + one unkeyable event → both produce records."""
        titled = _event("Standup")
        nokey = dict(_event("Other"), dedup_key_suffix="")
        records = self._run([titled, titled, nokey])  # titled appears twice
        self.assertEqual(len(records), 2, (
            "Should get 1 record for the duped titled event + 1 for the unkeyable event"
        ))

    def test_dedup_precedes_match_loop_structurally(self):
        """dedup block must appear before `for item in candidates:` in source."""
        dedup_pos = SOURCE.find("_seen_suffixes")
        match_loop_pos = SOURCE.find("for item in candidates:")
        self.assertGreater(dedup_pos, 0, "_seen_suffixes dedup variable not found in runner.py")
        self.assertGreater(match_loop_pos, 0, "`for item in candidates:` not found in runner.py")
        self.assertLess(dedup_pos, match_loop_pos, (
            "Dedup block must appear BEFORE `for item in candidates:` loop"
        ))

    def test_cross_calendar_comment_present(self):
        """Runner source must contain a comment explaining the cross-calendar rationale."""
        self.assertIn("Cross-calendar dedup", SOURCE, (
            "runner.py must have a comment explaining the cross-calendar dedup rationale"
        ))


def main():
    loader = unittest.TestLoader()
    suite = loader.loadTestsFromTestCase(TestCrossCalendarDedup)
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    sys.exit(0 if result.wasSuccessful() else 1)


if __name__ == "__main__":
    main()
