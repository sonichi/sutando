#!/usr/bin/env python3
"""The documented way to prove a filed question is visible returned False for a
question that was correctly filed.

`skills/proactive-loop/SKILL.md` tells every agent to verify a newly-written
pending question with:

    any('<distinctive phrase>' in x for x in [str(q) for q in get_waiting_questions()])

and states, in its own words, that "a `True` is the only proof the question exists
for anyone but you". But the returned dicts carried only `id` (40 chars), `title`,
and `snippet` (the first body line, clipped to 120) — so any phrase past roughly the
first 100 characters of an entry made that check return **False for a question that
was filed, sat above the `# Resolved` divider, and was counted**.

That is a verification step whose failure mode is reporting the healthy case as
broken — and the skill's surrounding text tells the reader a False means the write
did not land, which invites re-filing or escalating a question that is already fine.
Hit live on 2026-08-03 against a real entry.

Run: python3 tests/pending-questions-body-field.test.py
"""
from __future__ import annotations
import importlib.util
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

FIXTURE = """# Pending questions

## A section question with a long body

Opening line that lands inside the snippet.

Several paragraphs later, the DISTINCTIVE_DEEP_PHRASE appears well past the first
hundred characters of this entry — which is exactly where the operative detail of a
real question tends to live: the options, the ask, the correction.

- **[label, 2026-08-03]** a free-form bullet entry with BULLET_PHRASE inside it

# Resolved

## An archived question
ARCHIVED_PHRASE must never be reported as waiting.
"""


def _load(path, pq_file):
    spec = importlib.util.spec_from_file_location("cpq_body_test", path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    m.PQ_FILE = pq_file
    return m


class TestBodyField(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.pq = Path(self._tmp.name) / "pending-questions.md"
        self.pq.write_text(FIXTURE)
        self.m = _load(REPO / "src" / "check-pending-questions.py", self.pq)
        self.qs = self.m.get_waiting_questions()

    def tearDown(self):
        self._tmp.cleanup()

    def test_the_DOCUMENTED_assertion_finds_a_phrase_deep_in_the_body(self):
        # Verbatim the shape SKILL.md prescribes. This is the regression.
        found = any("DISTINCTIVE_DEEP_PHRASE" in x for x in [str(q) for q in self.qs])
        self.assertTrue(found, "the documented proof-of-filing must not report a filed question as missing")

    def test_every_question_carries_a_body_regardless_of_format(self):
        # A caller must not have to know which of two parsers produced a question
        # to know whether `body` is usable.
        self.assertTrue(self.qs, "fixture produced no questions — the test proves nothing")
        for q in self.qs:
            with self.subTest(title=q["title"][:40]):
                self.assertIn("body", q)
                self.assertTrue(q["body"], "body must not be empty")

    def test_a_bullet_entry_bodies_to_its_own_text(self):
        # `title` for a bullet is only the bracketed label, so the phrase must be
        # reachable via `body` — otherwise the bullet format keeps exactly the blind
        # spot this change removes for sections.
        b = [q for q in self.qs if q["title"].startswith("label,")]
        self.assertEqual(len(b), 1, "bullet entry not parsed")
        self.assertNotIn("BULLET_PHRASE", b[0]["title"], "label should not contain it — that is the point")
        self.assertIn("BULLET_PHRASE", b[0]["body"])
        self.assertTrue(any("BULLET_PHRASE" in str(q) for q in self.qs),
                        "the documented assertion must reach bullet entries too")

    def test_resolved_entries_are_STILL_excluded(self):
        # Over-trigger control: exposing more text must not widen what counts as
        # waiting. Everything below the divider stays out.
        blob = " ".join(str(q) for q in self.qs)
        self.assertNotIn("ARCHIVED_PHRASE", blob)

    def test_title_and_snippet_are_unchanged_in_shape(self):
        # morning-briefing.py reads `title` only; the notifier renders `snippet`.
        # Adding a key must not perturb either.
        q = self.qs[0]
        self.assertEqual(q["title"], "A section question with a long body")
        self.assertEqual(q["id"], q["title"][:40])
        self.assertLessEqual(len(q["snippet"]), 120)
        self.assertTrue(q["snippet"].startswith("Opening line"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
