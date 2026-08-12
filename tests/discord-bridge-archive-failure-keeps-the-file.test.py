#!/usr/bin/env python3
"""A failed archive must not destroy the task, nor silently re-enter the queue.
Quarantine must not overwrite an earlier one, and callers must honour False."""
from pathlib import Path
import ast
import os
import tempfile
import unittest

# Hermetic-bridge-test lint: explicit config root, access.json seeded under it.
_CFG = tempfile.mkdtemp(prefix="archive-test-cfg-")
os.environ["CLAUDE_CONFIG_DIR"] = _CFG
_ACCESS = Path(_CFG) / "channels" / "discord" / "access.json"
_ACCESS.parent.mkdir(parents=True, exist_ok=True)
_ACCESS.write_text('{"allowFrom": []}')

SRC = Path(__file__).resolve().parent.parent / "src" / "discord-bridge.py"


def _load(tasks_archive, results_archive):
    """Exec archive_path + archive_file against temp archive roots."""
    tree = ast.parse(SRC.read_text())
    wanted = {"archive_path", "archive_file"}
    body = [n for n in tree.body
            if isinstance(n, ast.FunctionDef) and n.name in wanted]
    missing = wanted - {n.name for n in body}
    if missing:
        raise AssertionError(f"not found in {SRC}: {sorted(missing)}")
    ns = {"Path": Path, "os": os, "ARCHIVE_TASKS_DIR": tasks_archive,
          "ARCHIVE_RESULTS_DIR": results_archive}
    exec(compile(ast.Module(body=body, type_ignores=[]), str(SRC), "exec"), ns)
    return ns["archive_file"]


