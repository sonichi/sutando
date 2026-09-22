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
  5. `handler_result_is_answer()` must read the REFUSAL-OR-ANSWER first line
     from wherever the ready body actually lives, never a hardcoded live
     path: an archived refusal behind an empty/whitespace live placeholder
     was misread as "answered" and never re-dispatched.
And the parity that makes this the one truth: on every fixture — single-state
and mixed — the answer equals what `watch-tasks-stream.sh`'s
`handler_result_exists` and `handler_result_is_answer` compute, running the
shipped bash functions themselves.

Run: python3 tests/task-dispatch-contract.test.py
"""
from __future__ import annotations

import re
import shlex
import subprocess
import os
import sys
import tempfile
import unittest
from io import StringIO
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

from delivery.task_dispatch import (  # noqa: E402
    clear_inflight, inflight_is_live, mark_inflight,
    _main,
    find_ready_result,
    find_ready_result_for_filename,
    has_ready_result,
    next_pending_task,
    owned_task_ids,
    _WORKER_HOLD_SUFFIXES,
    pending_candidates,
    worker_holds,
    WorkerHoldUnreadable,
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


# Fixtures for handler_result_is_answer's own question: is the READY body a
# refusal (re-dispatch) or a genuine answer (leave it), never "does $live say so".
REFUSAL_MARK = "could not safely process"
REFUSAL_BODY = f"I {REFUSAL_MARK} this Team-tier task because the restricted runtime x. No unrestricted fallback was used.\n"
ANSWER_BODY = "done\n"
ANSWER_FIXTURES = {
    "no_result": ([], False),
    "live_answer": ([(F, ANSWER_BODY)], True),
    "live_refusal": ([(F, REFUSAL_BODY)], False),
    "empty_live_only": ([(F, EMPTY)], False),
    "whitespace_live_only": ([(F, WS)], False),
    "archived_refusal_only": ([(f"archive/{F}", REFUSAL_BODY)], False),
    # Regression 5: these two used to read the empty/whitespace live placeholder
    # instead of the archived refusal that find-ready actually resolves to.
    "archived_refusal_plus_empty_live": (
        [(f"archive/2026-09/{F}", REFUSAL_BODY), (F, EMPTY)], False),
    "archived_refusal_plus_whitespace_live": (
        [(f"archive/{F}", REFUSAL_BODY), (F, WS)], False),
    "archived_answer_plus_empty_live": (
        [(f"archive/2026-09/{F}", ANSWER_BODY), (F, EMPTY)], True),
    "archived_answer_plus_whitespace_live": (
        [(f"archive/{F}", ANSWER_BODY), (F, WS)], True),
    "live_refusal_wins_over_archived_answer": (
        [(F, REFUSAL_BODY), (f"archive/{F}", ANSWER_BODY)], False),
    "live_answer_wins_over_stale_archive": (
        [(F, ANSWER_BODY), (f"archive/{F}", "stale\n")], True),
}


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


class HandlerResultIsAnswerTest(unittest.TestCase):
    """`handler_result_is_answer` in watch-tasks-stream.sh is bash. Run THAT
    function — its shipped text, with the watcher's own variables bound — on
    every ANSWER_FIXTURES case and require the reference verdict below: is the
    body `find_ready_result_for_filename` resolves to a refusal, or an answer?
    A caller that re-reads a hardcoded live path instead cannot tell the two
    apart when the ready body is archived — regression 5."""

    @classmethod
    def setUpClass(cls):
        text = WATCHER.read_text()
        mark = re.search(r'\nTERMINAL_REFUSAL_MARK="([^"]+)"\n', text)
        assert mark, "TERMINAL_REFUSAL_MARK not found in watch-tasks-stream.sh"
        assert mark.group(1) == REFUSAL_MARK, "test fixture drifted from the shipped marker text"
        m = re.search(r"\nhandler_result_is_answer\(\) \{\n(.*?)\n\}\n", text, re.S)
        assert m, "handler_result_is_answer() not found in watch-tasks-stream.sh"
        cls.function = m.group(0).strip("\n")
        cls.mark_line = mark.group(0).strip("\n")

    def _reference_verdict(self, results: Path) -> bool:
        found = find_ready_result_for_filename(results, F)
        if found is None:
            return False
        first = found.read_text().splitlines()[:1]
        return not (first and first[0].startswith(f"I {REFUSAL_MARK}"))

    def _watcher_verdict(self, results: Path) -> bool:
        script = "\n".join([
            f"SUTANDO_PY_BIN={shlex.quote(sys.executable)}",
            f"__REPO_ROOT={shlex.quote(str(REPO))}",
            f"RESULTS_DIR={shlex.quote(str(results))}",
            self.mark_line,
            self.function,
            f"handler_result_is_answer {shlex.quote(F)}",
        ])
        proc = subprocess.run(["/bin/bash", "-c", script], capture_output=True, text=True, timeout=30)
        self.assertIn(proc.returncode, (0, 1), proc.stderr)
        return proc.returncode == 0

    def test_same_verdict_on_every_answer_fixture(self):
        for fixture, (entries, expected) in ANSWER_FIXTURES.items():
            with self.subTest(fixture=fixture):
                with tempfile.TemporaryDirectory() as td:
                    results = Path(td) / "results"
                    results.mkdir()
                    _populate(results, entries)
                    watcher = self._watcher_verdict(results)
                    self.assertEqual(watcher, expected, fixture)
                    self.assertEqual(watcher, self._reference_verdict(results), fixture)

    def test_the_watcher_reads_the_resolved_ready_path_not_a_fixed_live_path(self):
        # The shipped function must read find-ready's own resolved path, never
        # $live directly -- that unconditional read is regression 5's shape.
        self.assertIn("find-ready", self.function)
        self.assertNotIn('< "$live"', self.function)


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

    def _hold_for_worker(self, task_id, suffix, worker="worker-1"):
        folder = Path(self.tmp.name) / "deliveries" / worker
        folder.mkdir(parents=True, exist_ok=True)
        (folder / f"{task_id}{suffix}").write_text("")
        return folder.parent

    def test_a_worker_held_task_is_held_back_under_every_sentinel_suffix(self):
        # The router's hand-off leaves the task in tasks/; each sentinel spelling
        # (fresh, accepted, claimed) must keep it out of the core's pick.
        for suffix in (".txt", ".accepted", ".claimed"):
            with self.subTest(suffix=suffix):
                for f in self.tasks_dir.glob("*"):
                    f.unlink()
                self._write_task("task-held.txt", priority="urgent")
                self._write_task("task-free.txt")
                deliveries = self._hold_for_worker("task-held", suffix)
                self.assertTrue(worker_holds(deliveries, "task-held.txt"))
                self.assertFalse(worker_holds(deliveries, "task-free.txt"))
                self.assertEqual(
                    list(pending_candidates(self.tasks_dir, self.results_dir,
                                            deliveries_dir=deliveries)),
                    ["task-free.txt"])
                self.assertEqual(
                    next_pending_task(self.tasks_dir, self.results_dir, deliveries_dir=deliveries),
                    "task-free.txt")
                for f in (deliveries / "worker-1").glob("*"):
                    f.unlink()

    def test_without_a_deliveries_dir_worker_holds_are_not_consulted(self):
        self._write_task("task-held.txt")
        self._hold_for_worker("task-held", ".claimed")
        self.assertEqual(list(pending_candidates(self.tasks_dir, self.results_dir)), ["task-held.txt"])

    def test_an_unreadable_deliveries_root_holds_the_task_instead_of_yielding_it(self):
        # Absent root = no pool (False, control); unreadable root = cannot decide:
        # worker_holds raises and the queue holds the task rather than delivering it.
        if os.geteuid() == 0:
            self.skipTest("root ignores directory modes")
        self._write_task("task-a.txt")
        deliveries = self._hold_for_worker("task-zzz", ".claimed")
        self.assertEqual(list(pending_candidates(self.tasks_dir, self.results_dir,
                                                 deliveries_dir=deliveries)), ["task-a.txt"])
        os.chmod(deliveries, 0)
        self.addCleanup(os.chmod, deliveries, 0o755)
        with self.assertRaises(WorkerHoldUnreadable):
            worker_holds(deliveries, "task-a.txt")
        with mock.patch("sys.stderr", StringIO()):
            self.assertEqual(list(pending_candidates(self.tasks_dir, self.results_dir,
                                                     deliveries_dir=deliveries)), [])
            self.assertIsNone(next_pending_task(self.tasks_dir, self.results_dir,
                                                deliveries_dir=deliveries))

    def test_a_root_that_is_not_a_directory_is_unreadable_not_absent(self):
        # A replaced or misconfigured deliveries path: ownership cannot be checked.
        self._write_task("task-a.txt")
        root = Path(self.tmp.name) / "deliveries-file"
        root.write_text("not a directory\n")
        with self.assertRaises(WorkerHoldUnreadable):
            worker_holds(root, "task-a.txt")
        with mock.patch("sys.stderr", StringIO()):
            self.assertEqual(list(pending_candidates(self.tasks_dir, self.results_dir,
                                                     deliveries_dir=root)), [])

    def test_worker_holds_is_false_for_an_absent_deliveries_root_or_a_bad_name(self):
        self.assertFalse(worker_holds(Path(self.tmp.name) / "nope", "task-a.txt"))
        deliveries = self._hold_for_worker("task-a", ".txt")
        self.assertFalse(worker_holds(deliveries, "../task-a.txt"))
        self.assertFalse(worker_holds(deliveries, ""))

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

    def test_sort_by_priority_orders_by_tier_then_mtime(self):
        low = self.tasks_dir / "task-low.txt"
        normal = self.tasks_dir / "task-normal.txt"
        urgent = self.tasks_dir / "task-urgent.txt"
        low.write_text("priority: low\ntask: x\n")
        normal.write_text("priority: normal\ntask: x\n")
        urgent.write_text("priority: urgent\ntask: x\n")
        now = 1_000_000.0
        os.utime(low, (now, now))
        os.utime(normal, (now + 1, now + 1))
        os.utime(urgent, (now + 2, now + 2))
        rc, out, err = self._run("sort-by-priority", str(self.tasks_dir))
        self.assertEqual(rc, 0)
        self.assertEqual(err, "")
        self.assertEqual(out.splitlines(), ["task-urgent.txt", "task-normal.txt", "task-low.txt"])

    def test_sort_by_priority_empty_dir_is_rc_1_no_output(self):
        rc, out, err = self._run("sort-by-priority", str(self.tasks_dir))
        self.assertEqual(rc, 1)
        self.assertEqual(out, "")
        self.assertEqual(err, "")

    def test_sort_by_priority_wrong_arity_is_usage_error(self):
        rc, _, err = self._run("sort-by-priority", str(self.tasks_dir), "extra")
        self.assertEqual(rc, 2)
        self.assertIn("usage:", err)

    def test_priority_tier_prints_the_parsed_tier(self):
        f = self.tasks_dir / "task-a.txt"
        f.write_text("priority: urgent\ntask: x\n")
        rc, out, err = self._run("priority-tier", str(f))
        self.assertEqual(rc, 0)
        self.assertEqual(out, "urgent\n")
        self.assertEqual(err, "")

    def test_priority_tier_wrong_arity_is_usage_error(self):
        rc, _, err = self._run("priority-tier", str(self.tasks_dir / "task-a.txt"), "extra")
        self.assertEqual(rc, 2)
        self.assertIn("usage:", err)

    def test_find_ready_both_outcomes(self):
        rc, out, _ = self._run("find-ready", str(self.results_dir), "task-a.txt")
        self.assertEqual(rc, 1)
        self.assertEqual(out, "")
        (self.results_dir / "task-a.txt").write_text("done\n")
        rc, out, _ = self._run("find-ready", str(self.results_dir), "task-a.txt")
        self.assertEqual((rc, out), (0, f"{self.results_dir / 'task-a.txt'}\n"))

    def test_find_ready_resolves_to_the_archive_behind_an_empty_live_placeholder(self):
        # This is the path handler_result_is_answer reads its refusal-or-answer
        # line from; a first-hit lookup here reintroduces regression 5.
        _write(self.results_dir / "archive" / "2026-09" / "task-a.txt")
        (self.results_dir / "task-a.txt").write_text("")
        rc, out, _ = self._run("find-ready", str(self.results_dir), "task-a.txt")
        self.assertEqual((rc, out), (0, f"{self.results_dir / 'archive' / '2026-09' / 'task-a.txt'}\n"))

    def test_find_ready_rejects_trailing_args(self):
        rc, _, err = self._run("find-ready", str(self.results_dir), "task-a.txt", "extra")
        self.assertEqual(rc, 2)
        self.assertIn("usage:", err)

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

    def test_pending_and_next_honour_deliveries_dir_in_either_option_order(self):
        (self.tasks_dir / "task-a.txt").write_text("task: x\n")
        deliveries = Path(self.tmp.name) / "deliveries"
        (deliveries / "w").mkdir(parents=True)
        (deliveries / "w" / "task-a.accepted").write_text("")
        for argv in (["--deliveries-dir", str(deliveries)],
                     ["--claims-dir", str(self.claims_dir), "--deliveries-dir", str(deliveries)],
                     ["--deliveries-dir", str(deliveries), "--claims-dir", str(self.claims_dir)]):
            for cmd in ("pending-candidates", "next-pending"):
                with self.subTest(cmd=cmd, argv=argv):
                    rc, out, _ = self._run(cmd, str(self.tasks_dir), str(self.results_dir), *argv)
                    self.assertEqual((rc, out), (1, ""))
        # Control: the same task is picked when the option is absent.
        rc, out, _ = self._run("next-pending", str(self.tasks_dir), str(self.results_dir))
        self.assertEqual((rc, out), (0, "task-a.txt\n"))

    def test_worker_holds_command_both_outcomes_and_usage(self):
        deliveries = Path(self.tmp.name) / "deliveries"
        (deliveries / "w").mkdir(parents=True)
        self.assertEqual(self._run("worker-holds", str(deliveries), "task-a.txt")[0], 1)
        (deliveries / "w" / "task-a.claimed").write_text("")
        self.assertEqual(self._run("worker-holds", str(deliveries), "task-a.txt")[0], 0)
        rc, _, err = self._run("worker-holds", str(deliveries), "task-a.txt", "extra")
        self.assertEqual(rc, 2)
        self.assertIn("usage:", err)

    def test_owned_by_command_prints_one_id_per_line(self):
        deliveries = Path(self.tmp.name) / "deliveries"
        (deliveries / "w1").mkdir(parents=True)
        (deliveries / "w1" / "task-a.txt").write_text("")
        (deliveries / "w1" / "task-a.accepted").write_text("")
        (deliveries / "w1" / "task-b.claimed").write_text("")
        rc, out, _ = self._run("owned-by", str(deliveries), "w1")
        self.assertEqual((rc, out.split()), (0, ["task-a", "task-b"]))

    def test_owned_by_command_is_zero_and_silent_for_an_undelivered_recipient(self):
        deliveries = Path(self.tmp.name) / "deliveries"
        deliveries.mkdir()
        rc, out, _ = self._run("owned-by", str(deliveries), "never")
        self.assertEqual((rc, out.strip()), (0, ""))

    def test_owned_by_command_exits_2_on_an_unreadable_folder(self):
        if os.geteuid() == 0:
            self.skipTest("root ignores directory modes")
        deliveries = Path(self.tmp.name) / "deliveries"
        (deliveries / "w1").mkdir(parents=True)
        os.chmod(deliveries / "w1", 0)
        self.addCleanup(os.chmod, deliveries / "w1", 0o755)
        rc, out, err = self._run("owned-by", str(deliveries), "w1")
        # 1 would read as "this worker owes nothing" and end the turn.
        self.assertEqual((rc, out.strip()), (2, ""))
        self.assertIn("owned-by", err)

    def test_owned_by_command_rejects_extra_arguments(self):
        rc, _, err = self._run("owned-by", str(self.tmp.name), "w1", "extra")
        self.assertEqual(rc, 2)
        self.assertIn("usage:", err)

    def test_worker_holds_command_exits_2_on_an_unreadable_root(self):
        if os.geteuid() == 0:
            self.skipTest("root ignores directory modes")
        deliveries = Path(self.tmp.name) / "deliveries"
        (deliveries / "w").mkdir(parents=True)
        (self.tasks_dir / "task-a.txt").write_text("task: x\n")
        os.chmod(deliveries, 0)
        self.addCleanup(os.chmod, deliveries, 0o755)
        rc, _, err = self._run("worker-holds", str(deliveries), "task-a.txt")
        self.assertEqual(rc, 2)
        self.assertIn("cannot", err)
        # The pick holds: nothing yielded, rc 1, and the reason is on stderr.
        rc, out, err = self._run("pending-candidates", str(self.tasks_dir), str(self.results_dir),
                                 "--deliveries-dir", str(deliveries))
        self.assertEqual((rc, out), (1, ""))
        self.assertIn("holding task-a.txt", err)

    def test_worker_holds_command_exits_2_on_a_non_directory_root(self):
        root = Path(self.tmp.name) / "deliveries-file"
        root.write_text("")
        rc, _, err = self._run("worker-holds", str(root), "task-a.txt")
        self.assertEqual(rc, 2)
        self.assertIn("cannot", err)

    def test_a_repeated_or_dangling_dir_option_is_a_usage_error(self):
        d = str(self.claims_dir)
        for extra in (["--deliveries-dir"], ["--deliveries-dir", ""],
                      ["--claims-dir", d, "--claims-dir", d],
                      ["--deliveries-dir", d, "--deliveries-dir", d]):
            with self.subTest(extra=extra):
                rc, _, err = self._run("next-pending", str(self.tasks_dir), str(self.results_dir), *extra)
                self.assertEqual(rc, 2)
                self.assertIn("usage:", err)

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

    def test_find_ready_via_interpreter(self):
        with tempfile.TemporaryDirectory() as td:
            results = Path(td)
            (results / "task-a.txt").write_text("done\n")
            ok = subprocess.run([sys.executable, str(CLI), "find-ready", str(results), "task-a.txt"],
                                capture_output=True, text=True, timeout=30)
            miss = subprocess.run([sys.executable, str(CLI), "find-ready", str(results), "task-b.txt"],
                                  capture_output=True, text=True, timeout=30)
        self.assertEqual((ok.returncode, ok.stdout), (0, f"{results / 'task-a.txt'}\n"))
        self.assertEqual(miss.returncode, 1, miss.stderr)



class InflightRecordTest(unittest.TestCase):
    """The at-most-once record between a confirmed submit and a ready result."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name) / "inflight"

    def test_a_marker_is_live_for_its_own_incarnation_only(self):
        mark_inflight(self.dir, "task-a.txt", "4242")
        self.assertTrue(inflight_is_live(self.dir, "task-a.txt", "4242"))
        self.assertFalse(inflight_is_live(self.dir, "task-a.txt", "9999"), "a marker from another core held")
        self.assertFalse((self.dir / "task-a.txt").exists(), "the stale marker was not removed")

    def test_an_unreadable_current_incarnation_keeps_the_marker_live(self):
        mark_inflight(self.dir, "task-a.txt", "4242")
        self.assertTrue(inflight_is_live(self.dir, "task-a.txt", ""))
        self.assertTrue((self.dir / "task-a.txt").exists())

    def test_a_blank_marker_already_on_disk_is_corrupt_not_live(self):
        self.dir.mkdir(parents=True)
        (self.dir / "task-z.txt").write_text("\n")
        self.assertFalse(inflight_is_live(self.dir, "task-z.txt", "4242"))
        self.assertFalse((self.dir / "task-z.txt").exists(), "the corrupt marker was left to hold the task")
        (self.dir / "task-z.txt").write_text("")
        self.assertFalse(inflight_is_live(self.dir, "task-z.txt", ""), "blank against unreadable must not read as live")

    def test_no_marker_is_not_live_and_clear_is_idempotent(self):
        self.assertFalse(inflight_is_live(self.dir, "task-a.txt", "4242"))
        clear_inflight(self.dir, "task-a.txt")
        mark_inflight(self.dir, "task-a.txt", "4242")
        clear_inflight(self.dir, "task-a.txt")
        self.assertFalse(inflight_is_live(self.dir, "task-a.txt", "4242"))

    def test_whitespace_in_a_filename_is_identity(self):
        mark_inflight(self.dir, "task-a b.txt", "4242")
        self.assertFalse(inflight_is_live(self.dir, "task-ab.txt", "4242"))

    def test_traversal_names_are_refused(self):
        for bad in ("", "../x.txt", "a/b.txt"):
            with self.assertRaises(ValueError):
                mark_inflight(self.dir, bad, "1")

    def test_an_empty_incarnation_is_refused_by_the_writer_and_the_cli(self):
        with self.assertRaises(ValueError):
            mark_inflight(self.dir, "task-e.txt", "  ")
        self.assertFalse((self.dir / "task-e.txt").exists())
        self.assertEqual(2, _main(["inflight-mark", str(self.dir), "task-e.txt", ""]))

    def test_concurrent_writers_never_race_on_a_shared_temp_path(self):
        import threading
        errors = []
        def _w(i):
            try:
                mark_inflight(self.dir, "task-c.txt", str(i))
            except Exception as exc:  # noqa: BLE001 - the point is that none happens
                errors.append(repr(exc))
        threads = [threading.Thread(target=_w, args=(i,)) for i in range(128)]
        for t in threads: t.start()
        for t in threads: t.join()
        self.assertEqual([], errors)
        self.assertTrue((self.dir / "task-c.txt").read_text().strip().isdigit())
        self.assertEqual([], list(self.dir.glob(".task-c.txt.*")), "a temp file was left behind")

    def test_cli_arms_in_process(self):
        # The subprocess round trip proves the exit codes; this hits the same arms
        # under the coverage tracer.
        self.assertEqual(1, _main(["inflight-live", str(self.dir), "task-p.txt", "1"]))
        self.assertEqual(0, _main(["inflight-mark", str(self.dir), "task-p.txt", "1"]))
        self.assertEqual(0, _main(["inflight-live", str(self.dir), "task-p.txt", "1"]))
        self.assertEqual(1, _main(["inflight-live", str(self.dir), "task-p.txt", "2"]))
        self.assertEqual(0, _main(["inflight-clear", str(self.dir), "task-p.txt"]))
        self.assertEqual(2, _main(["inflight-mark", str(self.dir), "task-p.txt"]))
        self.assertEqual(2, _main(["inflight-mark", str(self.dir), "../x.txt", "1"]))
        self.assertEqual(2, _main(["inflight-clear", str(self.dir), "task-p.txt", "extra"]))

    def test_an_unreadable_marker_is_cannot_decide_not_absent(self):
        import os as _os
        if _os.geteuid() == 0:
            self.skipTest("root cannot be denied a read")
        mark_inflight(self.dir, "task-u.txt", "1")
        (self.dir / "task-u.txt").chmod(0o000)
        try:
            with self.assertRaises(PermissionError):
                inflight_is_live(self.dir, "task-u.txt", "1")
            self.assertEqual(2, _main(["inflight-live", str(self.dir), "task-u.txt", "1"]))
        finally:
            (self.dir / "task-u.txt").chmod(0o644)
        self.assertEqual(0, _main(["inflight-live", str(self.dir), "task-u.txt", "1"]), "readable again, it is live")

    def test_cli_round_trip(self):
        script = Path(__file__).resolve().parent.parent / "src" / "delivery" / "task_dispatch.py"
        def run(*args):
            return subprocess.run([sys.executable, str(script), *args], capture_output=True, text=True)
        self.assertEqual(1, run("inflight-live", str(self.dir), "task-c.txt", "1").returncode)
        self.assertEqual(0, run("inflight-mark", str(self.dir), "task-c.txt", "1").returncode)
        self.assertEqual(0, run("inflight-live", str(self.dir), "task-c.txt", "1").returncode)
        self.assertEqual(1, run("inflight-live", str(self.dir), "task-c.txt", "2").returncode)
        self.assertEqual(0, run("inflight-clear", str(self.dir), "task-c.txt").returncode)
        self.assertEqual(2, run("inflight-mark", str(self.dir), "task-c.txt").returncode, "arity is checked")
        self.assertEqual(2, run("inflight-mark", str(self.dir), "../x.txt", "1").returncode)


