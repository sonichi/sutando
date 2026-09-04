#!/usr/bin/env python3
"""`_shell_scan` against BASH, not against my idea of bash.

The parser this replaces was wrong in ways only bash could settle — an escaped
quote, an even backslash run, an escaped space — so the oracle here IS bash: a
shell function stands in for the program and prints its real argv.
"""
import os
import base64
import subprocess
import sys
import unittest
from pathlib import Path

HOOKS = Path(__file__).resolve().parent.parent / "hooks"
sys.path.insert(0, str(HOOKS))
import _shell_scan as scan  # noqa: E402

def bash_argv(command, program="gh"):
    r"""The argv bash actually hands `program`, or None when bash refuses.

    Each argument is base64'd, so no byte the payload can contain is also a
    delimiter: a raw sentinel cannot express $'\cA', whose value IS the
    sentinel. The leading 'x' keeps an EMPTY argument on its own line, which
    base64 alone encodes to nothing at all.
    """
    shim = ('%s() { for a in "$@"; do printf x; printf %%s "$a" | base64 | tr -d "\\n"; '
            'printf "\\n"; done; }\n')
    script = (shim % program) + (shim % "my-tool") + command
    r = subprocess.run(["bash", "-c", script], capture_output=True, text=True)
    lines = [ln[1:] for ln in r.stdout.splitlines() if ln.startswith("x")]
    if r.returncode != 0 and not lines:
        return None
    try:
        return [base64.b64decode(ln).decode("utf-8", "replace") for ln in lines]
    except Exception:
        return None


def scan_argv(command, program="gh"):
    """The same argv, as the scanner sees it: the first segment whose head is
    `program`, minus that head."""
    out = []
    for seg in scan.segments(command):
        if seg and seg[0].basename_is(program):
            out += [w.text for w in seg[1:]]   # bash's fake prints EVERY call
    return out


