#!/usr/bin/env python3
"""The restart sweep's helpers, driven in-process.

The watcher reaches `line_relay`, `task_dispatch sweep-plan`, `resolve_inbox_entry
--batch` and `task_archive.retire_*` only as children of its bash, which the
coverage gate cannot see. These tests import the modules and drive the same paths
directly: the relay's drain-then-write and EOF, the sweep-plan CLI's argv handling,
the batch resolver's verdict lines, and the archive step's OSError branches.

Run: python3 tests/watcher-sweep-paths-in-process.test.py
"""
from __future__ import annotations
# ruff: noqa: E402

import contextlib
import functools
import io
import os
import sys
import tempfile
import threading
import types
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO / "skills" / "worker-pool" / "scripts"))

import line_relay
import resolve_inbox_entry as rie
import task_archive as ta
from delivery import task_dispatch as td

W = "0123456789abcdef0123456789abcdef"
RESOLVER = str(REPO / "skills" / "worker-pool" / "scripts" / "resolve-inbox-entry")
REFUSAL = "I could not safely process"


class Base(unittest.TestCase):
    def setUp(self):
        env = mock.patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        for k in list(os.environ):
            if k.startswith(("SUTANDO_", "TMUX")) or k in ("AGENT_ID", "AG2_AGENT_NAME", "CLAUDECODE"):
                del os.environ[k]
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.ws = Path(self._td.name).resolve()
        os.environ["HOME"] = str(self.ws / "home")
        self.tasks = self.ws / "tasks"
        self.inbox = self.ws / "deliveries" / W
        self.results = self.ws / "results"
        for d in (self.ws / "home", self.tasks / "archive", self.inbox, self.results):
            d.mkdir(parents=True)

    def ptr(self, tid, *, payload=True, age=0, suffix=".txt"):
        if payload:
            (self.tasks / f"{tid}.txt").write_text(f"id: {tid}\ntask: x\n")
        p = self.inbox / f"{tid}{suffix}"
        p.write_text("")
        if age:
            t = os.stat(p).st_mtime - age
            os.utime(p, (t, t))
        return p

    def stub_resolver(self, body: str) -> str:
        p = self.ws / "stub-resolver"
        p.write_text("#!/bin/sh\n" + body)
        p.chmod(0o755)
        return str(p)


class ResolveBatch(Base):
    def test_a_resolver_that_cannot_be_run_is_untrusted(self):
        got = td._resolve_batch(str(self.ws / "missing"), str(self.ws), [str(self.inbox / "task-a.txt")], 1.0)
        self.assertIsNone(got)

    def test_a_resolver_that_hangs_is_untrusted(self):
        slow = self.stub_resolver("sleep 5\n")
        self.assertIsNone(td._resolve_batch(slow, str(self.ws), ["x"], 0.2))

    def test_a_failed_run_or_a_missing_header_is_untrusted(self):
        self.assertIsNone(td._resolve_batch(self.stub_resolver("exit 1\n"), str(self.ws), ["x"], 1.0))
        self.assertIsNone(td._resolve_batch(self.stub_resolver("echo nope\n"), str(self.ws), ["x"], 1.0))

    def test_only_rc_tab_payload_tab_entry_lines_count(self):
        r = self.stub_resolver(f'echo "{td.RESOLVER_BATCH_HEADER}"\n'
                               'printf "garbage\\n"\n'
                               'printf "x\\t\\ty\\n"\n'
                               'printf "0\\t/p/task-a.txt\\t/i/task-a.txt\\n"\n'
                               'printf "3\\t\\t/i/task-b.txt\\n"\n')
        got = td._resolve_batch(r, str(self.ws), ["/i/task-a.txt", "/i/task-b.txt"], 1.0)
        self.assertEqual(got, {"/i/task-a.txt": (0, "/p/task-a.txt"), "/i/task-b.txt": (3, "")})


