#!/usr/bin/env python3
"""Contract for src/delivery/task_dispatch.py — the completion + priority
selection policy every external task-notifier (Codex, agy, Claude) delegates
to instead of carrying its own bash copy.

Pinned regressions, each one a defect the bash copies shipped:
  1. A zero-byte / whitespace-only live result is NOT a delivery.
  2. `task-probe-1-other.txt` is NOT an epoch archive of `task-probe`.
  3. `archive-YYYY-MM-DD/<stem>-<epoch>.txt` IS a delivery (the shape Codex's
     bash covered and the shared locator initially did not).
  4. A ready ARCHIVED result IS a delivery even when an empty or
     whitespace-only live `results/<id>.txt` also exists: the first-existing
     lookup stopped at the placeholder and requeued a completed task.
And the parity that makes this the one truth: on every fixture — single-state
and mixed — the answer equals what `watch-tasks-stream.sh`'s
`handler_result_exists` computes, running the shipped bash function itself.

Run: python3 tests/task-dispatch-contract.test.py
"""
from __future__ import annotations

import re
import shlex
import subprocess
import sys
import tempfile
import unittest
from io import StringIO
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

from delivery.task_dispatch import (  # noqa: E402
    _main,
    find_ready_result,
    has_ready_result,
    next_pending_task,
    pending_candidates,
)

CLI = REPO / "src" / "delivery" / "task_dispatch.py"
WATCHER = REPO / "src" / "watch-tasks-stream.sh"
TASK = "task-probe"
F = f"{TASK}.txt"


def _write(path: Path, text: str = "done\n") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


# name -> (relative path under results/, body). Every archive layout a consumer meets.
RESULT_FIXTURES = {
    "missing": None,
    "live-ready": (F, "done\n"),
    "live-empty": (F, ""),
    "live-whitespace": (F, "   \n\n"),
    "archive-flat": (f"archive/{F}", "done\n"),
    "archive-month": (f"archive/2026-09/{F}", "done\n"),
    "archive-flat-epoch": (f"archive/{TASK}-1700000000.txt", "done\n"),
    "archive-month-epoch": (f"archive/2026-09/{TASK}-1700000000.txt", "done\n"),
    "retention-exact": (f"archive-2026-07-26/{F}", "done\n"),
    "retention-epoch": (f"archive-2026-07-26/{TASK}-1784690000.txt", "done\n"),
    "archive-empty": (f"archive/{F}", ""),
    "decoy-flat": (f"archive/{TASK}-1-other.txt", "done\n"),
    "decoy-retention": (f"archive-2026-07-26/{TASK}-1-other.txt", "done\n"),
    "decoy-nondigit-suffix": (f"archive/{TASK}-1700000000x.txt", "done\n"),
}
EXPECTED_READY = {
    "live-ready", "archive-flat", "archive-month", "archive-flat-epoch",
    "archive-month-epoch", "retention-exact", "retention-epoch",
}

