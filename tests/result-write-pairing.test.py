#!/usr/bin/env python3
"""The result writer must refuse a body composed for a different task.

The incident these tests exist for: a session holding two claimed tasks wrote
each reply into the OTHER task's result file. Nothing downstream could notice,
because a result file states nothing about which task it answers. So the check
has to happen at the write, and it has to be all-or-nothing — a refused write
that still leaves a temp file behind is the same bug with extra steps.
"""
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

import result_write  # noqa: E402


class PairedResultWriteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self.tmp.name)
        self.results = self.ws / "results"
        self.receipts = result_write.receipts_dir_for(self.ws)

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, task_id, body, **kw):
        return result_write.write_paired_result(
            self.results, task_id, body, receipts_dir=self.receipts, **kw)

    def files_under(self, directory):
        d = Path(directory)
        if not d.is_dir():
            return []
        return sorted(p.name for p in d.iterdir())

    def test_interleaved_body_of_a_against_id_of_b_is_refused(self):
        """Two tasks in flight; A's body submitted against B's id."""
        body_a = "task: a1\nthe answer for A\n"
        with self.assertRaises(ValueError) as cm:
            self.write("b2", body_a)
        self.assertIn("pairing echo mismatch", str(cm.exception))
        self.assertIn("task: b2", str(cm.exception))

    def test_a_refused_write_leaves_zero_files(self):
        with self.assertRaises(ValueError):
            self.write("b2", "task: a1\nthe answer for A\n")
        self.assertEqual(self.files_under(self.results), [])
        self.assertEqual(self.files_under(self.receipts), [])
        self.assertFalse(self.results.exists(),
                         "a refused write must not even create results/")

    def test_neither_task_is_corrupted_by_the_crossed_submission(self):
        """The correct pairing for each still lands, untouched by the refusal."""
        self.write("a1", "task: a1\nthe answer for A\n")
        with self.assertRaises(ValueError):
            self.write("b2", "task: a1\nthe answer for A\n")
        self.write("b2", "task: b2\nthe answer for B\n")
        self.assertEqual((self.results / "task-a1.txt").read_text(),
                         "the answer for A\n")
        self.assertEqual((self.results / "task-b2.txt").read_text(),
                         "the answer for B\n")

    def test_happy_path_strips_the_echo_and_writes_the_canonical_name(self):
        out = self.write("a1", "task: a1\nline one\nline two\n")
        self.assertEqual(out, self.results / "task-a1.txt")
        self.assertEqual(out.read_text(), "line one\nline two\n")

    def test_success_leaves_no_temp_residue(self):
        self.write("a1", "task: a1\nbody\n", tmp_tag="core-2")
        self.assertEqual(self.files_under(self.results), ["task-a1.txt"])

    def test_success_records_a_pairing_receipt(self):
        self.write("a1", "task: a1\nbody\n")
        self.assertTrue(result_write.has_pairing_receipt(self.receipts, "a1"))
        self.assertFalse(result_write.has_pairing_receipt(self.receipts, "b2"))

    def test_write_is_atomic_via_replace(self):
        """No reader may ever observe a partially written result file."""
        calls = []
        real_replace = os.replace
        original = "the original body\n"
        self.results.mkdir(parents=True)
        (self.results / "task-a1.txt").write_text(original)

        def spy(src, dst):
            calls.append((str(src), str(dst)))
            self.assertEqual((self.results / "task-a1.txt").read_text(), original,
                             "destination changed before the atomic swap")
            return real_replace(src, dst)

        result_write.os.replace = spy
        try:
            self.write("a1", "task: a1\nreplacement\n")
        finally:
            result_write.os.replace = real_replace
        self.assertEqual(len(calls), 1)
        self.assertTrue(calls[0][0].endswith(".tmp"), calls[0][0])
        self.assertEqual((self.results / "task-a1.txt").read_text(), "replacement\n")

    def test_empty_body_refused(self):
        for body in ("", "   \n\n"):
            with self.subTest(body=body):
                with self.assertRaises(ValueError):
                    self.write("a1", body)
                self.assertEqual(self.files_under(self.results), [])

    def test_missing_echo_line_refused(self):
        with self.assertRaises(ValueError):
            self.write("a1", "just an answer, no echo line\n")
        self.assertEqual(self.files_under(self.results), [])

    def test_echo_only_body_refused(self):
        for body in ("task: a1", "task: a1\n", "task: a1\n   \n"):
            with self.subTest(body=body):
                with self.assertRaises(ValueError) as cm:
                    self.write("a1", body)
                self.assertIn("only the pairing echo", str(cm.exception))
                self.assertEqual(self.files_under(self.results), [])

    def test_echo_must_be_the_first_line_not_merely_present(self):
        with self.assertRaises(ValueError):
            self.write("a1", "preamble\ntask: a1\nbody\n")

    def test_prefix_ids_do_not_satisfy_each_other(self):
        """`task: a1` must not pair a task whose id merely starts with it."""
        with self.assertRaises(ValueError):
            self.write("a12", "task: a1\nbody\n")

    def test_crlf_echo_accepted(self):
        out = self.write("a1", "task: a1\r\nbody\r\n")
        self.assertEqual(out.read_bytes(), b"body\r\n")

    def test_task_id_accepts_the_spellings_callers_hold(self):
        for spelling in ("a1", "task-a1", "task-a1.txt", "/w/tasks/task-a1.txt"):
            with self.subTest(spelling=spelling):
                self.assertEqual(result_write.task_id_from(spelling), "a1")

    def test_body_must_echo_the_bare_id_whatever_spelling_was_passed(self):
        """Laxity about the argument must not become laxity about the check."""
        with self.assertRaises(ValueError):
            self.write("task-a1.txt", "task: task-a1.txt\nbody\n")
        out = self.write("task-a1.txt", "task: a1\nbody\n")
        self.assertEqual(out.name, "task-a1.txt")

    def test_unusable_task_id_refused(self):
        for bad in ("", "   ", "task-.txt", ".."):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    result_write.task_id_from(bad)


