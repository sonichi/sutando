#!/usr/bin/env python3
"""Behavioral test: a quote-reply anchor must survive a bridge restart.

Root cause (2026-08-11): the bridge threads a reply only when `reply_to_id` is
set, and the auto-thread path took it from `pending_reply_anchors.pop(task_id)`
— an IN-MEMORY dict. Any bridge restart between task creation and result
delivery empties that dict, so every reply afterwards landed as a fresh message
instead of a quote-reply, with nothing logged.

The recovery data already existed and was never read: the bridge WRITES
`source_message_id: <id>` into every task file at creation
(`discord-bridge.py`, task-write block) and the string appeared exactly once in
the whole module — on the write side. A durable field with no consumer.

Fix: `_anchor_from_task_file(task_id)` reads that field back, and the auto-thread
path falls back to it when the in-memory anchor is absent.

This test extracts the pure function's source and exercises it against REAL temp
files (no `import discord`, matching the other bridge tests' convention), plus a
structural guard that the delivery path actually consults the fallback — without
that guard the function could exist, pass every case here, and never be called.
"""
from pathlib import Path
import ast
import sys
import tempfile
import unittest

SRC = Path(__file__).resolve().parent.parent / "src" / "discord-bridge.py"


def _load_fn(name, tasks_dir):
    """Exec just the named function, with TASKS_DIR bound to a temp dir."""
    tree = ast.parse(SRC.read_text())
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            ns = {"TASKS_DIR": tasks_dir, "Path": Path}
            exec(compile(ast.Module(body=[node], type_ignores=[]), str(SRC), "exec"), ns)
            return ns[name]
    raise AssertionError(f"{name} not found in {SRC}")


def _task(dirpath, task_id, body):
    (dirpath / f"{task_id}.txt").write_text(body)


class AnchorRecovery(unittest.TestCase):
    def test_recovers_the_id_the_bridge_wrote(self):
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            fn = _load_fn("_anchor_from_task_file", d)
            _task(d, "task-1", "id: task-1\nsource_message_id: 1536878881250742322\n"
                               "channel_id: 999\ntask: hi\n")
            self.assertEqual(fn("task-1"), 1536878881250742322)

    def test_absent_field_is_none_not_a_crash(self):
        """A task written before the field existed must degrade, not raise."""
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            fn = _load_fn("_anchor_from_task_file", d)
            _task(d, "task-2", "id: task-2\nchannel_id: 999\ntask: hi\n")
            self.assertIsNone(fn("task-2"))

    def test_missing_file_is_none(self):
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            fn = _load_fn("_anchor_from_task_file", d)
            self.assertIsNone(fn("task-nope"))

    def test_non_numeric_id_is_none_never_a_string(self):
        """discord.MessageReference wants an int; a str would fail at send time,
        i.e. AFTER the result is already consumed."""
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            fn = _load_fn("_anchor_from_task_file", d)
            _task(d, "task-3", "id: task-3\nsource_message_id: not-a-number\n")
            self.assertIsNone(fn("task-3"))


class Wiring(unittest.TestCase):
    def test_delivery_path_consults_the_fallback(self):
        """Without this the function can exist, pass every case above, and never
        be called — which is exactly the defect it fixes, one level up."""
        text = SRC.read_text()
        self.assertIn("source_message_anchor = pending_reply_anchors.pop(task_id, None)", text)
        idx = text.index("source_message_anchor = pending_reply_anchors.pop(task_id, None)")
        window = text[idx:idx + 400]
        self.assertIn("_anchor_from_task_file(task_id)", window,
                      "the pop site must fall back to the durable task file")
        self.assertIn("if source_message_anchor is None:", window,
                      "the fallback must be conditional on the in-memory anchor being absent")


if __name__ == "__main__":
    unittest.main(verbosity=2)
