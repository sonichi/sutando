"""Local run ids (`src/local_task_protocol.py`): `run_task_id` and `split_run_id` are the one
constructor and parser of `<local id>~~<n>`, and `valid_local_task_id` accepts a run of a valid id only.

Run:
    python3 tests/local-task-run-ids.test.py
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import local_task_protocol as ltp  # noqa: E402


class RunIds(unittest.TestCase):
    def test_a_run_id_round_trips_and_the_first_run_is_the_bare_id(self):
        for base, seq, local in (("task-a", 1, "task-a"), ("task-a", 2, "task-a~~2"),
                                 ("task-dev~ask-1.x", 17, "task-dev~ask-1.x~~17")):
            self.assertEqual(ltp.run_task_id(base, seq), local)
            self.assertEqual(ltp.split_run_id(local), (base, seq))

    def test_a_malformed_run_is_refused_by_the_constructor_the_parser_and_the_validator(self):
        for base, seq in (("task-a~~2", 3), ("task-a", 0), ("task-a", True), ("task-a", 10 ** 7)):
            with self.assertRaises(ValueError):
                ltp.run_task_id(base, seq)
        for bad in ("task-a~~1", "task-a~~01", "task-a~~", "~~3", "task-a~~2~~3", "task-a~~x"):
            with self.assertRaises(ValueError):
                ltp.split_run_id(bad)
            self.assertFalse(ltp.valid_local_task_id(bad), bad)
        self.assertTrue(ltp.valid_local_task_id("task-dev~ask-1~~2"))
        self.assertFalse(ltp.valid_local_task_id("task-dev~../x~~2"), "a run of an invalid id is invalid")
        self.assertFalse(ltp.valid_local_task_id(None))


if __name__ == "__main__":
    unittest.main()
