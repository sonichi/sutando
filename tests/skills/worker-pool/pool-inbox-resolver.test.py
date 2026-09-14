#!/usr/bin/env python3
"""Contract tests for skills/worker-pool/scripts/resolve_inbox_entry.py.

The caller is the core watcher, which runs this as an executable and accepts
only an existing absolute path on the first line — so the cases below assert
through the shipped program, not only through resolve().
"""
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
SCRIPTS = REPO / "skills/worker-pool/scripts"
RESOLVER = SCRIPTS / "resolve-inbox-entry"          # the sh wrapper the core execs
MODULE   = SCRIPTS / "resolve_inbox_entry.py"
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

    def inbox(self, name, recipient="worker-1"):
        """An entry's PATH only — the file is not created, so a test using this
        alone measures the absent-delivery case."""
        return str(self.root / "deliveries" / recipient / name)

    def deliver(self, name, recipient="worker-1"):
        """A real zero-byte sentinel, the way the pool writes one. Positive cases
        must go through this: a path with no file behind it is not a delivery."""
        p = Path(self.inbox(name, recipient))
        p.parent.mkdir(parents=True, exist_ok=True)
        os.close(os.open(p, os.O_CREAT | os.O_EXCL))
        return str(p)

    def run_resolver(self, *args, env=None):
        """Through the SHIPPED wrapper, not `sys.executable` — the core execs this
        file, so a test that picked its own interpreter would never see it."""
        return subprocess.run([str(RESOLVER), *args], capture_output=True,
                              text=True, timeout=30, env={**os.environ, **(env or {})})


class TestTheShippedProgram(Base):
    def test_a_pending_sentinel_resolves_to_an_absolute_payload_path(self):
        want = self.payload()
        r = self.run_resolver(self.deliver("task-1.txt"))
        self.assertEqual(r.returncode, 0, r.stderr)
        got = r.stdout.strip()
        self.assertTrue(got.startswith("/"), f"not absolute: {got!r}")
        self.assertEqual(Path(got).resolve(), want.resolve())

    def test_the_accepted_name_resolves_to_the_same_payload(self):
        """A claimed sentinel spells the same task; neither name implies a
        different body, and the watcher announces whichever it saw."""
        want = self.payload()
        a = self.run_resolver(self.deliver("task-1.accepted")).stdout.strip()
        b = self.run_resolver(self.deliver("task-1.txt")).stdout.strip()
        self.assertEqual(a, b)
        self.assertEqual(Path(a).resolve(), want.resolve())

    def test_the_pending_name_resolves_after_the_sentinel_was_accepted(self):
        """The watcher saw `.txt`; by the time it asks, the folder may hold only
        `.accepted`. The delivery is the same one, so this must still resolve."""
        want = self.payload()
        entry = self.deliver("task-1.txt")
        pd.accept(Path(entry))
        self.assertFalse(Path(entry).exists())
        r = self.run_resolver(entry)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(Path(r.stdout.strip()).resolve(), want.resolve())

    def test_stdout_carries_the_path_and_nothing_else(self):
        # The caller reads the FIRST line and requires it to BE a file, so a
        # banner ahead of the answer would make the answer unusable.
        self.payload()
        r = self.run_resolver(self.deliver("task-1.txt"))
        self.assertEqual(len(r.stdout.strip().splitlines()), 1, r.stdout)


