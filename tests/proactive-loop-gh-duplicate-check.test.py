#!/usr/bin/env python3
"""Contract for gh-duplicate-check.py.

Built after filing a duplicate issue whose original my own search had PRINTED:
the search and the `gh issue create` sat in one command block, so nothing
consumed the result. The gate therefore has to EXIT non-zero, not print.

The discriminating test is `bare-word only`: the real pair shared no entity
tokens at all, and the first build of this tool — delegating tokenisation to
warn-already-triaged.py alone — returned PROCEED on it.
"""

import importlib.util
import io
import contextlib
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "skills" / "proactive-loop" / "scripts"
_s = importlib.util.spec_from_file_location("ghd", str(SCRIPTS / "gh-duplicate-check.py"))
ghd = importlib.util.module_from_spec(_s)
_s.loader.exec_module(ghd)

REAL = ("eslint: 34 of 300 runs (11%) are killed by timeout-minutes: 5, and the "
        "variance is npm ci (24s vs 274s), not the lint (constant 6s)")
ORIG = {"number": 3862, "state": "open", "html_url": "u",
        "title": "CI: eslint job times out in npm ci since 2026-09-03T22:37Z — 12 of "
                 "last 100 runs killed at the 5-min cap, reported as 'cancelled'",
        "body": "timeout-minutes: 5 ... npm ci ... eslint ... variance in install time"}


def run(argv, stub):
    """Run main() with search stubbed. Returns (rc, stdout, stderr)."""
    orig = ghd.search
    ghd.search = stub
    out, err = io.StringIO(), io.StringIO()
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = ghd.main(argv)
    finally:
        ghd.search = orig
    return rc, out.getvalue(), err.getvalue()


BASE = ["--repo", "o/n", "--title", REAL]


class TheFailureThatProducedIt(unittest.TestCase):
    def test_a_bare_word_only_duplicate_is_REFUSED(self):
        rc, out, _ = run(BASE, lambda r, t, **k: [ORIG] if t in ("eslint", "npm") else [])
        self.assertEqual(rc, 1, out)
        self.assertIn("3862", out)
        self.assertIn("DO NOT FILE", out)

    def test_bare_words_are_what_make_it_fire(self):
        # Entity tokens alone cannot see `eslint` — the shape of the original miss.
        toks, _ = ghd.load_tokens()
        self.assertNotIn("eslint", [t.lower() for t in toks(None, REAL)])
        self.assertIn("eslint", ghd.bare_words(REAL, set()))


class FailsClosed(unittest.TestCase):
    def test_every_search_failing_is_cannot_answer_not_clean(self):
        rc, _, err = run(BASE, lambda r, t, **k: None)
        self.assertEqual(rc, 2)
        self.assertIn("CANNOT ANSWER", err)

    def test_partial_failure_with_no_hits_is_cannot_answer(self):
        calls = {"n": 0}
        def flaky(r, t, **k):
            calls["n"] += 1
            return None if calls["n"] % 2 else []
        rc, _, err = run(BASE, flaky)
        self.assertEqual(rc, 2, err)
        self.assertIn("not fully searched", err)

    def test_partial_failure_that_still_finds_one_REFUSES(self):
        calls = {"n": 0}
        def flaky(r, t, **k):
            calls["n"] += 1
            return None if calls["n"] % 2 else [ORIG]
        rc, out, _ = run(BASE, flaky)
        self.assertEqual(rc, 1, out)
        self.assertIn("coverage is partial", out)

    def test_a_title_with_no_tokens_is_cannot_answer(self):
        rc, _, err = run(["--repo", "o/n", "--title", "a an it is"], lambda *a, **k: [])
        self.assertEqual(rc, 2)
        self.assertIn("CANNOT ANSWER", err)


class SearchContract(unittest.TestCase):
    def test_a_raising_fetch_returns_None_not_an_empty_list(self):
        # Collapsing these is how a failed search becomes a green light.
        self.assertIsNone(ghd.search("o/n", "x", timeout=0.001))

    def test_a_NON_ZERO_EXIT_also_returns_None(self):
        # The timeout test only reaches the `except` path, so it stays green
        # when this branch -- the one a 403 rate limit takes -- is broken.
        import subprocess as sp
        orig = ghd.subprocess.run
        ghd.subprocess.run = lambda *a, **k: sp.CompletedProcess(
            a[0], 1, stdout='{"message":"API rate limit exceeded"}', stderr="")
        try:
            self.assertIsNone(ghd.search("o/n", "x"))
        finally:
            ghd.subprocess.run = orig

    def test_unparseable_json_also_returns_None(self):
        import subprocess as sp
        orig = ghd.subprocess.run
        ghd.subprocess.run = lambda *a, **k: sp.CompletedProcess(a[0], 0, stdout="<html>", stderr="")
        try:
            self.assertIsNone(ghd.search("o/n", "x"))
        finally:
            ghd.subprocess.run = orig


