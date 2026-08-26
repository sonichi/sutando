#!/usr/bin/env python3
"""check_live_tree_drift: a live checkout far behind its upstream, or carrying
day-old dirty files, must WARN; a current clean tree stays ok. Born from
2026-08-26: the live tree was 116 behind with ~190 dirty files and no probe
noticed."""
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


if __name__ == "__main__":
    unittest.main(verbosity=1)