class TestItFailsClosed(Base):
    def test_a_name_that_is_not_a_sentinel_is_refused_with_empty_stdout(self):
        self.payload()
        for bad in ("notes.md", "task-1", "task-1.flag", "", "archive"):
            r = self.run_resolver(self.inbox(bad) if bad else "")
            self.assertNotEqual(r.returncode, 0, f"{bad!r} was accepted")
            self.assertEqual(r.stdout.strip(), "", f"{bad!r} printed something")

    def test_an_entry_with_no_sentinel_behind_it_is_refused(self):
        """The payload exists and the name is well formed; only the delivery is
        absent. Resolving on the name alone would dispatch unassigned work."""
        self.payload()
        r = self.run_resolver(self.inbox("task-1.txt"))
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual(r.stdout.strip(), "")
        self.assertIn("no delivery", r.stderr)

    def test_a_sentinel_whose_payload_is_absent_is_refused(self):
        # Delivered, but no body written: the pool must not name a file the core
        # would then dispatch as an empty task.
        r = self.run_resolver(self.deliver("task-missing.txt"))
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual(r.stdout.strip(), "")
        self.assertIn("no payload", r.stderr)

    def test_a_payload_only_in_the_archive_is_not_resolved(self):
        """The live `tasks/` name is what resolves; an archived-only body leaves
        it absent, so this refuses for absence rather than by inspecting archive."""
        (self.root / "tasks" / "archive").mkdir(parents=True, exist_ok=True)
        (self.root / "tasks" / "archive" / "task-1.txt").write_text("id: task-1\n", encoding="utf-8")
        r = self.run_resolver(self.deliver("task-1.txt"))
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual(r.stdout.strip(), "")

    def test_no_argument_and_too_many_are_both_usage_errors(self):
        for args in ((), ("a", "b")):
            r = self.run_resolver(*args)
            self.assertEqual(r.returncode, 2, r.stderr)
            self.assertEqual(r.stdout.strip(), "")

    def test_a_directory_at_the_payload_name_is_not_a_payload(self):
        (self.root / "tasks" / "task-1.txt").mkdir(parents=True)
        r = self.run_resolver(self.deliver("task-1.txt"))
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual(r.stdout.strip(), "")

    def test_a_folder_name_that_is_not_a_recipient_id_is_refused(self):
        """Delivered and payloaded, so the id is the only reason to refuse: the
        recipient segment is what says whose queue this is."""
        self.payload()
        entry = self.deliver("task-1.txt", recipient="Worker_1")
        r = self.run_resolver(entry)
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual(r.stdout.strip(), "")
        self.assertIn("recipient", r.stderr)


class TestItResolvesTheAssignedTree(Base):
    """`SUTANDO_WORKSPACE_DIR` is the workspace the WATCHER was assigned, and the
    watcher treats it as authoritative. A resolver that derives its own instead
    can answer for a tree whose claims, state and results belong elsewhere.
    """

    def test_an_assigned_workspace_that_disagrees_is_refused_through_the_wrapper(self):
        # The mismatch case: entry in tree A, the watcher serving tree B. Neither
        # is authoritative over the other, so the only safe answer is refusal.
        self.payload()
        entry = self.deliver("task-1.txt")
        other = self.root / "other"
        (other / "tasks").mkdir(parents=True)
        (other / "deliveries" / "worker-1").mkdir(parents=True)
        (other / "tasks" / "task-1.txt").write_text("WRONG TREE\n", encoding="utf-8")
        r = self.run_resolver(entry, env={"SUTANDO_WORKSPACE_DIR": str(other)})
        self.assertNotEqual(r.returncode, 0, "the resolver answered across two trees")
        self.assertEqual(r.stdout.strip(), "", "a mismatch must print no path")

    def test_the_assigned_workspace_agreeing_resolves(self):
        want = self.payload()
        entry = self.deliver("task-1.txt")
        r = self.run_resolver(entry, env={"SUTANDO_WORKSPACE_DIR": str(self.root)})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(Path(r.stdout.strip()), want)

    def test_unset_falls_back_to_the_entrys_own_tree(self):
        # The control that keeps the two above honest: without an assignment the
        # entry is the only source, and resolution must still work.
        want = self.payload()
        entry = self.deliver("task-1.txt")
        r = self.run_resolver(entry, env={"SUTANDO_WORKSPACE_DIR": ""})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(Path(r.stdout.strip()), want)

    def test_an_entry_outside_a_delivery_folder_is_refused(self):
        self.payload()
        r = self.run_resolver(str(self.root / "tasks" / "task-1.txt"))
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual(r.stdout.strip(), "")
        self.assertIn("deliveries", r.stderr)

    def test_a_workspace_that_disagrees_with_the_entry_is_refused(self):
        """An explicit tree is not a redirect: if it is not the one the entry
        lives in, the two disagree about ownership and neither is authoritative."""
        import resolve_inbox_entry as r
        self.payload()
        entry = self.deliver("task-1.txt")
        other = self.root / "elsewhere"
        (other / "deliveries" / "worker-1").mkdir(parents=True)
        (other / "tasks").mkdir(parents=True)
        (other / "tasks" / "task-1.txt").write_text("WRONG TREE\n", encoding="utf-8")
        with self.assertRaises(pd.NotDelivered):
            r.resolve(entry, other)


