#!/usr/bin/env python3
"""The Stop hook's unwatched-turn-end counter: one file per runtime identity,
written atomically, and a persistence failure that is an exit 1 the hook can
fail open on rather than a silent no-op that would block forever."""
from __future__ import annotations

import io
import os
import runpy
import stat
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))
import stop_hook_unwatched as shu  # noqa: E402

MODULE = REPO / "src" / "stop_hook_unwatched.py"
IDENT_ENV = ("SUTANDO_INSTANCE_ID", "SUTANDO_AGENT_ID", "AGENT_MXID", "AGENT_ID")


class Scoped(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = Path(self.tmp.name) / "state"
        self.saved = {k: os.environ.pop(k, None) for k in IDENT_ENV}

    def tearDown(self):
        for k, v in self.saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self.tmp.cleanup()

    def with_identity(self, instance=None, agent=None):
        for k in IDENT_ENV:
            os.environ.pop(k, None)
        if instance:
            os.environ["SUTANDO_INSTANCE_ID"] = instance
        if agent:
            os.environ["SUTANDO_AGENT_ID"] = agent


class TestCounterPath(Scoped):
    def test_the_canonical_identity_has_the_bare_name(self):
        self.assertEqual(shu.counter_path(self.state), self.state / shu.STEM)

    def test_two_actors_on_one_instance_id_get_two_files(self):
        self.with_identity("w1", "actor-a")
        a = shu.counter_path(self.state)
        self.with_identity("w1", "actor-b")
        b = shu.counter_path(self.state)
        self.assertNotEqual(a, b)
        self.assertTrue(a.name.startswith(shu.STEM + "-") and b.name.startswith(shu.STEM + "-"))


class TestBumpAndClear(Scoped):
    def test_bump_counts_from_one_and_creates_the_state_dir(self):
        self.assertFalse(self.state.exists())
        self.assertEqual(shu.bump(self.state), 1)
        self.assertEqual(shu.bump(self.state), 2)
        self.assertEqual(shu.counter_path(self.state).read_text(), "2\n")

    def test_bump_leaves_no_temp_file_behind(self):
        shu.bump(self.state)
        self.assertEqual(sorted(p.name for p in self.state.iterdir()), [shu.STEM])

    def test_garbage_in_the_file_restarts_the_count(self):
        self.state.mkdir(parents=True)
        shu.counter_path(self.state).write_text("not a number\n")
        self.assertEqual(shu.bump(self.state), 1)

    def test_clear_removes_the_file_and_is_idempotent(self):
        shu.bump(self.state)
        shu.clear(self.state)
        self.assertFalse(shu.counter_path(self.state).exists())
        shu.clear(self.state)  # a second clear has nothing to remove and does not raise

    @unittest.skipIf(os.geteuid() == 0, "root can write to a read-only dir")
    def test_bump_raises_when_the_dir_cannot_be_written_and_leaves_no_temp(self):
        self.state.mkdir(parents=True)
        self.state.chmod(stat.S_IRUSR | stat.S_IXUSR)
        try:
            with self.assertRaises(OSError):
                shu.bump(self.state)
            self.assertEqual(list(self.state.iterdir()), [])
        finally:
            self.state.chmod(stat.S_IRWXU)

    def test_a_failed_replace_unlinks_the_temp_file(self):
        # The counter path is a DIRECTORY: the temp file is written, os.replace fails,
        # and the temp must not be left behind for the next bump to trip over.
        shu.counter_path(self.state).mkdir(parents=True)
        with self.assertRaises(OSError):
            shu.bump(self.state)
        self.assertEqual(sorted(p.name for p in self.state.iterdir()), [shu.STEM])


class TestMain(Scoped):
    def run_main(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            rc = shu.main(list(args))
        return rc, out.getvalue(), err.getvalue()

    def test_bump_prints_the_new_count(self):
        self.assertEqual(self.run_main("bump", "--state", str(self.state))[:2], (0, "1\n"))
        self.assertEqual(self.run_main("bump", "--state", str(self.state))[:2], (0, "2\n"))

    def test_path_prints_the_counter_path_and_clear_removes_it(self):
        rc, out, _ = self.run_main("path", "--state", str(self.state))
        self.assertEqual((rc, out.strip()), (0, str(shu.counter_path(self.state))))
        self.run_main("bump", "--state", str(self.state))
        self.assertEqual(self.run_main("clear", "--state", str(self.state))[0], 0)
        self.assertFalse(shu.counter_path(self.state).exists())

    def test_usage_errors_are_rc_2(self):
        for argv in ((), ("bump",), ("bump", "--state"), ("frob", "--state", "x"), ("bump", "--nope", "x")):
            rc, _, err = self.run_main(*argv)
            self.assertEqual(rc, 2, argv)
            self.assertIn("usage:", err)

    def test_a_persistence_failure_is_rc_1_with_the_reason_on_stderr(self):
        shu.counter_path(self.state).mkdir(parents=True)
        rc, out, err = self.run_main("bump", "--state", str(self.state))
        self.assertEqual((rc, out), (1, ""))
        self.assertIn("stop_hook_unwatched: bump failed:", err)

    def test_the_module_runs_as_a_script(self):
        argv = sys.argv
        sys.argv = [str(MODULE), "path", "--state", str(self.state)]
        out = io.StringIO()
        try:
            with redirect_stdout(out), self.assertRaises(SystemExit) as cm:
                runpy.run_path(str(MODULE), run_name="__main__")
        finally:
            sys.argv = argv
        self.assertEqual(cm.exception.code, 0)
        self.assertEqual(out.getvalue().strip(), str(shu.counter_path(self.state)))


if __name__ == "__main__":
    unittest.main(verbosity=2)
