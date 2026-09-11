#!/usr/bin/env python3
"""The shared comment/command predicate, pinned by behaviour.

Three false-greens shipped because each guard carried its own `#` rule and none
of them was tested directly: a mention inside a quoted string counted as a call.
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "lib"))
from active_code import active_lines, invokes, unquoted  # noqa: E402

NAME = "discover-python-tests.sh"


class CommentBoundary(unittest.TestCase):
    def test_a_full_line_comment_is_dropped(self):
        self.assertEqual(active_lines("      # python3 dead.py"), [])

    def test_a_trailing_comment_is_cut_but_the_code_survives(self):
        self.assertEqual(active_lines("- run: python3 x.py  # why"),
                         ["- run: python3 x.py  "])

    def test_a_comment_after_a_separator_is_a_comment(self):
        """`:;# x` is a comment; a whitespace-only rule misses it."""
        self.assertEqual(active_lines("- run: :;# python3 dead.py"), ["- run: :;"])

    def test_a_hash_inside_a_token_is_not_a_comment(self):
        self.assertEqual(active_lines("colour=a#b"), ["colour=a#b"])

    def test_a_hash_inside_quotes_is_not_a_comment(self):
        self.assertEqual(active_lines('- run: echo "# not a comment"'),
                         ['- run: echo "# not a comment"'])


class CommandPosition(unittest.TestCase):
    def test_a_real_invocation_counts(self):
        for line in (f'bash scripts/{NAME} > "$F"', f"sh scripts/{NAME} > f",
                     f"scripts/{NAME} > f"):
            self.assertTrue(invokes(line, NAME), line)

    def test_an_echoed_quoted_call_is_not_an_invocation(self):
        """The defect: `echo "bash x.sh > f"` satisfied a substring+`>` check."""
        for line in (f'echo "bash scripts/{NAME} > files"', f"echo 'scripts/{NAME}'"):
            self.assertFalse(invokes(line, NAME), line)

    def test_a_commented_invocation_is_not_an_invocation(self):
        self.assertFalse(invokes(f"# bash scripts/{NAME} > f", NAME))
        self.assertFalse(invokes(f": > f ;# scripts/{NAME}", NAME))

    def test_unquoted_blanks_quoted_runs_only(self):
        self.assertEqual(unquoted("a 'bc' d"), "a      d")


if __name__ == "__main__":
    unittest.main(verbosity=2)
