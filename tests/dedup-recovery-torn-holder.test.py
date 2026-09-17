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
            b"a partial answer \xe2\x9c"))
        skip = self.plan(lambda r: (r / f"{HOLDER}.txt").write_text(
            "[no-send]\n"))
        self.assertNotEqual(torn[0], skip[0])
        self.assertEqual(torn, ("defer", None))

    def test_a_torn_holder_does_NOT_match_an_absent_one(self):
        # The reversal: absent means nothing is coming, torn means something is.
        torn = self.plan(lambda r: (r / f"{HOLDER}.txt").write_bytes(b"\xe2\x9c"))
        absent = self.plan(lambda r: None)
        self.assertEqual(torn[0], "defer")
        self.assertNotEqual(torn, absent)

    def test_a_valid_skip_is_NOT_honoured(self):
        # The marker must start at byte zero or it never parses. A holder whose
        # result IS a skip produced no reply, so honouring it would be wrong.
        skip = self.plan(lambda r: (r / f"{HOLDER}.txt").write_text("[no-send]\n"))
        self.assertNotEqual(skip[0], "honour")
        self.assertNotEqual(skip[0], "defer")

    def test_a_real_answer_IS_honoured(self):
        # The genuine honour control, on the ordinary path.
        ans = self.plan(lambda r: (r / f"{HOLDER}.txt").write_text("the real answer\n"))
        self.assertEqual(ans[0], "honour")


if __name__ == "__main__":
    unittest.main(verbosity=2)
