#!/usr/bin/env python3
"""The resolver's exit code is a verdict about the ENTRY only when the payload is
proven absent or non-regular; an access error is a failure of this call and must
stay retryable, or an old sentinel with a real payload is dropped for good."""
from __future__ import annotations

import io
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
SCRIPTS = REPO / "skills" / "worker-pool" / "scripts"
sys.path.insert(0, str(SCRIPTS))
import pool_delivery as pd  # noqa: E402
import resolve_inbox_entry as rie  # noqa: E402


class TestRegularFileState(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        for p in self.root.rglob("*"):
            try:
                p.chmod(stat.S_IRWXU)
            except OSError:
                pass
        self.tmp.cleanup()

    def test_regular(self):
        f = self.root / "f"; f.write_text("x")
        self.assertEqual(pd.regular_file_state(f), "regular")

    def test_absent_and_not_a_directory_are_absent(self):
        self.assertEqual(pd.regular_file_state(self.root / "nope"), "absent")
        f = self.root / "f"; f.write_text("x")
        self.assertEqual(pd.regular_file_state(f / "under-a-file"), "absent")

    def test_a_directory_and_a_symlink_are_non_regular(self):
        d = self.root / "d"; d.mkdir()
        self.assertEqual(pd.regular_file_state(d), "non-regular")
        t = self.root / "t"; t.write_text("x")
        ln = self.root / "ln"; ln.symlink_to(t)
        self.assertEqual(pd.regular_file_state(ln), "non-regular")

    @unittest.skipIf(os.geteuid() == 0, "root can open a mode-000 file")
    def test_an_access_error_is_unknown_not_absent(self):
        f = self.root / "f"; f.write_text("x"); f.chmod(0)
        self.assertEqual(pd.regular_file_state(f), "unknown")
        # The fail-closed predicate keeps answering False for its own callers.
        self.assertFalse(pd.is_regular_file(f))


class TestResolverExitCodes(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self.tmp.name) / "ws"
        (self.ws / "tasks").mkdir(parents=True)
        self.inbox = self.ws / "deliveries" / "w1"
        self.inbox.mkdir(parents=True)

    def tearDown(self):
        for p in self.ws.rglob("*"):
            try:
                p.chmod(stat.S_IRWXU)
            except OSError:
                pass
        self.tmp.cleanup()

    def sentinel(self, task_id):
        s = self.inbox / f"{task_id}{pd.PENDING_SUFFIX}"
        s.touch()
        return str(s)

    def main(self, entry):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            rc = rie.main([entry, "--workspace", str(self.ws)])
        return rc, out.getvalue().strip(), err.getvalue()

    def test_absent_payload_is_the_typed_3(self):
        rc, out, err = self.main(self.sentinel("task-a"))
        self.assertEqual((rc, out), (3, ""))
        self.assertIn("names no payload", err)
        self.assertIn("(absent)", err)

    def test_a_directory_at_the_payload_name_is_the_typed_3(self):
        (self.ws / "tasks" / "task-d.txt").mkdir()
        rc, out, err = self.main(self.sentinel("task-d"))
        self.assertEqual((rc, out), (3, ""))
        self.assertIn("(non-regular)", err)

    @unittest.skipIf(os.geteuid() == 0, "root can open a mode-000 file")
    def test_an_unreadable_payload_is_the_retryable_1(self):
        p = self.ws / "tasks" / "task-u.txt"
        p.write_text("id: task-u\nsource: test\ntask: x\n")
        p.chmod(0)
        rc, out, err = self.main(self.sentinel("task-u"))
        self.assertEqual((rc, out), (1, ""))
        self.assertIn("could not be opened; retry", err)
        p.chmod(0o600)
        rc, out, _ = self.main(self.sentinel("task-u"))
        self.assertEqual(rc, 0)
        self.assertEqual(Path(out).resolve(), p.resolve())

    @unittest.skipIf(os.geteuid() == 0, "root can open a mode-000 file")
    def test_the_shell_shim_keeps_rc_1_retryable_and_rc_3_typed(self):
        """src/inbox-resolve.sh maps the resolver's typed 3 to 4 and everything
        else to 3, which is what dispatch_task's one-attempt branch keys on."""
        p = self.ws / "tasks" / "task-s.txt"
        p.write_text("id: task-s\nsource: test\ntask: x\n")
        p.chmod(0)
        shim = REPO / "src" / "inbox-resolve.sh"
        # The shim reads the watcher's WORKSPACE_DIR variable and its timeout knob.
        wrapper = (f'WORKSPACE_DIR="{self.ws}"; SUTANDO_INBOX_RESOLVER_TIMEOUT=10; '
                   f'source "{shim}"; resolve_inbox_entry "$1"; echo "rc=$?"')
        env = dict(os.environ, SUTANDO_INBOX_RESOLVER=str(SCRIPTS / "resolve-inbox-entry"),
                   SUTANDO_WORKSPACE_DIR=str(self.ws))
        r = subprocess.run(["bash", "-c", wrapper, "_", self.sentinel("task-s")],
                           capture_output=True, text=True, env=env)
        self.assertIn("rc=3", r.stdout, r.stderr)
        p.chmod(0o600)
        r = subprocess.run(["bash", "-c", wrapper, "_", self.sentinel("task-s")],
                           capture_output=True, text=True, env=env)
        self.assertIn("rc=0", r.stdout, r.stderr)
        p.unlink()
        r = subprocess.run(["bash", "-c", wrapper, "_", self.sentinel("task-s")],
                           capture_output=True, text=True, env=env)
        self.assertIn("rc=4", r.stdout, r.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)