# Several result locations at once (RESULT_FIXTURES holds one, so it cannot see a placeholder
# hiding a ready archive). name -> ([(relative path, body), ...], verdict, ready body path or None)
EMPTY, WS = "", "   \n\n"
MIXED_FIXTURES = {
    "archive-plus-empty-live": (
        [(f"archive/2026-09/{F}", "done\n"), (F, EMPTY)], True, f"archive/2026-09/{F}"),
    "archive-plus-whitespace-live": (
        [(f"archive/{F}", "done\n"), (F, WS)], True, f"archive/{F}"),
    "retention-epoch-plus-empty-live": (
        [(f"archive-2026-07-26/{TASK}-1784690000.txt", "done\n"), (F, EMPTY)],
        True, f"archive-2026-07-26/{TASK}-1784690000.txt"),
    "month-epoch-plus-whitespace-live": (
        [(f"archive/2026-09/{TASK}-1700000000.txt", "done\n"), (F, WS)],
        True, f"archive/2026-09/{TASK}-1700000000.txt"),
    "flat-epoch-behind-empty-live-and-empty-month": (
        [(F, EMPTY), (f"archive/2026-09/{F}", EMPTY), (f"archive/{TASK}-1700000000.txt", "done\n")],
        True, f"archive/{TASK}-1700000000.txt"),
    "live-plus-stale-archive": (
        [(F, "fresh\n"), (f"archive/{F}", "stale\n")], True, F),
    "empty-live-only": ([(F, EMPTY)], False, None),
    "whitespace-live-only": ([(F, WS)], False, None),
    "empty-live-plus-empty-archive": ([(F, EMPTY), (f"archive/{F}", EMPTY)], False, None),
    "empty-live-plus-whitespace-retention": (
        [(F, EMPTY), (f"archive-2026-07-26/{F}", WS)], False, None),
    "empty-live-plus-decoy-suffix": (
        [(F, EMPTY), (f"archive/{TASK}-1-other.txt", "done\n")], False, None),
    "empty-live-plus-decoy-notes": (
        [(F, EMPTY), (f"archive/{TASK}-notes.txt", "done\n")], False, None),
    "empty-live-plus-decoy-longer-id": (
        [(F, EMPTY), (f"archive/{TASK}2.txt", "done\n"), (f"archive/2026-09/{TASK}2.txt", "done\n")],
        False, None),
}


def _populate(results: Path, entries) -> None:
    for rel, body in entries:
        _write(results / rel, body)


class HasReadyResultTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.results_dir = Path(self.tmp.name) / "results"
        self.results_dir.mkdir()

    def _build(self, fixture):
        spec = RESULT_FIXTURES[fixture]
        if spec is not None:
            _write(self.results_dir / spec[0], spec[1])

    def test_every_fixture_has_the_expected_verdict(self):
        for fixture in RESULT_FIXTURES:
            with self.subTest(fixture=fixture):
                with tempfile.TemporaryDirectory() as td:
                    results = Path(td) / "results"
                    results.mkdir()
                    spec = RESULT_FIXTURES[fixture]
                    if spec is not None:
                        _write(results / spec[0], spec[1])
                    self.assertEqual(
                        has_ready_result(results, F), fixture in EXPECTED_READY, fixture)

    def test_zero_byte_live_result_is_not_ready(self):
        # Regression 1: `[ -f ]` presence read this as delivered and buried the task.
        self._build("live-empty")
        self.assertFalse(has_ready_result(self.results_dir, F))

    def test_whitespace_only_live_result_is_not_ready(self):
        self._build("live-whitespace")
        self.assertFalse(has_ready_result(self.results_dir, F))

    def test_another_tasks_archive_is_not_mistaken_for_this_one(self):
        # Regression 2: `<stem>-[0-9]*.txt` matched `task-probe-1-other.txt`.
        self._build("decoy-flat")
        self.assertFalse(has_ready_result(self.results_dir, F))
        self.assertTrue(has_ready_result(self.results_dir, f"{TASK}-1-other.txt"))

    def test_retention_dir_epoch_archive_is_ready(self):
        # Regression 3: the retention scan was exact-name only.
        self._build("retention-epoch")
        self.assertTrue(has_ready_result(self.results_dir, F))

    def test_retention_dir_decoy_is_not_ready(self):
        self._build("decoy-retention")
        self.assertFalse(has_ready_result(self.results_dir, F))

    def test_empty_archived_file_is_not_ready(self):
        self._build("archive-empty")
        self.assertFalse(has_ready_result(self.results_dir, F))

    def test_a_path_traversal_filename_is_never_ready(self):
        _write(Path(self.tmp.name) / "passwd.txt")
        self.assertFalse(has_ready_result(self.results_dir, "../passwd.txt"))
        self.assertFalse(has_ready_result(self.results_dir, "../../etc/passwd.txt"))

    def test_a_bare_task_id_without_suffix_is_accepted(self):
        self._build("live-ready")
        self.assertTrue(has_ready_result(self.results_dir, TASK))


