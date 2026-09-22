#!/usr/bin/env python3
"""A watcher nobody reads holds its inbox without serving it — and says so.

A watcher keeps its inbox whether or not anything consumes what it announces,
and since a second session-role start now exits naming the holder rather than
doubling it, an unread holder stops tasks reaching the core until someone runs
`--force-restart`. From outside, the reader is the only difference between that
and a healthy watcher, so health-check asks for it.

Two halves, tested apart:
  * `watcher_identity.output_sink` — what fd 1 is and whether anything reads it,
    driven with recorded `lsof` output so no toolchain or live process is needed.
  * `check_task_watcher` — a live, sentinel-owning watcher whose output nothing
    reads must warn, and the same watcher with a reader must stay ok.

Run: python3 tests/health-check-unread-watcher.test.py  (exit 0/1)
"""
from __future__ import annotations

import contextlib
import importlib.util
import io
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))
import watcher_identity as wid  # noqa: E402

spec = importlib.util.spec_from_file_location("health_check", REPO / "src" / "health-check.py")
hc = importlib.util.module_from_spec(spec)
assert spec and spec.loader
spec.loader.exec_module(hc)

WATCHER_PID = "7100"
# Two tokens: the argv shape classify_argv can decide from a flattened string.
INBOX_ARGV = "bash src/watch-tasks-stream.sh"


def fake_lsof(fd1: str, readers: str = "", rc: int = 0):
    """Answer the two lsof calls output_sink makes: fd 1, then that path's openers."""
    def run(cmd, **kw):
        text = readers if "--" in cmd else fd1
        return mock.Mock(returncode=rc, stdout=text)
    return run


FD1_FILE = "p7100\nf1\ntREG\nn/ws/logs/watcher.log\n"
FD1_SOCKET = "p7100\nf1\ntunix\nn->0xdeadbeef\n"
FD1_NULL = "p7100\nf1\ntCHR\nn/dev/null\n"
FD1_TTY = "p7100\nf1\ntCHR\nn/dev/ttys004\n"
READER = "p9001\nf3\nar\nn/ws/logs/watcher.log\n"
WRITER_ONLY = "p9002\nf3\naw\nn/ws/logs/watcher.log\n"
SELF_ONLY = "p7100\nf1\naw\nn/ws/logs/watcher.log\n"


class OutputSink(unittest.TestCase):
    def test_a_file_with_a_reader_is_read(self):
        s = wid.output_sink(WATCHER_PID, run=fake_lsof(FD1_FILE, SELF_ONLY + READER))
        self.assertEqual((s.observed, s.kind, s.read), (True, "file", True))

    def test_a_file_with_only_writers_is_unread(self):
        """THE STRAY: the watcher and an fswatch hold it for writing, nobody reads."""
        s = wid.output_sink(WATCHER_PID, run=fake_lsof(FD1_FILE, SELF_ONLY + WRITER_ONLY))
        self.assertEqual((s.observed, s.kind, s.read), (True, "file", False))
        self.assertEqual(s.target, "/ws/logs/watcher.log")

    def test_the_watchers_own_write_handle_is_not_a_reader(self):
        s = wid.output_sink(WATCHER_PID, run=fake_lsof(FD1_FILE, SELF_ONLY))
        self.assertIs(s.read, False)

    def test_dev_null_is_decided_unread_without_asking_who_reads_it(self):
        s = wid.output_sink(WATCHER_PID, run=fake_lsof(FD1_NULL))
        self.assertEqual((s.kind, s.read), ("discarded", False))

    def test_a_terminal_counts_as_read(self):
        s = wid.output_sink(WATCHER_PID, run=fake_lsof(FD1_TTY))
        self.assertEqual((s.kind, s.read), ("tty", True))

    def test_a_socket_is_left_undecided_because_it_self_clears(self):
        """The watcher exits on its first failed write, so a dead peer resolves itself."""
        s = wid.output_sink(WATCHER_PID, run=fake_lsof(FD1_SOCKET))
        self.assertEqual((s.kind, s.read), ("stream", None))

    def test_an_unconsultable_lsof_is_not_observed(self):
        def boom(cmd, **kw):
            raise OSError("no lsof")
        s = wid.output_sink(WATCHER_PID, run=boom)
        self.assertEqual((s.observed, s.read), (False, None))

    def test_an_lsof_that_errors_is_not_observed(self):
        s = wid.output_sink(WATCHER_PID, run=fake_lsof(FD1_FILE, rc=2))
        self.assertIs(s.observed, False)

    def test_lsof_exit_1_is_an_answer_not_a_failure(self):
        """Exit 1 is lsof's "nothing matched": the path has no other opener."""
        s = wid.output_sink(WATCHER_PID, run=fake_lsof(FD1_FILE, SELF_ONLY, rc=1))
        self.assertEqual((s.observed, s.read), (True, False))


