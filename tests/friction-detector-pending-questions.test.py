#!/usr/bin/env python3
"""Tests for friction-detector.py check_pending_questions() free-form parsing.

Guards against the #1404 regression: old parser required **Status:** markers
that free-form pending-questions.md files never write — causing undercount to 0.

Per #1265 / #1404 convention:
- A ## section is open unless it carries an explicit resolved status.
- Sections below a `# Resolved` divider are excluded.

Run: python3 tests/friction-detector-pending-questions.test.py
Exit: 0 = pass, 1 = fail
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

# Load friction-detector (hyphenated filename) via importlib.
import importlib
import importlib.util
_SRC_DIR = Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(_SRC_DIR))
_spec = importlib.util.spec_from_file_location("friction_detector", _SRC_DIR / "friction-detector.py")
_fd_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_fd_mod)


def _make_module_with_pq(pq_content: str) -> list:
    """Call check_pending_questions with a temp pending-questions.md file."""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False) as f:
        f.write(pq_content)
        pq_path = Path(f.name)

    original = _fd_mod.personal_path

    def patched_personal_path(name, workspace=None):
        if name == "pending-questions.md":
            return str(pq_path)
        return original(name, workspace)

    _fd_mod.personal_path = patched_personal_path
    try:
        result = _fd_mod.check_pending_questions()
    finally:
        _fd_mod.personal_path = original
        pq_path.unlink(missing_ok=True)
    return result


# Dates for testing
_OLD = (datetime.now() - timedelta(days=3)).date().isoformat()
_NEW = datetime.now().date().isoformat()


class TestCheckPendingQuestionsFreForm(unittest.TestCase):

    def test_free_form_no_status_counted_as_open(self):
        """A section with no **Status:** field is open (free-form convention)."""
        content = f"""# Pending Questions

## Should we use Postgres or SQLite?
- **Asked:** {_OLD}
- Context: perf requirements unclear.
"""
        result = _make_module_with_pq(content)
        self.assertTrue(
            any("Postgres" in r or "SQLite" in r for r in result),
            f"Free-form section should be counted as open. Got: {result}",
        )

    def test_explicit_resolved_status_skipped(self):
        """A section with **Status:** resolved is excluded."""
        content = f"""# Pending Questions

## Old answered question
- **Asked:** {_OLD}
- **Status:** resolved
"""
        result = _make_module_with_pq(content)
        self.assertEqual(result, [], f"Resolved section should be excluded. Got: {result}")

    def test_below_resolved_divider_excluded(self):
        """Sections below `# Resolved` are not counted."""
        content = f"""# Pending Questions

## Open question
- **Asked:** {_OLD}

# Resolved

## Already done
- **Asked:** {_OLD}
"""
        result = _make_module_with_pq(content)
        self.assertTrue(
            any("Open question" in r for r in result),
            f"Open question should be present. Got: {result}",
        )
        self.assertFalse(
            any("Already done" in r for r in result),
            f"Section below # Resolved should be excluded. Got: {result}",
        )

    def test_none_open_placeholder_returns_empty(self):
        """Standard '(none open)' content returns empty."""
        content = "# Pending Questions\n\n_(none open)_\n"
        result = _make_module_with_pq(content)
        self.assertEqual(result, [])

    def test_fresh_question_not_stale(self):
        """A question asked today is not reported as stale."""
        content = f"""# Pending Questions

## Fresh today
- **Asked:** {_NEW}
"""
        result = _make_module_with_pq(content)
        self.assertEqual(result, [], f"Today's question should not be stale. Got: {result}")

    def test_multiple_sections_counted(self):
        """Multiple open free-form sections are all reported."""
        content = f"""# Pending Questions

## First question
- **Asked:** {_OLD}

## Second question
- **Asked:** {_OLD}
"""
        result = _make_module_with_pq(content)
        self.assertEqual(len(result), 2, f"Both sections should be counted. Got: {result}")

    def test_explicit_answered_status_skipped(self):
        """**Status:** answered (case-insensitive) is excluded."""
        content = f"""# Pending Questions

## Answered question
- **Asked:** {_OLD}
- **Status:** Answered
"""
        result = _make_module_with_pq(content)
        self.assertEqual(result, [])

    def test_age_included_in_output(self):
        """Age in days should appear in the output for old questions."""
        content = f"""# Pending Questions

## Old question
- **Asked:** {_OLD}
"""
        result = _make_module_with_pq(content)
        self.assertTrue(result, "Should have at least one result")
        self.assertIn("d old", result[0], f"Age not in output: {result[0]}")


class TestFreFormParserStructural(unittest.TestCase):
    """Structural checks on the source code to confirm the fix is in place."""

    SRC = (Path(__file__).resolve().parent.parent / "src" / "friction-detector.py").read_text()

    def test_does_not_require_status_unanswered(self):
        """Parser must NOT gate on current_status == 'unanswered' (old bug)."""
        self.assertNotIn(
            "current_status == \"unanswered\"",
            self.SRC,
            "Parser must not require **Status: unanswered** — free-form files never write this.",
        )

    def test_resolved_divider_honored(self):
        """Parser must split on `# Resolved` divider."""
        self.assertIn("Resolved", self.SRC)
        self.assertIn("re.split", self.SRC)

    def test_explicit_resolved_regex_present(self):
        """Parser must recognize explicit resolved/answered/done status."""
        self.assertIn("resolved|answered|done|complete", self.SRC)


if __name__ == "__main__":
    loader = unittest.TestLoader()
    suite = loader.loadTestsFromModule(__import__("__main__"))
    runner = unittest.TextTestRunner(verbosity=0)
    result = runner.run(suite)
    if result.wasSuccessful():
        print(f"All {result.testsRun} friction-detector-pending-questions tests passed.")
        sys.exit(0)
    else:
        sys.exit(1)
