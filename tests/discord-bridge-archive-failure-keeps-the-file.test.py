#!/usr/bin/env python3
"""A failed archive must not destroy the task, nor silently re-enter the queue.

`archive_file()` used to `unlink()` its source whenever the move raised, deleting
the file whose preservation is the reason archiving exists. Simply leaving it
instead makes it poll forever, so the failure path now quarantines it off the
`*.txt` glob.
"""
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
    ns = {"Path": Path, "ARCHIVE_TASKS_DIR": tasks_archive,
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
        """Quarantine can fail too — then the caller is owed the truth."""
        src = self.d / "task-4.txt"
        src.write_text("body")
        # An occupied non-empty directory at the target makes rename() raise.
        blocker = self.d / "task-4.txt.archive-failed"
        blocker.mkdir()
        (blocker / "occupied").write_text("x")
        self.assertFalse(self._broken()(src, "tasks", "task-4"))
        self.assertTrue(src.exists(), "still live, and still not deleted")

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

    def test_archive_file_never_unlinks(self):
        text = SRC.read_text()
        start = text.index("def archive_file(")
        end = text.index("\ndef ", start + 1)
        self.assertNotIn("unlink", text[start:end],
                         "archive_file must never delete its source")


if __name__ == "__main__":
    unittest.main(verbosity=2)
