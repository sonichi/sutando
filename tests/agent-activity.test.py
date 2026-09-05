#!/usr/bin/env python3
"""Tests for skills/agent-activity: the row writer and the transcript tailer's pure parts."""
from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).parent.parent
SCRIPTS = REPO / "skills" / "agent-activity" / "scripts"


def load(name):
    spec = importlib.util.spec_from_file_location(name.replace("-", "_"), SCRIPTS / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


card = load("activity")
tail = load("activity-tail")


class Writer(unittest.TestCase):
    def setUp(self):
        self.ws = Path(tempfile.mkdtemp())

    def rows(self):
        return [json.loads(l) for l in card.log_path(self.ws).read_text().splitlines()]

    def test_append_writes_one_row_with_kind_task_and_room(self):
        card.append("picked up", kind="processing", room="!r:s",
                    task={"id": "task-1", "from": "@q:s", "text": "hi"}, workspace=self.ws)
        card.append("CI green", kind="notice", room=None, workspace=self.ws)
        rows = self.rows()
        self.assertEqual([r["kind"] for r in rows], ["processing", "notice"])
        self.assertEqual(rows[0]["task"], {"id": "task-1", "from": "@q:s", "text": "hi"})
        self.assertEqual(rows[0]["room"], "!r:s")
        self.assertNotIn("room", rows[1])
        self.assertNotIn("done", rows[0])

    def test_done_row_is_marked(self):
        card.append("finished", kind="done", room=None, task={"id": "task-1"}, done=True, workspace=self.ws)
        self.assertTrue(self.rows()[0]["done"])

    def test_unknown_kind_is_refused(self):
        with self.assertRaises(ValueError):
            card.append("x", kind="verbose", room=None, workspace=self.ws)

    def test_default_room_is_the_owners_latest_ag2space_room(self):
        st = self.ws / "state"
        st.mkdir()
        (st / "last-owner-activity.json").write_text(json.dumps({"channel": "ag2space", "channel_id": "!dm:s"}))
        self.assertEqual(card.default_room(self.ws), "!dm:s")
        (st / "last-owner-activity.json").write_text(json.dumps({"channel": "discord", "channel_id": "123"}))
        self.assertIsNone(card.default_room(self.ws))
        (st / "last-owner-activity.json").unlink()
        self.assertIsNone(card.default_room(self.ws))


class TaskFile(unittest.TestCase):
    def test_task_file_fills_task_and_room_from_headers(self):
        ws = Path(tempfile.mkdtemp())
        p = ws / "task-abc.txt"
        p.write_text("id: task-abc\nchannel_id: !team:s\nuser_id: @q:s\ntask: Read the latest discussion in this room.\nsource: ag2space\n")
        task, room = card.task_from_file(p)
        self.assertEqual(task, {"id": "task-abc", "from": "@q:s", "text": "Read the latest discussion in this room."})
        self.assertEqual(room, "!team:s")
        out = subprocess.run([sys.executable, str(SCRIPTS / "activity.py"), "append", "picked up", "--kind", "processing",
                              "--task-file", str(p), "--workspace", str(ws)], capture_output=True, text=True)
        self.assertEqual(out.returncode, 0, out.stderr)
        rec = json.loads(out.stdout)
        self.assertEqual((rec["room"], rec["task"]["id"], rec["kind"]), ("!team:s", "task-abc", "processing"))
        self.assertTrue((ws / "state" / "agent-activity.jsonl").exists(), "the row must land in the given workspace")


class Tailer(unittest.TestCase):
    def setUp(self):
        self.ws = Path(tempfile.mkdtemp())
        (self.ws / "state").mkdir()
        (self.ws / "tasks").mkdir()
        self.log = self.ws / "state" / "agent-activity.jsonl"

    def write(self, *rows):
        self.log.write_text("".join(json.dumps(r) + "\n" for r in rows))

    def test_open_task_is_the_last_task_without_a_later_done(self):
        self.write(
            {"ts": 1, "room": "!a:s", "task": {"id": "t1", "from": "@q:s", "text": "one"}, "line": "a"},
            {"ts": 2, "room": "!a:s", "task": {"id": "t2", "from": "@q:s", "text": "two"}, "line": "b"},
            {"ts": 3, "task": {"id": "t2"}, "line": "c", "done": True},
        )
        task, room = tail.open_task(self.log)
        self.assertEqual((task["id"], room), ("t1", "!a:s"))
        self.write({"ts": 3, "task": {"id": "t1"}, "line": "c", "done": True})
        self.assertEqual(tail.open_task(self.log), (None, None))
        self.assertEqual(tail.open_task(self.ws / "nope.jsonl"), (None, None))

    def assistant(self, *blocks):
        return json.dumps({"type": "assistant", "message": {"content": list(blocks)}})

    def test_tool_descriptions_become_working_rows_and_reads_are_skipped(self):
        line = self.assistant(
            {"type": "tool_use", "name": "Bash", "input": {"command": "ls", "description": "List files"}},
            {"type": "tool_use", "name": "Read", "input": {"file_path": "/x", "description": "Read x"}},
            {"type": "tool_use", "name": "Bash", "input": {"command": "true"}},
        )
        self.assertEqual(list(tail.rows_from(line, self.ws)), [("working", "List files")])

    def test_narration_becomes_a_thinking_row_and_code_fences_do_not(self):
        line = self.assistant({"type": "text", "text": "Checking the log first.\nmore"},
                              {"type": "text", "text": "```\ncode\n```"},
                              {"type": "thinking", "thinking": "", "signature": "x"})
        self.assertEqual(list(tail.rows_from(line, self.ws)), [("thinking", "Checking the log first.")])

    def test_non_assistant_and_malformed_lines_yield_nothing(self):
        self.assertEqual(list(tail.rows_from("not json", self.ws)), [])
        self.assertEqual(list(tail.rows_from(json.dumps({"type": "user", "message": {"content": []}}), self.ws)), [])

    def test_a_task_file_named_in_a_call_adds_sender_and_a_20_char_quote(self):
        (self.ws / "tasks" / "task-abc123.txt").write_text(
            "id: task-abc123\ntask: Reading the newest task please\nsender_name: qingyun\n")
        ctx = tail.task_context('cat "$WS/tasks/task-abc123.txt"', self.ws)
        self.assertEqual(ctx, "from qingyun: Reading the newest t…")
        self.assertEqual(tail.task_context("ls", self.ws), "")
        (self.ws / "tasks" / "task-def456.txt").write_text("task: [Photo attached: /p.png]\nsender_name: q\n")
        self.assertEqual(tail.task_context("task-def456", self.ws), "from q: Photo attached")
        line = self.assistant({"type": "tool_use", "name": "Bash",
                               "input": {"command": "grep task tasks/task-abc123.txt", "description": "Read the newest task"}})
        self.assertEqual(list(tail.rows_from(line, self.ws)),
                         [("working", "Read the newest task: from qingyun: Reading the newest t…")])


class WriterCli(unittest.TestCase):
    def setUp(self):
        self.ws = Path(tempfile.mkdtemp())

    def run_main(self, *argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = card.main([*argv, "--workspace", str(self.ws)])
        return rc, json.loads(out.getvalue())

    def test_append_and_done_through_the_cli(self):
        rc, rec = self.run_main("append", "picked up", "--kind", "processing", "--task-id", "task-1",
                                "--from", "@q:s", "--text", "x" * 200, "--room", "!r:s")
        self.assertEqual(rc, 0)
        self.assertEqual((rec["kind"], rec["room"], rec["task"]["from"], len(rec["task"]["text"])), ("processing", "!r:s", "@q:s", 160))
        rc, rec = self.run_main("done", "finished", "--task-id", "task-1")
        self.assertTrue(rec["done"] and rec["kind"] == "done" and "room" not in rec)
        rc, rec = self.run_main("append", "CI green")
        self.assertEqual((rec["kind"], "task" in rec), ("notice", False))
        self.assertEqual(len(card.log_path(self.ws).read_text().splitlines()), 3)

    def test_done_without_a_task_is_refused(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            card.main(["done", "finished", "--workspace", str(self.ws)])

    def test_task_file_plus_task_id_override(self):
        p = self.ws / "t.txt"
        p.write_text("id: task-file\nchannel_id: !team:s\nuser_id: @q:s\ntask: hello\n")
        rc, rec = self.run_main("append", "x", "--task-file", str(p), "--task-id", "task-cli")
        self.assertEqual((rec["task"]["id"], rec["task"]["from"], rec["room"]), ("task-cli", "@q:s", "!team:s"))


class TailerRuntime(unittest.TestCase):
    def setUp(self):
        self.ws = Path(tempfile.mkdtemp())
        self.p = tail.paths(self.ws)
        (self.ws / "state").mkdir()
        (self.p["projects"] / "proj").mkdir(parents=True)
        self.transcript = self.p["projects"] / "proj" / "s1.jsonl"
        self.transcript.write_text("")

    def assistant(self, *blocks):
        return json.dumps({"type": "assistant", "message": {"content": list(blocks)}}) + "\n"

    def test_paths_and_newest_transcript(self):
        self.assertEqual(tail.newest_transcript(self.p["projects"]), self.transcript)
        self.assertIsNone(tail.newest_transcript(self.ws / "none"))
        self.assertEqual(self.p["log"], self.ws / "state" / "agent-activity.jsonl")

    def test_follower_emits_only_new_lines_for_the_open_task_and_dedups(self):
        self.transcript.write_text(self.assistant({"type": "text", "text": "old history"}))
        emitted = []
        f = tail.Follower(self.p, emit_fn=lambda *a: emitted.append(a))
        self.assertEqual(f.step(), 0, "history before start is not news")
        with open(self.transcript, "a") as t:
            t.write(self.assistant({"type": "tool_use", "name": "Bash", "input": {"command": "ls", "description": "List"}}))
        self.assertEqual(f.step(), 0, "no open task: nothing emitted")
        self.p["log"].write_text(json.dumps({"ts": 1, "room": "!r:s", "task": {"id": "t1", "from": "@q:s", "text": "hi"}, "line": "a"}) + "\n")
        with open(self.transcript, "a") as t:
            t.write(self.assistant({"type": "tool_use", "name": "Bash", "input": {"command": "ls", "description": "List"}}))
            t.write(self.assistant({"type": "tool_use", "name": "Bash", "input": {"command": "ls", "description": "List"}}))
            t.write(self.assistant({"type": "text", "text": "Deciding.\nmore"}))
        self.assertEqual(f.step(), 2)
        self.assertEqual([(e[0], e[1], e[2]["id"], e[3]) for e in emitted], [("working", "List", "t1", "!r:s"), ("thinking", "Deciding.", "t1", "!r:s")])
        newer = self.p["projects"] / "proj" / "s2.jsonl"
        newer.write_text(self.assistant({"type": "text", "text": "new session"}))
        os.utime(newer, (self.transcript.stat().st_mtime + 5,) * 2)
        self.assertEqual(f.step(), 1, "a newer transcript is followed from its start")
        self.assertEqual(f.path, newer)

    def test_writer_argv_and_emit(self):
        argv = tail.writer_argv("working", "x" * 300, {"id": "t1", "from": "@q:s", "text": "hi"}, "!r:s", self.ws)
        self.assertEqual(argv[2:4], ["append", "x" * tail.MAXLEN])
        self.assertEqual(argv[argv.index("--room") + 1], "!r:s")
        self.assertEqual(argv[argv.index("--workspace") + 1], str(self.ws))
        tail.emit("working", "hello", {"id": "t1"}, None, self.ws)
        rows = [json.loads(l) for l in self.p["log"].read_text().splitlines()]
        self.assertEqual((rows[0]["kind"], rows[0]["line"], rows[0]["task"]["id"]), ("working", "hello", "t1"))

    def test_follow_stops_without_a_transcript_and_polls_with_one(self):
        empty = Path(tempfile.mkdtemp())
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            tail.follow(tail.paths(empty))
        self.assertIn("no transcript", err.getvalue())
        calls = []

        def sleep(_):
            calls.append(1)
            if len(calls) == 2:
                raise KeyboardInterrupt
        with self.assertRaises(KeyboardInterrupt):
            tail.follow(self.p, sleep=sleep)
        self.assertEqual(len(calls), 2)

    def test_main_daemon_uses_the_pidfile_and_foreground_follows(self):
        spawned = []

        class Child:
            pid = 4242
        rc = tail.main(["--daemon", "--workspace", str(self.ws)], popen=lambda *a, **k: (spawned.append(a[0]), Child())[1])
        self.assertEqual((rc, self.p["pid"].read_text(), "--workspace" in spawned[0]), (0, "4242", True))
        self.p["pid"].write_text(str(os.getpid()))  # alive: a second start refuses
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            tail.main(["--daemon", "--workspace", str(self.ws)], popen=lambda *a, **k: self.fail("must not spawn"))
        self.assertIn("already running", out.getvalue())
        followed = []
        self.assertEqual(tail.main(["--workspace", str(self.ws)], run_follow=lambda p: followed.append(p["ws"])), 0)
        self.assertEqual(followed, [self.ws])


if __name__ == "__main__":
    unittest.main(verbosity=1)
