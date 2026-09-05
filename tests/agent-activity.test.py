#!/usr/bin/env python3
"""Tests for skills/agent-activity: the row writer and the session-bound hook."""
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


def load_at(path):
    spec = importlib.util.spec_from_file_location(path.stem.replace("-", "_"), path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def load(name):
    return load_at(SCRIPTS / f"{name}.py")


card = load("activity")
hook = load_at(REPO / "skills" / "agent-activity" / "hooks" / "activity-hook.py")


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


class Hook(unittest.TestCase):
    """The PreToolUse/Stop hook: rows bind to the session that claimed the task, or nothing is written."""

    def setUp(self):
        self.ws = Path(tempfile.mkdtemp())
        (self.ws / "state").mkdir()
        (self.ws / "tasks").mkdir()
        self.p = hook.paths(self.ws)
        self.runs = []
        self.run = lambda cmd, **kw: self.runs.append(cmd)

    def log(self, *rows):
        with open(self.p["log"], "a") as f:
            for r in rows:
                f.write(json.dumps(r) + "\n")

    def pre(self, sid, tool, inp):
        return {"hook_event_name": "PreToolUse", "session_id": sid, "tool_name": tool, "tool_input": inp}

    def test_open_tasks_skips_malformed_rows_and_closed_tasks(self):
        self.log({"ts": 1, "room": "!a:s", "task": {"id": "t1", "text": "one"}, "line": "a"},
                 {"ts": 2, "task": [], "line": "bad shape"},
                 {"ts": 3, "task": {"id": 7}, "line": "bad id"},
                 {"ts": 4, "task": {"id": "t2"}, "line": "b"},
                 {"ts": 5, "task": {"id": "t2"}, "line": "c", "done": True})
        with open(self.p["log"], "a") as f:
            f.write("not json\n")
        self.assertEqual(list(hook.open_tasks(self.p["log"])), ["t1"])
        self.assertEqual(hook.open_tasks(self.ws / "none.jsonl"), {})

    def test_processing_task_id_from_task_id_task_file_or_reference(self):
        (self.ws / "tasks" / "task-abc123.txt").write_text("id: task-abc123\ntask: x\n")
        self.assertEqual(hook.processing_task_id("python3 a/activity.py append 'x' --kind processing --task-id task-9", self.ws), "task-9")
        self.assertEqual(hook.processing_task_id("python3 activity.py append x --kind processing --task-file tasks/task-abc123.txt", self.ws), "task-abc123")
        self.assertEqual(hook.processing_task_id("python3 activity.py append x --kind processing --task-file tasks/task-zzz.txt", self.ws), "task-zzz")
        self.assertEqual(hook.processing_task_id("python3 activity.py append x --kind processing task-def456 room", self.ws), "task-def456")
        self.assertIsNone(hook.processing_task_id("python3 activity.py append x --kind notice", self.ws))
        self.assertIsNone(hook.processing_task_id("ls", self.ws))
        self.assertIsNone(hook.processing_task_id("python3 activity.py append 'unterminated --kind processing", self.ws))

    def test_working_line_skips_reads_and_shortens(self):
        self.assertIsNone(hook.working_line("Read", {"description": "Read x"}))
        self.assertIsNone(hook.working_line("Bash", {"command": "ls"}))
        self.assertIsNone(hook.working_line("Bash", "not a dict"))
        self.assertEqual(hook.working_line("Bash", {"description": "First sentence. Second one"}), "First sentence.")
        self.assertEqual(len(hook.working_line("Bash", {"description": "x" * 300})), 100)

    def test_a_processing_append_binds_the_task_to_this_session_and_writes_no_working_row(self):
        out = hook.handle(self.pre("S1", "Bash", {"command": "python3 activity.py append 'picked up' --kind processing --task-id task-1", "description": "Tag the task"}), self.p, self.run)
        self.assertEqual((out, self.runs), ([], []))
        self.assertEqual(hook.load_json(self.p["bind"], {}), {"task-1": "S1"})
        out = hook.handle(self.pre("S1", "Bash", {"command": "python3 activity.py done 'x' --task-id task-1", "description": "Close"}), self.p, self.run)
        self.assertEqual((out, self.runs), ([], []), "the writer's own calls never become rows")

    def test_working_rows_go_only_to_the_task_bound_to_this_session(self):
        self.log({"ts": 1, "room": "!team:s", "task": {"id": "t-team", "from": "@t:s", "text": "team task"}, "line": "a"},
                 {"ts": 2, "room": "!dm:s", "task": {"id": "t-dm", "from": "@q:s", "text": "owner task"}, "line": "b"})
        hook.bind(self.p, "t-team", "S-team")
        hook.bind(self.p, "t-dm", "S-dm")
        # the reviewer's control: a newer sidechain/other session must not reach the team room
        self.assertEqual(hook.handle(self.pre("S-side", "Bash", {"command": "x", "description": "private owner narration"}), self.p, self.run), [])
        self.assertEqual(self.runs, [])
        out = hook.handle(self.pre("S-dm", "Bash", {"command": "x", "description": "Run the gates"}), self.p, self.run)
        self.assertEqual(out, [("working", "Run the gates")])
        cmd = self.runs[-1]
        self.assertEqual((cmd[cmd.index("--task-id") + 1], cmd[cmd.index("--room") + 1], cmd[cmd.index("--workspace") + 1]), ("t-dm", "!dm:s", str(self.ws)))
        self.assertEqual(cmd[cmd.index("--from") + 1], "@q:s")
        out = hook.handle(self.pre("S-team", "Edit", {"file_path": "/x", "description": "Edit the doc"}), self.p, self.run)
        self.assertEqual((out, self.runs[-1][self.runs[-1].index("--room") + 1]), ([("working", "Edit the doc")], "!team:s"))
        self.log({"ts": 3, "task": {"id": "t-dm"}, "line": "done", "done": True})
        self.assertEqual(hook.handle(self.pre("S-dm", "Bash", {"command": "x", "description": "After done"}), self.p, self.run), [], "a closed task takes no more rows")

    def test_stop_writes_this_sessions_last_narration_from_complete_lines_only(self):
        self.log({"ts": 1, "room": "!dm:s", "task": {"id": "t1", "text": "x"}, "line": "a"})
        hook.bind(self.p, "t1", "S1")
        tp = self.ws / "s1.jsonl"
        rows = [json.dumps({"type": "user", "message": {"content": [{"type": "text", "text": "hi"}]}}),
                json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "Checking the log first.\nmore"}, {"type": "tool_use", "name": "Bash", "input": {}}]}})]
        tp.write_text("\n".join(rows) + "\n" + '{"type": "assistant", "message": {"content": [{"type": "text", "text": "HALF WRIT')
        out = hook.handle({"hook_event_name": "Stop", "session_id": "S1", "transcript_path": str(tp)}, self.p, self.run)
        self.assertEqual(out, [("thinking", "Checking the log first.")])
        self.assertEqual(hook.handle({"hook_event_name": "Stop", "session_id": "S-other", "transcript_path": str(tp)}, self.p, self.run), [], "another session's transcript never reaches this task")
        self.assertIsNone(hook.last_narration(self.ws / "missing.jsonl"))
        (self.ws / "fence.jsonl").write_text(json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "```\ncode\n```"}]}}) + "\nnot json\n")
        self.assertIsNone(hook.last_narration(self.ws / "fence.jsonl"))

    def test_payload_without_session_or_with_unknown_event_writes_nothing(self):
        self.assertEqual(hook.handle({"hook_event_name": "PreToolUse", "tool_name": "Bash", "tool_input": {"description": "x"}}, self.p, self.run), [])
        self.assertEqual(hook.handle({"hook_event_name": "Notification", "session_id": "S1"}, self.p, self.run), [])
        self.assertEqual(self.runs, [])

    def test_main_reads_stdin_and_always_exits_zero(self):
        self.log({"ts": 1, "room": "!dm:s", "task": {"id": "t1", "text": "x"}, "line": "a"})
        hook.bind(self.p, "t1", "S1")
        payload = json.dumps(self.pre("S1", "Bash", {"command": "ls", "description": "List files"}))
        self.assertEqual(hook.main(io.StringIO(payload), self.ws), 0)
        rows = [json.loads(l) for l in self.p["log"].read_text().splitlines()]
        self.assertEqual((rows[-1]["kind"], rows[-1]["line"], rows[-1]["task"]["id"]), ("working", "List files", "t1"))
        self.assertEqual(hook.main(io.StringIO("not json"), self.ws), 0)
        self.assertEqual(hook.main(io.StringIO("[1, 2]"), self.ws), 0)
        self.assertEqual(hook.main(io.StringIO(""), self.ws), 0)
        self.assertEqual(hook.emit.__name__, "emit")
        hook.emit("notice", "x", {"id": "t1", "from": 5, "text": None}, None, self.ws)
        rows = [json.loads(l) for l in self.p["log"].read_text().splitlines()]
        self.assertEqual((rows[-1]["kind"], "room" in rows[-1], rows[-1]["task"]), ("notice", False, {"id": "t1"}))


if __name__ == "__main__":
    unittest.main(verbosity=1)
