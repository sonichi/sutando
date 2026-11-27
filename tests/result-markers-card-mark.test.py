"""A result body that is only a card mark is closed silently by the bridge.

`[model-card]` and `[account-card]`, written alone as a result, ask the connector for that card; a
connector shows it and answers with its own no-send line. Should a body still reach the bridge as
the bare mark (a connector without the card, a path that missed it), the bridge must not post the
mark into the room as text. Carried over from #6685, whose other changes main already has.

Run:
    python3 tests/result-markers-card-mark.test.py
"""
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))
from result_markers import parse_markers  # noqa: E402


class CardMarkIsSilent(unittest.TestCase):
    def test_a_body_that_is_only_a_card_mark_is_a_skip(self):
        for mark in ("[model-card]", "[account-card]", "  [account-card]\n", "\n[model-card]  "):
            with self.subTest(mark=mark):
                r = parse_markers(mark)
                self.assertEqual([a.kind for a in r.actions], ["skip"])
                self.assertEqual(r.actions[0].value, "no-send")

    def test_a_mark_with_anything_else_is_an_ordinary_reply(self):
        for body in ("[model-card] and here is why", "Use [account-card] to switch", "[MODEL-CARD]",
                     "[other-card]", "I sent the [model-card]"):
            with self.subTest(body=body):
                self.assertFalse(any(a.kind == "skip" for a in parse_markers(body).actions), body)


if __name__ == "__main__":
    unittest.main()