class PairedResultWriteCliTests(unittest.TestCase):
    """The shell entries reach this module only through the CLI."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self.tmp.name)
        self.results = self.ws / "results"
        self.receipts = result_write.receipts_dir_for(self.ws)

    def tearDown(self):
        self.tmp.cleanup()

    def run_cli(self, task_id, body, *extra):
        return subprocess.run(
            [sys.executable, str(REPO / "src" / "result_write.py"), "write",
             task_id, "--workspace", str(self.ws), *extra],
            input=body, capture_output=True, text=True)

    def test_cli_happy_path_exits_zero_and_prints_the_path(self):
        p = self.run_cli("a1", "task: a1\nbody\n")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(p.stdout.strip(), str(self.results / "task-a1.txt"))
        self.assertEqual((self.results / "task-a1.txt").read_text(), "body\n")
        self.assertTrue(result_write.has_pairing_receipt(self.receipts, "a1"))

    def test_cli_refuses_crossed_body_with_exit_2_and_zero_writes(self):
        p = self.run_cli("b2", "task: a1\nthe answer for A\n")
        self.assertEqual(p.returncode, 2, p.stdout)
        self.assertIn("pairing echo mismatch", p.stderr)
        self.assertFalse(self.results.exists())
        self.assertFalse(self.receipts.exists())

    def test_cli_refuses_empty_stdin(self):
        p = self.run_cli("a1", "")
        self.assertEqual(p.returncode, 2)
        self.assertIn("empty result body", p.stderr)

    def test_cli_honours_explicit_directories(self):
        results = self.ws / "elsewhere" / "results"
        receipts = self.ws / "elsewhere" / "receipts"
        p = self.run_cli("a1", "task: a1\nbody\n",
                         "--results-dir", str(results),
                         "--receipts-dir", str(receipts))
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual((results / "task-a1.txt").read_text(), "body\n")
        self.assertTrue(result_write.has_pairing_receipt(receipts, "a1"))

    def test_cli_rejects_unknown_flags_rather_than_ignoring_them(self):
        p = self.run_cli("a1", "task: a1\nbody\n", "--nope", "x")
        self.assertEqual(p.returncode, 2)
        self.assertFalse(self.results.exists())

    def test_bare_invocation_is_a_usage_error(self):
        p = subprocess.run(
            [sys.executable, str(REPO / "src" / "result_write.py")],
            capture_output=True, text=True)
        self.assertEqual(p.returncode, 2)
        self.assertIn("usage:", p.stderr)


if __name__ == "__main__":
    unittest.main()
