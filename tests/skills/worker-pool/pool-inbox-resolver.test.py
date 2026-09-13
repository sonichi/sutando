#!/usr/bin/env python3
"""Contract tests for skills/worker-pool/scripts/resolve_inbox_entry.py.

The caller is the core watcher, which runs this as an executable and accepts
only an existing absolute path on the first line — so the cases below assert
through the shipped program, not only through resolve().
"""
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
SCRIPTS = REPO / "skills/worker-pool/scripts"
RESOLVER = SCRIPTS / "resolve_inbox_entry.py"
sys.path.insert(0, str(SCRIPTS))
import pool_delivery as pd  # noqa: E402


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        for d in ("tasks", "results", "deliveries/worker-1"):
            (self.root / d).mkdir(parents=True, exist_ok=True)
        self.addCleanup(self._tmp.cleanup)

    def payload(self, task_id="task-1", body="do a thing"):
        p = self.root / "tasks" / f"{task_id}.txt"
        p.write_text(f"id: {task_id}\nsource: test\ntask: {body}\n", encoding="utf-8")
        return p

    def run_resolver(self, *args, workspace=None):
        env = {**os.environ, "SUTANDO_WORKSPACE_DIR": str(workspace or self.root)}
        return subprocess.run([sys.executable, str(RESOLVER), *args],
                              capture_output=True, text=True, timeout=30, env=env)


class TestTheShippedProgram(Base):
    def test_a_pending_sentinel_resolves_to_an_absolute_payload_path(self):
        want = self.payload()
        r = self.run_resolver("task-1.txt")
        self.assertEqual(r.returncode, 0, r.stderr)
        got = r.stdout.strip()
        self.assertTrue(got.startswith("/"), f"not absolute: {got!r}")
        self.assertEqual(Path(got).resolve(), want.resolve())

    def test_the_accepted_name_resolves_to_the_same_payload(self):
        """A claimed sentinel spells the same task; neither name implies a
        different body, and the watcher announces whichever it saw."""
        want = self.payload()
        a = self.run_resolver("task-1.accepted").stdout.strip()
        b = self.run_resolver("task-1.txt").stdout.strip()
        self.assertEqual(a, b)
        self.assertEqual(Path(a).resolve(), want.resolve())

    def test_only_the_basename_is_read_so_a_path_also_resolves(self):
        want = self.payload()
        r = self.run_resolver(str(self.root / "deliveries" / "worker-1" / "task-1.txt"))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(Path(r.stdout.strip()).resolve(), want.resolve())

    def test_stdout_carries_the_path_and_nothing_else(self):
        # The caller reads the FIRST line and requires it to BE a file, so a
        # banner ahead of the answer would make the answer unusable.
        self.payload()
        r = self.run_resolver("task-1.txt")
        self.assertEqual(len(r.stdout.strip().splitlines()), 1, r.stdout)


class TestItFailsClosed(Base):
    def test_a_name_that_is_not_a_sentinel_is_refused_with_empty_stdout(self):
        self.payload()
        for bad in ("notes.md", "task-1", "task-1.flag", "", "archive"):
            r = self.run_resolver(bad)
            self.assertNotEqual(r.returncode, 0, f"{bad!r} was accepted")
            self.assertEqual(r.stdout.strip(), "", f"{bad!r} printed something")

    def test_a_sentinel_whose_payload_is_absent_is_refused(self):
        # No payload written: the pool must not name a file the core would then
        # dispatch as an empty task.
        r = self.run_resolver("task-missing.txt")
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual(r.stdout.strip(), "")
        self.assertIn("no payload", r.stderr)

    def test_a_payload_only_in_the_archive_is_not_resolved(self):
        """The live `tasks/` name is what resolves; an archived-only body leaves
        it absent, so this refuses for absence rather than by inspecting archive."""
        (self.root / "tasks" / "archive").mkdir(parents=True, exist_ok=True)
        (self.root / "tasks" / "archive" / "task-1.txt").write_text("id: task-1\n", encoding="utf-8")
        r = self.run_resolver("task-1.txt")
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual(r.stdout.strip(), "")

    def test_no_argument_and_too_many_are_both_usage_errors(self):
        for args in ((), ("a", "b")):
            r = self.run_resolver(*args)
            self.assertEqual(r.returncode, 2, r.stderr)
            self.assertEqual(r.stdout.strip(), "")

    def test_a_directory_at_the_payload_name_is_not_a_payload(self):
        (self.root / "tasks" / "task-1.txt").mkdir(parents=True)
        r = self.run_resolver("task-1.txt")
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual(r.stdout.strip(), "")