class Scoring(unittest.TestCase):
    def test_below_min_overlap_is_not_reported(self):
        weak = {"number": 1, "state": "open", "html_url": "u",
                "title": "something about eslint only", "body": ""}
        rc, out, _ = run(BASE + ["--min-overlap", "3"], lambda r, t, **k: [weak])
        self.assertEqual(rc, 0, out)
        self.assertIn("NO CANDIDATE", out)

    def test_clean_result_states_it_is_not_proof_of_absence(self):
        rc, out, _ = run(BASE, lambda r, t, **k: [])
        self.assertEqual(rc, 0)
        self.assertIn("not proof of absence", out)


    def test_a_clean_result_names_the_tokens_it_never_searched(self):
        # The cap drops 2-8 tokens on real titles, often the most distinctive
        # ones, so an unstated bound reads as a wider search than was run.
        rc, out, _ = run(BASE + ["--max-queries", "1"], lambda r, t, **k: [])
        self.assertEqual(rc, 0, out)
        self.assertIn("NOT searched", out)
        self.assertIn("never searched", out)

    def test_the_header_states_used_of_total(self):
        rc, out, _ = run(BASE + ["--max-queries", "1"], lambda r, t, **k: [])
        self.assertRegex(out, r"searched \S+ on 1 of \d+ token")

    def test_candidates_are_ranked_by_overlap(self):
        # weak scores 2, ORIG 3 — a real gap. The old fixture tied on a phantom
        # point: the token `lint` matching inside the word `eslint`.
        weak = {"number": 1, "state": "open", "html_url": "u",
                "title": "eslint variance", "body": ""}
        rc, out, _ = run(BASE, lambda r, t, **k: [weak, ORIG])
        self.assertEqual(rc, 1, out)
        self.assertLess(out.index("3862"), out.index("#1 "))

    def test_a_substring_only_match_does_not_reach_the_threshold(self):
        # `score()` matches substrings, so a short token can hit inside a longer
        # word; one real token plus such a hit must not clear an overlap of 2.
        sub = {"number": 2, "state": "open", "html_url": "u",
               "title": "eslint", "body": ""}
        rc, out, _ = run(BASE, lambda r, t, **k: [sub])
        self.assertEqual(rc, 0, out)


class Delegation(unittest.TestCase):
    def test_it_refuses_rather_than_reimplementing_the_tokenizer(self):
        orig = ghd.load_tokens
        ghd.load_tokens = lambda: (None, None)
        try:
            out, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                rc = ghd.main(BASE)
        finally:
            ghd.load_tokens = orig
        self.assertEqual(rc, 2)
        self.assertIn("drift", err.getvalue())


class DecisionParametersCannotSilenceTheSearch(unittest.TestCase):
    """`--max-queries 0` ran zero searches and scored NO CANDIDATE, rc 0 (review finding)."""

    def test_zero_queries_is_cannot_answer(self):
        rc, _, err = run(BASE + ["--max-queries", "0"], lambda *a, **k: [])
        self.assertEqual(rc, 2)
        self.assertIn("CANNOT ANSWER", err)

    def test_zero_overlap_is_cannot_answer(self):
        rc, _, err = run(BASE + ["--min-overlap", "0"], lambda *a, **k: [])
        self.assertEqual(rc, 2)
        self.assertIn("CANNOT ANSWER", err)


    def test_an_EXACT_duplicate_is_not_cleared_by_a_short_title(self):
        # "eslint bug" yields one token; with the default overlap of 2 no candidate
        # can reach the threshold, so rc 0 would be arithmetic (review finding).
        exact = {"number": 9, "state": "open", "html_url": "u",
                 "title": "eslint bug", "body": ""}
        rc, _, err = run(["--repo", "o/n", "--title", "eslint bug"],
                         lambda r, t, **k: [exact])
        self.assertEqual(rc, 2, err)
        self.assertIn("exact duplicate would score clean", err)

    def test_a_reachable_threshold_still_answers(self):
        exact = {"number": 9, "state": "open", "html_url": "u",
                 "title": "eslint bug", "body": ""}
        rc, out, _ = run(["--repo", "o/n", "--title", "eslint bug", "--min-overlap", "1"],
                         lambda r, t, **k: [exact])
        self.assertEqual(rc, 1, out)

    def test_one_of_each_is_still_allowed(self):
        rc, out, _ = run(BASE + ["--max-queries", "1", "--min-overlap", "1"],
                         lambda r, t, **k: [])
        self.assertEqual(rc, 0, out)


class WiredIntoTheInstruction(unittest.TestCase):
    """An unreferenced gate runs never — worse than one described only in prose.

    The tool shipped invoked by nothing, so the workflow that filed the duplicate
    was unchanged. These pin the wiring, not the script.
    """

    SKILL = Path(__file__).resolve().parents[1] / "skills" / "proactive-loop" / "SKILL.md"

    def test_the_loop_instruction_names_the_script(self):
        self.assertIn("gh-duplicate-check.py", self.SKILL.read_text())

    def test_it_is_chained_so_the_result_gates_the_create(self):
        # Naming the script is not enough: the `&&` is what makes the verdict a
        # precondition rather than something printed next to the action.
        text = self.SKILL.read_text()
        i = text.index("gh-duplicate-check.py")
        window = text[i:i + 700]
        self.assertIn("&& gh issue create", window)


if __name__ == "__main__":
    unittest.main(verbosity=2)