class TestItRefusesPlantedPaths(Base):
    """A delivery entry and a payload are REGULAR files. A directory or symlink
    at either name lets whoever planted it choose which body the core runs, and
    the caller adopts the returned basename as the task's identity.
    """

    def test_a_directory_at_the_sentinel_name_is_not_a_delivery(self):
        self.payload()
        d = Path(self.inbox("task-1.txt")); d.parent.mkdir(parents=True, exist_ok=True)
        d.mkdir()
        r = self.run_resolver(str(d))
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual(r.stdout.strip(), "")

    def test_a_symlink_at_the_sentinel_name_is_not_a_delivery(self):
        self.payload()
        real = self.root / "planted"; real.write_text("", encoding="utf-8")
        link = Path(self.inbox("task-1.txt")); link.parent.mkdir(parents=True, exist_ok=True)
        link.symlink_to(real)
        r = self.run_resolver(str(link))
        self.assertNotEqual(r.returncode, 0, "a symlink authorised a delivery")
        self.assertEqual(r.stdout.strip(), "")

    def test_a_symlinked_payload_cannot_redirect_the_task_identity(self):
        """The escape kewei measured: the payload name is a link to another
        task's body, and following it dispatches that body under this name."""
        other = self.root / "tasks" / "task-other.txt"
        other.write_text("id: task-other\ntask: NOT THIS ONE\n", encoding="utf-8")
        link = self.root / "tasks" / "task-1.txt"
        link.symlink_to(other)
        entry = self.deliver("task-1.txt")
        r = self.run_resolver(entry)
        self.assertNotEqual(r.returncode, 0, "a symlinked payload was accepted")
        self.assertEqual(r.stdout.strip(), "", "it printed a path into another task")


class TestTheWorkersResolvedInterpreter(Base):
    def test_it_runs_with_no_PATH_python_when_SUTANDO_PY_is_set(self):
        """The core execs this directly and a worker's PATH python3 may be the
        macOS CLT stub, which is why the launcher forwards SUTANDO_PY."""
        want = self.payload()
        entry = self.deliver("task-1.txt")
        bare = self.root / "nopy"; bare.mkdir()
        for t in ("sh", "dirname", "pwd", "command", "env"):
            src = shutil.which(t)
            if src:
                os.symlink(src, bare / t)
        r = self.run_resolver(entry, env={"PATH": str(bare), "SUTANDO_PY": sys.executable})
        self.assertEqual(r.returncode, 0, f"rc={r.returncode} err={r.stderr}")
        self.assertEqual(Path(r.stdout.strip()).resolve(), want.resolve())

    def test_no_interpreter_at_all_fails_loudly_and_prints_no_path(self):
        self.payload()
        entry = self.deliver("task-1.txt")
        bare = self.root / "nopy2"; bare.mkdir()
        for t in ("sh", "dirname", "pwd", "command"):
            src = shutil.which(t)
            if src:
                os.symlink(src, bare / t)
        r = self.run_resolver(entry, env={"PATH": str(bare),
                                         "SUTANDO_PY": "/nonexistent/python3"})
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual(r.stdout.strip(), "")
        self.assertIn("no usable interpreter", r.stderr)

    def test_the_interpreter_is_the_one_the_repos_resolver_names(self):
        """`command -v` and `[ -x ]` both pass on the macOS stub, and executing
        it is what raises the dialog — so the wrapper must ASK scripts/
        python-binary.sh. Redirect that resolver; the wrapper must follow it."""
        fake = self.root / "repo"
        (fake / "scripts").mkdir(parents=True)
        shutil.copytree(SCRIPTS, fake / "skills/worker-pool/scripts")
        marker = self.root / "chosen"
        stub = self.root / "marker-python3"
        stub.write_text(f"#!/bin/sh\necho ran > '{marker}'\n", encoding="utf-8")
        stub.chmod(0o755)
        (fake / "scripts" / "python-binary.sh").write_text(
            f"resolve_python() {{ printf '%s' '{stub}'; }}\n", encoding="utf-8")
        r = subprocess.run([str(fake / "skills/worker-pool/scripts/resolve-inbox-entry"),
                            self.deliver("task-1.txt")],
                           capture_output=True, text=True, timeout=30)
        self.assertTrue(marker.is_file(),
                        "the wrapper chose its own interpreter instead of the "
                        f"resolver's (rc={r.returncode} err={r.stderr})")


