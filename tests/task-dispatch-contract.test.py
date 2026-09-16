#!/usr/bin/env python3
"""Contract for src/delivery/task_dispatch.py — the shared completion +
priority-selection policy both Codex's and agy's task-notifier.sh delegate
to (sonichi#4303 review: the two bash-hand-rolled copies were behaviorally
wrong, not just duplicated).

Two regressions pinned here reproduce the review's own repro output
verbatim (`notifier_TYPE=0 canonical_found=... canonical_ready=...`):
  1. A zero-byte / partial live result must NOT read as delivered.
  2. `task-probe-1-other.txt` must NOT be mistaken for an epoch archive of
     `task-probe` — only an ALL-DIGITS suffix is an epoch archive.
"""
from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

from delivery.task_dispatch import (  # noqa: E402
    _main,
    has_ready_result,
    next_pending_task,
    pending_candidates,
)

CLI = REPO / "src" / "delivery" / "task_dispatch.py"


class HasReadyResultTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.results_dir = Path(self.tmp.name) / "results"
        self.results_dir.mkdir()

    def test_missing_result_is_not_ready(self):
        self.assertFalse(has_ready_result(self.results_dir, "task-probe.txt"))

    def test_nonempty_live_result_is_ready(self):
        (self.results_dir / "task-probe.txt").write_text("done\n")
        self.assertTrue(has_ready_result(self.results_dir, "task-probe.txt"))

    def test_zero_byte_live_result_is_not_ready(self):
        # Regression: a naive `[ -f ... ]` presence check (the bash copies'
        # bug) reads this as delivered and permanently suppresses the task.
        (self.results_dir / "task-probe.txt").write_text("")
        self.assertFalse(has_ready_result(self.results_dir, "task-probe.txt"))

    def test_whitespace_only_live_result_is_not_ready(self):
        (self.results_dir / "task-probe.txt").write_text("   \n\n")
        self.assertFalse(has_ready_result(self.results_dir, "task-probe.txt"))

    def test_legitimate_epoch_archive_is_ready(self):
        archive = self.results_dir / "archive"
        archive.mkdir()
        (archive / "task-probe-1700000000.txt").write_text("done\n")
        self.assertTrue(has_ready_result(self.results_dir, "task-probe.txt"))

    def test_another_tasks_flat_archive_is_not_mistaken_for_this_one(self):
        # Regression: a leading-digit glob matched `task-probe-1-other.txt`
        # (a real archive of a DIFFERENT task) as `task-probe`'s epoch archive.
        archive = self.results_dir / "archive"
        archive.mkdir()
        (archive / "task-probe-1-other.txt").write_text("done\n")
        self.assertFalse(has_ready_result(self.results_dir, "task-probe.txt"))
        # And the file's actual owner is still found correctly.
        self.assertTrue(has_ready_result(self.results_dir, "task-probe-1-other.txt"))

    def test_month_partitioned_archive_is_ready(self):
        month = self.results_dir / "archive" / "2026-09"
        month.mkdir(parents=True)
        (month / "task-probe.txt").write_text("done\n")
        self.assertTrue(has_ready_result(self.results_dir, "task-probe.txt"))

    def test_a_path_traversal_filename_is_never_ready(self):
        self.assertFalse(has_ready_result(self.results_dir, "../../etc/passwd.txt"))


class PendingCandidatesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.tasks_dir = Path(self.tmp.name) / "tasks"
        self.results_dir = Path(self.tmp.name) / "results"
        self.tasks_dir.mkdir()
        self.results_dir.mkdir()

    def _write_task(self, name, priority="normal"):
        (self.tasks_dir / name).write_text(f"priority: {priority}\ntask: x\n")

    def test_completed_task_is_excluded(self):
        self._write_task("task-a.txt")
        self._write_task("task-b.txt")
        (self.results_dir / "task-a.txt").write_text("done\n")
        self.assertEqual(next_pending_task(self.tasks_dir, self.results_dir), "task-b.txt")

    def test_zero_byte_result_does_not_suppress_the_task(self):
        self._write_task("task-a.txt")
        (self.results_dir / "task-a.txt").write_text("")
        self.assertEqual(next_pending_task(self.tasks_dir, self.results_dir), "task-a.txt")

    def test_urgent_beats_normal(self):
        self._write_task("task-normal.txt", priority="normal")
        self._write_task("task-urgent.txt", priority="urgent")
        self.assertEqual(next_pending_task(self.tasks_dir, self.results_dir), "task-urgent.txt")

    def test_no_pending_tasks_is_none(self):
        self.assertIsNone(next_pending_task(self.tasks_dir, self.results_dir))

    def test_pending_candidates_yields_every_unfinished_task_in_order(self):
        self._write_task("task-normal.txt", priority="normal")
        self._write_task("task-urgent.txt", priority="urgent")
        self._write_task("task-low.txt", priority="low")
        (self.results_dir / "task-low.txt").write_text("done\n")
        self.assertEqual(
            list(pending_candidates(self.tasks_dir, self.results_dir)),
            ["task-urgent.txt", "task-normal.txt"],
        )

    def test_a_directory_matching_the_glob_is_skipped_not_yielded(self):
        # `*.txt` glob matches by name only — a directory shaped like a task
        # filename must not be treated as one.
        (self.tasks_dir / "task-a.txt").mkdir()
        self._write_task("task-b.txt")
        self.assertEqual(list(pending_candidates(self.tasks_dir, self.results_dir)), ["task-b.txt"])

    def test_a_name_carrying_a_traversal_sentinel_is_never_yielded(self):
        # Defensive guard: never trust a name from the sort step, real glob or not.
        self._write_task("task-real.txt")
        real = list(Path(self.tasks_dir).glob("*.txt"))
        fake = mock.Mock(spec=Path)
        fake.is_file.return_value = True
        fake.name = "../escaped.txt"
        with mock.patch("delivery.task_dispatch.sort_tasks_by_priority", return_value=[fake, *real]):
            self.assertEqual(
                list(pending_candidates(self.tasks_dir, self.results_dir)), ["task-real.txt"])


