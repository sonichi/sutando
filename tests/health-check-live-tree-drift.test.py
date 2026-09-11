#!/usr/bin/env python3
"""check_live_tree_drift: a live checkout far behind its upstream, or carrying
day-old dirty files, must WARN; a current clean tree stays ok."""
import importlib.util
import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

_HC = Path(__file__).resolve().parent.parent / "src" / "health-check.py"
spec = importlib.util.spec_from_file_location("hc", _HC)
hc = importlib.util.module_from_spec(spec)
try:
    spec.loader.exec_module(hc)
except SystemExit:
    pass


def _git(cwd, *args):
    subprocess.run(["git", "-C", str(cwd), *args], check=True,
                   capture_output=True,
                   env={**os.environ,
                        "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
                        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"})


class LiveTreeDrift(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.origin = root / "origin.git"
        self.clone = root / "clone"
        # -b main on init+clone: a defaultBranch-dependent bare HEAD dangles
        # on CI, so the clone checks out no branch and set-upstream exits 128.
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main",
                        str(self.origin)], check=True)
        subprocess.run(["git", "init", "-q", str(root / "seed")], check=True)
        seed = root / "seed"
        (seed / "f.txt").write_text("a\n")
        _git(seed, "add", "f.txt"); _git(seed, "commit", "-qm", "c1")
        _git(seed, "push", "-q", str(self.origin), "HEAD:main")
        subprocess.run(["git", "clone", "-q", "-b", "main", str(self.origin), str(self.clone)],
                       check=True)
        _git(self.clone, "branch", "--set-upstream-to=origin/main")

    def tearDown(self):
        self.tmp.cleanup()

    def _advance_origin(self, n):
        seed = Path(self.tmp.name) / "seed"
        for i in range(n):
            (seed / "f.txt").write_text(f"rev{i}\n")
            _git(seed, "commit", "-aqm", f"c{i+2}")
        _git(seed, "push", "-q", str(self.origin), "HEAD:main")
        _git(self.clone, "fetch", "-q", "origin")

    def test_current_clean_tree_is_ok(self):
        r = hc.check_live_tree_drift(repo_root=self.clone)
        self.assertEqual(r["status"], "ok", r)

    def test_far_behind_warns(self):
        self._advance_origin(5)
        r = hc.check_live_tree_drift(repo_root=self.clone, behind_max=3)
        self.assertEqual(r["status"], "warn", r)
        self.assertIn("behind", r["detail"])

    def test_day_old_dirty_file_warns(self):
        f = self.clone / "f.txt"
        f.write_text("dirty\n")
        old = time.time() - 2 * 86400
        os.utime(f, (old, old))
        r = hc.check_live_tree_drift(repo_root=self.clone)
        self.assertEqual(r["status"], "warn", r)
        self.assertIn("dirty", r["detail"])

    def test_fresh_dirty_file_stays_ok(self):
        (self.clone / "f.txt").write_text("dirty\n")
        r = hc.check_live_tree_drift(repo_root=self.clone)
        self.assertEqual(r["status"], "ok", r)

    def test_untracked_files_do_not_warn(self):
        junk = self.clone / "junk.txt"
        junk.write_text("x\n")
        old = time.time() - 2 * 86400
        os.utime(junk, (old, old))
        r = hc.check_live_tree_drift(repo_root=self.clone)
        self.assertEqual(r["status"], "ok", r)

    def test_non_git_dir_is_ok_not_error(self):
        with tempfile.TemporaryDirectory() as plain:
            r = hc.check_live_tree_drift(repo_root=plain)
        self.assertEqual(r["status"], "ok", r)
        self.assertIn("not a git checkout", r["detail"])

    def test_deleted_tracked_file_counts_dirty_never_stale(self):
        (self.clone / "f.txt").unlink()  # " D" porcelain row with no mtime
        r = hc.check_live_tree_drift(repo_root=self.clone)
        self.assertEqual(r["status"], "ok", r)
        self.assertIn("1 tracked dirty", r["detail"])

    def test_probe_routes_every_git_call_through_git_argv(self):
        # Production-path proof: substitute the resolver and count real calls.
        calls = []
        real = hc.git_argv
        hc.git_argv = lambda *a: (calls.append(a) or real(*a))
        try:
            r = hc.check_live_tree_drift(repo_root=self.clone)
        finally:
            hc.git_argv = real
        self.assertEqual(r["status"], "ok", r)
        self.assertGreaterEqual(len(calls), 3, "probe bypassed git_argv")
        self.assertTrue(all(a[0] == "-C" for a in calls), calls)

    def test_no_runnable_git_is_ok_not_a_recurring_warn(self):
        def _raise(*a):
            raise hc.GitUnavailable("no git")
        real = hc.git_argv
        hc.git_argv = _raise
        try:
            r = hc.check_live_tree_drift(repo_root=self.clone)
        finally:
            hc.git_argv = real
        self.assertEqual(r["status"], "ok", r)
        self.assertIn("no runnable git", r["detail"])

    def test_internal_error_degrades_to_warn(self):
        real = hc.time.time
        hc.time.time = lambda: (_ for _ in ()).throw(RuntimeError("clock"))
        try:
            r = hc.check_live_tree_drift(repo_root=self.clone)
        finally:
            hc.time.time = real
        self.assertEqual(r["status"], "warn", r)
        self.assertIn("could not measure", r["detail"])

    def test_failed_status_read_warns_instead_of_reporting_clean(self):
        # A directory can never be opened as a file, whatever the uid, so this
        # fails `status` while rev-parse/rev-list still succeed.
        idx = self.clone / ".git" / "index"
        idx.unlink(); idx.mkdir()
        r = hc.check_live_tree_drift(repo_root=self.clone)
        self.assertEqual(r["status"], "warn", r)
        self.assertNotIn("tracked dirty", r["detail"],
                         "reported a dirty count it never measured")

    def test_old_staged_rename_warns(self):
        _git(self.clone, "mv", "f.txt", "renamed.txt")
        _git(self.clone, "add", "-A")
        old = time.time() - 2 * 86400
        os.utime(self.clone / "renamed.txt", (old, old))
        r = hc.check_live_tree_drift(repo_root=self.clone)
        self.assertEqual(r["status"], "warn", r)
        self.assertIn("renamed.txt", r["detail"], "named the arrow record, not the file")

    def test_fresh_staged_rename_stays_ok(self):
        # Control: without this, always-warn would satisfy the test above.
        _git(self.clone, "mv", "f.txt", "renamed.txt")
        _git(self.clone, "add", "-A")
        r = hc.check_live_tree_drift(repo_root=self.clone)
        self.assertEqual(r["status"], "ok", r)
        self.assertIn("1 tracked dirty", r["detail"])

    def test_porcelain_z_returns_rename_destination(self):
        # -z packs a rename as destination NUL original NUL; the destination is
        # the path that exists on disk, so only it can be age-checked.
        rec = hc._porcelain_z_tracked_paths("R  new.txt\x00old.txt\x00 M plain.txt\x00")
        self.assertEqual(rec, ["new.txt", "plain.txt"])
        self.assertEqual(hc._porcelain_z_tracked_paths("?? junk.txt\x00"), [])


if __name__ == "__main__":
    unittest.main(verbosity=1)