class MixedStateTest(unittest.TestCase):
    """Regression 4: a ready archive behind an empty or whitespace-only live
    placeholder. The verdict must come from walking every candidate to a READY
    body, never from the first path that merely exists."""

    def _results(self, fixture):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        results = Path(td.name) / "results"
        results.mkdir()
        _populate(results, MIXED_FIXTURES[fixture][0])
        return results

    def test_every_mixed_fixture_has_the_expected_verdict(self):
        for fixture, (_, expected, _) in MIXED_FIXTURES.items():
            with self.subTest(fixture=fixture):
                self.assertEqual(has_ready_result(self._results(fixture), F), expected, fixture)

    def test_find_ready_result_returns_the_ready_body_not_the_placeholder(self):
        for fixture, (_, _, ready_rel) in MIXED_FIXTURES.items():
            with self.subTest(fixture=fixture):
                results = self._results(fixture)
                found = find_ready_result(results, TASK)
                self.assertEqual(found, None if ready_rel is None else results / ready_rel, fixture)

    def test_ready_archive_behind_empty_live_is_a_delivery(self):
        results = self._results("archive-plus-empty-live")
        self.assertTrue(has_ready_result(results, F))
        self.assertEqual(find_ready_result(results, TASK), results / "archive" / "2026-09" / F)

    def test_ready_archive_behind_whitespace_live_is_a_delivery(self):
        results = self._results("archive-plus-whitespace-live")
        self.assertTrue(has_ready_result(results, F))
        self.assertEqual(find_ready_result(results, TASK), results / "archive" / F)

    def test_epoch_rearchive_behind_empty_live_is_a_delivery(self):
        results = self._results("retention-epoch-plus-empty-live")
        self.assertTrue(has_ready_result(results, F))

    def test_a_ready_live_result_wins_over_a_stale_archive(self):
        results = self._results("live-plus-stale-archive")
        self.assertEqual(find_ready_result(results, TASK), results / F)

    def test_placeholders_everywhere_are_still_not_a_delivery(self):
        for fixture in ("empty-live-only", "whitespace-live-only",
                        "empty-live-plus-empty-archive", "empty-live-plus-whitespace-retention"):
            with self.subTest(fixture=fixture):
                self.assertFalse(has_ready_result(self._results(fixture), F))
                self.assertIsNone(find_ready_result(self._results(fixture), TASK))

    def test_decoys_never_rescue_an_empty_live_placeholder(self):
        for fixture in ("empty-live-plus-decoy-suffix", "empty-live-plus-decoy-notes",
                        "empty-live-plus-decoy-longer-id"):
            with self.subTest(fixture=fixture):
                self.assertFalse(has_ready_result(self._results(fixture), F))

    def test_the_walk_reads_every_candidate_until_one_is_ready(self):
        # Two placeholders precede the ready flat epoch archive; the injected reader
        # records the walk, so a first-hit shortcut would show as a one-element list.
        results = self._results("flat-epoch-behind-empty-live-and-empty-month")
        seen = []

        def reader(path):
            seen.append(path)
            return path.read_text().strip() or None

        found = find_ready_result(results, TASK, reader=reader)
        self.assertEqual(found, results / "archive" / f"{TASK}-1700000000.txt")
        self.assertEqual(seen, [results / F, results / "archive" / "2026-09" / F, found])

    def test_a_reader_that_accepts_nothing_yields_none_after_the_full_walk(self):
        results = self._results("archive-plus-empty-live")
        seen = []
        self.assertIsNone(find_ready_result(results, TASK, reader=lambda p: seen.append(p)))
        self.assertEqual(len(seen), 2)

    def test_traversal_id_yields_no_candidates(self):
        results = self._results("archive-plus-empty-live")
        self.assertIsNone(find_ready_result(results, "../" + TASK))


