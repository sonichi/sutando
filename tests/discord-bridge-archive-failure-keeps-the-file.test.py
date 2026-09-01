#!/usr/bin/env python3
"""A failed archive must not destroy the task, nor silently re-enter the queue.
Quarantine must not overwrite an earlier one, and callers must honour False."""
from pathlib import Path
import ast
import os
import importlib.util
import tempfile
import unittest
from unittest import mock

# Hermetic-bridge-test lint: explicit config root, access.json seeded under it.
_CFG = tempfile.mkdtemp(prefix="archive-test-cfg-")
os.environ["CLAUDE_CONFIG_DIR"] = _CFG
# Literal, not a loop: the guard reads AST constants, so a loop variable hides
# the path from it. All three bridges are named below, so all three are seeded.
(Path(_CFG) / "channels" / "discord").mkdir(parents=True, exist_ok=True)
(Path(_CFG) / "channels" / "discord" / "access.json").write_text('{"allowFrom": []}')
(Path(_CFG) / "channels" / "slack").mkdir(parents=True, exist_ok=True)
(Path(_CFG) / "channels" / "slack" / "access.json").write_text('{"allowFrom": []}')
(Path(_CFG) / "channels" / "telegram").mkdir(parents=True, exist_ok=True)
(Path(_CFG) / "channels" / "telegram" / "access.json").write_text('{"allowFrom": []}')

SRC = Path(__file__).resolve().parent.parent / "src" / "discord-bridge.py"

_spec = importlib.util.spec_from_file_location(
    "_task_archive", Path(__file__).resolve().parent.parent / "src" / "task_archive.py")
_task_archive = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_task_archive)


def _load(tasks_archive, results_archive):
    """Bind the SHARED policy to temp archive roots. The bridges are adapters
    over this, so the contract is tested here once, not three times."""
    def archive(src, kind, task_id):
        return _task_archive.archive_file(
            src, kind, task_id,
            tasks_dir=tasks_archive, results_dir=results_archive,
            log=lambda m: None)
    return archive


def _load_pair(tasks_archive, results_archive, tasks_dir, cleared):
    """Exec archive_path + archive_file + _archive_delivered_pair together, so
    the shared cleanup policy is exercised rather than pattern-matched."""
    tree = ast.parse(SRC.read_text())
    wanted = {"_archive_delivered_pair"}
    body = [n for n in tree.body
            if isinstance(n, ast.FunctionDef) and n.name in wanted]
    missing = wanted - {n.name for n in body}
    if missing:
        raise AssertionError(f"not found in {SRC}: {sorted(missing)}")
    ns = {"Path": Path, "os": os, "TASKS_DIR": tasks_dir,
          "find_task_file": _task_archive.find_task_file,
          "archive_file": _load(tasks_archive, results_archive),
          "_clear_delivered": lambda t: cleared.append(t)}
    exec(compile(ast.Module(body=body, type_ignores=[]), str(SRC), "exec"), ns)
    return ns["_archive_delivered_pair"]


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
        """A second copy must exist at the instant the source is unlinked.

        Asserted by observing the filesystem from inside unlink, not by
        grepping the source: the link moved into a helper once already, and a
        source-text check reports that refactor as the deletion bug returning.
        """
        src = self.d / "task-8.txt"
        src.write_text("payload")
        observed = {}
        real_unlink = Path.unlink

        def spy(self_path, *a, **kw):
            if self_path == src:
                copies = [p for p in self.d.glob("task-8.txt*") if p != src]
                observed["copies_at_unlink"] = [p.name for p in copies]
                observed["bodies"] = [p.read_text() for p in copies]
            return real_unlink(self_path, *a, **kw)

        with mock.patch.object(Path, "unlink", spy):
            self.assertTrue(self._broken()(src, "tasks", "task-8"))

        self.assertTrue(observed, "the source was never unlinked at all")
        self.assertTrue(observed["copies_at_unlink"],
                        "source unlinked while it was the ONLY copy — data loss")
        self.assertIn("payload", observed["bodies"],
                      "the surviving copy must already hold the bytes")


