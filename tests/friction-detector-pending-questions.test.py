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

import os
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

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

    def test_divider_documented_in_a_comment_is_not_the_divider(self):
        """A `# Resolved` line inside an HTML comment must not end the active region.

        The live outage: the file's own banner warns writers not to append at EOF
        and quotes the rule it documents, putting `` # Resolved` heading `` at the
        start of a line inside the comment. The old anchor matched that, so the
        active region collapsed to the banner and every real section read as
        archived.
        """
        content = f"""<!-- =====
     WRITERS READ THIS FIRST — the parser truncates at the first `
# Resolved` heading
     and only counts sections ABOVE it.
     ===== -->

## Open question
- **Asked:** {_OLD}

# Resolved

## Already done
- **Asked:** {_OLD}
"""
        result = _make_module_with_pq(content)
        self.assertTrue(
            any("Open question" in r for r in result),
            f"Decoy inside the banner must not truncate the file. Got: {result}",
        )
        self.assertFalse(
            any("Already done" in r for r in result),
            f"The real divider must still be honored. Got: {result}",
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


class TestCheckGithubIssues(unittest.TestCase):
    """Tests for check_github_issues() stale-issue detection.

    Mocks subprocess.run to exercise the timezone-aware comparison path
    without requiring a live GitHub token.
    """

    def _call_with_mock_gh(self, updated_at: str):
        import json as _json
        import unittest.mock as _mock

        mock_items = [{"number": 99, "title": "test issue", "updatedAt": updated_at}]
        fake_result = _mock.MagicMock()
        fake_result.returncode = 0
        fake_result.stdout = _json.dumps(mock_items)

        with _mock.patch.object(_fd_mod.subprocess, "run", return_value=fake_result):
            return _fd_mod.check_github_issues()

    def test_stale_issue_reported(self):
        """Issues older than 7 days are reported as stale."""
        old_ts = (datetime.now(tz=__import__("datetime").timezone.utc) - timedelta(days=10)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        issues = self._call_with_mock_gh(old_ts)
        self.assertTrue(
            any("#99" in i for i in issues),
            f"Stale issue should be reported; got: {issues}",
        )

    def test_fresh_issue_not_reported(self):
        """Issues updated within 7 days are not reported."""
        fresh_ts = (datetime.now(tz=__import__("datetime").timezone.utc) - timedelta(days=3)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        issues = self._call_with_mock_gh(fresh_ts)
        self.assertFalse(
            any("#99" in i for i in issues),
            f"Fresh issue should not be reported; got: {issues}",
        )


class TestCheckStaleTasks(unittest.TestCase):
    """Completed task/result pairs must not be reported as stale work."""

    def _call(self, result_location: Optional[str]) -> list:
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = Path(temp_dir)
            tasks = workspace / "tasks"
            results = workspace / "results"
            tasks.mkdir()
            results.mkdir()

            name = "task-cron-main-loop-123.txt"
            task = tasks / name
            task.write_text("source: cron\n")
            old = time.time() - 2 * 3600
            os.utime(task, (old, old))

            if result_location == "live":
                (results / name).write_text("[no-send]\n")
            elif result_location == "bridge-archive":
                archive = results / "archive" / "2026-08"
                archive.mkdir(parents=True)
                (archive / name).write_text("[no-send]\n")
            elif result_location == "flat-archive":
                archive = results / "archive"
                archive.mkdir()
                (archive / name).write_text("[no-send]\n")
            elif result_location == "non-file":
                (results / name).mkdir()
            elif result_location == "retention-archive":
                archive = results / "archive-2026-08-02"
                archive.mkdir()
                (archive / name).write_text("[no-send]\n")

            original_workspace = _fd_mod.WORKSPACE
            original_results = _fd_mod.RESULTS_DIR
            _fd_mod.WORKSPACE = workspace
            _fd_mod.RESULTS_DIR = results
            try:
                return _fd_mod.check_stale_tasks()
            finally:
                _fd_mod.WORKSPACE = original_workspace
                _fd_mod.RESULTS_DIR = original_results

    def test_unprocessed_old_task_is_reported(self):
        self.assertEqual(len(self._call(None)), 1)

    def test_live_result_marks_task_complete(self):
        self.assertEqual(self._call("live"), [])

    def test_bridge_archived_result_marks_task_complete(self):
        self.assertEqual(self._call("bridge-archive"), [])

    def test_flat_archived_result_marks_task_complete(self):
        self.assertEqual(self._call("flat-archive"), [])

    def test_result_named_directory_does_not_mark_task_complete(self):
        self.assertEqual(len(self._call("non-file")), 1)

    def test_retention_archived_result_marks_task_complete(self):
        self.assertEqual(self._call("retention-archive"), [])


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

    def test_divider_logic_is_not_reimplemented_locally(self):
        """The divider cut must come from the shared helper, not a local regex.

        This replaces an `assertIn("re.split", SRC)` text-grep. That assertion
        tracked one spelling of the implementation rather than the property it
        cared about, so the 2026-07-30 refactor — which preserved the behavior and
        is covered behaviorally by test_below_resolved_divider_excluded and
        test_divider_documented_in_a_comment_is_not_the_divider — reported a
        failure with nothing behaviorally wrong.

        The property actually worth guarding is the opposite one: four readers
        each owning a private copy of this regex is what let a single defect go
        dark in four places at once, so a LOCAL redefinition is the regression.
        """
        self.assertIn("active_region", self.SRC,
                      "friction-detector must delegate the divider cut to pending_questions_md")
        self.assertNotIn("re.split(r'^#", self.SRC,
                         "divider regex must not be reimplemented locally")

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