class WatcherParityTest(unittest.TestCase):
    """`handler_result_exists` in watch-tasks-stream.sh is bash. Run THAT
    function — its shipped text, with the watcher's own variables bound — on
    every fixture, single-state and mixed, and require the same verdict as
    `has_ready_result`: one contract, two callers, one truth."""

    @classmethod
    def setUpClass(cls):
        text = WATCHER.read_text()
        m = re.search(r"\nhandler_result_exists\(\) \{\n(.*?)\n\}\n", text, re.S)
        assert m, "handler_result_exists() not found in watch-tasks-stream.sh"
        cls.function = m.group(0).strip("\n")

    def _watcher_verdict(self, results: Path) -> bool:
        script = "\n".join([
            f"SUTANDO_PY_BIN={shlex.quote(sys.executable)}",
            f"__REPO_ROOT={shlex.quote(str(REPO))}",
            f"RESULTS_DIR={shlex.quote(str(results))}",
            self.function,
            f"handler_result_exists {shlex.quote(F)}",
        ])
        proc = subprocess.run(["/bin/bash", "-c", script], capture_output=True, text=True, timeout=30)
        self.assertIn(proc.returncode, (0, 1), proc.stderr)
        return proc.returncode == 0

    def test_same_verdict_on_every_fixture(self):
        for fixture, spec in RESULT_FIXTURES.items():
            with self.subTest(fixture=fixture):
                with tempfile.TemporaryDirectory() as td:
                    results = Path(td) / "results"
                    results.mkdir()
                    if spec is not None:
                        _write(results / spec[0], spec[1])
                    self.assertEqual(
                        has_ready_result(results, F), self._watcher_verdict(results), fixture)

    def test_same_verdict_on_every_mixed_fixture(self):
        for fixture, (entries, expected, _) in MIXED_FIXTURES.items():
            with self.subTest(fixture=fixture):
                with tempfile.TemporaryDirectory() as td:
                    results = Path(td) / "results"
                    results.mkdir()
                    _populate(results, entries)
                    watcher = self._watcher_verdict(results)
                    self.assertEqual(has_ready_result(results, F), watcher, fixture)
                    self.assertEqual(watcher, expected, fixture)

    def test_the_watcher_delegates_rather_than_looking_up_itself(self):
        # The shipped function must reach the shared owner; a private
        # find_result-then-read pair is the first-hit shape being retired.
        self.assertIn("src/delivery/task_dispatch.py", self.function)
        self.assertIn("has-result", self.function)
        self.assertNotIn("find_result", self.function)
        self.assertNotIn("read_ready_result", self.function)


class PendingCandidatesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.tasks_dir = Path(self.tmp.name) / "tasks"
        self.results_dir = Path(self.tmp.name) / "results"
        self.claims_dir = Path(self.tmp.name) / "claims"
        self.tasks_dir.mkdir()
        self.results_dir.mkdir()
        self.claims_dir.mkdir()

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

    def test_retention_epoch_archive_suppresses_the_task(self):
        self._write_task("task-a.txt")
        _write(self.results_dir / "archive-2026-07-26" / "task-a-1784690000.txt")
        self.assertIsNone(next_pending_task(self.tasks_dir, self.results_dir))

    def test_ready_archive_behind_empty_live_placeholder_suppresses_the_task(self):
        # Regression 4: the placeholder was the first hit and the task requeued.
        self._write_task("task-a.txt")
        self._write_task("task-b.txt")
        _write(self.results_dir / "archive" / "2026-09" / "task-a.txt")
        (self.results_dir / "task-a.txt").write_text("")
        self.assertEqual(list(pending_candidates(self.tasks_dir, self.results_dir)), ["task-b.txt"])
        self.assertEqual(next_pending_task(self.tasks_dir, self.results_dir), "task-b.txt")

    def test_ready_archive_behind_whitespace_live_placeholder_suppresses_the_task(self):
        self._write_task("task-a.txt")
        _write(self.results_dir / "archive" / "task-a.txt")
        (self.results_dir / "task-a.txt").write_text("   \n\n")
        self.assertEqual(list(pending_candidates(self.tasks_dir, self.results_dir)), [])
        self.assertIsNone(next_pending_task(self.tasks_dir, self.results_dir))

    def test_epoch_rearchive_behind_empty_live_placeholder_suppresses_the_task(self):
        self._write_task("task-a.txt")
        _write(self.results_dir / "archive-2026-07-26" / "task-a-1784690000.txt")
        (self.results_dir / "task-a.txt").write_text("")
        self.assertIsNone(next_pending_task(self.tasks_dir, self.results_dir))

    def test_empty_live_placeholder_alone_keeps_the_task_pending(self):
        self._write_task("task-a.txt")
        (self.results_dir / "task-a.txt").write_text("")
        _write(self.results_dir / "archive" / "task-a.txt", "")
        self.assertEqual(list(pending_candidates(self.tasks_dir, self.results_dir)), ["task-a.txt"])

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

    def test_a_claimed_task_is_held_back(self):
        self._write_task("task-a.txt", priority="urgent")
        self._write_task("task-b.txt")
        (self.claims_dir / "task-a.txt").write_text("")
        self.assertEqual(
            list(pending_candidates(self.tasks_dir, self.results_dir, claims_dir=self.claims_dir)),
            ["task-b.txt"])
        self.assertEqual(
            next_pending_task(self.tasks_dir, self.results_dir, claims_dir=self.claims_dir),
            "task-b.txt")

    def test_without_a_claims_dir_claims_are_not_consulted(self):
        self._write_task("task-a.txt")
        (self.claims_dir / "task-a.txt").write_text("")
        self.assertEqual(list(pending_candidates(self.tasks_dir, self.results_dir)), ["task-a.txt"])

    def test_a_directory_matching_the_glob_is_skipped_not_yielded(self):
        (self.tasks_dir / "task-a.txt").mkdir()
        self._write_task("task-b.txt")
        self.assertEqual(list(pending_candidates(self.tasks_dir, self.results_dir)), ["task-b.txt"])

    def test_a_name_carrying_a_traversal_sentinel_is_never_yielded(self):
        self._write_task("task-real.txt")
        real = list(Path(self.tasks_dir).glob("*.txt"))
        fakes = []
        for bad in ("../escaped.txt", "sub/dir.txt", ""):
            fake = mock.Mock(spec=Path)
            fake.is_file.return_value = True
            fake.name = bad
            fakes.append(fake)
        with mock.patch("delivery.task_dispatch.sort_tasks_by_priority", return_value=[*fakes, *real]):
            self.assertEqual(
                list(pending_candidates(self.tasks_dir, self.results_dir)), ["task-real.txt"])


