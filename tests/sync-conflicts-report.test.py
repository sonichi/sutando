#!/usr/bin/env python3
"""`scripts/sync-conflicts-report.py` must report ONLY real unmerged content.

The reporter exists because `_resolve_conflicts_keep_ours` preserves every
discarded incoming file and nothing then says whether any of them still hold
content the live copy lacks. Measured on one host 2026-08-05: 13 preserved
files, 6 of them strict SUBSETS of the local copy (keeping ours was correct and
lossless), 1 a legacy flat path, 3 carrying real peer content unmerged for
hours. A reporter that flagged all 13 would be as useless as the silence it
replaces -- the whole value is the discrimination, so the negative cases are
tested as hard as the positive one.
"""
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts" / "sync-conflicts-report.py"


class TestSyncConflictsReport(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self._tmp.name) / "workspace"
        self.ws.mkdir()
        subprocess.run(["git", "init", "-q", str(self.ws)], check=True)
        self.batch = self.ws / ".git" / "sutando-sync-conflicts" / "20260805T000000Z-origin_host_peer"

    def tearDown(self):
        self._tmp.cleanup()

    def _pair(self, name: str, live: str, saved: str) -> None:
        """Write the live copy and the preserved incoming copy of one file."""
        (self.ws / "memory").mkdir(exist_ok=True)
        (self.ws / "memory" / name).write_text(live)
        d = self.batch / "memory"
        d.mkdir(parents=True, exist_ok=True)
        (d / name).write_text(saved)

    def _run(self):
        return subprocess.run([sys.executable, str(SCRIPT), str(self.ws)],
                              capture_output=True, text=True)

    def test_reports_a_file_whose_peer_copy_holds_unmerged_content(self):
        """The case it exists for: the peer appended a section we never got."""
        self._pair("lost.md", "# a\nline1\n",
                   "# a\nline1\n" + "\n".join(f"new{i}" for i in range(10)) + "\n")
        r = self._run()
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn("lost.md", r.stdout)

    def test_a_peer_copy_that_is_a_strict_SUBSET_is_NOT_reported(self):
        """6 of 13 real conflicts were this: an older, shorter peer copy.

        Keeping ours lost nothing. Reporting it would train the operator to
        ignore the output, which is how the previous silence started.
        """
        self._pair("subset.md",
                   "# b\n" + "\n".join(f"l{i}" for i in range(40)) + "\n",
                   "# b\nl0\nl1\n")
        r = self._run()
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertNotIn("subset.md", r.stdout)

    def test_a_trivial_line_difference_is_NOT_reported(self):
        """Reformatting is not content. Two real batches differed by 2-3 lines."""
        self._pair("trivial.md", "# c\nx\ny\n", "# c\nx\ny\nz\nw\n")
        r = self._run()
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertNotIn("trivial.md", r.stdout)

    def test_mixed_batch_reports_only_the_lossy_file(self):
        """The discrimination, exercised in one run rather than three."""
        self._pair("lost.md", "# a\n", "# a\n" + "\n".join(f"n{i}" for i in range(9)) + "\n")
        self._pair("subset.md", "# b\n" + "\n".join(f"l{i}" for i in range(30)) + "\n", "# b\nl0\n")
        r = self._run()
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn("lost.md", r.stdout)
        self.assertNotIn("subset.md", r.stdout)

    def test_a_live_file_deleted_since_the_conflict_is_reported(self):
        """Peer content whose destination no longer exists is the worst case:
        nothing on disk holds it and no diff would surface it."""
        d = self.batch / "memory"
        d.mkdir(parents=True, exist_ok=True)
        (d / "gone.md").write_text("only copy\n")
        r = self._run()
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn("gone.md", r.stdout)
        self.assertIn("MISSING", r.stdout)

    def test_no_conflicts_directory_is_clean_not_an_error(self):
        """The overwhelmingly common case. Must be exit 0 and quiet, or a
        30-minute cron turns into a nag."""
        r = self._run()
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertIn("no unmerged peer content", r.stdout)

    def test_no_argument_uses_the_canonical_resolver_NOT_the_caller_cwd(self):
        """Run from a directory that is not the workspace and not a git repo.

        With `Path.cwd()` as the fallback this exits 2 ("not a git repo") -- it
        would be answering about whatever directory invoked it. The cron path
        invokes it from the REPO, not the workspace, so cwd is never the right
        answer. With `resolve_workspace()` it resolves the real workspace and
        reports on that instead. `cwd-lint` gates the same rule statically;
        this pins the behaviour it protects.
        """
        elsewhere = Path(self._tmp.name) / "not-the-workspace"
        elsewhere.mkdir()
        r = subprocess.run([sys.executable, str(SCRIPT)], cwd=str(elsewhere),
                           capture_output=True, text=True)
        self.assertNotEqual(
            r.returncode, 2,
            "no-arg run resolved the caller's cwd instead of the workspace: " + r.stdout)
        self.assertNotIn("not a git repo", r.stdout)

    def test_a_non_git_directory_exits_2_rather_than_claiming_clean(self):
        """A tool that cannot look must not report 'nothing found' -- that is
        indistinguishable from a real clean result."""
        d = Path(self._tmp.name) / "notarepo"
        d.mkdir()
        r = subprocess.run([sys.executable, str(SCRIPT), str(d)],
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 2, r.stdout)
        self.assertNotIn("no unmerged peer content", r.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)
