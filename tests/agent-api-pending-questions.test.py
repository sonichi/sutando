#!/usr/bin/env python3
"""Structural regression test for agent-api.py GET /status pending-questions fix (2026-06-07).

The /status endpoint's questions parser used to require **Status:** or **Options:**
markers to recognise a pending-questions.md entry (post-#1265 free-form format).
This caused the web UI "Asks" panel to always show 0 questions when there were 12+.

Guards: the fix must survive future refactors that re-introduce the old gate.

Run: python3 tests/agent-api-pending-questions.test.py
Exit: 0 = all pass, 1 = failure
"""
from __future__ import annotations
import sys
import unittest
from pathlib import Path

SRC = (Path(__file__).resolve().parent.parent / "src" / "agent-api.py").read_text()


class TestAgentApiPendingQuestionsParser(unittest.TestCase):
    """GET /status questions parser must handle free-form pending-questions.md.

    post-#1265 format: no **Status:** markers; sections use [RESOLVED ...] prefix.
    pre-fix: **Status:** not in body → skip ALL free-form sections → 0 questions.
    """

    def test_preamble_skip_present(self):
        """Parser must skip the preamble chunk (before first ## header) via i==0 check."""
        self.assertIn(
            "i == 0",
            SRC,
            "GET /status questions parser must skip preamble chunk via i==0 guard",
        )

    def test_resolved_prefix_check_present(self):
        """Parser must skip [RESOLVED ...] sections via title prefix check."""
        self.assertIn(
            "startswith('[RESOLVED')",
            SRC,
            "Parser must check title.startswith('[RESOLVED') to skip resolved sections",
        )

    def test_status_gate_count_reduced(self):
        """The **Status:** not in body gate should appear at most once (POST /answer path only).

        The GET /status path should NOT have this gate — it would exclude all free-form
        questions (which don't have **Status:** markers per post-#1265 convention).
        """
        import re
        # Count occurrences of the status-gate pattern
        matches = re.findall(r"Status.*not in body|not in body.*Status", SRC)
        self.assertLessEqual(
            len(matches), 1,
            f"Status gate found {len(matches)} times — GET /status path should not "
            "have this gate (only POST /answer path should use it for finding "
            "structured questions to update). More than 1 occurrence means the "
            "GET /status fix was reverted.",
        )

    def test_resolved_text_check_not_removed(self):
        """The **Status:** resolved check in POST /answer must still be present.

        This gate is intentional — when ANSWERING a question, we need to find
        sections with **Status:** to update. Don't accidentally remove it.
        """
        self.assertIn(
            "resolved|answered|done|complete",
            SRC,
            "The **Status:** resolved skip in POST /answer must remain — "
            "needed to avoid re-answering already-resolved questions",
        )


if __name__ == "__main__":
    suite = unittest.TestLoader().loadTestsFromTestCase(TestAgentApiPendingQuestionsParser)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    sys.exit(0 if result.wasSuccessful() else 1)