def probe_with_sink(sink) -> dict:
    """check_task_watcher over one live, sentinel-owning watcher with this sink."""
    with tempfile.TemporaryDirectory() as td:
        ws = Path(td)
        (ws / "state" / "cores").mkdir(parents=True)
        (ws / "state" / "cores" / f"{hc._host_label()}.alive").write_text("{}")
        (ws / "state" / "watch-tasks-stream.pid").write_text(WATCHER_PID)
        saved = (hc.WORKSPACE_DIR, hc._proc_argv, hc._watcher_trees, hc._ps_snapshot,
                 hc._pid_parent, hc._pid_instance_id, hc._pid_actor_id)
        try:
            hc.WORKSPACE_DIR = ws
            hc._proc_argv = lambda pid: INBOX_ARGV
            hc._watcher_trees = lambda *a, **k: {WATCHER_PID: {WATCHER_PID}}
            hc._ps_snapshot = lambda *a, **k: ""
            hc._pid_parent = lambda pid, ps=None: None
            hc._pid_instance_id = lambda pid: ""
            hc._pid_actor_id = lambda pid: ""
            with mock.patch.object(hc.watcher_identity, "output_sink", lambda p: sink):
                return hc.check_task_watcher()
        finally:
            (hc.WORKSPACE_DIR, hc._proc_argv, hc._watcher_trees, hc._ps_snapshot,
             hc._pid_parent, hc._pid_instance_id, hc._pid_actor_id) = saved


class Probe(unittest.TestCase):
    def test_an_unread_watcher_warns_and_names_what_to_run(self):
        r = probe_with_sink(wid.OutputSink(True, "file", "/ws/logs/watcher.log", False))
        self.assertEqual(r["status"], "warn")
        self.assertIn("nothing is reading", r["detail"])
        self.assertIn("/ws/logs/watcher.log", r["detail"])
        self.assertIn("--force-restart", r["detail"])

    def test_a_read_watcher_stays_ok(self):
        r = probe_with_sink(wid.OutputSink(True, "file", "/ws/logs/watcher.log", True))
        self.assertEqual(r["status"], "ok")

    def test_an_undecided_sink_stays_ok(self):
        """A socket says nothing either way; silence must not become a warning."""
        r = probe_with_sink(wid.OutputSink(True, "stream", "->0xdeadbeef", None))
        self.assertEqual(r["status"], "ok")

    def test_an_unobserved_sink_stays_ok(self):
        """No lsof is not evidence of a stray."""
        r = probe_with_sink(wid.OutputSink(False, "unknown", "", None))
        self.assertEqual(r["status"], "ok")


class OutputSinkEdges(unittest.TestCase):
    """lsof output shapes the happy paths above never produce."""

    def test_blank_lines_and_a_typeless_record_before_the_typed_one_are_skipped(self):
        # `p` opens the block; each `f` starts a record; a blank line is skipped;
        # a record with no `t` field says nothing and the next one is read.
        fd1 = "p7100\nf1\nn/dev/null\n\nf1\ntREG\nn/ws/logs/watcher.log\n"
        s = wid.output_sink(WATCHER_PID, run=fake_lsof(fd1, SELF_ONLY + READER))
        self.assertEqual((s.kind, s.target, s.read), ("file", "/ws/logs/watcher.log", True))

    def test_an_unconsultable_reader_lookup_is_unobserved_but_keeps_the_target(self):
        def run(cmd, **kw):
            if "--" in cmd:
                return mock.Mock(returncode=2, stdout="")
            return mock.Mock(returncode=0, stdout=FD1_FILE)
        s = wid.output_sink(WATCHER_PID, run=run)
        self.assertEqual((s.observed, s.kind, s.target, s.read), (False, "file", "/ws/logs/watcher.log", None))

    def test_an_unfamiliar_kind_is_reported_as_itself_and_left_undecided(self):
        s = wid.output_sink(WATCHER_PID, run=fake_lsof("p7100\nf1\ntDIR\nn/ws\n"))
        self.assertEqual((s.observed, s.kind, s.read), (True, "DIR", None))


class OutputSinkCli(unittest.TestCase):
    """`output-sink <pid>`: `<kind> <target>` then `read=yes|no|unknown`; rc 2 when
    lsof could not be consulted, rc 64 on a bad pid."""

    def _run(self, argv, sink=None):
        out, err = io.StringIO(), io.StringIO()
        ctx = mock.patch.object(wid, "output_sink", lambda p: sink) if sink else contextlib.nullcontext()
        with ctx, contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = wid.main(argv)
        return rc, out.getvalue().splitlines(), err.getvalue()

    def test_an_unread_file_prints_its_target_and_read_no(self):
        rc, out, _ = self._run(["output-sink", WATCHER_PID],
                               wid.OutputSink(True, "file", "/ws/logs/watcher.log", False))
        self.assertEqual((rc, out), (0, ["file /ws/logs/watcher.log", "read=no"]))

    def test_a_read_tty_prints_read_yes(self):
        rc, out, _ = self._run(["output-sink", WATCHER_PID], wid.OutputSink(True, "tty", "/dev/ttys004", True))
        self.assertEqual((rc, out), (0, ["tty /dev/ttys004", "read=yes"]))

    def test_an_undecided_stream_prints_read_unknown(self):
        rc, out, _ = self._run(["output-sink", WATCHER_PID], wid.OutputSink(True, "stream", "", None))
        self.assertEqual((rc, out), (0, ["stream", "read=unknown"]))

    def test_unobserved_is_rc_2_and_says_why(self):
        rc, out, err = self._run(["output-sink", WATCHER_PID], wid.OutputSink(False, "unknown", "", None))
        self.assertEqual((rc, out), (2, ["unknown", "read=unknown"]))
        self.assertIn("why=", err)

    def test_a_missing_or_non_numeric_pid_is_a_usage_error(self):
        self.assertEqual(self._run(["output-sink"])[0], 64)
        self.assertEqual(self._run(["output-sink", "abc"])[0], 64)


if __name__ == "__main__":
    unittest.main(verbosity=2)
