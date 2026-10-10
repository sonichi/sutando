#!/usr/bin/env python3
"""src/task_handover.py: the row that says the core has a task the Stop hook handed it inline.

Run: python3 tests/task-handover.test.py
"""
from __future__ import annotations

import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))
import task_handover as m  # noqa: E402


class Handover(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.ws = Path(self.td.name)
        (self.ws / "tasks").mkdir()
        self.task = self.ws / "tasks" / "task-1.txt"
        self.task.write_text("id: task-1\nchannel_id: local-voice\nuser_id: voice-local\ntask: draw a tree\n", encoding="utf-8")

    def tearDown(self):
        self.td.cleanup()

    def rows(self):
        p = self.ws / "state" / "agent-activity.jsonl"
        return [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines()] if p.exists() else []

    def test_one_processing_row_from_the_task_headers(self):
        m.note_handed_over(self.task, self.ws)
        (row,) = self.rows()
        self.assertEqual(row["kind"], "processing")
        self.assertEqual(row["task"]["id"], "task-1")
        self.assertEqual(row["task"]["text"], "draw a tree")
        self.assertEqual(row["room"], "local-voice")
        self.assertNotIn("projection", row, "the session's row, not a bus projection")

    def test_a_repeat_is_a_no_op(self):
        m.note_handed_over(self.task, self.ws)
        m.note_handed_over(self.task, self.ws)
        self.assertEqual(len(self.rows()), 1)

    def test_cli_fails_open_on_a_missing_file(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            rc = m.main([str(self.ws / "tasks" / "missing.txt")])
        self.assertEqual(rc, 0)
        self.assertIn("task_handover:", err.getvalue())


if __name__ == "__main__":
    unittest.main()
