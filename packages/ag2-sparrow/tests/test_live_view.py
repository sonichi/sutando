#!/usr/bin/env python3
"""The live view of a connector with no terminal: what a turn writes, and what a viewer follows."""
import io
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from ag2_sparrow import live_view  # noqa: E402
from ag2_sparrow.adapters.codex import codex_connector as cc  # noqa: E402


class Base(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.work = Path(tmp.name)
        self.path = Path(live_view.path_of(self.work))
        self.w = live_view.Writer(self.path, clock=lambda: 0)

    def lines(self):
        return self.path.read_text().splitlines()


class Writing(Base):
    def test_a_turn_is_written_as_a_person_would_read_it(self):
        self.w.turn("Susan, owner", "list the files\nand count them")
        self.w.say("I'll look.\nThere are ")
        self.w.say("3 files")
        self.w.step("ran", "ls -la", "")
        self.w.step("ran", "false", "exit 1")
        self.w.say(".\n")
        self.w.done("completed")
        got = self.lines()
        self.assertEqual(got[0], "")
        self.assertRegex(got[1], r"^\u2500\u2500 \d\d:\d\d:\d\d Susan, owner \u2500\u2500$")
        self.assertEqual(got[2:], ["> list the files and count them", "I'll look.", "There are 3 files",
                                   "\u2022 ran ls -la", "\u2022 ran false [exit 1]", ".",
                                   "\u2500\u2500 turn completed \u2500\u2500"])
        self.assertEqual(os.stat(self.path).st_mode & 0o777, 0o600)

    def test_long_text_is_cut_and_a_log_past_its_size_starts_over_with_its_tail(self):
        self.w.line("x" * (live_view.LINE_MAX * 2))
        self.assertEqual(len(self.lines()[0]), live_view.LINE_MAX)
        self.w.turn("who " * 100, "m" * 1000)
        self.assertLessEqual(max(len(ln) for ln in self.lines()), live_view.LINE_MAX)
        self.assertLessEqual(len(self.lines()[-1]), 302)
        self.path.write_text("old line\n" * (live_view.KEEP_BYTES // 9 + 10))
        self.w.line("newest")
        size = os.path.getsize(self.path)
        self.assertLess(size, live_view.KEPT_TAIL + 100)
        self.assertEqual((self.lines()[0], self.lines()[-1]), ("old line", "newest"))

    def test_a_view_that_cannot_be_written_never_raises(self):
        blocked = live_view.Writer(self.work / "a-file" / "state" / live_view.NAME)
        (self.work / "a-file").write_text("")
        blocked.turn("Susan, owner", "hi")
        blocked.say("words\n")
        blocked.step("ran", "ls")
        blocked.done("completed")
        self.assertFalse((self.work / "a-file" / "state").exists())


class Following(Base):
    def follow(self, steps, first=live_view.SHOWN_FIRST):
        """Run `follow` while `steps` (callables) run one per poll; what it printed."""
        out, todo = io.StringIO(), list(steps)

        def sleep(_):
            if todo:
                todo.pop(0)()
        live_view.follow(self.path, out=out, stop=lambda: not todo, sleep=sleep, first=first)
        return out.getvalue()

    def test_a_viewer_gets_the_end_of_the_log_then_each_new_line(self):
        for n in range(100):
            self.w.line("old %d" % n)
        got = self.follow([lambda: self.w.line("new 1"), lambda: self.w.say("half"), lambda: self.w.say(" line\n"),
                           lambda: None], first=3)
        self.assertEqual(got.splitlines(), ["old 97", "old 98", "old 99", "new 1", "half line"])

    def test_a_log_not_there_yet_is_waited_for_and_one_that_starts_over_is_read_from_its_start(self):
        got = self.follow([lambda: None, lambda: self.w.line("first"), lambda: None])
        self.assertEqual(got.splitlines(), [live_view.WAITING, "first"])

        def start_over():
            os.replace(str(self.path), str(self.path) + ".old")
            self.w.line("after the restart")
        got = self.follow([lambda: self.w.line("second"), start_over, lambda: None])
        self.assertEqual(got.splitlines(), ["first", "second", "after the restart"])
        # An empty log says it is waiting, once.
        self.path.write_text("")
        self.assertEqual(self.follow([lambda: None, lambda: None]).splitlines(), [live_view.WAITING])

    def test_a_half_written_line_is_not_shown_until_it_ends(self):
        self.path.parent.mkdir(parents=True)
        self.path.write_text("whole\npart")
        got = self.follow([lambda: None, lambda: self.path.write_text("whole\npartial done\n"), lambda: None])
        self.assertEqual(got.splitlines(), ["whole", "partial done"])


class CodexTurn(unittest.TestCase):
    """What the Codex connector hands the view for a turn's items."""

    def test_each_kind_of_item_is_one_line_and_the_agents_own_message_is_not_repeated(self):
        step = cc.view_step
        self.assertEqual(step({"type": "commandExecution", "command": ["ls", "-la"], "status": "completed", "exitCode": 0}),
                         ("ran", "ls -la", ""))
        self.assertEqual(step({"type": "commandExecution", "command": "make test", "status": "failed", "exitCode": 2}),
                         ("ran", "make test", "exit 2"))
        self.assertEqual(step({"type": "commandExecution", "command": "x", "status": "declined"}), ("ran", "x", "declined"))
        self.assertEqual(step({"type": "fileChange", "changes": [{"path": "a.py"}, {"path": "b.py"}, "junk"]}),
                         ("changed", "a.py, b.py", ""))
        self.assertEqual(step({"type": "mcpToolCall", "tool": "room_read"}), ("called", "room_read", ""))
        self.assertEqual(step({"type": "webSearch", "query": "tmux"}), ("searched", "tmux", ""))
        self.assertEqual(step({"type": "somethingNew"}), ("somethingNew", "", ""))
        for hidden in ({"type": "agentMessage", "text": "hi"}, {"type": "reasoning"}, {"type": None}, {}):
            self.assertIsNone(step(hidden), hidden)

    def test_a_served_turn_leaves_its_lines_in_the_work_roots_view_and_the_command_follows_them(self):
        from test_codex_connector import run_connector, write_task, gateway_mid_task
        with tempfile.TemporaryDirectory() as d:
            write_task(d, "task-chat-1", "owner asks")
            (Path(d) / "tasks" / "task-2.txt").write_text(gateway_mid_task("task-2", "a guest asks", "guest"))
            os.utime(Path(d) / "tasks" / "task-2.txt", (time.time() + 5, time.time() + 5))
            self.assertEqual(run_connector(d, once=2, extra=["--allow-non-owner"]), 0)
            text = Path(live_view.path_of(d)).read_text()
            self.assertRegex(text, r"\u2500\u2500 \d\d:\d\d:\d\d owner, owner \u2500\u2500\n> owner asks\nSTUB echo: ")
            self.assertRegex(text, r"\u2500\u2500 \d\d:\d\d:\d\d Bob, restricted \u2500\u2500\n> \[AG2Space bob\] a guest asks\n")
            self.assertEqual(text.count("\u2500\u2500 turn completed \u2500\u2500\n"), 2)
            self.assertNotIn("thought", text)
            out, stop = io.StringIO(), threading.Event()
            live_view.follow(live_view.path_of(d), out=out, stop=stop.is_set, sleep=lambda _: stop.set())
            self.assertEqual(out.getvalue(), text)


if __name__ == "__main__":
    unittest.main()
