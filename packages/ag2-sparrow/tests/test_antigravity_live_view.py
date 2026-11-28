#!/usr/bin/env python3
"""The Antigravity connector's live view: what an agy turn writes to `<work>/state/live-view.log` (its words, tool
calls and each permission decision, never the prompt's framing), and `ag2-sparrow-antigravity-local view`, which
follows that file read-only. Runs against tests/agy_stub_acp.py.

Run:
    python3 packages/ag2-sparrow/tests/test_antigravity_live_view.py
"""
import contextlib
import io
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_antigravity_adapter as agy  # noqa: E402  (isolates os.environ and the lock home on import)

from ag2_sparrow import acp_host, live_view  # noqa: E402
from ag2_sparrow.adapters.antigravity import cli  # noqa: E402

TURN_HEAD = r"\u2500\u2500 \d\d:\d\d:\d\d Alice, owner \u2500\u2500"


class Steps(unittest.TestCase):
    def test_each_acp_tool_call_is_one_step(self):
        step = acp_host.view_step
        self.assertEqual(step({"kind": "execute", "title": "x", "rawInput": {"command": "ls -la"}}), ("ran", "ls -la"))
        self.assertEqual(step({"kind": "execute", "rawInput": {"CommandLine": "date"}}), ("ran", "date"))
        self.assertEqual(step({"kind": "edit", "title": "Edit", "locations": [{"path": "/w/a"}, {"path": "/w/b"}, "x"]}),
                         ("changed", "/w/a, /w/b"))
        self.assertEqual(step({"kind": "fetch", "title": "Fetch https://e.com/"}), ("fetched", "Fetch https://e.com/"))
        self.assertEqual(step({"title": "mcp_ag2-space_room_read"}), ("called", "mcp_ag2-space_room_read"))
        self.assertEqual(step({"rawInput": {"command": ["git", "status"]}}), ("ran", "git status"))


class Pending(unittest.TestCase):
    """An ACP `tool_call` is a proposal: permission may still refuse it, or it may fail. The view says what it
    did only on the call's own `completed` update."""

    def run_updates(self, *updates):
        with tempfile.TemporaryDirectory() as d:
            path = Path(live_view.path_of(d))
            turn = acp_host.Turn()
            turn.live, turn.view = True, live_view.Writer(path)
            for u in updates:
                turn.on_update({"update": u})
            return path.read_text().splitlines()

    CALL = {"sessionUpdate": "tool_call", "toolCallId": "c1", "kind": "execute", "title": "t",
            "rawInput": {"command": "rm -rf build"}}

    def test_a_proposed_call_is_pending_then_what_it_did_once_completed(self):
        self.assertEqual(self.run_updates(self.CALL, {"sessionUpdate": "tool_call_update", "toolCallId": "c1",
                                                      "status": "completed"}),
                         ["\u2022 pending rm -rf build", "\u2022 ran rm -rf build"])

    def test_a_refused_or_failed_call_is_never_shown_as_done(self):
        refused = self.run_updates(self.CALL)       # permission refused it: no completed update ever comes
        failed = self.run_updates(self.CALL, {"sessionUpdate": "tool_call_update", "toolCallId": "c1", "status": "failed"})
        self.assertEqual(refused, ["\u2022 pending rm -rf build"])
        self.assertEqual(failed, ["\u2022 pending rm -rf build", "\u2022 failed rm -rf build"])

    def test_completion_is_tied_to_its_own_call_id(self):
        other = {"sessionUpdate": "tool_call_update", "toolCallId": "c2", "status": "completed"}
        self.assertEqual(self.run_updates(self.CALL, other), ["\u2022 pending rm -rf build"])
        done = dict(self.CALL, status="completed")
        self.assertEqual(self.run_updates(done), ["\u2022 ran rm -rf build"], "a call reported already done")