class TestMainInProcess(Base):
    """The wrapper cases above prove the shipped path; these run main() in this
    process so its branches are measured rather than only exercised."""

    def _main(self, *args):
        import io
        from contextlib import redirect_stdout, redirect_stderr
        import resolve_inbox_entry as r
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            rc = r.main(list(args))
        return rc, out.getvalue(), err.getvalue()

    def test_it_prints_the_payload_and_returns_zero(self):
        want = self.payload()
        rc, out, _ = self._main(self.deliver("task-1.txt"))
        self.assertEqual(rc, 0)
        self.assertEqual(Path(out.strip()).resolve(), want.resolve())

    def test_an_absent_payload_returns_one(self):
        rc, out, err = self._main(self.deliver("task-missing.txt"))
        self.assertEqual((rc, out.strip()), (1, ""))
        self.assertIn("no payload", err)

    def test_an_absent_delivery_returns_one(self):
        """A different exception type than the case above, and main() must map
        both to the same refusal the caller understands."""
        self.payload()
        rc, out, err = self._main(self.inbox("task-1.txt"))
        self.assertEqual((rc, out.strip()), (1, ""))
        self.assertIn("no delivery", err)

    def test_a_wrong_argument_count_returns_two(self):
        for args in ((), ("a", "b")):
            rc, out, err = self._main(*args)
            self.assertEqual((rc, out.strip()), (2, ""))
            self.assertIn("usage", err)

    def test_an_entry_outside_a_delivery_folder_returns_one(self):
        self.payload()
        rc, out, err = self._main(str(self.root / "tasks" / "task-1.txt"))
        self.assertEqual((rc, out.strip()), (1, ""))
        self.assertIn("deliveries", err)


class TestItDelegatesRatherThanReimplementing(Base):
    def test_the_entry_contract_is_OBTAINED_from_pool_delivery(self):
        """Behaviourally, not by agreeing numerically: an inverse hand-rolled
        here would match the real layout today and drift the moment the forward
        builder moves. Redirect the owner and the answer must follow."""
        import resolve_inbox_entry as r
        self.payload("task-9")
        real = pd.parse_entry
        pd.parse_entry = lambda entry, ws=None: (self.root, "worker-1", "task-9", False)
        try:
            got = r.resolve("anything-at-all")
        finally:
            pd.parse_entry = real
        self.assertEqual(got.resolve(), pd.payload_path(self.root, "task-9").resolve(),
                         "the resolver did not obtain the entry contract from pool_delivery")

    def test_the_payload_path_is_OBTAINED_from_pool_delivery(self):
        import resolve_inbox_entry as r
        entry = self.deliver("task-1.txt")
        elsewhere = self.root / "moved"
        elsewhere.mkdir()
        (elsewhere / "task-1.txt").write_text("id: task-1\n", encoding="utf-8")
        real = pd.payload_path
        pd.payload_path = lambda ws, tid: elsewhere / f"{tid}.txt"
        try:
            got = r.resolve(entry, self.root)
        finally:
            pd.payload_path = real
        self.assertEqual(got.resolve(), (elsewhere / "task-1.txt").resolve(),
                         "the resolver did not obtain its path from pool_delivery")

    def test_the_payload_path_agrees_with_pool_delivery_unpatched(self):
        import resolve_inbox_entry as r
        self.payload()
        self.assertEqual(r.resolve(self.deliver("task-1.txt"), self.root).resolve(),
                         pd.payload_path(self.root, "task-1").resolve())

    def test_the_sentinel_grammar_comes_from_pool_delivery(self):
        import resolve_inbox_entry as r
        self.payload()
        # Spelled literally: a test reading the constant cannot catch it moving.
        self.assertIsNone(pd.parse_sentinel("task-1.flag"))
        with self.assertRaises(pd.NotDelivered):
            r.resolve(self.deliver("task-1.flag"), self.root)

    def test_it_holds_no_inverse_of_the_layout_of_its_own(self):
        """The defect this class exists to prevent is a second owner, so the
        module must not name the folder the forward builder owns."""
        src = MODULE.read_text(encoding="utf-8")
        self.assertNotIn("deliveries", src,
                         "resolve_inbox_entry re-spells the delivery layout")


if __name__ == "__main__":
    unittest.main(verbosity=2)