class MainDispatchTest(unittest.TestCase):
    """In-process `_main` calls: the bash callers' whole contract, visible to coverage."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.tasks_dir = Path(self.tmp.name) / "tasks"
        self.results_dir = Path(self.tmp.name) / "results"
        self.claims_dir = Path(self.tmp.name) / "claims"
        self.tasks_dir.mkdir()
        self.results_dir.mkdir()
        self.claims_dir.mkdir()

    def _run(self, *argv):
        out, err = StringIO(), StringIO()
        with mock.patch("sys.stdout", out), mock.patch("sys.stderr", err):
            rc = _main(list(argv))
        return rc, out.getvalue(), err.getvalue()

    def test_too_few_args_prints_usage_and_exits_2(self):
        rc, _, err = self._run("has-result", str(self.results_dir))
        self.assertEqual(rc, 2)
        self.assertIn("usage:", err)
        self.assertEqual(self._run()[0], 2)

    def test_has_result_both_outcomes(self):
        (self.results_dir / "task-a.txt").write_text("done\n")
        self.assertEqual(self._run("has-result", str(self.results_dir), "task-a.txt")[0], 0)
        self.assertEqual(self._run("has-result", str(self.results_dir), "task-b.txt")[0], 1)

    def test_has_result_sees_a_ready_archive_behind_an_empty_live_placeholder(self):
        # The `has-result` exit code is what every bash caller reads.
        _write(self.results_dir / "archive" / "2026-09" / "task-a.txt")
        (self.results_dir / "task-a.txt").write_text("")
        self.assertEqual(self._run("has-result", str(self.results_dir), "task-a.txt")[0], 0)
        (self.results_dir / "task-b.txt").write_text("   \n")
        self.assertEqual(self._run("has-result", str(self.results_dir), "task-b.txt")[0], 1)

    def test_pending_candidates_omits_a_task_whose_ready_result_is_archived_behind_a_placeholder(self):
        (self.tasks_dir / "task-a.txt").write_text("task: x\n")
        (self.tasks_dir / "task-b.txt").write_text("task: y\n")
        _write(self.results_dir / "archive" / "task-a.txt")
        (self.results_dir / "task-a.txt").write_text("")
        rc, out, _ = self._run("pending-candidates", str(self.tasks_dir), str(self.results_dir))
        self.assertEqual((rc, out), (0, "task-b.txt\n"))
        rc, out, _ = self._run("next-pending", str(self.tasks_dir), str(self.results_dir))
        self.assertEqual((rc, out), (0, "task-b.txt\n"))

    def test_has_result_rejects_trailing_args(self):
        rc, _, err = self._run("has-result", str(self.results_dir), "task-a.txt", "--claims-dir", "x")
        self.assertEqual(rc, 2)
        self.assertIn("usage:", err)

    def test_pending_candidates_both_outcomes(self):
        self.assertEqual(self._run("pending-candidates", str(self.tasks_dir), str(self.results_dir))[0], 1)
        (self.tasks_dir / "task-a.txt").write_text("task: x\n")
        (self.tasks_dir / "task-b.txt").write_text("task: y\n")
        rc, out, _ = self._run("pending-candidates", str(self.tasks_dir), str(self.results_dir))
        self.assertEqual(rc, 0)
        self.assertEqual(sorted(out.split()), ["task-a.txt", "task-b.txt"])

    def test_pending_candidates_honours_claims_dir(self):
        (self.tasks_dir / "task-a.txt").write_text("task: x\n")
        (self.claims_dir / "task-a.txt").write_text("")
        rc, out, _ = self._run("pending-candidates", str(self.tasks_dir), str(self.results_dir),
                               "--claims-dir", str(self.claims_dir))
        self.assertEqual((rc, out), (1, ""))

    def test_next_pending_both_outcomes(self):
        self.assertEqual(self._run("next-pending", str(self.tasks_dir), str(self.results_dir))[0], 1)
        (self.tasks_dir / "task-a.txt").write_text("task: x\n")
        rc, out, _ = self._run("next-pending", str(self.tasks_dir), str(self.results_dir))
        self.assertEqual((rc, out), (0, "task-a.txt\n"))

    def test_next_pending_honours_claims_dir(self):
        (self.tasks_dir / "task-a.txt").write_text("task: x\n")
        (self.claims_dir / "task-a.txt").write_text("")
        rc, out, _ = self._run("next-pending", str(self.tasks_dir), str(self.results_dir),
                               "--claims-dir", str(self.claims_dir))
        self.assertEqual((rc, out), (1, ""))

    def test_malformed_claims_option_is_a_usage_error(self):
        for extra in (["--claims-dir"], ["--claims-dir", ""], ["--bogus", "x"], ["stray"]):
            with self.subTest(extra=extra):
                rc, _, err = self._run("next-pending", str(self.tasks_dir), str(self.results_dir), *extra)
                self.assertEqual(rc, 2)
                self.assertIn("usage:", err)

    def test_unknown_command_exits_2(self):
        rc, _, err = self._run("bogus-command", str(self.tasks_dir), str(self.results_dir))
        self.assertEqual(rc, 2)
        self.assertIn("unknown command", err)


class CliSmokeTest(unittest.TestCase):
    """One real subprocess: the file is executable as the bash callers invoke it."""

    def test_has_result_via_interpreter(self):
        with tempfile.TemporaryDirectory() as td:
            results = Path(td)
            (results / "task-a.txt").write_text("done\n")
            ok = subprocess.run([sys.executable, str(CLI), "has-result", str(results), "task-a.txt"],
                                capture_output=True, text=True, timeout=30)
            miss = subprocess.run([sys.executable, str(CLI), "has-result", str(results), "task-b.txt"],
                                  capture_output=True, text=True, timeout=30)
        self.assertEqual(ok.returncode, 0, ok.stderr)
        self.assertEqual(miss.returncode, 1, miss.stderr)


if __name__ == "__main__":
    unittest.main()
