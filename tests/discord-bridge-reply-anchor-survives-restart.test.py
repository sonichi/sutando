#!/usr/bin/env python3
"""A quote-reply anchor must survive a bridge restart: `pending_reply_anchors`
is in-memory, so recovery reads `source_message_id` back off the task file."""
from pathlib import Path
import ast
import os
import sys
import tempfile
import unittest

# Hermetic-bridge-test lint: explicit config root, access.json seeded under it.
_CFG = tempfile.mkdtemp(prefix="anchor-test-cfg-")
os.environ["CLAUDE_CONFIG_DIR"] = _CFG
_ACCESS = Path(_CFG) / "channels" / "discord" / "access.json"
_ACCESS.parent.mkdir(parents=True, exist_ok=True)
_ACCESS.write_text('{"allowFrom": []}')

SRC = Path(__file__).resolve().parent.parent / "src" / "discord-bridge.py"
sys.path.insert(0, str(SRC.parent))
from task_archive import find_task_file  # noqa: E402

BODY = "id: {i}\nsource_message_id: 1536878881250742322\nchannel_id: 999\ntask: hi\n"
ANCHOR = 1536878881250742322


def _load_fn(name, tasks_dir, archive_dir):
    """Exec just the named function against temp task/archive roots."""
    tree = ast.parse(SRC.read_text())
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            ns = {"TASKS_DIR": tasks_dir, "ARCHIVE_TASKS_DIR": archive_dir,
                  "find_task_file": find_task_file, "Path": Path}
            exec(compile(ast.Module(body=[node], type_ignores=[]), str(SRC), "exec"), ns)
            return ns[name]
    raise AssertionError(f"{name} not found in {SRC}")


class AnchorRecovery(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.tasks = Path(self._td.name) / "tasks"
        self.archive = Path(self._td.name) / "archive"
        for d in (self.tasks, self.archive):
            d.mkdir(parents=True)
        self.fn = _load_fn("_anchor_from_task_file", self.tasks, self.archive)

    def tearDown(self):
        self._td.cleanup()

    def _archived(self, month, name, task_id):
        (self.archive / month).mkdir(exist_ok=True)
        (self.archive / month / name).write_text(BODY.format(i=task_id))

    def test_recovers_the_id_the_bridge_wrote(self):
        (self.tasks / "task-1.txt").write_text(BODY.format(i="task-1"))
        self.assertEqual(self.fn("task-1"), ANCHOR)

    def test_recovers_from_a_CLAIMED_task_file(self):
        """Production renames claimed work, so a bare-name lookup misses exactly
        the tasks that are in flight."""
        (self.tasks / "task-2.claimed-core-3.txt").write_text(BODY.format(i="task-2"))
        self.assertEqual(self.fn("task-2"), ANCHOR)

    def test_recovers_from_the_monthly_ARCHIVE(self):
        """The task can be archived before its result is delivered."""
        self._archived("2026-08", "task-3.txt", "task-3")
        self.assertEqual(self.fn("task-3"), ANCHOR)

    def test_recovers_from_a_CLAIMED_file_in_the_archive(self):
        self._archived("2026-07", "task-4.claimed-core-1.txt", "task-4")
        self.assertEqual(self.fn("task-4"), ANCHOR)

    def test_live_file_wins_over_a_stale_archived_one(self):
        (self.tasks / "task-5.txt").write_text(
            "id: task-5\nsource_message_id: 999000111222333444\n")
        self._archived("2026-06", "task-5.txt", "task-5")
        self.assertEqual(self.fn("task-5"), 999000111222333444)

    def test_absent_field_is_none_not_a_crash(self):
        (self.tasks / "task-6.txt").write_text("id: task-6\nchannel_id: 999\n")
        self.assertIsNone(self.fn("task-6"))

    def test_missing_file_is_none(self):
        self.assertIsNone(self.fn("task-nope"))

    def test_non_numeric_id_is_none_never_a_string(self):
        """discord.MessageReference wants an int; a str fails at send time,
        i.e. after the result has already been consumed."""
        (self.tasks / "task-7.txt").write_text("id: task-7\nsource_message_id: nope\n")
        self.assertIsNone(self.fn("task-7"))

    def test_an_unreadable_candidate_is_skipped_not_raised(self):
        """A directory where a task file is expected: recovery is best-effort
        and must never take down the delivery loop."""
        (self.tasks / "task-8.txt").mkdir()
        self._archived("2026-05", "task-8.txt", "task-8")
        self.assertEqual(self.fn("task-8"), ANCHOR,
                         "should skip the unreadable one and read the archive")

    def test_a_failing_locator_degrades_to_none(self):
        """If the locator itself raises, return None rather than propagating."""
        def boom(*_a, **_k):
            raise OSError("locator exploded")
        tree = ast.parse(SRC.read_text())
        node = next(n for n in tree.body
                    if isinstance(n, ast.FunctionDef)
                    and n.name == "_anchor_from_task_file")
        ns = {"TASKS_DIR": self.tasks, "ARCHIVE_TASKS_DIR": self.archive,
              "find_task_file": boom, "Path": Path}
        exec(compile(ast.Module(body=[node], type_ignores=[]), str(SRC), "exec"), ns)
        self.assertIsNone(ns["_anchor_from_task_file"]("task-9"))


class Wiring(unittest.TestCase):
    def test_delivery_path_consults_the_fallback(self):
        """Without this the function can pass every case above and never run."""
        text = SRC.read_text()
        anchor = "source_message_anchor = pending_reply_anchors.pop(task_id, None)"
        self.assertIn(anchor, text)
        window = text[text.index(anchor):text.index(anchor) + 400]
        self.assertIn("_anchor_from_task_file(task_id)", window,
                      "the pop site must fall back to the durable task file")
        self.assertIn("if source_message_anchor is None:", window,
                      "the fallback must be conditional on the in-memory anchor")

    def test_the_locator_is_the_canonical_one(self):
        """A bare TASKS_DIR / f'{task_id}.txt' cannot see a claimed file."""
        text = SRC.read_text()
        start = text.index("def _anchor_from_task_file(")
        end = text.index("\ndef ", start + 1)
        self.assertIn("find_task_file(TASKS_DIR, task_id)", text[start:end])


if __name__ == "__main__":
    unittest.main(verbosity=2)
