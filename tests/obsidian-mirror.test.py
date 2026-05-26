#!/usr/bin/env python3
"""Tests for src/obsidian-mirror.py (PR #1082 companion tests).

Tests the core mirror functions: _write_task_mirror idempotency and Result
preservation, _write_result_mirror update-in-place and standalone creation,
_mirror_asks/_mirror_note idempotency, _parse_since, sweep with --since
filtering, and the main() opt-in gate.

Run: python3 tests/obsidian-mirror.test.py
Exit: 0 on pass, 1 on fail.
"""
import importlib.util
import os
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

# Hyphenated filename — use importlib
_spec = importlib.util.spec_from_file_location(
    "obsidian_mirror", ROOT / "src" / "obsidian-mirror.py"
)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

_write_task_mirror = _mod._write_task_mirror
_write_result_mirror = _mod._write_result_mirror
_mirror_asks = _mod._mirror_asks
_mirror_note = _mod._mirror_note
_parse_since = _mod._parse_since
_ensure_vault = _mod._ensure_vault
_task_id_from_path = _mod._task_id_from_path
sweep = _mod.sweep
main = _mod.main


def _make_vault(base: Path) -> Path:
    vault = base / "vault"
    _ensure_vault(vault)
    return vault


def _make_workspace(base: Path) -> Path:
    ws = base / "workspace"
    (ws / "tasks").mkdir(parents=True)
    (ws / "results").mkdir(parents=True)
    (ws / "notes").mkdir(parents=True)
    return ws


TASK_CONTENT = "id: task-123\ntimestamp: 2026-05-26T12:00:00Z\ntask: do something\nsource: slack\naccess_tier: owner\npriority: normal\n"


class TestTaskIdFromPath(unittest.TestCase):
    def test_parses_numeric_id(self):
        self.assertEqual(_task_id_from_path(Path("task-1234567890.txt")), "task-1234567890")

    def test_parses_hyphenated_id(self):
        self.assertEqual(_task_id_from_path(Path("task-chat-1234.txt")), "task-chat-1234")

    def test_returns_none_for_result_file(self):
        self.assertIsNone(_task_id_from_path(Path("result-123.txt")))

    def test_returns_none_for_non_task_file(self):
        self.assertIsNone(_task_id_from_path(Path("pending-questions.md")))


