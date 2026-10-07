#!/usr/bin/env python3
"""check_live_tree_drift measures staleness against the TRUNK.

Two shapes made it certify the drift it exists to catch: a branch with no
upstream (distance defaulted to 0) and a PR branch tracking itself (0 behind
by construction, however far the trunk moved).

Run: python3 tests/live-tree-drift-reference.test.py
"""
import importlib.util
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

_spec = importlib.util.spec_from_file_location("hc", ROOT / "src/health-check.py")
hc = importlib.util.module_from_spec(_spec)
try:
    _spec.loader.exec_module(hc)
except SystemExit:
    pass


def _git(root, *args):
    return subprocess.run(["git", "-C", str(root), *args],
                          capture_output=True, text=True)


def _make_repo(commits_ahead_on_trunk=0, branch=None, set_upstream=False):
    """origin/main with N commits, plus a checkout that may lag behind it."""
    tmp = Path(tempfile.mkdtemp())
    origin, work = tmp / "origin", tmp / "work"
    subprocess.run(["git", "init", "-q", "-b", "main", str(origin)], check=True)
    _git(origin, "config", "user.email", "t@t.t")
    _git(origin, "config", "user.name", "t")
    (origin / "f.txt").write_text("0\n")
    _git(origin, "add", "-A"); _git(origin, "commit", "-qm", "base")
    subprocess.run(["git", "clone", "-q", str(origin), str(work)], check=True)
    _git(work, "config", "user.email", "t@t.t")
    _git(work, "config", "user.name", "t")
    if branch:
        _git(work, "checkout", "-q", "-b", branch)
        if set_upstream:            # a PR branch tracking ITSELF
            _git(work, "push", "-q", "-u", "origin", branch)
        else:                       # no upstream at all
            pass
    for i in range(commits_ahead_on_trunk):
        (origin / "f.txt").write_text(f"{i+1}\n")
        _git(origin, "add", "-A"); _git(origin, "commit", "-qm", f"c{i}")
    _git(work, "fetch", "-q", "origin")
    return work


class TrunkIsTheReference(unittest.TestCase):
    def test_no_upstream_and_far_behind_trunk_warns(self):
        w = _make_repo(commits_ahead_on_trunk=40, branch="rescue/local")
        self.assertNotEqual(_git(w, "rev-parse", "--abbrev-ref", "@{upstream}").returncode, 0,
                            "precondition: this branch has no upstream")
        r = hc.check_live_tree_drift(repo_root=w, behind_max=30)
        self.assertEqual(r["status"], "warn", r)
        self.assertIn("40 commits behind", r["detail"])

    def test_branch_tracking_itself_and_far_behind_trunk_warns(self):
        w = _make_repo(commits_ahead_on_trunk=40, branch="feat/x", set_upstream=True)
        self.assertEqual(
            _git(w, "rev-list", "--count", "HEAD..@{upstream}").stdout.strip(), "0",
            "precondition: a self-tracking branch is 0 behind its own upstream")
        r = hc.check_live_tree_drift(repo_root=w, behind_max=30)
        self.assertEqual(r["status"], "warn", r)
        self.assertIn("40 commits behind", r["detail"])

    def test_current_tree_is_ok(self):
        # Control: the probe must not warn when it can measure and is current.
        w = _make_repo(commits_ahead_on_trunk=0)
        r = hc.check_live_tree_drift(repo_root=w, behind_max=30)
        self.assertEqual(r["status"], "ok", r)
        self.assertIn("0 behind", r["detail"])

    def test_just_under_threshold_is_ok(self):
        w = _make_repo(commits_ahead_on_trunk=29, branch="rescue/local")
        r = hc.check_live_tree_drift(repo_root=w, behind_max=30)
        self.assertEqual(r["status"], "ok", r)

    def test_unmeasurable_distance_is_not_zero(self):
        # No upstream and no trunk ref: the honest answer is "unmeasured".
        w = _make_repo(commits_ahead_on_trunk=40, branch="rescue/local")
        _git(w, "remote", "remove", "origin")
        r = hc.check_live_tree_drift(repo_root=w, behind_max=30)
        self.assertEqual(r["status"], "warn", r)
        self.assertIn("UNMEASURED", r["detail"])

    def test_not_a_git_checkout_stays_ok(self):
        r = hc.check_live_tree_drift(repo_root=Path(tempfile.mkdtemp()), behind_max=30)
        self.assertEqual(r["status"], "ok", r)


if __name__ == "__main__":
    unittest.main(verbosity=2)
