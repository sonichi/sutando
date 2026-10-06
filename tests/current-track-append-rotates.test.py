#!/usr/bin/env python3
"""append() must rotate when its own write crosses the read budget.

The head is read first by every proactive pass, so an append that pushes it over
budget costs every pass until some later probe warns and someone rotates by hand.
Measured twice on one host in three passes: an entry ABOUT the budget crossed it.

Each case here failed against the parent revision.
"""
import importlib.util
import sys
import unittest
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))
SPEC = importlib.util.spec_from_file_location("current_track", SRC / "current_track.py")
ct = importlib.util.module_from_spec(SPEC)
sys.modules["current_track"] = ct
SPEC.loader.exec_module(ct)

import tempfile  # noqa: E402


def entry(tag: str, filler: int = 0, stamp: str = "") -> str:
    """A real entry's heading carries a date stamp, and plan() reads AGE from it.

    Without one, _orientation() is undetermined and both ends are protected on
    purpose — so a tagless fixture archives the MIDDLE and pins nothing about
    oldest-first. Stamp the headings the way every real writer does.
    """
    head = f"## {stamp} — {tag}" if stamp else f"## {tag}"
    return f"{head}\n\n" + ("x" * filler) + "\n"


class AppendRotates(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.head = Path(self.tmp.name) / "current-track.md"
        self.arch = Path(self.tmp.name) / "current-track-archive.md"
        self.addCleanup(self.tmp.cleanup)

    def test_append_under_budget_does_not_rotate(self):
        self.head.write_text("preamble\n" + entry("old", 100))
        r = ct.append(self.head, entry("new", 100), keep_bytes=8192)
        self.assertIsNone(r)
        self.assertFalse(self.arch.exists(), "nothing should be archived under budget")
        self.assertIn("## old", self.head.read_text())
        self.assertIn("## new", self.head.read_text())
        self.assertLessEqual(len(self.head.read_text().encode()), 8192)

    def test_append_that_crosses_the_budget_rotates_in_the_same_call(self):
        # Two old entries plus the new one exceed 4096; the oldest must move. Stamped
        # ascending, so orientation is newest-LAST exactly as the append-ordered file is.
        self.head.write_text("preamble\n"
                             + entry("oldest", 1500, "2026-09-01T00:00Z")
                             + entry("middle", 1500, "2026-09-02T00:00Z"))
        r = ct.append(self.head, entry("newest", 1500, "2026-09-03T00:00Z"), keep_bytes=4096)
        self.assertIsNotNone(r, "crossing the budget must rotate")
        head = self.head.read_text()
        self.assertLessEqual(len(head.encode()), 4096, head[:200])
        # Match the TAG, not a "## tag" prefix: a real heading carries its stamp first.
        self.assertIn("newest", head, "the entry just written must survive in the head")
        self.assertIn("oldest", self.arch.read_text(), "the oldest must be in the archive")
        self.assertNotIn("oldest", head, "the archived entry must leave the head")
        # Nothing deleted: head + archive still holds every entry.
        both = head + self.arch.read_text()
        for tag in ("oldest", "middle", "newest"):
            self.assertIn(tag, both, tag)

    def test_auto_rotate_false_appends_without_rotating(self):
        self.head.write_text("preamble\n"
                             + entry("oldest", 1500, "2026-09-01T00:00Z")
                             + entry("middle", 1500, "2026-09-02T00:00Z"))
        r = ct.append(self.head, entry("newest", 1500, "2026-09-03T00:00Z"),
                      keep_bytes=4096, auto_rotate=False)
        self.assertIsNone(r)
        self.assertFalse(self.arch.exists())
        self.assertGreater(len(self.head.read_text().encode()), 4096)

    def test_pinned_head_that_cannot_fit_still_appends_and_loses_nothing(self):
        # A head made entirely of holds cannot be rotated under budget; the append
        # must still land rather than raise, and the result says it is oversized.
        self.head.write_text("preamble\n"
                             + entry("HOLD one — in force until superseded", 1500)
                             + entry("HOLD two — in force until superseded", 1500))
        r = ct.append(self.head, entry("HOLD three — in force until superseded", 1500),
                      keep_bytes=4096)
        self.assertIsNotNone(r)
        self.assertTrue(r.oversized, "a pin-only head cannot get under budget")
        head = self.head.read_text()
        for tag in ("HOLD one", "HOLD two", "HOLD three"):
            self.assertIn(tag, head, f"{tag} is pinned and must stay in the head")

    def test_default_signature_keeps_the_head_under_the_real_budget(self):
        # Red at the parent on BEHAVIOUR, not on a signature: the old two-arg call,
        # asking only that the head end up under DEFAULT_KEEP.
        big = "".join(entry(f"e{i}", 1200, f"2026-09-{i + 1:02d}T00:00Z") for i in range(30))
        self.head.write_text("preamble\n" + big)
        self.assertGreater(len(self.head.read_text().encode()), ct.DEFAULT_KEEP,
                           "fixture must start OVER budget or it proves nothing")
        ct.append(self.head, entry("fresh", 50, "2026-10-01T00:00Z"))
        self.assertLessEqual(len(self.head.read_text().encode()), ct.DEFAULT_KEEP)
        self.assertIn("fresh", self.head.read_text())

    def test_append_does_not_deadlock_on_the_writer_lock(self):
        # locked() is not reentrant, so nesting rotate() inside append()'s lock HANGS
        # rather than failing: this case completing IS the assertion.
        self.head.write_text("preamble\n" + entry("a", 3000, "2026-09-01T00:00Z"))
        ct.append(self.head, entry("b", 3000, "2026-09-02T00:00Z"), keep_bytes=4096)
        self.assertIn("— b", self.head.read_text())
        # And the lock is released afterwards, so a second write still works.
        ct.append(self.head, entry("c", 10, "2026-09-03T00:00Z"), keep_bytes=4096)
        self.assertIn("— c", self.head.read_text())


if __name__ == "__main__":
    unittest.main(verbosity=1)
