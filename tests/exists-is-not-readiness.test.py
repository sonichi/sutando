#!/usr/bin/env python3
"""Pins `_read_when_nonempty`, the readiness poll for codex-core-launcher.

Every writer in that suite truncates before it writes, so a file exists and is
empty for a moment. #2362 fixed the counter site; #3483 fixes the pid site. This
covers the remaining three, one of which fails toward GREEN:

    assertNotIn("send-keys", "")  -> PASSES

An empty read there reports success without ever having observed the notifier,
so no CI run can ever point at it. That is the case these tests exist for.
"""

import importlib.util
import time
import unittest
from pathlib import Path

LAUNCHER = Path(__file__).resolve().parent.parent / "tests" / "codex-core-launcher.test.py"


def _load():
    spec = importlib.util.spec_from_file_location("_codex_core_launcher", LAUNCHER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class WhyEmptyIsNotAMiss(unittest.TestCase):
    """The premise: "" satisfies the assertions these sites use."""

    def test_assertnotin_passes_against_an_empty_read(self):
        self.assertNotIn("send-keys", "")      # the false green, stated outright

    def test_assertin_fails_against_an_empty_read(self):
        with self.assertRaises(AssertionError):
            self.assertIn("--session sutando-core", "")


class ReadWhenNonemptyTests(unittest.TestCase):
    def setUp(self):
        self.mod = _load()
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "f"

    def test_returns_none_for_a_missing_file(self):
        self.assertIsNone(self.mod._read_when_nonempty(self.path, time.monotonic() + 0.2))

    def test_returns_none_for_an_existing_but_empty_file(self):
        """The defect's exact shape: exists() is true, content is not there."""
        self.path.write_text("")
        self.assertTrue(self.path.exists())
        self.assertIsNone(self.mod._read_when_nonempty(self.path, time.monotonic() + 0.2))

    def test_returns_content_when_present(self):
        self.path.write_text("heartbeat-started")
        self.assertEqual(
            self.mod._read_when_nonempty(self.path, time.monotonic() + 1),
            "heartbeat-started")

    def test_waits_through_the_truncate_window(self):
        import threading
        def run():
            fh = open(self.path, "w")     # exists, empty
            time.sleep(0.25)
            fh.write("late"); fh.close()
        t = threading.Thread(target=run, daemon=True); t.start()
        self.addCleanup(t.join)
        self.assertEqual(
            self.mod._read_when_nonempty(self.path, time.monotonic() + 5), "late")

    def test_a_torn_write_can_still_yield_partial_content(self):
        """Bound of the fix, stated so nobody reads it as more than it is.

        The helper returns the FIRST non-empty read, so a writer that emits in
        chunks can hand back a prefix. Same exposure as the code it replaces --
        not a regression, and not covered.
        """
        import threading
        def run():
            fh = open(self.path, "w")
            fh.write("partial"); fh.flush()
            time.sleep(0.30)
            fh.write("-and-the-rest"); fh.close()
        t = threading.Thread(target=run, daemon=True); t.start()
        self.addCleanup(t.join)
        got = self.mod._read_when_nonempty(self.path, time.monotonic() + 5)
        self.assertEqual(got, "partial")


if __name__ == "__main__":
    unittest.main(verbosity=2)