class OwnedTaskIdsTest(unittest.TestCase):
    """The worker's own question. `worker_holds` answers the core's; both must
    read the same sentinel stages, so they share one suffix set."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.deliveries = Path(self.tmp.name) / "deliveries"
        (self.deliveries / "w1").mkdir(parents=True)

    def tearDown(self):
        self.tmp.cleanup()

    def _touch(self, name, recipient="w1"):
        (self.deliveries / recipient / name).write_text("")

    def test_every_stage_yields_its_task_id_exactly_once(self):
        self._touch("task-a.txt")
        self._touch("task-a.accepted")
        self._touch("task-b.claimed")
        self.assertEqual(owned_task_ids(self.deliveries, "w1"), ["task-a", "task-b"])

    def test_a_non_sentinel_name_is_ignored(self):
        self._touch("notes.log")
        self.assertEqual(owned_task_ids(self.deliveries, "w1"), [])

    def test_absent_folder_is_nothing_owed(self):
        self.assertEqual(owned_task_ids(self.deliveries, "never-delivered"), [])

    def test_unreadable_folder_is_undecidable_not_empty(self):
        os.chmod(self.deliveries / "w1", 0o000)
        try:
            with self.assertRaises(WorkerHoldUnreadable):
                owned_task_ids(self.deliveries, "w1")
        finally:
            os.chmod(self.deliveries / "w1", 0o755)

    def test_a_traversing_recipient_is_refused(self):
        self._touch("task-a.txt")
        self.assertEqual(owned_task_ids(self.deliveries, "../w1"), [])

    def test_it_shares_the_suffix_set_with_worker_holds(self):
        # A stage added for worker_holds must reach this function too, or the
        # core and the worker disagree about what was delivered.
        for suffix in _WORKER_HOLD_SUFFIXES:
            self._touch(f"task-s{suffix}")
            self.assertIn("task-s", owned_task_ids(self.deliveries, "w1"))
            self.assertTrue(worker_holds(self.deliveries, "task-s.txt"))
            (self.deliveries / "w1" / f"task-s{suffix}").unlink()

    def test_the_cli_prints_one_id_per_line_and_exits_2_when_undecidable(self):
        self._touch("task-a.txt")
        r = subprocess.run([sys.executable, str(CLI), "owned-by", str(self.deliveries), "w1"],
                           capture_output=True, text=True)
        self.assertEqual((r.returncode, r.stdout.split()), (0, ["task-a"]))
        os.chmod(self.deliveries / "w1", 0o000)
        try:
            r2 = subprocess.run([sys.executable, str(CLI), "owned-by", str(self.deliveries), "w1"],
                                capture_output=True, text=True)
        finally:
            os.chmod(self.deliveries / "w1", 0o755)
        self.assertEqual(r2.returncode, 2)
        self.assertIn("owned-by", r2.stderr)



if __name__ == "__main__":
    unittest.main()