class CliTest(unittest.TestCase):
    """The bash callers shell out to this file directly, so the CLI surface
    is a load-bearing contract, not incidental."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.tasks_dir = Path(self.tmp.name) / "tasks"
        self.results_dir = Path(self.tmp.name) / "results"
        self.tasks_dir.mkdir()
        self.results_dir.mkdir()

    def _run(self, *args):
        return subprocess.run(
            [sys.executable, str(CLI), *args],
            capture_output=True, text=True, timeout=10,
        )

    def test_has_result_exit_codes(self):
        (self.results_dir / "task-a.txt").write_text("done\n")
        self.assertEqual(self._run("has-result", str(self.results_dir), "task-a.txt").returncode, 0)
        self.assertEqual(self._run("has-result", str(self.results_dir), "task-b.txt").returncode, 1)

    def test_next_pending_prints_name_and_exits_zero(self):
        (self.tasks_dir / "task-a.txt").write_text("task: x\n")
        result = self._run("next-pending", str(self.tasks_dir), str(self.results_dir))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "task-a.txt")

    def test_next_pending_exits_nonzero_when_queue_empty(self):
        result = self._run("next-pending", str(self.tasks_dir), str(self.results_dir))
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, "")

    def test_pending_candidates_lists_every_unfinished_task(self):
        (self.tasks_dir / "task-a.txt").write_text("task: x\n")
        (self.tasks_dir / "task-b.txt").write_text("task: y\n")
        (self.results_dir / "task-a.txt").write_text("done\n")
        result = self._run("pending-candidates", str(self.tasks_dir), str(self.results_dir))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "task-b.txt")


class MainDispatchTest(unittest.TestCase):
    """In-process `_main` calls — coverage can't see into `CliTest`'s subprocess."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.tasks_dir = Path(self.tmp.name) / "tasks"
        self.results_dir = Path(self.tmp.name) / "results"
        self.tasks_dir.mkdir()
        self.results_dir.mkdir()

    def test_too_few_args_prints_usage_and_exits_2(self):
        with mock.patch("sys.stderr"):
            self.assertEqual(_main(["has-result", str(self.results_dir)]), 2)
        self.assertEqual(_main([]), 2)

    def test_has_result_both_outcomes(self):
        (self.results_dir / "task-a.txt").write_text("done\n")
        self.assertEqual(_main(["has-result", str(self.results_dir), "task-a.txt"]), 0)
        self.assertEqual(_main(["has-result", str(self.results_dir), "task-b.txt"]), 1)

    def test_pending_candidates_both_outcomes(self):
        self.assertEqual(_main(["pending-candidates", str(self.tasks_dir), str(self.results_dir)]), 1)
        (self.tasks_dir / "task-a.txt").write_text("task: x\n")
        self.assertEqual(_main(["pending-candidates", str(self.tasks_dir), str(self.results_dir)]), 0)

    def test_next_pending_both_outcomes(self):
        self.assertEqual(_main(["next-pending", str(self.tasks_dir), str(self.results_dir)]), 1)
        (self.tasks_dir / "task-a.txt").write_text("task: x\n")
        self.assertEqual(_main(["next-pending", str(self.tasks_dir), str(self.results_dir)]), 0)

    def test_unknown_command_exits_2(self):
        with mock.patch("sys.stderr"):
            rc = _main(["bogus-command", str(self.tasks_dir), str(self.results_dir)])
        self.assertEqual(rc, 2)


if __name__ == "__main__":
    unittest.main()
