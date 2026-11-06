#!/usr/bin/env python3
"""Every shell-reading guard delegates to `_shell_scan`; none keeps a copy.

The duplication was the defect, not just a symptom of it: `_newline_separators`
was byte-identical in two hooks, so one reviewer's three findings had to be
fixed twice or land twice. Copies drift, and the copy nobody remembers is the
one that ships the bug — so this pins the single owner rather than the fix.

The population is DISCOVERED, never listed. A hardcoded list cannot fail on a
guard it does not name, so the next guard added would be exempt by accident —
the same "passes while certifying nothing" shape the positive control below
exists to prevent, one level up.
"""
import re
import unittest
from pathlib import Path

HOOKS = Path(__file__).resolve().parent.parent / "hooks"

DELEGATES = re.compile(r"import _shell_scan")

# Signatures of hand-rolled shell parsing. A guard that needs any of these is
# re-implementing the scanner rather than calling it.
PRIVATE_PARSER = (
    (re.compile(r"^import shlex", re.M), "imports shlex directly"),
    (re.compile(r"def _newline_separators"), "keeps its own newline/comment pass"),
    (re.compile(r"lex\.commenters"), "configures its own lexer"),
    (re.compile(r"punctuation_chars"), "configures its own lexer"),
)

# Guards still parsing shell themselves, with the issue tracking each.
# `test_no_exemption_is_stale` fails once one no longer needs the entry.
NOT_YET_MIGRATED = {
    "comment-signature-guard.py": "sonichi/sutando#3849",
    "review-authority-guard.py": "sonichi/sutando#3849",
}


def shell_reading_guards():
    """Every hook that tokenizes a shell command, found rather than named.

    Covers the two realistic shapes: delegating to the shared scanner, or
    carrying a `shlex`-based parser. A guard tokenizing some third way is
    outside this predicate and would still need naming.
    """
    found = []
    for path in sorted(HOOKS.glob("*.py")):
        if path.name.startswith("_"):
            continue  # the scanner itself is not one of its own callers
        source = path.read_text()
        if DELEGATES.search(source) or any(rx.search(source) for rx, _ in PRIVATE_PARSER):
            found.append(path.name)
    return found


class EachGuardDelegates(unittest.TestCase):
    def test_the_shared_scanner_exists(self):
        """Positive control: if it were missing, every check below would pass
        vacuously — a guard cannot fail to duplicate a module that is absent."""
        self.assertTrue((HOOKS / "_shell_scan.py").is_file())

    def test_discovery_finds_more_than_the_two_this_pr_converted(self):
        """Second positive control, on the POPULATION rather than the detector.

        The defect this replaced was a hardcoded pair. If discovery silently
        narrowed to those two again, every check below would still pass while
        saying nothing about the rest of `hooks/`.
        """
        guards = shell_reading_guards()
        self.assertIn("inline-body-substitution-guard.py", guards)
        self.assertIn("release-target-guard.py", guards)
        self.assertGreater(len(guards), 2, f"discovery collapsed to {guards}")

    def test_no_guard_keeps_a_private_parser(self):
        findings = []
        for name in shell_reading_guards():
            if name in NOT_YET_MIGRATED:
                continue
            source = (HOOKS / name).read_text()
            for pattern, why in PRIVATE_PARSER:
                if pattern.search(source):
                    findings.append(f"{name} {why}")
        self.assertEqual(findings, [], "; ".join(findings))

    def test_every_guard_actually_imports_it(self):
        for name in shell_reading_guards():
            if name in NOT_YET_MIGRATED:
                continue
            source = (HOOKS / name).read_text()
            self.assertIn("import _shell_scan", source, f"{name} does not delegate")
            self.assertRegex(source, r"_shell_scan\.(segments|words)\(",
                             f"{name} imports the scanner but never calls it")

    def test_no_exemption_is_stale(self):
        """An exemption outliving its reason is a permanent hole wearing a
        deadline. Both directions fail: a vanished file, and one that has since
        migrated but is still listed."""
        for name, tracked_by in NOT_YET_MIGRATED.items():
            path = HOOKS / name
            self.assertTrue(path.is_file(), f"{name} is exempt but no longer exists")
            self.assertTrue(tracked_by.strip(), f"{name} is exempt with no tracking issue")
            source = path.read_text()
            self.assertTrue(
                any(rx.search(source) for rx, _ in PRIVATE_PARSER),
                f"{name} no longer keeps a private parser — drop it from NOT_YET_MIGRATED",
            )

    def test_the_detector_can_still_fire(self):
        """A checker that cannot produce a finding certifies nothing."""
        findings = [why for pattern, why in PRIVATE_PARSER
                    if pattern.search("import shlex\ndef _newline_separators(x):\n"
                                      "    lex.commenters = ''\n    punctuation_chars=True\n")]
        self.assertEqual(len(findings), len(PRIVATE_PARSER))


if __name__ == "__main__":
    unittest.main(verbosity=2)
