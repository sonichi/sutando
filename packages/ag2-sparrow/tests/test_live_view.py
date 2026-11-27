#!/usr/bin/env python3
"""The live view of a connector with no terminal: what a turn writes, and what a viewer follows."""
import argparse
import importlib.util
import io
import os
import queue
import sys
import tempfile
import threading
import time
import types
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from ag2_sparrow import live_view  # noqa: E402
from ag2_sparrow.adapters.codex import codex_connector as cc, connect_cli  # noqa: E402


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
    def test_a_message_answered_with_a_card_is_shown_as_a_turn_with_no_model_in_it(self):
        self.w.carded("Susan, owner", "/model", "model-card")
        shown = self.lines()
        self.assertTrue(shown[1].endswith("Susan, owner ──"), shown)
        self.assertEqual(shown[2:], ["> /model", "• answered with model-card (no model turn)", "── turn completed ──"])
        self.w.carded("Susan, owner", "switch account", "")
        self.assertIn("• answered with a card (no model turn)", self.lines())

    def test_the_waiting_line_says_nothing_about_typing(self):
        # The panel that shows this view takes a message under it: "read-only" there was wrong.
        self.assertNotIn("read-only", live_view.WAITING)
        self.assertNotIn("typed", live_view.WAITING)

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

    def test_nothing_written_can_drive_the_viewers_terminal(self):
        """An escape sequence, a carriage return or a bell in the agent's words, a command or a sender's
        message reaches the log as spaces: the view shows text, it never acts on it."""
        nasty = "\x1b]0;title\x07\x1b[2J\x1b[31mred\r\x08over\x9b31m\u2028next\x00\x7f\u202e\u2066"
        self.w.turn("Bob" + nasty, "hi" + nasty)
        self.w.say("words " + nasty + "\nmore\n")
        self.w.step("ran", "echo " + nasty, "exit " + nasty)
        self.w.done("completed" + nasty)
        raw = self.path.read_bytes()
        text = raw.decode("utf-8")
        for ch in text:
            self.assertTrue(ch == "\n" or (ord(ch) >= 0x20 and not 0x7f <= ord(ch) < 0xa0
                                            and ch not in "\u2028\u2029\u202e\u2066"), repr(ch))
        self.assertIn("words ]0;title [2J [31mred over 31m next", text)
        self.assertEqual(text.count("\n"), 7, "and it starts no line of its own")

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

    def test_a_log_someone_else_wrote_is_shown_as_inert_text(self):
        """The reader does not trust the file: a log an older build wrote, or anything appended to it,
        cannot drive the viewer's terminal either."""
        self.path.parent.mkdir(parents=True)
        self.path.write_bytes(b"old \x1b]0;title\x07\x1b[2Jline\r\x08x\n")
        added = "new \x1b[31mred \u202egnp.exe\u2069\n".encode()

        def append():
            with open(self.path, "ab") as f:
                f.write(added)
        got = self.follow([append, lambda: None])
        self.assertEqual(got.splitlines(), ["old  ]0;title  [2Jline  x", "new  [31mred  gnp.exe "])
        self.assertTrue(all(ch == "\n" or ch.isprintable() for ch in got), repr(got))

    def test_a_log_trimmed_to_its_tail_does_not_repeat_what_was_shown(self):
        for n in range(50):
            self.w.line("line %d" % n)

        def trim_and_add():
            kept = "".join("line %d\n" % n for n in range(40, 50))
            Path(str(self.path) + ".new").write_text(kept)
            os.replace(str(self.path) + ".new", str(self.path))
            self.w.line("after the trim")
        got = self.follow([lambda: None, trim_and_add, lambda: None], first=3)
        self.assertEqual(got.splitlines(), ["line 47", "line 48", "line 49", "after the trim"])

    def test_a_log_that_cannot_be_read_is_waited_out_never_raised(self):
        self.w.line("one")
        real = open

        def unreadable(path, *a, **kw):
            if str(path) == str(self.path):
                raise OSError("gone mid-read")
            return real(path, *a, **kw)
        with mock.patch("builtins.open", unreadable):
            self.assertEqual(self.follow([lambda: None, lambda: None]), live_view.WAITING + "\n")
            self.assertEqual(live_view._after(self.path, b"one"), 0)
            self.assertEqual(live_view._start_of_last(self.path, 4, 3), 0)
        self.assertEqual(live_view._after(self.path, b""), 0, "nothing shown yet: from the start")
        self.assertEqual(live_view._after(self.path, b"one"), 4)

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

    def test_a_completed_command_in_a_turn_is_written_as_it_completes(self):
        with tempfile.TemporaryDirectory() as d:
            events = queue.Queue()
            c = cc.Connector(lambda: types.SimpleNamespace(events=events),
                             argparse.Namespace(state=os.path.join(d, "state")), note="")
            c.thread_id = "t1"
            for m in ({"method": "item/completed", "params": {"threadId": "t1", "turnId": "u1", "item": {
                          "type": "commandExecution", "command": ["ls", "\x1b[2J"], "status": "completed", "exitCode": 0}}},
                      {"method": "item/agentMessage/delta", "params": {"threadId": "t1", "turnId": "u1", "delta": "done\n"}},
                      {"method": "turn/completed", "params": {"threadId": "t1", "turn": {"id": "u1", "status": "completed"}}}):
                events.put(m)
            self.assertEqual(c.wait_turn("u1", 5), ("completed", "done\n", None))
            self.assertEqual(Path(live_view.path_of(d)).read_text(), "\u2022 ran ls [2J\ndone\n")
        # A connector given no state dir (a unit under test) keeps no view and serves the turn the same.
        bare = cc.Connector(lambda: types.SimpleNamespace(events=events), argparse.Namespace(), note="")
        self.assertIsNone(bare.view)

    def test_the_view_command_follows_the_agents_log_until_interrupted(self):
        seen = []

        def followed(path):
            seen.append(path)
            raise KeyboardInterrupt
        with mock.patch.object(connect_cli, "selected", return_value=("job", "")), \
                mock.patch.object(connect_cli, "load_config", return_value=({"WORK": "/w"}, "")), \
                mock.patch.object(connect_cli.cm, "work_root", return_value="/w"), \
                mock.patch.object(connect_cli.live_view, "follow", followed):
            self.assertEqual(connect_cli.view(argparse.Namespace()), 0)
        self.assertEqual(seen, [live_view.path_of("/w")])
        err = io.StringIO()
        with mock.patch.object(connect_cli, "selected", return_value=(None, "several jobs are installed")), \
                mock.patch.object(connect_cli.live_view, "follow", followed), mock.patch.object(sys, "stderr", err):
            self.assertEqual(connect_cli.view(argparse.Namespace()), connect_cli.EXIT_UNUSABLE)
        self.assertEqual((seen, err.getvalue()), ([live_view.path_of("/w")], "error: several jobs are installed\n"))
        with mock.patch.object(connect_cli, "selected", return_value=("job", "")), \
                mock.patch.object(connect_cli, "load_config", return_value=({}, "no config")), \
                mock.patch.object(sys, "stderr", io.StringIO()):
            self.assertEqual(connect_cli.view(argparse.Namespace()), connect_cli.EXIT_UNUSABLE)

    def test_the_differential_recording_reads_the_view_with_its_clock_times_replaced(self):
        spec = importlib.util.spec_from_file_location("adapter_differential_driver",
                                                      Path(__file__).resolve().parent / "adapter_differential_driver.py")
        driver = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(driver)
        with tempfile.TemporaryDirectory() as d:
            w = live_view.Writer(live_view.path_of(d))
            w.turn("owner, owner", "at 12:34:56 exactly")
            w.done("completed")
            got = driver.read_state(d, Path(d))
        self.assertEqual(got, {live_view.NAME: "\n\u2500\u2500 <hh:mm:ss> owner, owner \u2500\u2500\n"
                                                "> at <hh:mm:ss> exactly\n\u2500\u2500 turn completed \u2500\u2500\n"})

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
