#!/usr/bin/env python3
"""morning-briefing.py speaks the pending-question line without a ranking claim.
(Which questions are waiting is the reader's business — see
tests/pending-questions-core-readers-delegate.test.py.)
"""
import importlib.util
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "src" / "morning-briefing.py"


def _load():
    # src/ is on the path so the module's `from util_paths import …` resolves.
    sys.path.insert(0, str(REPO / "src"))
    spec = importlib.util.spec_from_file_location("morning_briefing", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class SpokenQuestionLineMakesNoRankingClaim(unittest.TestCase):
    """The one question the briefing speaks is index 0 of an UNRANKED list.

    The reader yields its own order. Calling that "Top item" states a
    priority the code never computed, and only this one line is ever spoken — so a
    high-urgency question below index 0 is both unspoken and implicitly outranked.
    """

    def test_the_line_does_not_call_index_zero_the_top_item(self) -> None:
        mod = _load()
        line = mod.synthesize(None, [], [], [], ["first filed", "urgent but later"], None)
        self.assertIn("2 pending questions", line)
        self.assertIn("first filed", line)
        self.assertNotIn("Top item", line)

    def test_a_single_question_still_reads_naturally(self) -> None:
        mod = _load()
        line = mod.synthesize(None, [], [], [], ["only one"], None)
        self.assertIn("One pending question waiting: only one", line)
        self.assertNotIn("Top item", line)


if __name__ == "__main__":
    unittest.main()
