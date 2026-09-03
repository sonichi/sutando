#!/usr/bin/env python3
"""An arming comparison must print what it RESOLVED, and refuse a self-comparison.

`git stash push <file>` reverts to the current branch's HEAD, not the parent, so
"parent vs HEAD" on a feature branch runs the same bytes twice. Two arms with
identical results is exactly what a correct no-op change looks like, so nothing
in the output distinguishes the artifact from a finding.

Run: python3 tests/arm-test-distinct-revisions.test.py
"""
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
ARM = REPO / "scripts" / "arm-test.py"

if not ARM.is_file():
    raise SystemExit(f"arm-test.py not found at {ARM} — refusing to report a "
                     "green run in which no test executed")


def git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)


class TestArming(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        d = Path(self.tmp)
        git(d, "init", "-q", ".")
        git(d, "config", "user.email", "t@t")
        git(d, "config", "user.name", "t")
        (d / "src").mkdir()
        (d / "tests").mkdir()
        (d / "src" / "m.py").write_text("VALUE = 1\n")
        (d / "tests" / "t.py").write_text(
            "import sys, importlib.util\n"
            "s = importlib.util.spec_from_file_location('m', 'src/m.py')\n"
            "m = importlib.util.module_from_spec(s); s.loader.exec_module(m)\n"
            "print('OK' if m.VALUE == 2 else 'FAILED')\n"
            "sys.exit(0 if m.VALUE == 2 else 1)\n")
        git(d, "add", "-A")
        git(d, "commit", "-qm", "base")
        self.base = git(d, "rev-parse", "HEAD").stdout.strip()
        (d / "src" / "m.py").write_text("VALUE = 2\n")
        git(d, "add", "src/m.py")
        git(d, "commit", "-qm", "fix")
        self.head = git(d, "rev-parse", "HEAD").stdout.strip()

    def _run(self, *extra):
        return subprocess.run(
            [sys.executable, str(ARM), "tests/t.py", "--file", "src/m.py", *extra],
            cwd=self.tmp, capture_output=True, text=True)

    def test_same_size_arms_written_in_one_second_still_discriminate(self):
        # `VALUE = 1` and `VALUE = 2` are the same SIZE and land in the same
        # second, so timestamp-mode pyc invalidation reuses stale bytecode.
        r = self._run("--rev", self.base, "--rev", self.head, "--no-worktree-arm")
        self.assertRegex(r.stdout, r"ARM " + self.base[:9] + r".*rc=1")
        self.assertRegex(r.stdout, r"ARM " + self.head[:9] + r".*rc=0")

    def test_a_real_comparison_prints_both_resolved_shas(self):
        r = self._run("--rev", self.base, "--rev", self.head, "--no-worktree-arm")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn(self.base[:9], r.stdout)
        self.assertIn(self.head[:9], r.stdout)
        # The arms must actually differ in outcome, or the fixture proves nothing.
        self.assertIn("rc=1", r.stdout)
        self.assertIn("rc=0", r.stdout)

    def test_a_clean_tree_does_not_turn_a_real_comparison_into_a_refusal(self):
        # THE BLOCKER (@yixuan-ag2, #3817): on a clean tree the implicit arm's
        # blob equals HEAD's, so the dupe check fired on arms nobody compared.
        r = self._run("--rev", self.base, "--rev", self.head)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("tree arm skipped", r.stderr)
        self.assertNotIn("NOT A COMPARISON", r.stderr)
        self.assertNotIn("ARM worktree", r.stdout)

    def test_a_dirty_tree_still_gets_its_own_arm(self):
        (Path(self.tmp) / "src" / "m.py").write_text("VALUE = 3\n")
        r = self._run("--rev", self.head)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("ARM worktree", r.stdout)
        self.assertNotIn("tree arm skipped", r.stderr)

    def test_a_silent_test_does_not_print_rc_twice(self):
        (Path(self.tmp) / "tests" / "t.py").write_text("import sys\nsys.exit(0)\n")
        r = self._run("--rev", self.head, "--no-worktree-arm")
        self.assertNotIn("rc=0  rc=0", r.stdout)
        self.assertRegex(r.stdout, r"rc=0\s*$")

    def test_two_arms_on_the_same_revision_are_refused(self):
        # THE POINT: this is what a stashed "parent" really was.
        r = self._run("--rev", self.head, "--rev", "HEAD", "--no-worktree-arm")
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn("NOT A COMPARISON", r.stderr)

    def test_the_refusal_names_the_shared_blob(self):
        r = self._run("--rev", self.head, "--rev", "HEAD", "--no-worktree-arm")
        blob = git(self.tmp, "rev-parse", "HEAD:src/m.py").stdout.strip()
        self.assertIn(blob[:9], r.stderr)

    def test_the_file_is_restored_afterwards(self):
        before = (Path(self.tmp) / "src" / "m.py").read_bytes()
        self._run("--rev", self.base, "--no-worktree-arm")
        self.assertEqual((Path(self.tmp) / "src" / "m.py").read_bytes(), before)

    def test_the_working_tree_is_an_arm_by_default(self):
        (Path(self.tmp) / "src" / "m.py").write_text("VALUE = 3\n")
        r = self._run("--rev", self.base)
        self.assertIn("worktree", r.stdout)
        # VALUE=3 fails the test, so the worktree arm must be rc=1, and its blob
        # is not in git at all — the row still has to name one.
        self.assertRegex(r.stdout, r"ARM worktree.*rc=1")

    def test_an_unresolvable_rev_is_skipped_loudly_not_silently(self):
        r = self._run("--rev", "does-not-exist", "--rev", self.head,
                      "--no-worktree-arm")
        self.assertIn("does not resolve", r.stderr)
        self.assertIn("SKIPPED", r.stderr)
        self.assertIn(self.head[:9], r.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)