class TestWriteTaskMirror(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.vault = _make_vault(self.tmp)
        self.task_file = self.tmp / "task-123.txt"
        self.task_file.write_text(TASK_CONTENT)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_creates_mirror_file(self):
        result = _write_task_mirror(self.vault, self.task_file)
        self.assertTrue(result)
        mirror = self.vault / "Sutando" / "Agent" / "Tasks" / "task-123.md"
        self.assertTrue(mirror.exists())

    def test_status_is_pending_for_new_task(self):
        _write_task_mirror(self.vault, self.task_file)
        mirror = self.vault / "Sutando" / "Agent" / "Tasks" / "task-123.md"
        content = mirror.read_text()
        self.assertIn("status: pending", content)

    def test_idempotent_returns_false_on_second_call(self):
        _write_task_mirror(self.vault, self.task_file)
        result2 = _write_task_mirror(self.vault, self.task_file)
        self.assertFalse(result2)

    def test_preserves_result_block_on_rewrite(self):
        # First write the task mirror, then manually add a Result section
        _write_task_mirror(self.vault, self.task_file)
        mirror = self.vault / "Sutando" / "Agent" / "Tasks" / "task-123.md"
        existing = mirror.read_text()
        mirror.write_text(existing + "\n## Result\n\ndone!\n")
        # Rewrite the task file (simulate modified task source)
        self.task_file.write_text(TASK_CONTENT + "extra: field\n")
        _write_task_mirror(self.vault, self.task_file)
        new_content = mirror.read_text()
        self.assertIn("## Result", new_content)
        self.assertIn("done!", new_content)

    def test_status_completed_when_result_block_present(self):
        _write_task_mirror(self.vault, self.task_file)
        mirror = self.vault / "Sutando" / "Agent" / "Tasks" / "task-123.md"
        existing = mirror.read_text()
        mirror.write_text(existing + "\n## Result\n\ncompleted\n")
        self.task_file.write_text(TASK_CONTENT + "extra: field\n")
        _write_task_mirror(self.vault, self.task_file)
        self.assertIn("status: completed", mirror.read_text())

    def test_invalid_filename_returns_false(self):
        bad = self.tmp / "not-a-task.txt"
        bad.write_text("whatever")
        self.assertFalse(_write_task_mirror(self.vault, bad))


class TestWriteResultMirror(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.vault = _make_vault(self.tmp)
        self.result_file = self.tmp / "task-456.txt"
        self.result_file.write_text("Task completed successfully.")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_creates_standalone_mirror_when_task_not_seen(self):
        result = _write_result_mirror(self.vault, self.result_file)
        self.assertTrue(result)
        mirror = self.vault / "Sutando" / "Agent" / "Tasks" / "task-456.md"
        self.assertTrue(mirror.exists())

    def test_standalone_mirror_has_completed_status(self):
        _write_result_mirror(self.vault, self.result_file)
        mirror = self.vault / "Sutando" / "Agent" / "Tasks" / "task-456.md"
        self.assertIn("status: completed", mirror.read_text())

    def test_updates_existing_task_mirror_in_place(self):
        # Pre-create a task mirror at pending status
        task_file = self.tmp / "task-456-src.txt"
        task_file.write_text(TASK_CONTENT.replace("task-123", "task-456"))
        src = self.tmp / "task-456.txt"
        src_as_task = self.tmp / "taskfile-456.txt"
        src_as_task.write_text(TASK_CONTENT.replace("task-123", "task-456"))
        _write_task_mirror(self.vault, src_as_task)
        # Now deliver the result
        _write_result_mirror(self.vault, self.result_file)
        mirror = self.vault / "Sutando" / "Agent" / "Tasks" / "task-456.md"
        content = mirror.read_text()
        self.assertIn("## Result", content)
        self.assertIn("Task completed successfully.", content)
        self.assertIn("status: completed", content)

    def test_idempotent_returns_false_on_second_call(self):
        _write_result_mirror(self.vault, self.result_file)
        result2 = _write_result_mirror(self.vault, self.result_file)
        self.assertFalse(result2)

    def test_invalid_filename_returns_false(self):
        bad = self.tmp / "proactive-123.txt"
        bad.write_text("something")
        self.assertFalse(_write_result_mirror(self.vault, bad))


class TestMirrorAsks(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.vault = _make_vault(self.tmp)
        self.ws = _make_workspace(self.tmp)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_copies_pending_questions(self):
        (self.ws / "pending-questions.md").write_text("# Questions\n\n- Q1\n")
        result = _mirror_asks(self.vault, self.ws)
        self.assertTrue(result)
        self.assertEqual(
            (self.vault / "Sutando" / "Agent" / "Asks.md").read_text(),
            "# Questions\n\n- Q1\n",
        )

    def test_idempotent_returns_false(self):
        (self.ws / "pending-questions.md").write_text("same content")
        _mirror_asks(self.vault, self.ws)
        result2 = _mirror_asks(self.vault, self.ws)
        self.assertFalse(result2)

    def test_returns_false_when_file_absent(self):
        self.assertFalse(_mirror_asks(self.vault, self.ws))


class TestMirrorNote(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.vault = _make_vault(self.tmp)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_mirrors_md_file(self):
        note = self.tmp / "my-note.md"
        note.write_text("# Note\n\nHello world\n")
        result = _mirror_note(self.vault, note)
        self.assertTrue(result)
        dest = self.vault / "Sutando" / "Agent" / "Notes" / "my-note.md"
        self.assertTrue(dest.exists())

    def test_skips_non_md_file(self):
        txt = self.tmp / "not-a-note.txt"
        txt.write_text("plain text")
        self.assertFalse(_mirror_note(self.vault, txt))

    def test_idempotent_returns_false(self):
        note = self.tmp / "note.md"
        note.write_text("# same")
        _mirror_note(self.vault, note)
        result2 = _mirror_note(self.vault, note)
        self.assertFalse(result2)


class TestParseSince(unittest.TestCase):
    def test_seconds(self):
        self.assertEqual(_parse_since("30"), 30)

    def test_minutes(self):
        self.assertEqual(_parse_since("30m"), 1800)

    def test_hours(self):
        self.assertEqual(_parse_since("1h"), 3600)

    def test_days(self):
        self.assertEqual(_parse_since("1d"), 86400)

    def test_empty_returns_zero(self):
        self.assertEqual(_parse_since(""), 0)


class TestSweep(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.vault = _make_vault(self.tmp)
        self.ws = _make_workspace(self.tmp)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_sweeps_tasks_and_results(self):
        (self.ws / "tasks" / "task-1.txt").write_text(TASK_CONTENT)
        (self.ws / "results" / "task-1.txt").write_text("done")
        counts = sweep(self.vault, self.ws)
        self.assertGreaterEqual(counts["tasks"], 1)
        self.assertGreaterEqual(counts["results"], 1)

    def test_since_filter_excludes_old_files(self):
        old = self.ws / "tasks" / "task-old.txt"
        old.write_text(TASK_CONTENT)
        # backdate mtime by 2h
        past = time.time() - 7200
        os.utime(old, (past, past))
        counts = sweep(self.vault, self.ws, since_seconds=3600)
        # Old file should not be mirrored
        self.assertEqual(counts["tasks"], 0)

    def test_since_filter_includes_recent_files(self):
        recent = self.ws / "tasks" / "task-new.txt"
        recent.write_text(TASK_CONTENT.replace("task-123", "task-new"))
        counts = sweep(self.vault, self.ws, since_seconds=3600)
        self.assertEqual(counts["tasks"], 1)

    def test_empty_workspace_returns_zero_counts(self):
        counts = sweep(self.vault, self.ws)
        self.assertEqual(counts["tasks"], 0)
        self.assertEqual(counts["results"], 0)


class TestMainOptInGate(unittest.TestCase):
    def setUp(self):
        self._saved_env = os.environ.get("SUTANDO_OBSIDIAN_MIRROR")
        self._saved_ws = os.environ.get("SUTANDO_WORKSPACE")
        self.tmp = Path(tempfile.mkdtemp())
        self.ws = _make_workspace(self.tmp)
        os.environ["SUTANDO_WORKSPACE"] = str(self.ws)

    def tearDown(self):
        if self._saved_env is not None:
            os.environ["SUTANDO_OBSIDIAN_MIRROR"] = self._saved_env
        elif "SUTANDO_OBSIDIAN_MIRROR" in os.environ:
            del os.environ["SUTANDO_OBSIDIAN_MIRROR"]
        if self._saved_ws is not None:
            os.environ["SUTANDO_WORKSPACE"] = self._saved_ws
        elif "SUTANDO_WORKSPACE" in os.environ:
            del os.environ["SUTANDO_WORKSPACE"]
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_returns_0_when_env_not_set(self):
        if "SUTANDO_OBSIDIAN_MIRROR" in os.environ:
            del os.environ["SUTANDO_OBSIDIAN_MIRROR"]
        rc = main([])
        self.assertEqual(rc, 0)

    def test_force_bypasses_gate(self):
        if "SUTANDO_OBSIDIAN_MIRROR" in os.environ:
            del os.environ["SUTANDO_OBSIDIAN_MIRROR"]
        vault_arg = str(self.tmp / "vault")
        rc = main(["--force", "--vault", vault_arg])
        self.assertEqual(rc, 0)

    def test_env_enabled_runs_sweep(self):
        os.environ["SUTANDO_OBSIDIAN_MIRROR"] = "1"
        vault_arg = str(self.tmp / "vault")
        rc = main(["--vault", vault_arg])
        self.assertEqual(rc, 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