class ArchiveFailureIsNotDeletion(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.d = Path(self._td.name)

    def tearDown(self):
        self._td.cleanup()

    def _broken(self):
        """Clobber the archive root with a FILE so archive_path's mkdir raises —
        a real unwritable-archive failure, no mocking."""
        broken = self.d / "arch-tasks"
        broken.write_text("not a directory")
        return _load(broken, self.d / "arch-results")

    def test_the_happy_path_still_moves_the_file(self):
        src = self.d / "task-1.txt"
        src.write_text("id: task-1\ntask: do the thing\n")
        fn = _load(self.d / "arch-tasks", self.d / "arch-results")
        self.assertTrue(fn(src, "tasks", "task-1"))
        self.assertFalse(src.exists())
        moved = list((self.d / "arch-tasks").rglob("task-1.txt"))
        self.assertEqual(len(moved), 1)
        self.assertIn("do the thing", moved[0].read_text())

    def test_a_failed_archive_never_DELETES_the_task(self):
        """The regression. Pre-fix the file was unlinked and gone."""
        src = self.d / "task-2.txt"
        src.write_text("id: task-2\ntask: irreplaceable\n")
        self._broken()(src, "tasks", "task-2")
        survivor = self.d / "task-2.txt.archive-failed"
        self.assertTrue(survivor.exists(), "the task must still be on disk")
        self.assertIn("irreplaceable", survivor.read_text())

    def test_the_survivor_leaves_the_live_glob(self):
        """Preserving it in place would poll forever; this is the other half."""
        src = self.d / "task-3.txt"
        src.write_text("body")
        self.assertTrue(self._broken()(src, "tasks", "task-3"),
                        "quarantined counts as having left the live queue")
        self.assertEqual(sorted(p.name for p in self.d.glob("task-*.txt")), [],
                         "nothing may still match the queue's *.txt glob")
        self.assertEqual([p.name for p in self.d.glob("task-*.archive-failed")],
                         ["task-3.txt.archive-failed"])

    def test_returns_False_only_when_it_is_still_live(self):
        """Quarantine can fail too — then the caller is owed the truth. A
        read-only parent blocks the link; a taken name no longer does, since the
        collision loop routes around it."""
        sub = self.d / "ro"
        sub.mkdir()
        src = sub / "task-4.txt"
        src.write_text("body")
        fn = self._broken()
        os.chmod(sub, 0o500)
        try:
            self.assertFalse(fn(src, "tasks", "task-4"))
            self.assertTrue(src.exists(), "still live, and still not deleted")
        finally:
            os.chmod(sub, 0o700)

    def test_absent_source_reports_success(self):
        fn = _load(self.d / "arch-tasks", self.d / "arch-results")
        self.assertTrue(fn(self.d / "nope.txt", "tasks", "task-5"))

    def test_results_kind_routes_to_the_results_archive(self):
        src = self.d / "task-6.txt"
        src.write_text("done")
        fn = _load(self.d / "arch-tasks", self.d / "arch-results")
        self.assertTrue(fn(src, "results", "task-6"))
        self.assertEqual(len(list((self.d / "arch-results").rglob("task-6.txt"))), 1)
        self.assertEqual(len(list((self.d / "arch-tasks").rglob("*.txt"))), 0)

    def test_quarantine_never_overwrites_an_earlier_one(self):
        """rename() replaces an existing file on POSIX; a repeated task id would
        destroy the only preserved copy."""
        src = self.d / "task-7.txt"
        src.write_text("new")
        prior = self.d / "task-7.txt.archive-failed"
        prior.write_text("old")
        self.assertTrue(self._broken()(src, "tasks", "task-7"))
        self.assertEqual(prior.read_text(), "old",
                         "the earlier quarantine must survive intact")
        survivors = sorted(p.name for p in self.d.glob("task-7.txt.archive-failed*"))
        self.assertEqual(survivors,
                         ["task-7.txt.archive-failed", "task-7.txt.archive-failed.1"])
        self.assertEqual((self.d / "task-7.txt.archive-failed.1").read_text(), "new")

    def test_archive_file_never_unlinks_its_source_before_a_copy_exists(self):
        """It may unlink only after the hard link is in place — an unlink that
        can run on the failure path is the deletion bug returning."""
        text = SRC.read_text()
        start = text.index("def archive_file(")
        end = text.index("\ndef ", start + 1)
        body = text[start:end]
        self.assertIn("os.link(", body, "quarantine must link before unlinking")
        self.assertLess(body.index("os.link("), body.index("src.unlink()"),
                        "unlink must come after the link, never before")


class CallersHonourTheReturn(unittest.TestCase):
    """A False return that every caller discards is not a contract."""

    def test_delivery_sentinel_clears_only_when_the_result_is_gone(self):
        tree = ast.parse(SRC.read_text())
        clears = [n for n in ast.walk(tree)
                  if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                  and n.func.id == "_clear_delivered"]
        gated = [c for n in ast.walk(tree)
                 if isinstance(n, ast.If) and isinstance(n.test, ast.Name)
                 and n.test.id == "_gone"
                 for c in ast.walk(ast.Module(body=n.body, type_ignores=[]))
                 if isinstance(c, ast.Call) and isinstance(c.func, ast.Name)
                 and c.func.id == "_clear_delivered"]
        self.assertGreaterEqual(len(clears), 2)
        self.assertEqual(len(gated), len(clears),
                         "every _clear_delivered must sit behind `if _gone:` — "
                         "clearing while the result is still live permits a resend")

    def test_the_gate_reads_the_RESULT_archive(self):
        tree = ast.parse(SRC.read_text())
        assigns = [n for n in ast.walk(tree)
                   if isinstance(n, ast.Assign)
                   and any(isinstance(t, ast.Name) and t.id == "_gone" for t in n.targets)
                   and isinstance(n.value, ast.Call)
                   and getattr(n.value.func, "id", None) == "archive_file"
                   and n.value.args
                   and getattr(n.value.args[0], "id", None) == "result_file"]
        self.assertGreaterEqual(len(assigns), 2,
                                "each gated site needs its own "
                                "`_gone = archive_file(result_file, ...)`")


if __name__ == "__main__":
    unittest.main(verbosity=2)
