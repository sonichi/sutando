#!/usr/bin/env python3
"""Behavioural tests for agent-api.py's pending-questions parser + answer path.

History of the bug these guard:

  #1265 moved pending-questions.md to a free-form format (prose sections, no
  **Status:** markers). A 2026-06-07 fix taught GET /status to read it, but left
  the *writer* (POST /answer) requiring a **Status:**/**Options:** line to
  recognise a section — so every free-form question was listed in the UI and
  then 404'd with "question Q1 not found or already answered" when answered.
  The two paths also minted/consumed ids positionally, so any rewrite of the
  file between the poll and the click re-pointed an id at another section.

  The earlier version of this file asserted on the *source text* of agent-api.py
  and explicitly permitted the writer-side gate to remain ("intentional"). It
  passed for the entire time POST /answer was 100% broken. These tests drive the
  parser instead.

Run: python3 tests/agent-api-pending-questions.test.py
Exit: 0 = all pass, 1 = failure
"""
from __future__ import annotations

import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec)
    sys.modules[name] = m
    spec.loader.exec_module(m)
    return m


api = _load("agent_api", REPO / "src" / "agent-api.py")
cpq = _load("check_pending_questions", REPO / "src" / "check-pending-questions.py")

# The format the agent actually writes today: prose under a `## ` heading, no
# metadata fields, resolved items parked below a `# Resolved` divider.
FREE_FORM = """# Pending Questions

_Open decisions awaiting owner input._

## ❓ Re-auth the Station Gmail connector?
The connector returns HTTP 410. Reconnect, or say "drop it" and I'll use the MCP.

## ❓ Rebuild the Swift menu-bar app?
Dead since Jul 6. It owns the watcher auto-restart safety net.

## [RESOLVED 2026-07-01] ❓ Enable cross-machine sync?
Closed in place with the title-prefix convention, above the divider.

# Resolved & archived detail

## ❓ An old question that was already dealt with
Archived — must never be offered as open.
"""

STRUCTURED = """# Pending Questions

## Ship the timer tool?
- **Status:** unanswered
- **Options:** Yes | No | Later
"""


class TestParse(unittest.TestCase):
    def test_free_form_questions_are_listed(self):
        """No **Status:**/**Options:** markers — still open questions."""
        qs = api.parse_pending_questions(FREE_FORM)
        self.assertEqual([q["text"] for q in qs], [
            "❓ Re-auth the Station Gmail connector?",
            "❓ Rebuild the Swift menu-bar app?",
        ])

    def test_archive_below_resolved_divider_is_excluded(self):
        """Sections under `# Resolved` are audit trail, not open questions."""
        titles = [q["text"] for q in api.parse_pending_questions(FREE_FORM)]
        self.assertNotIn("❓ An old question that was already dealt with", titles)

    def test_resolved_title_prefix_is_excluded(self):
        """The free-form format closes a question in place with a [RESOLVED] prefix."""
        titles = [q["text"] for q in api.parse_pending_questions(FREE_FORM)]
        self.assertFalse([t for t in titles if "Enable cross-machine sync" in t])

    def test_ids_are_stable_when_the_file_is_rewritten(self):
        """The agent rewrites this file constantly. An id minted by one GET must
        still name the same question after unrelated sections move around it —
        a positional id silently re-points at its neighbour."""
        before = {q["text"]: q["id"] for q in api.parse_pending_questions(FREE_FORM)}
        shifted = FREE_FORM.replace(
            "## ❓ Re-auth",
            "## ❓ A brand-new question jumped the queue\nBody.\n\n## ❓ Re-auth",
            1,
        )
        after = {q["text"]: q["id"] for q in api.parse_pending_questions(shifted)}
        self.assertEqual(before["❓ Rebuild the Swift menu-bar app?"],
                         after["❓ Rebuild the Swift menu-bar app?"])

    def test_duplicate_titles_get_distinct_ids(self):
        dupes = "# Q\n\n## Same title\nOne.\n\n## Same title\nTwo.\n"
        ids = [q["id"] for q in api.parse_pending_questions(dupes)]
        self.assertEqual(len(set(ids)), 2, f"ids collided: {ids}")

    def test_structured_format_still_parses(self):
        qs = api.parse_pending_questions(STRUCTURED)
        self.assertEqual(len(qs), 1)
        self.assertEqual(qs[0]["options"], ["Yes", "No", "Later"])

    def test_explicitly_answered_section_is_not_open(self):
        answered = STRUCTURED.replace("**Status:** unanswered", "**Status:** Answered — yes")
        self.assertEqual(api.parse_pending_questions(answered), [])


class TestAnswer(unittest.TestCase):
    def test_free_form_question_is_answerable(self):
        """The regression: listed by GET /status, then 404 on POST /answer."""
        qs = api.parse_pending_questions(FREE_FORM)
        updated = api.answer_pending_question(FREE_FORM, qs[0], "drop it")
        self.assertIn("drop it", updated)
        still_open = [q["text"] for q in api.parse_pending_questions(updated)]
        self.assertNotIn(qs[0]["text"], still_open)
        # The other question, and the archive, are untouched.
        self.assertEqual(still_open, ["❓ Rebuild the Swift menu-bar app?"])
        self.assertIn("## ❓ An old question that was already dealt with", updated)

    def test_answer_silences_the_notifier_too(self):
        """check-pending-questions.py treats a status-less section as unanswered.
        If the answer doesn't land on a **Status:** line it keeps DMing the owner
        hourly about a question they already answered."""
        with tempfile.TemporaryDirectory() as tmp:
            pq = Path(tmp) / "pending-questions.md"
            qs = api.parse_pending_questions(FREE_FORM)
            pq.write_text(api.answer_pending_question(FREE_FORM, qs[0], "drop it"))
            cpq.PQ_FILE = pq
            waiting = [q["title"] for q in cpq.get_waiting_questions()]
            self.assertNotIn(qs[0]["text"], waiting)
            self.assertIn("❓ Rebuild the Swift menu-bar app?", waiting)

    def test_structured_status_line_is_updated_in_place(self):
        qs = api.parse_pending_questions(STRUCTURED)
        updated = api.answer_pending_question(STRUCTURED, qs[0], "Later")
        self.assertIn("- **Status:** Answered", updated)  # bullet preserved
        self.assertNotIn("unanswered", updated)
        self.assertIn("- **Options:** Yes | No | Later", updated)  # options preserved
        self.assertEqual(api.parse_pending_questions(updated), [])

    def test_answer_with_regex_escapes_is_written_literally(self):
        """A raw answer goes into an re.sub replacement — \\1 must not expand."""
        qs = api.parse_pending_questions(STRUCTURED)
        updated = api.answer_pending_question(STRUCTURED, qs[0], r"use \1 and \g<0>")
        self.assertIn(r"use \1 and \g<0>", updated)

    def test_multiline_answer_cannot_forge_a_heading(self):
        """Answers are collapsed to one line — otherwise '## ' in an answer would
        inject a new question section."""
        qs = api.parse_pending_questions(FREE_FORM)
        updated = api.answer_pending_question(FREE_FORM, qs[0], "yes\n## ❓ injected?\nbody")
        titles = [q["text"] for q in api.parse_pending_questions(updated)]
        self.assertNotIn("❓ injected?", titles)


if __name__ == "__main__":
    loader = unittest.TestLoader()
    suite = unittest.TestSuite([
        loader.loadTestsFromTestCase(TestParse),
        loader.loadTestsFromTestCase(TestAnswer),
    ])
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    sys.exit(0 if result.wasSuccessful() else 1)
