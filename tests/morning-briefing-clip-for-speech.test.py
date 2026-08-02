#!/usr/bin/env python3
"""The briefing is spoken, so a clipped question title must not end mid-word.

Regression: `get_pending_questions` used a hard `title[:60]`. On 2026-08-02 that
rendered a real pending question as "... (no urgency; nothing bl" -- cut inside
"blocked" and leaving the parenthetical unclosed. Voice reads that aloud verbatim.
"""
import importlib.util
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load():
    spec = importlib.util.spec_from_file_location("mb", ROOT / "src" / "morning-briefing.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["mb"] = mod
    try:
        spec.loader.exec_module(mod)
    except SystemExit:
        pass
    return mod


mb = _load()
REAL = "WIRE — awaiting your verdict / steer (no urgency; nothing blocked on me)"


class TestClipForSpeech(unittest.TestCase):
    def test_the_2026_08_02_case_no_longer_ends_mid_word(self):
        out = mb.clip_for_speech(REAL, 60)
        self.assertFalse(out.endswith("bl"), out)
        self.assertNotIn("nothing bl", out)

    def test_never_ends_inside_a_word(self):
        out = mb.clip_for_speech(REAL, 60)
        body = out.rstrip("…")
        # every whitespace-separated token that survived must be a whole token
        self.assertTrue(all(tok in REAL.split() for tok in body.split()), out)

    def test_an_unmatched_open_paren_is_dropped_not_spoken_half(self):
        out = mb.clip_for_speech(REAL, 60)
        self.assertEqual(out.count("("), out.count(")"), out)

    def test_result_never_exceeds_the_limit(self):
        for limit in (10, 20, 60, 200):
            for text in (REAL, "A" * 300, "word " * 40):
                self.assertLessEqual(len(mb.clip_for_speech(text, limit)), limit)

    def test_text_that_already_fits_is_returned_unchanged(self):
        self.assertEqual(mb.clip_for_speech("Short title", 60), "Short title")
        exact = "x" * 60
        self.assertEqual(mb.clip_for_speech(exact, 60), exact)

    def test_a_single_unbroken_token_still_clips_rather_than_returning_whole(self):
        out = mb.clip_for_speech("A" * 300, 60)
        self.assertLessEqual(len(out), 60)
        self.assertTrue(out.endswith("…"), out)

    def test_clipping_is_marked_so_a_listener_knows_it_was_cut(self):
        self.assertTrue(mb.clip_for_speech(REAL, 60).endswith("…"))


# --- review finding, qingyun-wu on 38a6bf91 -----------------------------------
# When the parenthetical opens at character 0, trimming it emptied `head` and the
# old fallback hard-sliced the ORIGINAL text -- putting the "(" straight back and
# recreating the exact fragment this function exists to remove.

LEADING_PAREN = "(This parenthetical title keeps going for much longer than the speech limit allows)"

BRACKET_SHAPES = [
    LEADING_PAREN,
    "(short) then a much longer tail that will certainly be clipped somewhere",
    "Leading text (then a parenthetical that never closes because it is long",
    "((doubly nested opener that keeps going well past any sane speech limit))",
    "No brackets at all, just a long ordinary title that must clip on a word",
    "(a)(b)(c) followed by a long tail of ordinary words to force the clipping",
]


class TestLeadingParenthetical(unittest.TestCase):
    def test_the_reviewers_exact_repro(self):
        out = mb.clip_for_speech(LEADING_PAREN, 30)
        self.assertEqual(out.count("("), out.count(")"), out)
        self.assertNotIn("(This parenthetical title kee", out)
        self.assertLessEqual(len(out), 30)

    def test_a_clip_is_never_left_with_an_unclosed_bracket(self):
        """The axis, not just the one string the review happened to find.

        A clipped result must be balanced, within the limit, and marked. Text
        returned UNCHANGED is exempt on purpose -- see the next test.
        """
        checked = 0
        for text in BRACKET_SHAPES:
            for limit in (8, 10, 12, 15, 20, 25, 30, 40, 50, 60, 80, 120):
                out = mb.clip_for_speech(text, limit)
                if out == text:
                    continue
                checked += 1
                self.assertEqual(out.count("("), out.count(")"), (limit, text, out))
                self.assertLessEqual(len(out), limit, (limit, out))
                self.assertTrue(out.endswith("\u2026"), (limit, out))
        self.assertGreater(checked, 40, "axis should exercise many clipped cases")

    def test_unbalanced_text_that_FITS_is_returned_unchanged(self):
        """Deliberate: we do not edit a title we did not clip.

        "Leading text (unclosed" is already unbalanced at the source. Silently
        rewriting a title that fits would be a worse defect than passing it
        through, so this is an exemption, not an oversight.
        """
        fits = "Leading text (unclosed"
        self.assertEqual(mb.clip_for_speech(fits, 60), fits)

    def test_a_title_that_is_only_an_open_bracket_still_terminates(self):
        for limit in (2, 3, 5, 10):
            out = mb.clip_for_speech("(" * 40, limit)
            self.assertLessEqual(len(out), limit, (limit, out))

if __name__ == "__main__":
    unittest.main(verbosity=2)
