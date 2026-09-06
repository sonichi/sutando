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
from unittest import mock
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


def _append_many(ws, writer, count, live_rows):
    import pathlib as _pl
    c = load_at(REPO / "skills" / "agent-activity" / "scripts" / "activity.py")
    for i in range(count):
        kind, done = ("done", True) if i == count - 1 else ("working", False)
        c.append(f"{writer}-{i}", kind=kind, room=None, task={"id": f"task-{writer}"}, done=done,
                 workspace=_pl.Path(ws), live_rows=live_rows)


def _archived(ws):
    """Every archived row, across the per-day files (and the undated one for torn rows)."""
    out = []
    for f in sorted(Path(ws, "state").glob("agent-activity.archive*.jsonl")):
        out += f.read_text().splitlines()
    return out


def _bind_in_process(ws, task_id, sid):
    import pathlib as _pl
    h = load_at(REPO / "skills" / "agent-activity" / "hooks" / "activity-hook.py")
    h.bind(h.paths(_pl.Path(ws)), task_id, sid)


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

    def test_the_live_log_rotates_to_an_archive(self):
        for i in range(card.LIVE_ROWS + 3):
            card.append(f"row {i}", kind="notice", room=None, workspace=self.ws)
        live = card.log_path(self.ws).read_text().splitlines()
        arch = _archived(self.ws)
        self.assertEqual((len(live), len(arch)), (card.LIVE_ROWS, 3))
        self.assertIn('"row 0"', arch[0]); self.assertIn(f'"row {card.LIVE_ROWS + 2}"', live[-1])

    def test_concurrent_appenders_lose_no_row_across_rotation(self):
        # Rotation used to replace the inode while another writer held it, losing that writer's row.
        # Eight processes x 30 rows, live window 25: every row lands exactly once, done rows included.
        import multiprocessing as mp
        with mp.get_context("fork").Pool(8) as pool:
            pool.starmap(_append_many, [(str(self.ws), f"w{i}", 30, 25) for i in range(8)])
            pool.close(); pool.join()  # workers exit on their own: a terminate() under coverage deadlocks them
        live = card.log_path(self.ws).read_text().splitlines()
        arch = _archived(self.ws)
        lines = [json.loads(l)["line"] for l in live + arch]
        expected = [f"w{i}-{j}" for i in range(8) for j in range(30)]
        self.assertEqual(sorted(lines), sorted(expected), f"lost={set(expected)-set(lines)} dup={len(lines)-len(set(lines))}")
        self.assertEqual(len(live), 25)
        self.assertEqual(sum(1 for l in live + arch if json.loads(l).get("done")), 8, "every done row survived")

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


