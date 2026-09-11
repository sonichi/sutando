#!/usr/bin/env python3
"""Contract for memory-relevance-check.py.

The first version of this script scored on ANY shared word and called 31 of 32
real feedback memory files "relevant" to a claim about a refresh button -- a
detector that always fires is not a detector, and these tests exist so that
regression can never again ship silently: a positive control (the real
refresh-button case must hit exactly the on-point file) paired with negative
controls (an unrelated claim, and a claim built only from words common across
the whole fixture corpus, must both come back NONE FOUND).
"""

import contextlib
import importlib.util
import io
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1] / "skills" / "proactive-loop" / "scripts"
_s = importlib.util.spec_from_file_location("mrc", str(SCRIPTS / "memory-relevance-check.py"))
mrc = importlib.util.module_from_spec(_s)
_s.loader.exec_module(mrc)


def _corpus(**files):
    d = Path(tempfile.mkdtemp())
    out = []
    for name, text in files.items():
        p = d / name
        p.write_text(text)
        out.append(p)
    return sorted(out)


def _run(claim, files):
    buf = io.StringIO()
    with mock.patch.object(mrc, "memory_files", lambda: files), \
            mock.patch.object(sys, "argv", ["mrc", "--claim", claim]), \
            contextlib.redirect_stdout(buf):
        rc = mrc.main()
    return rc, buf.getvalue()


class Tokenizing(unittest.TestCase):
    def test_short_words_are_dropped(self):
        self.assertNotIn("a", mrc.words("a big red dog ran"))
        self.assertNotIn("ran", mrc.words("a big red dog ran"))  # len 3 < MIN_LEN

    def test_a_real_word_is_kept(self):
        self.assertIn("button", mrc.words("the button stays"))

    def test_hyphenated_and_underscored_tokens_survive(self):
        self.assertIn("show-only", mrc.words("a show-only icon"))
        self.assertIn("owner_verdicts", mrc.words("calls owner_verdicts once"))

    def test_case_is_folded(self):
        self.assertIn("button", mrc.words("BUTTON Button"))


class DocumentFrequencyDiscrimination(unittest.TestCase):
    """The bug this script was rewritten to fix: a word common across the whole
    corpus must not count as evidence, however many files happen to share it."""

    def test_a_word_in_every_file_does_not_make_them_relevant(self):
        # "system" appears in all 4 fixtures below -- with 32 real files and a
        # 25%-of-corpus cap this is exactly the shape that produced 31/32 hits.
        files = _corpus(**{
            f"f{i}.md": "the system does something unrelated to the claim here"
            for i in range(4)
        })
        rc, out = _run("the system needs work", files)
        self.assertEqual(rc, 2, "every token common -> cannot answer, not a false hit")
        self.assertIn("NO DISCRIMINATIVE TOKENS", out)

    def test_a_word_rare_in_the_corpus_does_count(self):
        files = _corpus(**{
            "on-point.md": "the wordmark button icon design was flagged as unspecified",
            "unrelated-1.md": "a totally different bridge restart problem",
            "unrelated-2.md": "a totally different quota exhaustion problem",
            "unrelated-3.md": "a totally different memory rotation problem",
            "unrelated-4.md": "a totally different task watcher problem",
        })
        rc, out = _run("shipping the wordmark button icon with no design spec", files)
        self.assertEqual(rc, 1)
        self.assertIn("on-point.md", out)
        self.assertNotIn("unrelated-1.md", out)


class SharedTokenThreshold(unittest.TestCase):
    def test_a_single_shared_rare_token_is_not_enough(self):
        # MIN_SHARED_TOKENS=3: one coincidental overlap must not fire the gate.
        files = _corpus(**{
            "maybe.md": "unrelated content entirely but mentions gizmotron once",
            "filler.md": "nothing relevant in this one at all whatsoever",
        })
        rc, out = _run("a claim about gizmotron and nothing else shared", files)
        self.assertIn(rc, (0, 2))
        self.assertNotIn("maybe.md", out)

    def test_three_shared_rare_tokens_does_fire(self):
        files = _corpus(**{
            "match.md": "gizmotron whirlwind paradox all appear in this file together",
            "filler.md": "nothing relevant in this one at all whatsoever",
        })
        rc, out = _run("a claim mentioning gizmotron whirlwind paradox", files)
        self.assertEqual(rc, 1)
        self.assertIn("match.md", out)


class Refusals(unittest.TestCase):
    def test_missing_memory_dir_is_cannot_answer(self):
        with mock.patch.object(mrc, "memory_files", lambda: []):
            rc, out = _run("anything at all here", [])
        self.assertEqual(rc, 2)
        self.assertIn("MEMORY DIR NOT FOUND", out)

    def test_empty_claim_is_refused(self):
        files = _corpus(**{"f.md": "content"})
        rc, out = _run("   ", files)
        self.assertEqual(rc, 2)

    def test_a_claim_with_no_searchable_words_cannot_answer(self):
        files = _corpus(**{"f.md": "content here"})
        rc, out = _run("go do it", files)  # every word < MIN_LEN
        self.assertEqual(rc, 2)
        self.assertIn("NO NOUNS", out)

    def test_missing_claim_flag_is_refused(self):
        files = _corpus(**{"f.md": "content"})
        buf = io.StringIO()
        with mock.patch.object(mrc, "memory_files", lambda: files), \
                mock.patch.object(sys, "argv", ["mrc"]), \
                contextlib.redirect_stdout(buf):
            rc = mrc.main()
        self.assertEqual(rc, 2)
        self.assertIn("usage:", buf.getvalue())


class EndToEndOnTheRealCorpus(unittest.TestCase):
    """Positive control against the actual installed memory dir: the exact
    incident this script was built for must be found, by name, in the real
    corpus -- not a fixture standing in for it."""

    def test_the_refresh_button_claim_finds_the_design_intent_memory(self):
        files = mrc.memory_files()
        if not files:
            self.skipTest("no installed memory dir on this host")
        names = {f.name for f in files}
        if "feedback_ask_for_design_intent_not_around_it.md" not in names:
            self.skipTest("the specific memory this test pins is not installed here")
        rc, out = _run(
            "shipping a refresh button, persistent vs show-only-on-update icon, "
            "no spec named which one", files)
        self.assertEqual(rc, 1)
        self.assertIn("feedback_ask_for_design_intent_not_around_it.md", out)

    def test_an_unrelated_real_claim_does_not_false_positive_on_the_same_memory(self):
        files = mrc.memory_files()
        if not files:
            self.skipTest("no installed memory dir on this host")
        rc, out = _run(
            "checking the discord bridge process for a stale pid before restarting it",
            files)
        # Negative control for the test above: without it, a checker that always
        # says RELEVANT would still pass the positive case.
        self.assertNotIn("feedback_ask_for_design_intent_not_around_it.md", out)


if __name__ == "__main__":
    unittest.main(verbosity=2)