class IsAnswer(Base):
    def test_without_a_refusal_prefix_any_ready_body_answers(self):
        (self.results / "task-a.txt").write_text("done\n")
        self.assertTrue(td._is_answer(self.results, "task-a.txt", {"task-a.txt"}, ""))

    def test_a_result_outside_the_live_listing_is_not_an_answer(self):
        (self.results / "task-a.txt").write_text("done\n")
        self.assertFalse(td._is_answer(self.results, "task-a.txt", set(), REFUSAL))

    def test_an_empty_body_is_not_an_answer(self):
        (self.results / "task-a.txt").write_text("  \n")
        self.assertFalse(td._is_answer(self.results, "task-a.txt", {"task-a.txt"}, REFUSAL))

    def test_a_ready_body_that_cannot_be_opened_is_not_an_answer(self):
        (self.results / "task-a.txt").write_text("done\n")
        with mock.patch.object(td, "find_ready_result_for_filename", return_value=self.results):
            self.assertFalse(td._is_answer(self.results, "task-a.txt", {"task-a.txt"}, REFUSAL))


class SweepPlan(Base):
    def test_a_missing_results_dir_reads_as_nothing_answered(self):
        (self.tasks / "task-a.txt").write_text("id: task-a\ntask: x\n")
        keep, counts = td.sweep_plan(self.tasks, self.ws / "no-results", refusal_prefix=REFUSAL)
        self.assertEqual((keep, counts["answered"], counts["batch"]), (["task-a.txt"], 0, "none"))

    def test_an_entry_the_batch_said_nothing_about_is_kept(self):
        self.ptr("task-a", payload=False, age=3600)
        header_only = self.stub_resolver(f'echo "{td.RESOLVER_BATCH_HEADER}"\n')
        keep, counts = td.sweep_plan(self.inbox, self.results, resolver=header_only, workspace=str(self.ws))
        self.assertEqual((keep, counts["batch"], counts["stale"]), (["task-a.txt"], "ok", 0))

    def test_an_entry_removed_under_the_sweep_is_kept_not_aged(self):
        self.ptr("task-a", payload=False, age=3600)
        vanishing = self.stub_resolver(f'echo "{td.RESOLVER_BATCH_HEADER}"\n'
                                       'while IFS= read -r e; do rm -f "$e"; printf "3\\t\\t%s\\n" "$e"; done\n')
        keep, counts = td.sweep_plan(self.inbox, self.results, resolver=vanishing, workspace=str(self.ws))
        self.assertEqual((keep, counts["stale"]), (["task-a.txt"], 0))

    def test_the_injected_clock_decides_staleness(self):
        p = self.ptr("task-a", payload=False)
        mtime = int(os.stat(p).st_mtime)
        rc3 = self.stub_resolver(f'echo "{td.RESOLVER_BATCH_HEADER}"\n'
                                 'while IFS= read -r e; do printf "3\\t\\t%s\\n" "$e"; done\n')
        kw = dict(resolver=rc3, workspace=str(self.ws), race_window=10)
        self.assertEqual(td.sweep_plan(self.inbox, self.results, now=mtime + 5, **kw)[0], ["task-a.txt"])
        self.assertEqual(td.sweep_plan(self.inbox, self.results, now=mtime + 60, **kw)[0], [])