class TestItResolvesTheAssignedTree(Base):
    def test_it_honours_the_workspace_variable_the_watcher_honours(self):
        """The spawner points a worker at its own tree with
        SUTANDO_WORKSPACE_DIR; a resolver that ignored it would answer about the
        default workspace while the watcher watched the worker's — two trees."""
        want = self.payload()
        other = Path(self._tmp.name) / "not-this-one"
        (other / "tasks").mkdir(parents=True)
        (other / "tasks" / "task-1.txt").write_text("id: task-1\nWRONG TREE\n", encoding="utf-8")
        r = self.run_resolver("task-1.txt")            # env names self.root
        self.assertEqual(r.returncode, 0, r.stderr)
        got = Path(r.stdout.strip()).resolve()
        self.assertEqual(got, want.resolve())
        self.assertNotEqual(got, (other / "tasks" / "task-1.txt").resolve())

    def test_an_unset_variable_does_not_silently_use_the_wrong_tree(self):
        """Control: with the variable absent the answer must NOT be this test's
        tree — it falls back to the sanctioned resolver, which is a different
        tree here, so the case above is measuring the variable and not luck."""
        self.payload()
        env = {k: v for k, v in os.environ.items() if k != "SUTANDO_WORKSPACE_DIR"}
        r = subprocess.run([sys.executable, str(RESOLVER), "task-1.txt"],
                           capture_output=True, text=True, timeout=30, env=env)
        # Either outcome is correct, but NEITHER may be this tree — an `if rc==0`
        # guard here would let the case pass without measuring anything.
        mine = (self.root / "tasks" / "task-1.txt").resolve()
        if r.returncode == 0:
            self.assertNotEqual(Path(r.stdout.strip()).resolve(), mine)
        else:
            self.assertEqual(r.stdout.strip(), "", r.stdout)
            self.assertNotIn(str(mine), r.stdout)


class TestItDelegatesRatherThanReimplementing(Base):
    def test_the_payload_path_is_OBTAINED_from_pool_delivery(self):
        """Behaviourally, not by agreeing numerically: an equivalent hand-rolled
        `tasks/<id>.txt` here would match the real layout today and drift the
        moment pool_delivery moves it. Redirect the owner and the answer must
        follow — a reimplementation would ignore the redirect."""
        import resolve_inbox_entry as r
        elsewhere = self.root / "moved"
        (elsewhere).mkdir()
        (elsewhere / "task-1.txt").write_text("id: task-1\n", encoding="utf-8")
        real = pd.payload_path
        pd.payload_path = lambda ws, tid: elsewhere / f"{tid}.txt"
        try:
            got = r.resolve("task-1.txt", self.root)
        finally:
            pd.payload_path = real
        self.assertEqual(got.resolve(), (elsewhere / "task-1.txt").resolve(),
                         "the resolver did not obtain its path from pool_delivery")

    def test_the_payload_path_agrees_with_pool_delivery_unpatched(self):
        import resolve_inbox_entry as r
        self.payload()
        self.assertEqual(r.resolve("task-1.txt", self.root).resolve(),
                         pd.payload_path(self.root, "task-1").resolve())

    def test_the_sentinel_grammar_comes_from_pool_delivery(self):
        import resolve_inbox_entry as r
        self.payload()
        # Spelled literally: a test reading the constant cannot catch it moving.
        self.assertIsNone(pd.parse_sentinel("task-1.flag"))
        with self.assertRaises(ValueError):
            r.resolve("task-1.flag", self.root)


if __name__ == "__main__":
    unittest.main(verbosity=2)