class MatchesBash(unittest.TestCase):
    """The scanner tokenizes and unquotes; it deliberately does NOT expand — a
    guard must never run a subshell to decide whether to allow one. So bash is
    the oracle for TOKENIZATION on words it would not rewrite, and separately
    for whether a word gets rewritten at all."""

    LITERAL = [
        'gh pr comment 1 --body "plain text"',
        "gh pr comment 1 --body 'single quoted'",
        'gh pr comment 1 --body "has \\"escaped\\" quotes"',
        'gh pr comment 1 --body "trailing backslash \\\\"',
        'gh pr comment 1 --body one\\ word\\ with\\ escaped\\ spaces',
        'gh pr comment 1 --body "a#b not a comment"',
        'gh pr comment 1 --body x/y#frag',
        'gh pr comment 1 --body "literal \\$HOME"',
        'gh pr comment 1 --body ""',
        "gh pr comment 1 --body \"mixed 'inner' quotes\"",
        'gh pr view 1; gh pr comment 2 --body "second command"',
        'gh pr comment 1 --body "paren ( ) inside"',
        'gh pr comment 1 --body "literal \\$(true)"',
        '"gh" pr comment 1 --body "quoted program"',
        # A newline separates commands; a CR does not. The replaced normaliser's
        # two copies differed exactly here, so pin against bash, not reasoning.
        'gh release create v1 --target abc123\ngh pr view 2',
        'gh release create v1 --target abc123\r\ngh pr view 2',
        'gh release create v1 --target abc123\rgh pr view 2',
        # ANSI-C quoting: bash DROPS the $ and decodes, so a scanner that keeps
        # the $ hands a guard a value gh never receives.
        "gh release create v1 --target $'abc1234'",
        "gh release create v1 --target $'\\x61bc1234'",
        "gh release create v1 --target $'\\141bc1234'",
        "gh pr comment 1 --body $'a\\tb'",
        "gh pr comment 1 --body $'quote\\'inside'",
        "gh pr comment 1 --body $'\\cA'",
        "gh pr comment 1 --body $'\\e[0m'",
        "gh pr comment 1 --body pre$'mid'post",
        # A NUL cannot reach argv, so bash truncates the SPAN that contains it
        # and keeps concatenating: $'a\\0b'c is "ac". A scanner that retains the
        # NUL sees a target bash never delivers, and the guard under-denies.
        "gh release create v1 --target $'abc1234\\0suffix'",
        "gh release create v1 --target $'abc1234\\x00suffix'",
        "gh release create v1 --target $'abc1234\\000suffix'",
        "gh release create v1 --target $'abc1234\\0'suffix",
        "gh pr comment 1 --body $'a\\0b'$'c\\0d'",
        "gh pr comment 1 --body $'\\0lead'",
        "gh pr comment 1 --body pre$'abc\\0suf'post",
        "gh pr comment 1 --body $'plain' --title after",
        # $"..." is locale translation: bash drops the $ too, so the same
        # under-deny as $'...' reached the sha match by a second route.
        'gh release create v1 --target $"abc1234"',
        'gh pr comment 1 --body $"hello world"',
        # A line continuation: bash removes the backslash AND the newline, so
        # emitting the newline splits where bash never splits.
        'gh release create v1 \\\n  --target abc',
        'gh pr comment 1 --body "a \\\n b"',
        "gh pr comment 1 --body 'a \\\n b'",
    ]

    # bash rewrites these too and we deliberately do NOT flag them: `$VAR` is an
    # interpolation, not a code span, and denying it would deny most safe bodies.
    EXPANDED_BUT_ALLOWED = [
        'gh pr comment 1 --body "$HOME is interpolated"',
        'gh pr comment 1 --body "path is $PWD"',
    ]

    # bash REWRITES these; the scanner must flag them, never evaluate them.
    EXPANDING = [
        'gh pr comment 1 --body "sub $(true) here"',
        ': tag\\ #3830; gh pr comment 1 --body "use `true` here"',
        'gh pr comment 1 --body "prefix \\\\$(true) suffix"',
    ]

    def test_tokenization_matches_bash_on_every_literal_case(self):
        mismatches, checked = [], 0
        for cmd in self.LITERAL:
            expected = bash_argv(cmd)
            self.assertIsNotNone(expected, f"oracle did not run: {cmd!r}")
            checked += 1
            got = scan_argv(cmd)
            if got != expected:
                mismatches.append((cmd, expected, got))
        self.assertEqual(checked, len(self.LITERAL), "every literal case must be measured")
        self.assertEqual(mismatches, [], f"{len(mismatches)} of {checked} disagree with bash")

    def test_bash_really_rewrites_every_expanding_case(self):
        """Positive control on the OTHER half: if bash left these alone, the
        `expands` assertions below would be pinning nothing."""
        for cmd in self.EXPANDING:
            argv = bash_argv(cmd)
            self.assertIsNotNone(argv, f"oracle did not run: {cmd!r}")
            literal = scan_argv(cmd)
            self.assertNotEqual(argv, literal,
                                f"bash did NOT rewrite this, so it is not an expanding case: {cmd!r}")

    def test_the_scanner_flags_exactly_those(self):
        for cmd in self.EXPANDING:
            flagged = any(w.expands for seg in scan.segments(cmd) for w in seg)
            self.assertTrue(flagged, f"expansion not detected: {cmd!r}")
        for cmd in self.LITERAL:
            flagged = any(w.expands for seg in scan.segments(cmd) for w in seg)
            self.assertFalse(flagged, f"false positive on a literal: {cmd!r}")

    def test_a_plain_variable_is_rewritten_by_bash_and_still_allowed(self):
        """The policy line, pinned from both sides: bash DOES rewrite it (so
        this is not a vacuous case), and the scanner still lets it through."""
        for cmd in self.EXPANDED_BUT_ALLOWED:
            argv = bash_argv(cmd)
            self.assertIsNotNone(argv)
            self.assertNotEqual(argv, scan_argv(cmd), f"bash left it alone: {cmd!r}")
            flagged = any(w.expands for seg in scan.segments(cmd) for w in seg)
            self.assertFalse(flagged, f"$VAR must not be flagged: {cmd!r}")

    def test_the_oracle_can_actually_fail(self):
        expected = bash_argv('gh pr comment 1 --body "x"')
        self.assertEqual(expected, ["pr", "comment", "1", "--body", "x"])
        self.assertNotEqual(expected, ["pr", "comment", "1", "--body", "WRONG"])