class Durable(unittest.TestCase):
    """A finished task leaves a summary line, and the archive is split by the rows' own day."""

    def setUp(self):
        self.ws = Path(tempfile.mkdtemp())
        (self.ws / "state").mkdir()

    def test_a_done_row_writes_a_summary_with_started_rows_and_days(self):
        t = {"id": "task-s1", "event": "$ev", "from": "@q:s"}
        card.append("picked up", kind="processing", room="!r:s", task=t, workspace=self.ws)
        card.append("Bash: tests", kind="working", room="!r:s", task=t, workspace=self.ws)
        card.append("other task", kind="working", room="!r:s", task={"id": "task-s2"}, workspace=self.ws)
        rec = card.append("replied", kind="done", room="!r:s", task=t, done=True, workspace=self.ws)
        lines = card.summaries_path(self.ws).read_text().splitlines()
        self.assertEqual(len(lines), 1)
        s = json.loads(lines[0])
        first = json.loads(card.log_path(self.ws).read_text().splitlines()[0])["ts"]
        self.assertEqual((s["task"], s["line"], s["room"], s["rows"], s["started"], s["ts"]),
                         (t, "replied", "!r:s", 3, first, rec["ts"]))
        self.assertEqual(s["days"], [card.day_of(rec["ts"])])
        self.assertNotIn("task-s1", card.open_task_index(self.ws), "a summarized task leaves the index")
        self.assertIn("task-s2", card.open_task_index(self.ws), "an open task stays in it")

    def test_the_summary_is_exact_after_its_rows_have_rotated_out(self):
        # The reviewers' case: with the window compressed, every row of the task has left the live
        # log by the time it finishes. The summary must still say 3 rows and the real start.
        t = {"id": "task-r1", "event": "$ev"}
        card.append("picked up", kind="processing", room="!r:s", task=t, workspace=self.ws, live_rows=2)
        first = json.loads(card.log_path(self.ws).read_text().splitlines()[0])["ts"]
        card.append("Bash: tests", kind="working", room="!r:s", task=t, workspace=self.ws, live_rows=2)
        for i in range(4):
            card.append(f"noise {i}", kind="notice", room=None, workspace=self.ws, live_rows=2)
        live = [json.loads(l).get("task", {}).get("id") for l in card.log_path(self.ws).read_text().splitlines()]
        self.assertNotIn("task-r1", live, "precondition: the task's rows have rotated out")
        rec = card.append("replied", kind="done", room="!r:s", task=t, done=True, workspace=self.ws, live_rows=2)
        s = json.loads(card.summaries_path(self.ws).read_text().splitlines()[-1])
        self.assertEqual((s["rows"], s["started"], s["ts"]), (3, first, rec["ts"]))

    def test_days_is_every_utc_day_of_the_span_not_its_two_ends(self):
        self.assertEqual(card.day_range(1_757_000_000, 1_757_000_000), ["2025-09-04"])
        self.assertEqual(card.day_range(1_756_900_000, 1_757_100_000), ["2025-09-03", "2025-09-04", "2025-09-05"])
        e = {"started": 1_756_900_000, "rows": 4, "task": {"id": "task-d"}, "room": "!r:s"}
        s = card.summarize({"ts": 1_757_100_000, "line": "replied", "task": {"id": "task-d"}, "room": "!r:s"}, e, self.ws)
        self.assertEqual(s["days"], ["2025-09-03", "2025-09-04", "2025-09-05"])

    def test_a_torn_index_and_an_unreadable_log_leave_the_writer_working(self):
        card.index_path(self.ws).parent.mkdir(parents=True, exist_ok=True)
        card.index_path(self.ws).write_text("not json")
        rec = card.append("x", kind="working", room="!r:s", task={"id": "task-t"}, workspace=self.ws)
        self.assertEqual(card.open_task_index(self.ws)["task-t"]["rows"], 1)
        self.assertEqual(card.summaries_path(self.ws).exists(), False)

    def test_a_done_row_without_a_task_leaves_no_summary(self):
        card.append("closed", kind="done", room=None, done=True, workspace=self.ws)
        self.assertFalse(card.summaries_path(self.ws).exists())

    def test_rotation_splits_the_archive_by_the_rows_own_day(self):
        path = card.log_path(self.ws)
        rows = [{"ts": 1_700_000_000 + i * 86400, "line": f"day{i}", "kind": "notice"} for i in range(3)]
        rows.append({"ts": rows[-1]["ts"] + 10, "line": "kept", "kind": "notice"})
        text = "\n".join([json.dumps(rows[0]), "not json"] + [json.dumps(r) for r in rows[1:]]) + "\n"
        path.write_text(text)
        card.rotate(path, keep=1)
        files = sorted(f.name for f in Path(self.ws, "state").glob("agent-activity.archive*.jsonl"))
        self.assertEqual(files, ["agent-activity.archive.2023-11-14.jsonl", "agent-activity.archive.2023-11-15.jsonl",
                                 "agent-activity.archive.2023-11-16.jsonl", "agent-activity.archive.jsonl"])
        self.assertIn("day1", (self.ws / "state" / "agent-activity.archive.2023-11-15.jsonl").read_text())
        self.assertEqual((self.ws / "state" / "agent-activity.archive.jsonl").read_text(), "not json\n")
        self.assertEqual(path.read_text().strip(), json.dumps(rows[-1]))


