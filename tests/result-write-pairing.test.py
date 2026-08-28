#!/usr/bin/env python3
"""The result writer must refuse a body composed for a different task.

The incident these tests exist for: a session holding two claimed tasks wrote
each reply into the OTHER task's result file. Nothing downstream could notice,
because a result file states nothing about which task it answers. So the check
has to happen at the write, and it has to be all-or-nothing — a refused write
that still leaves a temp file behind is the same bug with extra steps.
"""
import io
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

import result_write  # noqa: E402
from delivery.readiness import read_ready_result  # noqa: E402


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
        # Assert the PROPERTY, not the spelling — `.endswith(".tmp")` also
        # failed for any caller passing tmp_tag, so it never held generally.
        src = Path(calls[0][0])
        self.assertEqual(src.parent, self.results, calls[0][0])
        self.assertTrue(src.name.startswith(".task-a1.txt.tmp"), calls[0][0])
        self.assertNotEqual(src, self.results / "task-a1.txt")
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


class CliInProcessTests(unittest.TestCase):
    """Same CLI, called in-process so the coverage gate can see the lines.

    PairedResultWriteCliTests above spawns a subprocess: it proves the real shell
    entry works, but the gate instruments only this process, so those lines read
    as uncovered.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self.tmp.name)
        self.results = self.ws / "results"
        self._stdin = sys.stdin

    def tearDown(self):
        sys.stdin = self._stdin
        self.tmp.cleanup()

    def cli(self, argv, body=""):
        sys.stdin = io.StringIO(body)
        return result_write._write_cli(argv)

    def test_happy_path_returns_zero_and_writes(self):
        rc = self.cli(["a1", "--workspace", str(self.ws)], "task: a1\nbody\n")
        self.assertEqual(rc, 0)
        self.assertEqual((self.results / "task-a1.txt").read_text(), "body\n")

    def test_explicit_dirs_bypass_workspace_resolution(self):
        r = self.ws / "R"; k = self.ws / "K"
        rc = self.cli(["a2", "--results-dir", str(r), "--receipts-dir", str(k)],
                      "task: a2\nbody\n")
        self.assertEqual(rc, 0)
        self.assertTrue((r / "task-a2.txt").is_file())
        self.assertTrue(result_write.has_pairing_receipt(k, "a2"))

    def test_crossed_body_returns_two_and_writes_nothing(self):
        rc = self.cli(["b2", "--workspace", str(self.ws)], "task: a1\nwrong\n")
        self.assertEqual(rc, 2)
        self.assertFalse(self.results.exists() and any(self.results.iterdir()))

    def test_usage_errors_return_two(self):
        for argv in ([], ["x", "novalue"], ["x", "--results-dir"],
                     ["x", "--bogus", "v"]):
            with self.subTest(argv=argv):
                self.assertEqual(self.cli(argv, "task: x\nb\n"), 2)


class ReceiptFailuresDoNotLoseTheResult(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_unwritable_receipts_dir_still_returns_the_result(self):
        """A durable result must never be rolled back for a diagnostic sidecar."""
        blocker = self.ws / "blocker"
        blocker.write_text("")
        out = result_write.write_paired_result(
            self.ws / "results", "c3", "task: c3\nbody\n",
            receipts_dir=blocker / "under-a-file")
        self.assertEqual(out.read_text(), "body\n")

    def test_has_pairing_receipt_is_false_when_the_path_raises(self):
        """pathlib swallows most of these, so raise at the seam the guard wraps."""
        orig = result_write.receipt_path
        def boom(*a, **k):
            raise OSError("unreadable")
        result_write.receipt_path = boom
        try:
            self.assertFalse(result_write.has_pairing_receipt(self.ws, "c3"))
        finally:
            result_write.receipt_path = orig

    def test_resolve_dirs_falls_back_to_the_workspace_helper(self):
        """No explicit dirs and no --workspace: the module must resolve one."""
        results, receipts = result_write._resolve_dirs({})
        self.assertEqual(results.name, "results")
        self.assertEqual(receipts.parts[-2:], result_write.RECEIPTS_SUBPATH)



class ConcurrentDefaultWritersTest(unittest.TestCase):
    """Two writers with no explicit tmp_tag must not share a temp path: the
    first os.replace would move the file the second is about to replace."""

    def _race(self, tags):
        import threading
        with tempfile.TemporaryDirectory() as td:
            results = Path(td)
            barrier = threading.Barrier(2)
            real = result_write.os.replace

            def synced(src, dst):
                # Hold both writers until each has created its temp file, so
                # the collision is deterministic rather than timing-dependent.
                barrier.wait(timeout=10)
                return real(src, dst)

            out, errs = [], []

            def go(tag):
                try:
                    out.append(result_write.write_paired_result(
                        results, "race", "task: race\nbody\n", tmp_tag=tag))
                except Exception as e:      # noqa: BLE001 — recorded, not raised
                    errs.append(type(e).__name__)

            result_write.os.replace = synced
            try:
                ts = [threading.Thread(target=go, args=(t,)) for t in tags]
                for t in ts:
                    t.start()
                for t in ts:
                    t.join(timeout=20)
            finally:
                result_write.os.replace = real
            return len(out), errs

    def test_default_tags_do_not_collide(self):
        returned, errs = self._race(["", ""])
        self.assertEqual(errs, [], "concurrent default writers lost a write")
        self.assertEqual(returned, 2)

    def test_explicit_tags_still_work(self):
        returned, errs = self._race(["core-1", "core-2"])
        self.assertEqual(errs, [])
        self.assertEqual(returned, 2)

    def test_the_race_harness_can_actually_fail(self):
        """Control: force the pre-fix shared name and the collision appears."""
        import threading
        with tempfile.TemporaryDirectory() as td:
            results = Path(td)
            barrier = threading.Barrier(2)
            real = result_write.os.replace
            errs = []

            def synced(src, dst):
                barrier.wait(timeout=10)
                return real(src, dst)

            def go():
                try:
                    # the shared name the default used to produce
                    result_write.write_paired_result(results, "race",
                                           "task: race\nbody\n", tmp_tag="shared")
                except Exception as e:      # noqa: BLE001
                    errs.append(type(e).__name__)

            result_write.os.replace = synced
            try:
                ts = [threading.Thread(target=go) for _ in range(2)]
                for t in ts:
                    t.start()
                for t in ts:
                    t.join(timeout=20)
            finally:
                result_write.os.replace = real
        self.assertIn("FileNotFoundError", errs,
                      "harness cannot produce the collision it claims to prevent")


class ReceiptAttestsTheBytesItPublished(unittest.TestCase):
    """A receipt that only proves a file EXISTS cannot tell a correct pairing
    from a reply composed for another task. It must name the bytes."""

    def _write(self, td, task="task-A", body="task: A\nthe answer for A\n"):
        res, rec = Path(td) / "results", Path(td) / "receipts"
        out = result_write.write_paired_result(res, task, body, receipts_dir=rec)
        return rec, out.read_text()

    def test_receipt_names_the_task_and_the_published_bytes(self):
        with tempfile.TemporaryDirectory() as td:
            rec, published = self._write(td)
            self.assertTrue(result_write.receipt_attests(rec, "A", published))

    def test_a_body_composed_for_another_task_is_not_attested(self):
        with tempfile.TemporaryDirectory() as td:
            rec, _ = self._write(td)
            self.assertFalse(
                result_write.receipt_attests(rec, "A", "answer composed for B"),
                "the receipt attested bytes it never saw")

    def test_a_receipt_does_not_attest_a_different_task(self):
        with tempfile.TemporaryDirectory() as td:
            rec, published = self._write(td)
            self.assertFalse(result_write.receipt_attests(rec, "B", published))

    def test_a_pre_upgrade_empty_receipt_attests_nothing_but_still_exists(self):
        # Additive by design: the presence gate keeps its current answer, so
        # results already pending at upgrade cannot be stranded by this change.
        with tempfile.TemporaryDirectory() as td:
            rec, published = self._write(td)
            result_write.receipt_path(rec, "A").write_text("")
            self.assertFalse(result_write.receipt_attests(rec, "A", published))
            self.assertTrue(result_write.has_pairing_receipt(rec, "A"))

    def test_a_corrupt_receipt_is_not_attestation(self):
        with tempfile.TemporaryDirectory() as td:
            rec, published = self._write(td)
            result_write.receipt_path(rec, "A").write_text("{not json")
            self.assertFalse(result_write.receipt_attests(rec, "A", published))


class AttestationBitesAtTheIdFormBridgesHold(unittest.TestCase):
    """A source scan proves the delivery call EXISTS; only this proves it BITES.

    Writers hold `abc`, delivery consumers hold `task-abc`, and a receipt lookup
    that misses fails OPEN — so a wrong id form is enforcement that does nothing
    and still greps as wired.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        ws = Path(self.tmp.name)
        self.results = ws / "results"
        self.results.mkdir()
        self.receipts = result_write.receipts_dir_for(ws)
        self.receipts.mkdir(parents=True)
        result_write.write_paired_result(
            self.results, "abc", "task: abc\nthe answer\n",
            receipts_dir=self.receipts)
        self.published = self.results / "task-abc.txt"

    def test_a_crossed_body_is_refused_in_either_id_form(self):
        self.published.write_text("someone else's answer\n")
        for form in ("abc", "task-abc"):
            with self.subTest(form):
                self.assertIsNone(
                    read_ready_result(
                        self.published,
                        attests=result_write.receipt_verifier(self.receipts, form)),
                    f"{form}: a crossed body was delivered")

    def test_the_intact_body_still_passes(self):
        """Positive control: a gate that refuses everything is not a gate."""
        self.assertEqual(
            read_ready_result(
                self.published,
                attests=result_write.receipt_verifier(self.receipts, "task-abc")),
            "the answer")


