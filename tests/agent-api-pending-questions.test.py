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
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

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

    def test_duplicate_id_survives_resolving_an_earlier_duplicate(self):
        """The #2103 review's blocking case: a stale duplicate-title id must
        never migrate to a neighbour.

        Three open sections share a title. The UI is handed an id for the
        *second* section; the agent then answers the *first*, rewriting the
        file. The id the UI still holds must resolve the same (second) section
        — never the third — or, at worst, 404. It must not silently record the
        owner's answer against a different question.

        This is the guarantee an occurrence-count suffix (`-2`/`-3`) breaks:
        answering the first section renumbers the survivors, so `...-2` slides
        from the second section onto the third.
        """
        dupes = (
            "# Pending Questions\n\n"
            "## Same title\nFirst — ALPHA.\n\n"
            "## Same title\nSecond — BRAVO.\n\n"
            "## Same title\nThird — CHARLIE.\n"
        )
        before = api.parse_pending_questions(dupes)
        self.assertEqual(len(before), 3)
        id_for_second = before[1]["id"]
        id_for_third = before[2]["id"]

        # The agent resolves the first duplicate and rewrites the file.
        rewritten = api.answer_pending_question(dupes, before[0], "done with the first")
        after = api.parse_pending_questions(rewritten)

        # The originally-issued ids still name their own sections, unchanged.
        second = next((q for q in after if q["id"] == id_for_second), None)
        self.assertIsNotNone(second, "the second section's id stopped resolving")
        self.assertIn("BRAVO", second["detail"])
        self.assertNotIn("CHARLIE", second["detail"])  # never the neighbour

        third = next((q for q in after if q["id"] == id_for_third), None)
        self.assertIsNotNone(third)
        self.assertIn("CHARLIE", third["detail"])

        # And answering through the stale id lands on BRAVO, leaving CHARLIE open.
        final = api.answer_pending_question(rewritten, second, "picked BRAVO")
        still_open = api.parse_pending_questions(final)
        self.assertEqual([q["detail"] for q in still_open], ["Third — CHARLIE."])

    def test_ids_are_independent_of_sibling_count(self):
        """A section's id must not depend on how many same-title siblings are
        currently open. Removing an earlier duplicate must leave every survivor's
        id byte-for-byte identical (no renumbering)."""
        dupes = (
            "# Q\n\n"
            "## Same title\nAlpha body.\n\n"
            "## Same title\nBravo body.\n\n"
            "## Same title\nCharlie body.\n"
        )
        by_detail = {q["detail"]: q["id"] for q in api.parse_pending_questions(dupes)}
        # Drop the first duplicate entirely (not just answer it).
        pruned = dupes.replace("## Same title\nAlpha body.\n\n", "", 1)
        after = {q["detail"]: q["id"] for q in api.parse_pending_questions(pruned)}
        self.assertEqual(after["Bravo body."], by_detail["Bravo body."])
        self.assertEqual(after["Charlie body."], by_detail["Charlie body."])

    def test_structured_format_still_parses(self):
        qs = api.parse_pending_questions(STRUCTURED)
        self.assertEqual(len(qs), 1)
        self.assertEqual(qs[0]["options"], ["Yes", "No", "Later"])

    def test_explicitly_answered_section_is_not_open(self):
        answered = STRUCTURED.replace("**Status:** unanswered", "**Status:** Answered — yes")
        self.assertEqual(api.parse_pending_questions(answered), [])


    def test_dated_heading_parses_asked_and_age_days(self):
        """`## YYYY-MM-DD — ...` gives a bare-date `asked` and a non-negative age."""
        dated = "# Q\n\n## 2020-01-01 — sutando-life CI: something old\nBody.\n"
        q = api.parse_pending_questions(dated)[0]
        self.assertEqual(q["asked"], "2020-01-01")
        expected_age = (datetime.now() - datetime(2020, 1, 1)).days
        self.assertEqual(q["age_days"], expected_age)

    def test_datetime_heading_t_z_form_parses_asked_and_age_days(self):
        """`## YYYY-MM-DDTHH:MMZ — ...` is the other heading shape in the wild."""
        dated = "# Q\n\n## 2020-01-01T02:20Z — should this host do X?\nBody.\n"
        q = api.parse_pending_questions(dated)[0]
        self.assertEqual(q["asked"], "2020-01-01T02:20Z")
        expected_age = (datetime.now() - datetime(2020, 1, 1, 2, 20)).days
        self.assertEqual(q["age_days"], expected_age)

    def test_undated_heading_gives_null_asked_and_age(self):
        """No leading date on the heading — must not fabricate an age."""
        q = api.parse_pending_questions(FREE_FORM)[0]
        self.assertIsNone(q["asked"])
        self.assertIsNone(q["age_days"])


class TestPendingQuestionRows(unittest.TestCase):
    """`_pending_question_rows()` orders by age — oldest first — with undated
    questions sorted last (never defaulted to age 0, which would put them
    first instead)."""

    def _rows(self, content: str) -> list[dict]:
        with tempfile.TemporaryDirectory() as tmp:
            pending = Path(tmp) / "pending-questions.md"
            pending.write_text(content)
            with mock.patch.object(api, "personal_path", return_value=pending):
                return api._pending_question_rows()

    def test_rows_are_oldest_first_undated_sorts_last(self):
        today = datetime.now()
        just_now = today.strftime("%Y-%m-%d")  # age_days == 0
        mid = (today - timedelta(days=10)).strftime("%Y-%m-%d")
        old = (today - timedelta(days=40)).strftime("%Y-%m-%d")
        # Undated BEFORE the age-0 heading: the one order in which defaulting
        # undated to 0 would survive a stable sort and pass anyway.
        mixed = (
            "# Pending Questions\n\n"
            "## Undated question\nBody.\n\n"
            f"## {just_now} — asked just now\nBody.\n\n"
            f"## {old} — asked weeks ago\nBody.\n\n"
            f"## {mid} — asked a while ago\nBody.\n"
        )
        texts = [row["text"] for row in self._rows(mixed)]
        self.assertEqual(texts, [
            f"{old} — asked weeks ago",
            f"{mid} — asked a while ago",
            f"{just_now} — asked just now",
            "Undated question",
        ])

    def test_rows_carry_asked_and_age_days_after_stripping_offsets(self):
        rows = self._rows("# Q\n\n## 2020-01-01 — old one\nBody.\n")
        self.assertNotIn("start", rows[0])
        self.assertNotIn("end", rows[0])
        self.assertEqual(rows[0]["asked"], "2020-01-01")
        self.assertIsInstance(rows[0]["age_days"], int)


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
        loader.loadTestsFromTestCase(TestPendingQuestionRows),
        loader.loadTestsFromTestCase(TestAnswer),
    ])
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    sys.exit(0 if result.wasSuccessful() else 1)