class CallersHonourTheReturn(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.d = Path(self._td.name)

    def tearDown(self):
        self._td.cleanup()

    """A False return that every caller discards is not a contract."""

    def test_the_pair_clears_the_sentinel_when_the_result_IS_archived(self):
        cleared = []
        pair = _load_pair(self.d / "at", self.d / "ar", self.d / "tasks", cleared)
        (self.d / "tasks").mkdir()
        res = self.d / "task-1.txt"; res.write_text("r")
        (self.d / "tasks" / "task-1.txt").write_text("t")
        pair(res, "task-1")
        self.assertEqual(cleared, ["task-1"], "a fully archived pair must retire its sentinel")
        self.assertFalse(res.exists(), "the result must leave the live queue")

    def test_the_pair_KEEPS_the_sentinel_when_the_result_survives(self):
        # Both routes must fail for the result to still be live: the move (broken
        # archive root) AND the quarantine (unlink refused). A quarantined file
        # HAS left the live glob, so that alone is correctly treated as gone.
        class _NoUnlink(type(Path())):
            def unlink(self, *a, **k):
                raise OSError("unlink refused")
        cleared = []
        broken = self.d / "at"; broken.write_text("not a directory")
        tasks = self.d / "tasks"; tasks.mkdir()
        pair = _load_pair(broken, broken, tasks, cleared)
        res = _NoUnlink(self.d / "task-2.txt"); res.write_text("r")
        pair(res, "task-2")
        self.assertEqual(cleared, [], "a result still under its live name must KEEP its "
                                      "sentinel — clearing it permits a second send")
        self.assertTrue(Path(res).exists(), "a failed archive must never delete the result")

    def test_the_pair_resolves_a_CLAIMED_task_not_a_rebuilt_bare_name(self):
        cleared = []
        tasks = self.d / "tasks"; tasks.mkdir()
        pair = _load_pair(self.d / "at", self.d / "ar", tasks, cleared)
        claimed = tasks / "task-3.claimed-core-1.txt"; claimed.write_text("t")
        res = self.d / "task-3.txt"; res.write_text("r")
        pair(res, "task-3")
        self.assertFalse(claimed.exists(),
                         "a claimed task must be archived, not stranded under its claim name")

    def test_unlink_failure_after_a_successful_link_reports_still_live(self):
        # link() succeeds, unlink() raises -> the second except, which must
        # report False rather than claim the source is gone.
        class _NoUnlink(type(Path())):
            def unlink(self, *a, **k):
                raise OSError("unlink refused")
        # archive root clobbered by a FILE so shutil.move raises for real and
        # the quarantine path runs; link() then succeeds beside the source.
        broken = self.d / "at2"; broken.write_text("not a directory")
        archive = _load(broken, broken)
        src = _NoUnlink(self.d / "task-4.txt"); src.write_text("body")
        self.assertFalse(archive(src, "tasks", "task-4"),
                         "if the source is still under its live name, say so")
        self.assertTrue(Path(src).exists(), "never delete on the failure path")


class EveryBridgeDelegatesTheNeverDeletePolicy(unittest.TestCase):
    """#2819's fix was Discord-only; slack and telegram unlinked the source on a
    failed archive, destroying the only copy of the task."""

    BRIDGES = ("discord-bridge.py", "slack-bridge.py", "telegram-bridge.py")

    def _fn(self, name):
        tree = ast.parse((SRC.parent / name).read_text())
        fns = [n for n in tree.body
               if isinstance(n, ast.FunctionDef) and n.name == "archive_file"]
        self.assertEqual(len(fns), 1, f"{name}: expected exactly one archive_file")
        return fns[0]

    def test_no_bridge_unlinks_its_source_on_a_failed_archive(self):
        for name in self.BRIDGES:
            calls = [c for c in ast.walk(self._fn(name)) if isinstance(c, ast.Call)
                     and getattr(c.func, "attr", "") == "unlink"]
            self.assertEqual(calls, [], f"{name} deletes the task it failed to archive")

    def test_every_bridge_routes_through_the_shared_policy(self):
        for name in self.BRIDGES:
            delegates = [c for c in ast.walk(self._fn(name)) if isinstance(c, ast.Call)
                         and getattr(c.func, "id", "") == "_shared_archive_file"]
            self.assertEqual(len(delegates), 1,
                             f"{name} must call the shared archive policy exactly once")


if __name__ == "__main__":
    unittest.main(verbosity=2)