class SweepPlanCli(Base):
    def run_cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = td._main(["sweep-plan", *args])
        return rc, out.getvalue(), err.getvalue()

    def test_too_few_arguments_is_a_usage_error(self):
        rc, out, err = self.run_cli(str(self.inbox))
        self.assertEqual((rc, out), (2, ""))
        self.assertIn("sweep-plan <inbox> <results_dir>", err)

    def test_an_unknown_or_dangling_option_is_a_usage_error(self):
        for extra in (["--bogus", "x"], ["--race-window"], ["--resolver", RESOLVER, "--workspace"]):
            rc, out, err = self.run_cli(str(self.inbox), str(self.results), *extra)
            self.assertEqual((rc, out), (2, ""), extra)
            self.assertIn("usage", err)

    def test_a_non_numeric_window_or_timeout_is_a_usage_error(self):
        for extra in (["--race-window", "soon"], ["--resolver-timeout", "later"]):
            rc, out, _ = self.run_cli(str(self.inbox), str(self.results), *extra)
            self.assertEqual((rc, out), (2, ""), extra)

    def test_an_empty_inbox_prints_only_the_done_line(self):
        rc, out, err = self.run_cli(str(self.inbox), str(self.results))
        self.assertEqual((rc, out, err), (0, f"{td.SWEEP_PLAN_DONE}\n", ""))

    def test_entries_print_as_inbox_paths_then_done_with_counts_on_stderr(self):
        (self.tasks / "task-a.txt").write_text("id: task-a\ntask: x\n")
        (self.tasks / "task-b.txt").write_text("id: task-b\ntask: x\n")
        (self.results / "task-a.txt").write_text("done\n")
        rc, out, err = self.run_cli(str(self.tasks), str(self.results), "--resolver", "", "--workspace", str(self.ws),
                                    "--race-window", "10", "--refusal-prefix", REFUSAL, "--resolver-timeout", "1")
        self.assertEqual((rc, out), (0, f"{self.tasks}/task-b.txt\n{td.SWEEP_PLAN_DONE}\n"))
        self.assertIn("sweep plan over 2 entries: 0 stale, 1 answered, 1 to dispatch (batch resolve: none;", err)

    def test_the_pool_resolver_drops_a_stale_pointer(self):
        self.ptr("task-live")
        self.ptr("task-old", payload=False, age=3600)
        rc, out, err = self.run_cli(str(self.inbox), str(self.results), "--resolver", RESOLVER,
                                    "--workspace", str(self.ws), "--refusal-prefix", REFUSAL)
        self.assertEqual((rc, out), (0, f"{self.inbox}/task-live.txt\n{td.SWEEP_PLAN_DONE}\n"), err)
        self.assertIn("1 stale, 0 answered, 1 to dispatch (batch resolve: ok;", err)