class AttestsCliAndCrossTaskReceipt(unittest.TestCase):
    """The `attests` subcommand exists so a SHELL caller can check attestation
    rather than presence; it is only reachable through the CLI entry."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        ws = Path(self.tmp.name)
        self.results = ws / "results"
        self.results.mkdir()
        self.receipts = result_write.receipts_dir_for(ws)
        self.receipts.mkdir(parents=True)
        result_write.write_paired_result(
            self.results, "abc", "task: abc\nthe answer\n",
            receipts_dir=self.receipts)

    def _cli(self, *argv):
        return result_write._attests_cli(
            [*argv, "--results-dir", str(self.results),
             "--receipts-dir", str(self.receipts)])

    def test_a_receipt_naming_a_different_task_does_not_attest(self):
        """A receipt is only evidence for the task it names. Copying one over
        another id must not launder the body it never covered."""
        other = result_write.receipt_path(self.receipts, "abc").read_text()
        result_write.receipt_path(self.receipts, "zzz").write_text(other)
        (self.results / "task-zzz.txt").write_text("the answer\n")
        self.assertFalse(
            result_write.receipt_attests(self.receipts, "zzz", "the answer\n"))

    def test_cli_exits_0_when_the_receipt_attests_the_current_bytes(self):
        self.assertEqual(self._cli("abc"), 0)

    def test_cli_exits_1_after_the_result_is_overwritten(self):
        (self.results / "task-abc.txt").write_text("someone else's answer\n")
        self.assertEqual(self._cli("abc"), 1)

    def test_cli_exits_1_for_an_empty_receipt(self):
        """Presence is not attestation — the case the old shell gate missed."""
        result_write.receipt_path(self.receipts, "abc").write_text("")
        self.assertEqual(self._cli("abc"), 1)

    def test_cli_exits_1_when_there_is_no_result_file(self):
        self.assertEqual(self._cli("nosuch"), 1)

    def test_cli_rejects_a_missing_task_id(self):
        self.assertEqual(result_write._attests_cli([]), 2)

    def test_cli_rejects_a_dangling_or_unknown_flag(self):
        self.assertEqual(result_write._attests_cli(["abc", "--results-dir"]), 2)
        self.assertEqual(result_write._attests_cli(["abc", "--nope", "x"]), 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
