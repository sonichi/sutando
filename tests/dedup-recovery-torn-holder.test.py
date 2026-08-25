#!/usr/bin/env python3
"""A holder result that is mid-write must not read as a deliberate skip.

Strict read raised UnicodeDecodeError (a ValueError, so it escaped
`except OSError`); the caller then retired the original owner request.
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

    def test_a_torn_holder_does_not_read_as_a_skip(self):
        torn = self.plan(lambda r: (r / f"{HOLDER}.txt").write_bytes(
            b"task: holder0000001\n\xff\xfe partial"))
        skip = self.plan(lambda r: (r / f"{HOLDER}.txt").write_text(
            "task: holder0000001\n[no-send]\n"))
        self.assertNotEqual(torn[0], skip[0])
        self.assertEqual(torn[0], "report")

    def test_a_torn_holder_matches_an_absent_one(self):
        # Unreadable is unreadable; a half-written file is not evidence.
        torn = self.plan(lambda r: (r / f"{HOLDER}.txt").write_bytes(b"\xff\xfe"))
        absent = self.plan(lambda r: None)
        self.assertEqual(torn, absent)

    def test_a_whole_skip_is_still_honoured(self):
        # Control: the fix must not turn every holder into a report.
        skip = self.plan(lambda r: (r / f"{HOLDER}.txt").write_text(
            "task: holder0000001\n[no-send]\n"))
        self.assertEqual(skip[0], "honour")


if __name__ == "__main__":
    unittest.main(verbosity=2)
