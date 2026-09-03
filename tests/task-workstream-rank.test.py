"""The workstream ranker must REFUSE on a tie rather than pick by list order.

Regression: the ranking was re-derived by the classifier each pass instead of
living in code. On a three-way tie it fell back to insertion order — the exact
arbitrary pick scoring was meant to remove — while printing a shortlist that
made the choice look deliberate.
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "skills" / "task-workstream-grouping" / "scripts"))

from rank_workstreams import best_match, score  # noqa: E402

KW = ["thread", "cinny", "panel"]


class RankWorkstreams(unittest.TestCase):
    def test_refuses_on_a_tie(self):
        """The live failure: three candidates at an identical score."""
        tie = [(cid, "thread cinny panel") for cid in ("a", "b", "c")]
        self.assertIsNone(best_match(tie, KW))

    def test_tie_refusal_is_not_order_dependent(self):
        """Reversing the input must not change the answer — that is what an
        insertion-order fallback would fail."""
        tie = [(cid, "thread cinny panel") for cid in ("a", "b", "c")]
        self.assertEqual(best_match(tie, KW), best_match(list(reversed(tie)), KW))

    def test_picks_on_a_clear_margin(self):
        """Control: a guard that never picks would pass every refusal test."""
        clear = [("winner", "cinny thread panel cinny thread panel"), ("other", "thread")]
        self.assertEqual(best_match(clear, KW), "winner")

    def test_margin_is_the_discriminator(self):
        """One point apart is a tie for this purpose; the default margin is 2."""
        near = [("a", "thread cinny"), ("b", "thread")]
        self.assertIsNone(best_match(near, KW))
        self.assertEqual(best_match(near, KW, min_margin=1), "a")

    def test_no_match_and_empty_both_refuse(self):
        self.assertIsNone(best_match([], KW))
        self.assertIsNone(best_match([("x", "nothing relevant here")], KW))

    def test_score_counts_occurrences_case_insensitively(self):
        self.assertEqual(score("Thread THREAD thread", ["thread"]), 3)


if __name__ == "__main__":
    unittest.main()
