#!/usr/bin/env python3
"""A years-overdue reminder must not outrank a real one in the briefing.

`reminders.py --due-today` returns today's AND overdue items by contract, with no
upper bound on how overdue. Live on this host the whole list was:

    [Reminders] How launch (due Monday, January 13, 2020 at 11:52:42 AM)
    [Reminders] 7-minute timer (due Sunday, November 3, 1219 at 12:00:00 AM)

Both rendered every morning as "Reminders due" — implying action on a six-year-old
item and a corrupt date, and holding 2 of the 5 slots permanently.

Demote, never drop: they are the owner's data, and this module already refuses to
hide a calendar it could not read.
"""
from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

REAL = "[Reminders] Call the dentist (due Sunday, August 9, 2026 at 9:00:00 AM)"
JUNK_2020 = "[Reminders] How launch (due Monday, January 13, 2020 at 11:52:42 AM)"
JUNK_1219 = "[Reminders] 7-minute timer (due Sunday, November 3, 1219 at 12:00:00 AM)"
LAST_DEC = "[Reminders] Recent (due Friday, December 5, 2025 at 9:00:00 AM)"


def _load():
    spec = importlib.util.spec_from_file_location(
        "mb", REPO / "src" / "morning-briefing.py")
    m = importlib.util.module_from_spec(spec)
    sys.modules["mb"] = m
    try:
        spec.loader.exec_module(m)
    except SystemExit:
        pass
    return m


class StaleReminders(unittest.TestCase):
    def setUp(self):
        self.mb = _load()

    def test_the_live_junk_pair_is_demoted_below_a_real_item(self):
        out = self.mb._demote_stale_reminders([JUNK_2020, JUNK_1219, REAL])
        self.assertEqual(out[0], REAL, "a real due-today item must come first")

    def test_nothing_is_dropped(self):
        """Demote, not discard — hiding the owner's reminder is the worse failure."""
        items = [JUNK_2020, JUNK_1219, REAL]
        out = self.mb._demote_stale_reminders(items)
        self.assertEqual(sorted(out), sorted(items))

    def test_year_1219_parses(self):
        """The first regex used a 16xx-21xx alternation and missed 1219 — the single
        most obviously-corrupt date on the host went undetected."""
        self.assertEqual(self.mb._reminder_due_year(JUNK_1219), 1219)
        self.assertEqual(self.mb._reminder_due_year(JUNK_2020), 2020)
        self.assertEqual(self.mb._reminder_due_year(REAL), 2026)

    def test_a_recent_overdue_item_is_not_demoted(self):
        """Year granularity means `year_now - 1` would demote a December item in
        January — one month overdue, not a year. The bound must be 2 years."""
        out = self.mb._demote_stale_reminders([LAST_DEC, REAL],
                                             now=self.mb.datetime(2026, 8, 9).timestamp())
        self.assertEqual(out[0], LAST_DEC)

    def test_an_unparseable_date_keeps_its_position(self):
        """A format change must never bury a live reminder."""
        undated = "[Reminders] No date here"
        out = self.mb._demote_stale_reminders([undated, REAL])
        self.assertEqual(out[0], undated)

    def test_empty_and_single_are_safe(self):
        self.assertEqual(self.mb._demote_stale_reminders([]), [])
        self.assertEqual(self.mb._demote_stale_reminders([JUNK_1219]), [JUNK_1219])


if __name__ == "__main__":
    unittest.main(verbosity=2)
