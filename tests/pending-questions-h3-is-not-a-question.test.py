#!/usr/bin/env python3
"""A `###` heading is body text, not a new question.
Why it matters is in the PR body."""
import importlib.util
import pathlib
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent

BASE = """# Pending questions

## First question

Body of the first.

"""
DIVIDER = "\n# Resolved\n\n## An old one\n"


def _reader(path):
    spec = importlib.util.spec_from_file_location(
        "cpq", ROOT / "src" / "check-pending-questions.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.PQ_FILE = path
    return mod


class TestHeadingLevelDecidesWhatCounts(unittest.TestCase):
    def _count(self, body):
        with tempfile.TemporaryDirectory() as td:
            p = pathlib.Path(td) / "pq.md"
            p.write_text(BASE + body + DIVIDER)
            return _reader(p).get_waiting_questions()

    def test_h3_does_not_add_a_question(self):
        q = self._count("### Swallowed by the section above\n\nSome body.\n")
        self.assertEqual(len(q), 1, [x.get("title") for x in q])
        self.assertFalse(
            any("Swallowed" in (x.get("title") or "") for x in q),
            "an h3 must not surface as its own question title")

    def test_h2_does_add_a_question(self):
        q = self._count("## Counted as its own question\n\nSome body.\n")
        self.assertEqual(len(q), 2, [x.get("title") for x in q])
        self.assertTrue(any("Counted" in (x.get("title") or "") for x in q))

    def test_the_h3_text_is_still_findable_which_is_why_a_substring_check_lies(self):
        """The failure mode the skill warns about: present, but not a question."""
        q = self._count("### Swallowed by the section above\n\nSome body.\n")
        self.assertTrue(any("Swallowed" in str(x) for x in q),
                        "the text survives inside the previous section...")
        self.assertFalse(any("Swallowed" in (x.get("title") or "") for x in q),
                         "...so only a title-level check can tell the two apart")


if __name__ == "__main__":
    unittest.main(verbosity=2)
