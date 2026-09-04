#!/usr/bin/env python3
"""Both shell-reading guards delegate to `_shell_scan`; neither keeps a copy.

The duplication was the defect, not just a symptom of it: `_newline_separators`
was byte-identical in both hooks, so one reviewer's three findings had to be
fixed twice or land twice. Copies drift, and the copy nobody remembers is the
one that ships the bug — so this pins the single owner rather than the fix.
"""
import re
import unittest
from pathlib import Path

HOOKS = Path(__file__).resolve().parent.parent / "hooks"
GUARDS = ["inline-body-substitution-guard.py", "release-target-guard.py"]

# Signatures of hand-rolled shell parsing. A guard that needs any of these is
# re-implementing the scanner rather than calling it.
PRIVATE_PARSER = (
    (re.compile(r"^import shlex", re.M), "imports shlex directly"),
    (re.compile(r"def _newline_separators"), "keeps its own newline/comment pass"),
    (re.compile(r"lex\.commenters"), "configures its own lexer"),
    (re.compile(r"punctuation_chars"), "configures its own lexer"),
)


class EachGuardDelegates(unittest.TestCase):
    def test_the_shared_scanner_exists(self):
        """Positive control: if it were missing, every check below would pass
        vacuously — a guard cannot fail to duplicate a module that is absent."""
        self.assertTrue((HOOKS / "_shell_scan.py").is_file())

    def test_no_guard_keeps_a_private_parser(self):
        findings = []
        for name in GUARDS:
            src = (HOOKS / name).read_text()
            for pattern, why in PRIVATE_PARSER:
                if pattern.search(src):
                    findings.append(f"{name} {why}")
        self.assertEqual(findings, [], "; ".join(findings))

    def test_every_guard_actually_imports_it(self):
        for name in GUARDS:
            src = (HOOKS / name).read_text()
            self.assertIn("import _shell_scan", src, f"{name} does not delegate")
            self.assertRegex(src, r"_shell_scan\.(segments|words)\(",
                             f"{name} imports the scanner but never calls it")

    def test_the_detector_can_still_fire(self):
        """A checker that cannot produce a finding certifies nothing."""
        findings = [why for pattern, why in PRIVATE_PARSER
                    if pattern.search("import shlex\ndef _newline_separators(x):\n"
                                      "    lex.commenters = ''\n    punctuation_chars=True\n")]
        self.assertEqual(len(findings), len(PRIVATE_PARSER))


if __name__ == "__main__":
    unittest.main(verbosity=2)