class MessageEvent(unittest.TestCase):
    """Every row carries the user message's event id, and the writer knows queued and consolidated."""

    def setUp(self):
        self.ws = Path(tempfile.mkdtemp())
        (self.ws / "state").mkdir()
        (self.ws / "tasks").mkdir()

    def _task(self, name, room="!r:s", event="$ev1"):
        body = f"id: {name}\nuser_id: @q:s\nsender_name: qingyun\ntask: Fix the thing\nsource: ag2space\n"
        if room:
            body += f"channel_id: {room}\n"
        if event:
            body += f"source_message_id: {event}\n"
        (self.ws / "tasks" / f"{name}.txt").write_text(body)
        return self.ws / "tasks" / f"{name}.txt"

    def _rows(self):
        return [json.loads(l) for l in (self.ws / "state" / "agent-activity.jsonl").read_text().splitlines()]

    def test_task_file_carries_the_message_event_id(self):
        task, room = card.task_from_file(self._task("task-e1"))
        self.assertEqual((task["event"], room), ("$ev1", "!r:s"))

    def test_queued_writes_a_notice_with_the_event_and_skips_a_roomless_file(self):
        rc = card.main(["queued", "--task-file", str(self._task("task-q1")), "--workspace", str(self.ws)])
        self.assertEqual(rc, 0)
        rows = self._rows()
        self.assertEqual((rows[-1]["kind"], rows[-1]["line"], rows[-1]["room"], rows[-1]["task"]["event"]),
                         ("notice", "queued", "!r:s", "$ev1"))
        # a cron/bookkeeping task names no room: nothing is written, and never to a default room
        card.main(["queued", "--task-file", str(self._task("task-cron-1", room=None)), "--workspace", str(self.ws)])
        self.assertEqual(len(self._rows()), 1)
        # a room-addressed compatibility task with no source_message_id cannot mount under a message:
        # nothing is written for it either (a queued row nobody can place is misleading drawer activity)
        card.main(["queued", "--task-file", str(self._task("task-noevent", event=None)), "--workspace", str(self.ws)])
        self.assertEqual(len(self._rows()), 1)

    def test_done_into_marks_a_consolidated_reply(self):
        card.main(["done", "consolidated", "--task-id", "task-d1", "--into", "$holder", "--event", "$ev1",
                       "--room", "!r:s", "--workspace", str(self.ws)])
        row = self._rows()[-1]
        self.assertTrue(row["done"])
        self.assertEqual((row["task"]["into"], row["task"]["event"]), ("$holder", "$ev1"))

    def test_pickup_row_carries_the_message_event_id(self):
        p = hook.paths(self.ws); runs = []
        self._task("task-p1")
        hook.handle({"hook_event_name": "PreToolUse", "session_id": "S1", "tool_name": "Read",
                     "tool_input": {"file_path": str(self.ws / "tasks" / "task-p1.txt")}}, p, lambda cmd, **kw: runs.append(cmd))
        cmd = runs[-1]
        self.assertEqual(cmd[cmd.index("--event") + 1], "$ev1")

    def test_queued_with_a_missing_task_file_writes_nothing_and_never_raises(self):
        self.assertEqual(card.main(["queued", "--task-file", str(self.ws / "tasks" / "task-gone.txt"), "--workspace", str(self.ws)]), 0)
        self.assertFalse(card.log_path(self.ws).exists())

    def test_consolidated_into_handles_a_missing_result_and_a_bare_numeric_target(self):
        p = hook.paths(self.ws)
        self.assertIsNone(hook.consolidated_into(self.ws, "task-none"), "no result file: not a dedup, not a crash")
        (self.ws / "results").mkdir()
        self._task("task-77", event="$ev77")
        (self.ws / "results" / "task-x2.txt").write_text("[deduped: 77]\n")
        self.assertEqual(hook.consolidated_into(self.ws, "task-x2"), "$ev77", "a bare id resolves to its task file")
        (self.ws / "results" / "task-x3.txt").write_text("[deduped: task-unknown]\n")
        self.assertEqual(hook.consolidated_into(self.ws, "task-x3"), "", "a dedup whose holder is gone still reads as consolidated")
        del p

    def test_a_result_write_closes_a_task_whose_rows_rotated_out_of_the_live_log(self):
        # Bind + processing through the writer, evict it with noise past the window, then the result
        # write: the done row and the summary must still land, from the writer's index.
        p = hook.paths(self.ws); runs = []
        self._task("task-rot", event="$evr")
        card.append("picked up", kind="processing", room="!r:s", task={"id": "task-rot", "event": "$evr"}, workspace=self.ws, live_rows=2)
        for i in range(4):
            card.append(f"noise {i}", kind="notice", room=None, workspace=self.ws, live_rows=2)
        self.assertNotIn("task-rot", hook.open_tasks(p["log"]), "precondition: gone from the live log")
        hook.bind(p, "task-rot", "S1")
        (self.ws / "results").mkdir(exist_ok=True); (self.ws / "results" / "task-rot.txt").write_text("done.\n")
        out = hook.handle({"hook_event_name": "PostToolUse", "session_id": "S1", "tool_name": "Write",
                           "tool_input": {"file_path": str(self.ws / "results" / "task-rot.txt")}}, p, lambda cmd, **kw: runs.append(cmd))
        self.assertEqual(out, [("done", "replied")])
        self.assertEqual((runs[-1][2], runs[-1][runs[-1].index("--event") + 1]), ("done", "$evr"))

    def test_a_second_sessions_pickup_keeps_the_binding_of_a_task_whose_rows_rotated_out(self):
        # A bound BEFORE rotation, 4 rows evict it from the live log, another session binds B:
        # A's binding must survive the prune and A's later result write must still close it.
        p = hook.paths(self.ws); runs = []
        self._task("task-a2", event="$eva"); self._task("task-b2", event="$evb")
        card.append("picked up", kind="processing", room="!r:s", task={"id": "task-a2", "event": "$eva"}, workspace=self.ws, live_rows=2)
        hook.bind(p, "task-a2", "S1")
        for i in range(4):
            card.append(f"noise {i}", kind="notice", room=None, workspace=self.ws, live_rows=2)
        self.assertNotIn("task-a2", hook.open_tasks(p["log"]), "precondition: A left the live log")
        card.append("picked up", kind="processing", room="!r:s", task={"id": "task-b2", "event": "$evb"}, workspace=self.ws, live_rows=2)
        hook.bind(p, "task-b2", "S2")
        self.assertEqual(hook.load_json(p["bind"], {}).get("task-a2"), "S1", "A's binding survived B's pickup")
        (self.ws / "results").mkdir(exist_ok=True); (self.ws / "results" / "task-a2.txt").write_text("done.\n")
        out = hook.handle({"hook_event_name": "PostToolUse", "session_id": "S1", "tool_name": "Write",
                           "tool_input": {"file_path": str(self.ws / "results" / "task-a2.txt")}}, p, lambda cmd, **kw: runs.append(cmd))
        self.assertEqual(out, [("done", "replied")])
        # The stubbed emit did not write; the real writer's done row closes A in the index, and the
        # next bind prunes it while keeping B.
        card.append("replied", kind="done", room="!r:s", task={"id": "task-a2", "event": "$eva"}, done=True, workspace=self.ws, live_rows=2)
        hook.bind(p, "task-b2", "S2")
        self.assertEqual(sorted(hook.load_json(p["bind"], {})), ["task-b2"])

    def test_a_dedup_pointer_result_closes_as_consolidated_into_the_holder_message(self):
        p = hook.paths(self.ws); runs = []
        self._task("task-x1", event="$evx"); self._task("task-h1", event="$evh")
        (self.ws / "results").mkdir()
        (self.ws / "results" / "task-x1.txt").write_text("[deduped: task-h1]\n")
        with open(p["log"], "a") as f:
            f.write(json.dumps({"ts": 1, "kind": "processing", "line": "picked up", "room": "!r:s",
                                "task": {"id": "task-x1", "event": "$evx"}}) + "\n")
        hook.bind(p, "task-x1", "S1")
        out = hook.handle({"hook_event_name": "PostToolUse", "session_id": "S1", "tool_name": "Write",
                           "tool_input": {"file_path": str(self.ws / "results" / "task-x1.txt")}}, p, lambda cmd, **kw: runs.append(cmd))
        self.assertEqual(out, [("done", "consolidated")])
        cmd = runs[-1]
        self.assertEqual((cmd[2], cmd[cmd.index("--into") + 1], cmd[cmd.index("--event") + 1]), ("done", "$evh", "$evx"))
        # a real reply stays "replied" with no --into
        runs.clear()
        (self.ws / "results" / "task-x1.txt").write_text("Here is the answer.\n")
        out = hook.handle({"hook_event_name": "PostToolUse", "session_id": "S1", "tool_name": "Write",
                           "tool_input": {"file_path": str(self.ws / "results" / "task-x1.txt")}}, p, lambda cmd, **kw: runs.append(cmd))
        self.assertEqual(out, [("done", "replied")])
        self.assertNotIn("--into", runs[-1])


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

    def test_first_touch_of_a_task_file_binds_and_writes_processing_from_its_headers(self):
        (self.ws / "tasks" / "task-abc123.txt").write_text(
            "id: task-abc123\nchannel_id: !team:s\nuser_id: @q:s\nsender_name: qingyun\ntask: Read the latest discussion in this room.\nsource: ag2space\n")
        out = hook.handle(self.pre("S1", "Read", {"file_path": str(self.ws / "tasks" / "task-abc123.txt")}), self.p, self.run)
        self.assertEqual(out, [("processing", "Your agent is working on a task from qingyun: Read the latest disc…")])
        cmd = self.runs[-1]
        self.assertEqual((cmd[2], cmd[cmd.index("--task-id") + 1], cmd[cmd.index("--room") + 1], cmd[cmd.index("--from") + 1]), ("append", "task-abc123", "!team:s", "@q:s"))
        self.assertEqual(cmd[cmd.index("--text") + 1], "Read the latest discussion in this room.")
        self.assertEqual(hook.load_json(self.p["bind"], {}), {"task-abc123": "S1"})
        # a second touch (grep of the same file) neither rebinds nor repeats the row
        self.assertEqual(hook.handle(self.pre("S1", "Bash", {"command": "grep task tasks/task-abc123.txt", "description": "Read it again"}), self.p, self.run), [])
        # an archived task or a results path is not a task-file touch
        self.assertEqual(hook.task_file_refs("cat tasks/archive/task-abc123.txt results/task-abc123.txt"), [])
        self.assertEqual(hook.task_file_refs("cat tasks/task-abc999.txt tasks/task-abc999.txt"), ["task-abc999"])
        # a task file that does not exist binds nothing
        self.assertEqual(hook.handle(self.pre("S1", "Read", {"file_path": "tasks/task-000000.txt"}), self.p, self.run), [])

    def test_a_late_touch_of_an_answered_task_neither_binds_nor_reopens_it(self):
        # The result exists (answered by a dedup or an earlier session): a re-read, a dedup check
        # naming the file, or another session's touch must not write a Processing row after the Done.
        (self.ws / "tasks" / "task-abc123.txt").write_text("id: task-abc123\nchannel_id: !dm:s\nuser_id: @q:s\ntask: hi\n")
        (self.ws / "results").mkdir(exist_ok=True); (self.ws / "results" / "task-abc123.txt").write_text("[deduped: task-zzz]")
        self.assertEqual(hook.handle(self.pre("S2", "Read", {"file_path": str(self.ws / "tasks" / "task-abc123.txt")}), self.p, self.run), [])
        self.assertEqual(hook.load_json(self.p["bind"], {}), {})
        self.assertEqual(self.runs, [])
        self.assertEqual(hook.pickup_line({"from": "@q:s", "text": "x" * 30}), "Your agent is working on a task from @q:s: " + "x" * 20 + "…")
        with mock.patch.dict(os.environ, {"AGENT_DISPLAY_NAME": "air"}):
            self.assertTrue(hook.pickup_line({"sender": "qingyun", "text": "hi"}).startswith("air is working on a task from qingyun: hi"))

    def test_answered_sees_every_archive_shape_the_delivery_paths_write(self):
        # Live, gateway (epoch suffix) and bridge (month dir) shapes; 0 of 400 live ids sat flat.
        ws = self.ws; r = ws / "results"; r.mkdir(exist_ok=True)
        shapes = {
            "task-11111111111111": r / "task-11111111111111.txt",
            "task-22222222222222": r / "archive" / "task-22222222222222-1788657562.txt",
            "task-33333333333333": r / "archive" / "2026-06" / "task-33333333333333.txt",
        }
        for path in shapes.values():
            path.parent.mkdir(parents=True, exist_ok=True); path.write_text("reply")
        for tid in shapes:
            self.assertTrue(hook.answered(ws, tid), tid)
        self.assertFalse(hook.answered(ws, "task-99999999999999"))
        self.assertFalse(hook.answered(ws, "../etc/passwd"))

    def test_agent_name_comes_from_env_then_the_manifest_then_the_fallback(self):
        with mock.patch.dict(os.environ, {"AGENT_DISPLAY_NAME": ""}), mock.patch.object(hook, "manifest_config", return_value=""):
            self.assertTrue(hook.pickup_line({"sender": "q", "text": "x"}).startswith("Your agent is working"))
        with mock.patch.dict(os.environ, {"AGENT_DISPLAY_NAME": ""}), mock.patch.object(hook, "manifest_config", return_value="air"):
            self.assertTrue(hook.pickup_line({"sender": "q", "text": "x"}).startswith("air is working"))
        with mock.patch.dict(os.environ, {"AGENT_DISPLAY_NAME": "env-air"}), mock.patch.object(hook, "manifest_config", return_value="air"):
            self.assertTrue(hook.pickup_line({"sender": "q", "text": "x"}).startswith("env-air is working"))
        self.assertEqual(hook.manifest_config("AGENT_DISPLAY_NAME"), "")  # declared, unset by default

    def test_an_unreadable_or_malformed_manifest_reads_as_unset_not_a_crash(self):
        with mock.patch.object(hook.Path, "read_text", side_effect=OSError("gone")):
            self.assertEqual(hook.manifest_config("AGENT_DISPLAY_NAME"), "")
        with mock.patch.object(hook.Path, "read_text", return_value="{not json"):
            self.assertEqual(hook.manifest_config("AGENT_DISPLAY_NAME"), "")
        with mock.patch.object(hook.Path, "read_text", return_value='{"config": {"AGENT_DISPLAY_NAME": 7}}'):
            self.assertEqual(hook.manifest_config("AGENT_DISPLAY_NAME"), "")  # non-string value is not a name

    def post(self, sid, tool, inp):
        return {"hook_event_name": "PostToolUse", "session_id": sid, "tool_name": tool, "tool_input": inp, "tool_response": {}}

    def test_done_is_written_after_the_result_write_and_only_if_the_file_exists(self):
        self.log({"ts": 1, "room": "!dm:s", "task": {"id": "task-abc123", "from": "@q:s", "text": "x"}, "line": "picked up"})
        hook.bind(self.p, "task-abc123", "S1")
        rpath = self.ws / "results" / "task-abc123.txt"
        write = {"file_path": str(rpath), "content": "reply"}
        # PreToolUse: the write is only proposed — no done row, and not a working row either
        self.assertEqual(hook.handle(self.pre("S1", "Write", dict(write, description="Reply")), self.p, self.run), [])
        # PostToolUse with the file absent (denied, or the tool failed): still nothing
        self.assertEqual(hook.handle(self.post("S1", "Write", write), self.p, self.run), [])
        self.assertEqual(self.runs, [])
        # the tool ran and produced the artifact: now the task closes
        rpath.parent.mkdir(exist_ok=True); rpath.write_text("reply")
        # another session's PostToolUse on the same path closes nothing
        self.assertEqual(hook.handle(self.post("S-other", "Write", write), self.p, self.run), [])
        out = hook.handle(self.post("S1", "Bash", {"command": "cat > \"$WS/results/task-abc123.txt\" << EOF"}), self.p, self.run)
        self.assertEqual(out, [("done", "replied")])
        cmd = self.runs[-1]
        self.assertEqual((cmd[2], cmd[cmd.index("--task-id") + 1], cmd[cmd.index("--room") + 1]), ("done", "task-abc123", "!dm:s"))
        self.assertEqual(hook.result_file_refs("results/task-abc123.txt and results/task-abc123.txt"), ["task-abc123"])

    def test_two_open_tasks_in_one_session_the_newest_wins(self):
        # Review blocker: an abandoned older task must not keep receiving the session's rows.
        self.log({"ts": 1, "room": "!r1:s", "task": {"id": "task-aaa111", "from": "@q:s", "text": "old"}, "line": "a"},
                 {"ts": 2, "room": "!r2:s", "task": {"id": "task-bbb222", "from": "@q:s", "text": "new"}, "line": "b"})
        hook.bind(self.p, "task-aaa111", "S1")
        hook.bind(self.p, "task-bbb222", "S1")
        task, room = hook.bound_task(self.p, "S1")
        self.assertEqual((task["id"], room), ("task-bbb222", "!r2:s"))

    def test_bind_survives_concurrent_writers_and_prunes_closed_tasks(self):
        # Review blocker: the production writer, N processes, one workspace — every binding must land.
        import multiprocessing as mp
        self.log(*[{"ts": i, "room": "!r:s", "task": {"id": f"task-c{i:06d}", "text": "x"}, "line": "a"} for i in range(8)])
        ws = str(self.ws)
        with mp.get_context("fork").Pool(8) as pool:
            pool.starmap(_bind_in_process, [(ws, f"task-c{i:06d}", f"S{i}") for i in range(8)])
            pool.close(); pool.join()  # workers exit on their own: a terminate() under coverage deadlocks them
        binds = hook.load_json(self.p["bind"], {})
        self.assertEqual(sorted(binds), [f"task-c{i:06d}" for i in range(8)], binds)
        self.assertFalse(list((self.ws / "state").glob(".agent-activity.sessions.json.*.tmp")), "no temp files left")
        # a task with a done row is pruned on the next bind
        self.log({"ts": 9, "task": {"id": "task-c000000"}, "line": "done", "done": True})
        hook.bind(self.p, "task-c000001", "S1")
        self.assertNotIn("task-c000000", hook.load_json(self.p["bind"], {}))

    def test_task_id_scope_includes_chat_tasks_and_excludes_bookkeeping(self):
        self.assertEqual(hook.task_file_refs("tasks/task-chat-1779570142563.txt tasks/task-cron-x.txt tasks/task-bench-1.txt tasks/task-workstream-grouping-2.txt"), ["task-chat-1779570142563"])
        self.assertEqual(hook.result_file_refs("results/task-abc123.txt results/task-project-grouping-9.txt"), ["task-abc123"])

    def test_done_text_says_when_nothing_was_sent(self):
        self.assertEqual(hook.done_text('{"content": "[no-send]\\n"}'), "closed, no message sent from here")
        self.assertEqual(hook.done_text('{"content": "[deduped: task-1]"}'), "closed, no message sent from here")
        self.assertEqual(hook.done_text('{"content": "Done, here it is"}'), "replied")

    def test_last_narration_reads_only_the_tail_of_a_large_transcript(self):
        tp = self.ws / "big.jsonl"
        filler = json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "old " * 200}]}}) + "\n"
        with open(tp, "w") as f:
            for _ in range(200):  # ~170 KB, well past the 64 KB window
                f.write(filler)
            f.write(json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "the newest line\nmore"}]}}) + "\n")
        self.assertGreater(tp.stat().st_size, hook.TAIL_BYTES)
        self.assertEqual(hook.last_narration(tp), "the newest line")
        # the window's first, partial line is dropped rather than parsed
        with open(tp, "a") as f:
            f.write('{"type": "assistant", "message": {"content": [{"type": "text", "text": "HALF')
        self.assertEqual(hook.last_narration(tp), "the newest line")

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
