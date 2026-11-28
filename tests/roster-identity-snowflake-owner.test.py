#!/usr/bin/env python3
"""Snowflake validity has ONE owner, and every consumer reads it through him.

The grammar was spelled three times -- the schema's scalar predicate, the
schema's list validator, and the migrator's own compiled pattern. The copies
agreed on every input tried, which is exactly what makes this a repository
contract rather than a bug report: replacing one left the others answering the
old grammar, so the next change to any of them ships a divergence nobody sees.

Two instruments, because either alone is satisfiable without the other: a
RUNTIME control that the consumers really call the owner, and a SOURCE guard
that nobody has added a fourth spelling for the runtime control to miss.
"""
import pathlib
import re
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills/collaboration-intelligence/scripts"
sys.path.insert(0, str(SCRIPTS))
import roster_identity as ri  # noqa: E402
import migrate_roster_identity as mig  # noqa: E402

H = "1358841611580080161"


class EveryConsumerReadsTheOwnerAtCallTime(unittest.TestCase):
    """Replacing the owner must move every answer that depends on it."""

    def setUp(self):
        self._orig = ri.is_snowflake
        self.addCleanup(setattr, ri, "is_snowflake", self._orig)

    def _answers(self):
        return (ri.is_snowflake(H), ri._snowflake_list([H]), mig._is_snowflake(H))

    def test_baseline_accepts(self):
        self.assertEqual(self._answers(), (True, [H], True))

    def test_a_rejecting_owner_rejects_everywhere(self):
        ri.is_snowflake = lambda v: False
        scalar, listed, migrator = self._answers()
        self.assertFalse(scalar)
        self.assertEqual(listed, [], "the list validator must delegate")
        self.assertFalse(migrator, "the migrator must delegate")

    def test_an_accepting_owner_accepts_everywhere(self):
        # The opposite direction, so a consumer hardcoded to False cannot pass.
        ri.is_snowflake = lambda v: True
        self.assertEqual(ri._snowflake_list(["nope"]), ["nope"])
        self.assertTrue(mig._is_snowflake("nope"))


class ExtractionIsBuiltFromTheOwnersGrammar(unittest.TestCase):
    """Boundaries and MXID exclusion wrap the one grammar, not a copy of it."""

    def test_the_compiled_pattern_contains_the_owners_pattern(self):
        self.assertIn(ri.SNOWFLAKE_PATTERN, mig._SNOWFLAKE.pattern)

    def test_extraction_keeps_its_unicode_boundary(self):
        self.assertEqual(mig._snowflakes(H), [H])
        self.assertEqual(mig._snowflakes("١" + H), [],
                         "a run touching a Unicode digit is rejected whole")

    def test_extraction_keeps_its_matrix_exclusion(self):
        self.assertEqual(mig._snowflakes("@" + H + ":ag2.space"), [])


class NoFourthSpellingExists(unittest.TestCase):
    """The source guard: the grammar literal may appear once, at the owner.

    Token-specific on the quantifier, so it cannot be satisfied by renaming a
    function and cannot fire on unrelated code.
    """

    FILES = ("roster_identity.py", "migrate_roster_identity.py")
    LITERAL = re.compile(r"\{17,\s*20\}")

    def test_the_quantifier_is_written_exactly_once(self):
        hits = []
        for name in self.FILES:
            text = (SCRIPTS / name).read_text(encoding="utf-8")
            for n, line in enumerate(text.splitlines(), 1):
                if self.LITERAL.search(line):
                    hits.append(f"{name}:{n}: {line.strip()}")
        self.assertEqual(len(hits), 1, "one owner, one spelling:\n" + "\n".join(hits))
        self.assertTrue(hits[0].startswith("roster_identity.py"),
                        "the owner is the schema module, not the migrator: " + hits[0])

    def test_the_migrator_declares_no_digit_class_of_its_own(self):
        text = (SCRIPTS / "migrate_roster_identity.py").read_text(encoding="utf-8")
        self.assertNotIn("[0-9]", text,
                         "an ASCII-digit class in the migrator is a second grammar")


if __name__ == "__main__":
    unittest.main(verbosity=2)
