"""Pins best_match's input contract. The dict-shape guard exists because a
2-key dict silently unpacks to its KEY STRINGS, scoring 0 for every candidate
— a whole night of None verdicts (2026-09-02).

Lives under tests/ because CI discovers Python tests with
`find tests -name '*.test.py'` (tests/ci-covers-every-python-test.test.py).
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "skills" / "task-workstream-grouping" / "scripts"))

from rank_workstreams import best_match  # noqa: E402


class BestMatchContract(unittest.TestCase):
    def test_tuples_rank(self):
        got = best_match(
            [("a", "agent settings design iteration"), ("b", "unrelated stream")],
            ["agent", "settings", "design"],
        )
        self.assertEqual(got, "a")

    def test_dicts_refused_loudly(self):
        with self.assertRaises(TypeError):
            best_match([{"id": "a", "text": "agent settings design"}], ["agent"])

    def test_a_dict_after_a_tuple_is_refused_too(self):
        # keweichen on #3758: a candidate-zero check let a later dict score its
        # keys silently (-> None) or return a phantom key id (-> "id").
        mixed = [("real", "zzz"), {"id": "b", "text": "agent settings"}]
        with self.assertRaises(TypeError):
            best_match(mixed, ["agent"])
        with self.assertRaises(TypeError):
            best_match(mixed, ["text", "ext"])

    def test_accepts_a_generator(self):
        # `candidates = list(candidates)` is load-bearing: the guard indexes
        # candidates[0], and a generator would raise the guard's own TypeError.
        got = best_match((c for c in [("a", "agent settings"), ("b", "zzz")]), ["agent", "settings"])
        self.assertEqual(got, "a")

    def test_margin_tie_returns_none(self):
        got = best_match(
            [("a", "agent settings"), ("b", "agent settings")],
            ["agent", "settings"],
        )
        self.assertIsNone(got)

    def test_no_score_returns_none(self):
        self.assertIsNone(best_match([("a", "zzz")], ["agent"]))


if __name__ == "__main__":
    unittest.main()
