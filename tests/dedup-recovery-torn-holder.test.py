#!/usr/bin/env python3
"""A holder result that is mid-write is RETRYABLE, not missing.

Reverses this file's earlier contract at the reviewer's request on #3317. It
asserted torn == absent ("a half-written file is not evidence"), which takes a
terminal decision against an answer that may land moments later.
"""
import pathlib
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "src"))
from dedup_recovery import plan_dedup_recovery  # noqa: E402

HOLDER = "task-holder0000001"


class TornHolderTest(unittest.TestCase):
    def plan(self, write):
        d = pathlib.Path(tempfile.mkdtemp())
        results, tasks = d / "results", d / "tasks"
        results.mkdir(), tasks.mkdir()
        write(results)
        return plan_dedup_recovery(results, tasks, "task-orig00000001", HOLDER,
                                   "chan", "task-newid00000001")

    def test_a_torn_holder_defers_rather_than_deciding(self):
        torn = self.plan(lambda r: (r / f"{HOLDER}.txt").write_bytes(
            b"task: holder0000001\n\xff\xfe partial"))
        skip = self.plan(lambda r: (r / f"{HOLDER}.txt").write_text(
            "task: holder0000001\n[no-send]\n"))
        self.assertNotEqual(torn[0], skip[0])
        self.assertEqual(torn, ("defer", None))

    def test_a_torn_holder_does_NOT_match_an_absent_one(self):
        # The reversal: absent means nothing is coming, torn means something is.
        torn = self.plan(lambda r: (r / f"{HOLDER}.txt").write_bytes(b"\xff\xfe"))
        absent = self.plan(lambda r: None)
        self.assertEqual(torn[0], "defer")
        self.assertNotEqual(torn, absent)

    def test_a_whole_skip_is_still_honoured(self):
        # Control: the fix must not turn every holder into a report.
        skip = self.plan(lambda r: (r / f"{HOLDER}.txt").write_text(
            "task: holder0000001\n[no-send]\n"))
        self.assertEqual(skip[0], "honour")


if __name__ == "__main__":
    unittest.main(verbosity=2)
