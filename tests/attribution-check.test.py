#!/usr/bin/env python3
"""scripts/attribution-check.py: replay a day of agent-activity rows against the task files and results.

Correct, unlinked, wrong room, wrong task (another task in flight) and stale rows; report-only by
default, a gate with --max-wrong-pct; redacted output (no task ids, rooms or text). In process.

Run: python3 tests/attribution-check.test.py   (stdlib only)
"""
import contextlib
import importlib.util
import io
import json
import os
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("attribution_check", REPO / "scripts" / "attribution-check.py")
ac = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ac)

T0 = 1793500000  # 2026-11-01T01:06:40Z


def _iso(t):
    import datetime as dt
    return dt.datetime.fromtimestamp(t, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class Attribution(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self.tmp.name)
        for d in ("tasks/archive", "results/archive", "state"):
            (self.ws / d).mkdir(parents=True)
        self._task("task-a", T0, "!roomA:s.space", end=T0 + 300, archived=True)
        self._task("task-b", T0 + 1000, "!roomB:s.space", end=T0 + 1300)
        self.rows = []

    def tearDown(self):
        self.tmp.cleanup()

    def _task(self, tid, start, room, end=None, archived=False):
        d = self.ws / ("tasks/archive" if archived else "tasks")
        (d / f"{tid}.txt").write_text(f"id: {tid}\ntimestamp: {_iso(start)}\nchannel_id: {room}\n\nsecret body\n")
        if end is not None:
            if archived:
                (self.ws / "results/archive" / f"{tid}-{end}.txt").write_text("done")
            else:
                p = self.ws / "results" / f"{tid}.txt"
                p.write_text("done")
                os.utime(p, (end, end))

    def _row(self, ts, task, room, kind="working"):
        self.rows.append({"ts": ts, "kind": kind, "line": "private words", "room": room,
                          "task": {"id": task, "text": "private request"} if task else None})

    def _run(self, *extra):
        with open(self.ws / "state" / "agent-activity.archive.2026-11-01.jsonl", "w") as f:
            f.writelines(json.dumps(r) + "\n" for r in self.rows)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = ac.main(["--workspace", str(self.ws), "--day", "2026-11-01", *extra])
        return rc, json.loads(out.getvalue()), out.getvalue()

    def test_classifies_each_kind_of_row(self):
        self._row(T0 + 10, "task-a", "!roomA:s.space")            # correct
        self._row(T0 + 20, "task-a", "!roomB:s.space")            # wrong room
        self._row(T0 + 1100, "task-a", "!roomA:s.space")          # task-b in flight: wrong task
        self._row(T0 + 700, "task-a", "!roomA:s.space")           # late, nothing else running: stale
        self._row(T0 + 30, None, "!roomA:s.space")                # no task: unlinked
        self._row(T0 + 40, "task-zzz", "!roomA:s.space")          # unknown task: unlinked
        self._row(T0 + 1010, "task-b", None, kind="done")         # live result by mtime, no room on row
        rc, r, _ = self._run()
        self.assertEqual(rc, 0)
        self.assertEqual(r["counts"], {"correct": 1, "wrong_room": 1, "wrong_task": 1, "stale_no_other_task": 1,
                                       "unlinked": 1, "unlinked_unknown_task": 1, "correct_no_room": 1})
        self.assertEqual((r["correct_pct"], r["unlinked_pct"], r["wrong_pct"], r["stale_pct"]), (28.6, 28.6, 28.6, 14.3))
        self.assertEqual(r["examples"]["wrong_task"][0]["side"], "after_result")

    def test_grace_keeps_a_row_just_after_the_result(self):
        self._row(T0 + 350, "task-a", "!roomA:s.space")
        self.assertEqual(self._run()[1]["counts"], {"correct": 1})
        self.assertEqual(self._run("--grace", "10")[1]["counts"], {"stale_no_other_task": 1})

    def test_gate_only_with_a_threshold(self):
        self._row(T0 + 10, "task-a", "!roomB:s.space")
        self.assertEqual(self._run()[0], 0)
        self.assertEqual(self._run("--max-wrong-pct", "1")[0], 1)
        self._row(T0 + 11, "task-a", "!roomA:s.space")
        self.assertEqual(self._run("--max-wrong-pct", "60")[0], 0)

    def test_output_is_redacted(self):
        self._row(T0 + 10, "task-a", "!roomB:s.space")
        self._row(T0 + 1100, "task-a", "!roomA:s.space")
        _, _, raw = self._run()
        for s in ("task-a", "roomA", "roomB", "private", "secret"):
            self.assertNotIn(s, raw)

    def test_falls_back_to_the_live_log_and_skips_bad_lines(self):
        (self.ws / "state" / "agent-activity.jsonl").write_text(
            "not json\n" + json.dumps({"ts": T0 + 5, "kind": "working", "room": "!roomA:s.space", "task": "task-a"}) + "\n")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            ac.main(["--workspace", str(self.ws), "--day", "2001-01-01"])
        self.assertEqual(json.loads(out.getvalue())["counts"], {"correct": 1})

    def test_month_partitioned_archives_are_found(self):
        (self.ws / "tasks/archive/2026-10").mkdir()
        (self.ws / "results/archive/2026-10").mkdir()
        (self.ws / "tasks/archive/2026-10/task-m.txt").write_text(
            f"id: task-m\ntimestamp: {_iso(T0 + 3000)}\nchannel_id: !roomM:s.space\ntask: x\n")
        (self.ws / "results/archive/2026-10/task-m.txt").write_text("done")
        os.utime(self.ws / "results/archive/2026-10/task-m.txt", (T0 + 3100, T0 + 3100))
        self._row(T0 + 3050, "task-m", "!roomM:s.space")
        self._row(T0 + 3500, "task-m", "!roomM:s.space")          # after its month-archived result
        self.assertEqual(self._run()[1]["counts"], {"correct": 1, "stale_no_other_task": 1})

    def test_window_opens_at_the_processing_row(self):
        # task-b's message is at T0+1000, but it is picked up only at T0+1200: a task-a row at
        # T0+1100 had no other task in flight, so it is stale, not misattributed.
        self._row(T0 + 1200, "task-b", "!roomB:s.space", kind="processing")
        self._row(T0 + 1100, "task-a", "!roomA:s.space")
        self._row(T0 + 1150, "task-b", "!roomB:s.space")          # before pickup, past a 10 s grace
        c = self._run("--grace", "10")[1]["counts"]
        self.assertEqual(c["stale_no_other_task"], 2)
        self.assertEqual(c["correct"], 1)

    def test_missing_log_is_a_clear_error(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = ac.main(["--workspace", str(self.ws), "--day", "2001-01-01"])
        self.assertEqual((rc, json.loads(out.getvalue())["error"]), (2, "no activity log: FileNotFoundError"))

    def test_workspace_defaults_to_the_loader(self):
        orig = ac.resolve_workspace
        ac.resolve_workspace = lambda migrate=True: self.ws
        try:
            self._row(T0 + 10, "task-a", "!roomA:s.space")
            with open(self.ws / "state" / "agent-activity.archive.2026-11-01.jsonl", "w") as f:
                f.writelines(json.dumps(r) + "\n" for r in self.rows)
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                ac.main(["--day", "2026-11-01"])
            self.assertEqual(json.loads(out.getvalue())["counts"], {"correct": 1})
        finally:
            ac.resolve_workspace = orig

    def test_task_without_timestamp_uses_file_time(self):
        p = self.ws / "tasks" / "task-c.txt"
        p.write_text("id: task-c\nchannel_id: !roomC:s.space\n")
        os.utime(p, (T0 + 2000, T0 + 2000))
        self._row(T0 + 2010, "task-c", "!roomC:s.space")
        self.assertEqual(self._run()[1]["counts"], {"correct": 1})


if __name__ == "__main__":
    unittest.main()