class ResolverBatch(Base):
    def verdicts(self, entries, workspace=None):
        out = io.StringIO()
        rie.batch(entries, str(self.ws) if workspace is None else workspace, out=out)
        lines = out.getvalue().splitlines()
        self.assertEqual(lines[0], rie.BATCH_HEADER)
        return {ln.split("\t", 2)[2]: tuple(ln.split("\t", 2)[:2]) for ln in lines[1:]}

    def test_hit_no_payload_and_undelivered_each_get_their_single_call_verdict(self):
        hit = str(self.ptr("task-a"))
        gone = str(self.ptr("task-b", payload=False))
        undelivered = str(self.inbox / "task-c.txt")
        got = self.verdicts([hit, gone, undelivered, "not-a-sentinel"])
        self.assertEqual(got[hit], ("0", str(self.tasks / "task-a.txt")))
        self.assertEqual(got[gone], (str(rie.NO_PAYLOAD_RC), ""))
        self.assertEqual(got[undelivered], ("1", ""))
        self.assertEqual(got["not-a-sentinel"], ("1", ""))

    def test_a_payload_that_is_not_a_regular_file_is_no_payload(self):
        p = str(self.ptr("task-d", payload=False))
        (self.tasks / "task-d.txt").mkdir()
        self.assertEqual(self.verdicts([p])[p], (str(rie.NO_PAYLOAD_RC), ""))

    def test_an_empty_batch_is_just_the_header(self):
        self.assertEqual(self.verdicts([]), {})

    def test_main_batch_reads_entries_from_stdin(self):
        hit = str(self.ptr("task-a"))
        buf = io.StringIO()
        with mock.patch.object(sys, "stdin", io.StringIO(f"{hit}\n\n{self.inbox / 'task-z.txt'}\n")), \
                mock.patch.object(rie, "batch", functools.partial(rie.batch, out=buf)):
            self.assertEqual(rie.main(["--batch", "--workspace", str(self.ws)]), 0)
        lines = buf.getvalue().splitlines()
        self.assertEqual(lines[0], rie.BATCH_HEADER)
        self.assertEqual(lines[1:], [f"0\t{self.tasks / 'task-a.txt'}\t{hit}", f"1\t\t{self.inbox / 'task-z.txt'}"])

    def test_main_single_entry_prints_the_payload_or_the_reason(self):
        hit, gone = str(self.ptr("task-a")), str(self.ptr("task-b", payload=False))
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            self.assertEqual(rie.main([hit, "--workspace", str(self.ws)]), 0)
            self.assertEqual(rie.main(["--workspace", str(self.ws), gone]), rie.NO_PAYLOAD_RC)
        self.assertEqual(out.getvalue(), f"{self.tasks / 'task-a.txt'}\n")
        self.assertIn("resolve_inbox_entry: sentinel task-b names no payload", err.getvalue())

    def test_main_usage_errors(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(rie.main([]), 2)
            self.assertEqual(rie.main([""]), 2)
            self.assertEqual(rie.main(["a", "b"]), 2)
            self.assertEqual(rie.main(["--workspace"]), 2)
        self.assertIn("usage:", err.getvalue())
        self.assertIn("--workspace needs a directory", err.getvalue())


class LineRelay(Base):
    def relay_bytes(self, data: bytes) -> bytes:
        r, w = os.pipe()
        got = bytearray()

        def read():
            with os.fdopen(r, "rb") as f:
                got.extend(f.read())

        reader = threading.Thread(target=read)
        reader.start()
        line_relay.relay(io.BytesIO(data), w)
        os.close(w)
        reader.join(10)
        return bytes(got)

    def test_lines_leave_whole_and_a_missing_final_newline_is_added(self):
        self.assertEqual(self.relay_bytes(b"a\nbb\nccc"), b"a\nbb\nccc\n")

    def relay_writes(self, data: bytes, *, chunk: "int | None" = None) -> "tuple[bytes, list[bytes]]":
        """(output, the bytes of each os.write the relay issued); `chunk` caps what a
        write accepts, so a short write is answered the way a pipe would answer it."""
        real_write = os.write
        writes: list[bytes] = []
        r, w = os.pipe()

        def write(fd, buf):
            if fd != w:
                return real_write(fd, buf)
            view = memoryview(buf)[:chunk] if chunk else memoryview(buf)
            writes.append(bytes(view))
            return real_write(fd, view)

        got = bytearray()

        def read():
            with os.fdopen(r, "rb") as f:
                got.extend(f.read())

        reader = threading.Thread(target=read)
        reader.start()
        with mock.patch.object(os, "write", write):
            line_relay.relay(io.BytesIO(data), w)
        os.close(w)
        reader.join(10)
        return bytes(got), writes

    def test_lines_read_in_one_batch_still_leave_in_one_write_each(self):
        # Lines a pipe would deliver together are never coalesced: a write is kept
        # whole only up to PIPE_BUF, and the sweep writes its own lines to the FIFO.
        lines = [b"/inbox/task-%03d.txt\n" % i for i in range(64)]
        out, writes = self.relay_writes(b"".join(lines))
        self.assertEqual(out, b"".join(lines))
        self.assertEqual(writes, lines)

    def test_an_unterminated_last_line_is_its_own_terminated_write(self):
        out, writes = self.relay_writes(b"first\nlast")
        self.assertEqual(out, b"first\nlast\n")
        self.assertEqual(writes, [b"first\n", b"last\n"])

    def test_a_short_write_resumes_from_where_it_stopped(self):
        out, writes = self.relay_writes(b"abc\nde\n", chunk=2)
        self.assertEqual(out, b"abc\nde\n")
        self.assertEqual(writes, [b"ab", b"c\n", b"de", b"\n"])

    def test_eof_with_no_input_writes_nothing(self):
        self.assertEqual(self.relay_bytes(b""), b"")

    def test_stdin_is_drained_while_stdout_is_not_being_read(self):
        data = b"".join(b"%07d\n" % i for i in range(300_000))
        src_r, src_w = os.pipe()
        dst_r, dst_w = os.pipe()
        fed = threading.Event()
        got, errors = bytearray(), []

        def feed():
            with os.fdopen(src_w, "wb", buffering=0) as f:
                f.write(data)
            fed.set()

        def read():
            if not fed.wait(20):
                errors.append("stdin stalled behind a full stdout pipe")
            with os.fdopen(dst_r, "rb") as f:
                got.extend(f.read())

        threads = [threading.Thread(target=feed), threading.Thread(target=read)]
        for t in threads:
            t.start()
        with os.fdopen(src_r, "rb") as src:
            line_relay.relay(src, dst_w)
        os.close(dst_w)
        for t in threads:
            t.join(30)
        self.assertEqual(errors, [])
        self.assertEqual(bytes(got), data)

    def test_main_copies_stdin_to_stdout_and_swallows_a_broken_pipe(self):
        r, w = os.pipe()
        fake_in = types.SimpleNamespace(buffer=io.BytesIO(b"x\ny\n"))
        fake_out = types.SimpleNamespace(fileno=lambda: w)
        with mock.patch.object(sys, "stdin", fake_in), mock.patch.object(sys, "stdout", fake_out):
            self.assertEqual(line_relay.main(), 0)
        os.close(w)
        with os.fdopen(r, "rb") as f:
            self.assertEqual(f.read(), b"x\ny\n")

        r2, w2 = os.pipe()
        os.close(r2)
        fake_in = types.SimpleNamespace(buffer=io.BytesIO(b"nobody listens\n"))
        fake_out = types.SimpleNamespace(fileno=lambda: w2)
        try:
            with mock.patch.object(sys, "stdin", fake_in), mock.patch.object(sys, "stdout", fake_out):
                self.assertEqual(line_relay.main(), 0)
        finally:
            os.close(w2)


class ArchiveErrors(Base):
    def fifo_lock(self):
        os.mkfifo(self.inbox / ta.POINTER_LOCK_NAME)

    def test_a_lock_that_is_not_a_regular_file_fails_that_folder_and_is_logged(self):
        ptr = self.ptr("task-a")
        self.fifo_lock()
        logs = []
        self.assertEqual(ta.retire_delivery_pointers(self.ws / "deliveries", "task-a", log=logs.append), [])
        self.assertTrue(ptr.exists())
        self.assertEqual(len(logs), 1)
        self.assertIn("retire pointer for task-a in", logs[0])
        self.assertIn(f"{ta.POINTER_LOCK_NAME} is not a regular file", logs[0])

    def test_a_deliveries_root_that_cannot_be_listed_is_logged(self):
        root = self.ws / "deliveries-as-a-file"
        root.write_text("")
        logs = []
        self.assertEqual(ta.retire_delivery_pointers(root, "task-a", log=logs.append), [])
        self.assertEqual(len(logs), 1)
        self.assertIn(f"retire pointers for task-a: cannot list {root}", logs[0])

    def test_a_missing_root_is_silent(self):
        logs = []
        self.assertEqual(ta.retire_delivery_pointers(self.ws / "nope", "task-a", log=logs.append), [])
        self.assertEqual(logs, [])

    def test_migration_without_an_archive_dir_still_retires_a_processed_task(self):
        (self.tasks / "archive").rmdir()
        (self.tasks / "processed").mkdir()
        (self.tasks / "processed" / "task-a.txt").write_text("id: task-a\ntask: x\n")
        ptr = self.ptr("task-a", payload=False)
        counts = ta.retire_archived_pointers(self.ws, log=lambda m: None)
        self.assertEqual(counts, {"retired": 1, "kept_pending": 0, "kept_unarchived": 0})
        self.assertFalse(ptr.exists())
        self.assertTrue((self.inbox / ta.POINTER_ARCHIVE_DIR / "task-a.txt").is_file())

    def test_migration_without_a_deliveries_root_returns_zero_counts(self):
        ws = self.ws / "other"
        (ws / "tasks" / "archive").mkdir(parents=True)
        self.assertEqual(ta.retire_archived_pointers(ws, log=lambda m: None),
                         {"retired": 0, "kept_pending": 0, "kept_unarchived": 0})

    def test_migration_logs_a_folder_it_cannot_retire_in(self):
        (self.tasks / "archive" / "task-a.txt").write_text("id: task-a\ntask: x\n")
        ptr = self.ptr("task-a", payload=False)
        self.fifo_lock()
        logs = []
        counts = ta.retire_archived_pointers(self.ws, log=logs.append)
        self.assertEqual(counts["retired"], 0)
        self.assertTrue(ptr.exists())
        self.assertEqual(len(logs), 1)
        self.assertIn("retire pointer for task-a in", logs[0])


if __name__ == "__main__":
    unittest.main(verbosity=2)