class ControlCharacters(unittest.TestCase):
    """Every agy line goes through live_view.Writer, so an escape sequence in agy's words or a tool's title
    reaches the log as spaces: it cannot recolour, retitle or move the cursor of the viewer's terminal."""

    def test_an_escape_sequence_in_agys_text_or_a_tool_title_is_neutralized(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(live_view.path_of(d))
            turn = acp_host.Turn()
            turn.live, turn.view = True, live_view.Writer(path)
            esc = "\x1b[31mred\x1b]0;pwned\x07\x1b[2J"
            turn.on_update({"update": {"sessionUpdate": "agent_message_chunk",
                                       "content": {"type": "text", "text": "hi " + esc + " there\n"}}})
            turn.on_update({"update": {"sessionUpdate": "tool_call", "kind": "execute", "title": "t",
                                       "rawInput": {"command": "echo " + esc}}})
            turn.on_update({"update": {"sessionUpdate": "tool_call", "kind": "other", "title": "Tool " + esc}})
            turn.on_update({"update": {"sessionUpdate": "tool_call_update", "status": "failed", "title": esc}})
            turn.view.step("refused", "command: " + esc, "reason " + esc)
            raw = path.read_bytes()
            for byte in (b"\x1b", b"\x07"):
                self.assertNotIn(byte, raw)
            self.assertIn(b"red", raw)
            self.assertEqual(len(raw.splitlines()), 5)
            self.assertEqual(turn.text, "hi " + esc + " there\n", "the reply itself is untouched")


class SlowLog(unittest.TestCase):
    """The ACP reader thread that answers permission requests also delivers `session/update`s; writing the
    live view there would hold every reply behind the log's disk I/O."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.w = agy.Work(tmp.name)
        self.path = Path(live_view.path_of(self.w.root))

    def stalled(self, release):
        real = os.open

        def slow(path, *a, **kw):
            if str(path).startswith(str(self.path)):
                release.wait(5)
            return real(path, *a, **kw)
        return mock.patch.object(live_view.os, "open", side_effect=slow)

    def on_reader(self, fn):
        t = threading.Thread(target=fn, daemon=True)
        t.start()
        t.join(1)
        return not t.is_alive()

    def test_a_stalled_log_never_holds_the_acp_reader(self):
        view = agy.gc.Connector(self.w.args()).view
        turn = acp_host.Turn()
        turn.live, turn.view = True, view
        release = threading.Event()
        with self.stalled(release):
            returned = self.on_reader(lambda: turn.on_update(
                {"update": {"sessionUpdate": "tool_call", "kind": "execute", "rawInput": {"command": "ls"}}}))
            release.set()
            self.assertTrue(returned, "the reader went on while the log was stalled")
            self.assertTrue(view.drain(5))
        self.assertEqual(self.path.read_text().splitlines(), ["\u2022 pending ls"])

    def test_a_backlog_is_bounded_and_says_what_it_left_out(self):
        view = live_view.Writer(self.path, background=True)
        release = threading.Event()
        with self.stalled(release):
            self.assertTrue(self.on_reader(lambda: [view.line("n%d" % i) for i in range(live_view.QUEUE_MAX + 50)]))
            release.set()
            self.assertTrue(view.drain(5))
        lines = self.path.read_text().splitlines()
        notices = [ln for ln in lines if "not shown" in ln]
        shown = [ln for ln in lines if "not shown" not in ln]
        self.assertTrue(notices, lines[-3:])
        # The drops can be reported in one notice or two (did the writer take its first line before the
        # first drop?), so count lines, not notices: each is shown or counted as not shown.
        self.assertLessEqual(len(shown), live_view.QUEUE_MAX + 1)
        left_out = sum(int(ln.split()[1]) for ln in notices)
        self.assertEqual(len(shown) + left_out, live_view.QUEUE_MAX + 50)

    def test_a_log_path_that_is_not_a_regular_file_is_never_written(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        os.mkfifo(self.path)                    # opening a FIFO for writing waits for a reader
        self.assertTrue(self.on_reader(lambda: live_view.Writer(self.path).line("x")))
        os.unlink(self.path)
        target = self.path.parent / "elsewhere"
        target.write_text("kept\n")
        os.symlink(target, self.path)
        live_view.Writer(self.path).line("x")
        self.assertEqual(target.read_text(), "kept\n")


class AgyTurn(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.w = agy.Work(tmp.name)
        self.path = Path(live_view.path_of(self.w.root))

    def test_a_turn_writes_who_it_is_for_the_agents_words_and_how_it_ended(self):
        self.w.task("task-1", "STUB:TEXT hi")
        self.w.serve()
        text = self.path.read_text()
        self.assertRegex(text, TURN_HEAD + r"\n> STUB:TEXT hi\nhello from agy-stub\n\u2500\u2500 turn completed \u2500\u2500\n$")
        self.assertEqual(os.stat(self.path).st_mode & 0o777, 0o600)
        # Never the framing the connector wraps the message in, nor where agy keeps its home.
        for framing in ("access_tier", "source_message_id", "write the result", str(self.w.home)):
            self.assertNotIn(framing, text)

    def test_each_permission_decision_is_written_with_its_reason(self):
        self.w.task("task-2", "STUB:EDIT")
        self.w.serve()
        lines = self.path.read_text().splitlines()
        self.assertIn("\u2022 allowed file change: WriteFile notes.txt [under the working directory]", lines)
        self.assertIn("\u2022 refused file change: WriteFile /etc/stub-outside [outside the working directory]", lines)
        self.assertEqual(lines[-2:], ["edited", "\u2500\u2500 turn completed \u2500\u2500"])

    def test_a_refused_command_and_a_tool_call_update(self):
        self.w.task("task-3", "STUB:SHELL")
        self.w.task("task-4", "write the result to results/task-4.txt")
        self.w.serve(2)
        lines = self.path.read_text().splitlines()
        self.assertTrue(any(ln.startswith("\u2022 refused command: rm -rf ~") for ln in lines), lines)
        # The result-file edit was refused, so it stays pending and is never shown as done.
        self.assertIn("\u2022 pending File results/task-4.txt", lines)
        self.assertNotIn("\u2022 changed File results/task-4.txt", lines)
        self.assertEqual(sum(ln == "\u2500\u2500 turn completed \u2500\u2500" for ln in lines), 2)

    def test_view_follows_the_jobs_log_and_never_reads_stdin(self):
        self.w.task("task-5", "STUB:TEXT hi")
        self.w.serve()
        out, stop = io.StringIO(), threading.Event()
        real = live_view.follow
        with mock.patch.object(cli, "selected", return_value=(cli.LOCAL, "")), \
                mock.patch.object(cli, "load_config", return_value=({"ROOT": str(self.w.root)}, "")), \
                mock.patch.object(live_view, "follow",
                                  side_effect=lambda p: real(p, out=out, stop=stop.is_set, sleep=lambda _: stop.set())), \
                mock.patch.object(sys, "stdin", None):
            self.assertEqual(cli.main(["view"]), 0)
        self.assertEqual(out.getvalue(), self.path.read_text())

    def test_view_without_a_job_is_refused(self):
        err = io.StringIO()
        with mock.patch.object(cli, "selected", return_value=(None, "several jobs: pass --agent")), \
                contextlib.redirect_stderr(err):
            self.assertEqual(cli.main(["view"]), cli.EXIT_UNUSABLE)
        self.assertIn("several jobs", err.getvalue())


if __name__ == "__main__":
    unittest.main()
