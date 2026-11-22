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
        self.assertIn("\u2022 changed File results/task-4.txt", lines)
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
