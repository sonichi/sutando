"""Pins best_match's input contract. The dict-shape guard exists because a
2-key dict silently unpacks to its KEY STRINGS, scoring 0 for every candidate
— a whole night of None verdicts (2026-09-02)."""
import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).parent))
from rank_workstreams import best_match


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