class UnderDeniesTheOldParserMissed(unittest.TestCase):
    """Each of these had the value's danger hidden from the old parser."""

    def _body(self, cmd):
        for seg in scan.segments(cmd):
            if seg and seg[0].basename_is("gh"):
                for i, w in enumerate(seg):
                    if w.text == "--body" and i + 1 < len(seg):
                        return seg[i + 1]
        return None

    def test_escaped_quote_does_not_end_the_token_early(self):
        cmd = ': tag\\ #3830; gh pr comment 1 --body "use `true` here"'
        w = self._body(cmd)
        self.assertIsNotNone(w, "the gh segment must survive the escaped-space `#`")
        self.assertTrue(w.expands, "an active backtick must be seen")

    def test_an_even_backslash_run_leaves_the_substitution_active(self):
        w = self._body('gh pr comment 1 --body "prefix \\\\$(true) suffix"')
        self.assertIsNotNone(w)
        self.assertTrue(w.expands, "\\\\ is a literal backslash; $( is still live")

    def test_an_escaped_space_is_not_a_word_boundary(self):
        ws = scan.words('gh pr comment 1 --body one\\ two')
        self.assertIn("one two", [w.text for w in ws])

    def test_an_escaped_dollar_paren_is_inert(self):
        w = self._body('gh pr comment 1 --body "literal \\$(true)"')
        self.assertIsNotNone(w)
        self.assertFalse(w.expands, "an ODD run escapes it; nothing is substituted")


class FalsePositiveOnCommentThenNewline(unittest.TestCase):
    def test_a_comment_then_a_new_line_disarms_before_a_different_tool(self):
        """`gh ...;# note` then a NON-gh tool on the next line: the old parser
        normalised the break to `;;`, which it did not recognise, so `gh` stayed
        armed and the unrelated tool's --title was reported."""
        cmd = 'gh pr view 1;# note\nmy-tool --title "built $(true)"'
        heads = [seg[0].text for seg in scan.segments(cmd)]
        self.assertIn("my-tool", heads)
        for seg in scan.segments(cmd):
            if seg[0].basename_is("gh"):
                self.assertNotIn("--title", [w.text for w in seg])

    def test_the_comment_is_removed_not_merged(self):
        segs = scan.segments('gh pr view 1;# note\nmy-tool --title "x"')
        self.assertTrue(all("note" not in w.text for seg in segs for w in seg))


class RefusesRatherThanGuessing(unittest.TestCase):
    def test_an_unterminated_quote_scans_nothing(self):
        for cmd in ['gh pr comment 1 --body "open', "gh pr comment 1 --body 'open",
                    'gh pr comment 1 --body "a\\']:
            self.assertEqual(scan.words(cmd), [], f"should refuse: {cmd!r}")
            self.assertIsNone(bash_argv(cmd), "and bash refuses it too")


class ProgramIdentity(unittest.TestCase):
    def test_a_path_qualified_program_still_matches(self):
        w = scan.words("/opt/homebrew/bin/gh pr view 1")[0]
        self.assertTrue(w.basename_is("gh"))

    def test_case_folding_is_opt_in(self):
        w = scan.words("/opt/homebrew/bin/GH pr view 1")[0]
        self.assertFalse(w.basename_is("gh"))
        self.assertTrue(w.basename_is("gh", fold=True))

    def test_a_quoted_program_name_matches(self):
        self.assertTrue(scan.words('"gh" pr view 1')[0].basename_is("gh"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
