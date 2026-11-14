#!/usr/bin/env python3
"""morning-briefing.py speaks the pending-question COUNT and where to open it — never the
questions (they were sent when asked; a briefing listing them is a scheduled reminder) — and
says "unknown" when the room could not be read, never "none" or "clean"."""
import importlib.util
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "src" / "morning-briefing.py"


def _load():
    sys.path.insert(0, str(REPO / "src"))
    spec = importlib.util.spec_from_file_location("morning_briefing", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class CountAndLinkOnly(unittest.TestCase):
    def test_the_line_is_a_count_with_the_link_and_no_question_text(self):
        mod = _load()
        line = mod.synthesize(None, [], [], [], {"count": 2, "link": "https://r/#/db", "unavailable": False,
                                                 "reason": None}, None)
        self.assertIn("2 pending questions are waiting in your Pending questions database.", line)
        self.assertIn("Open it: https://r/#/db.", line)
        one = mod.synthesize(None, [], [], [], {"count": 1, "link": None, "unavailable": False, "reason": None}, None)
        self.assertIn("One pending question is waiting", one)
        self.assertNotIn("Open it", one)
        self.assertNotIn("Everything looks clean", one)

    def test_zero_is_silent_and_clean_but_unknown_is_said_and_never_clean(self):
        mod = _load()
        zero = mod.synthesize(None, [], [], [], {"count": 0, "link": None, "unavailable": False, "reason": None}, [])
        self.assertNotIn("pending question", zero)
        self.assertIn("Everything looks clean", zero)
        unknown = mod.synthesize(None, [], [], [], {"count": None, "link": None, "unavailable": True,
                                                    "reason": "room down"}, [])
        self.assertIn("Pending questions: unknown — the room was unreachable (room down).", unknown)
        self.assertNotIn("Everything looks clean", unknown)
        self.assertNotIn("0 pending", unknown)

    def test_the_source_does_not_speak_titles_and_no_longer_reads_the_notifier(self):
        src = SCRIPT.read_text()
        self.assertNotIn("pending_qs[0]", src)
        self.assertNotIn("VISIBLE_PREFIX", src)
        self.assertNotIn("check-pending-questions", src)
        self.assertNotIn("below_fold", src)


if __name__ == "__main__":
    unittest.main()
