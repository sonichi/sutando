#!/usr/bin/env python3
"""`_commits_behind` must refuse to answer when the refs share no history.

Run: python3 tests/health-check-commits-behind-shallow.test.py
"""

from __future__ import annotations

import importlib.util
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

spec = importlib.util.spec_from_file_location(
    "health_check", REPO / "src" / "health-check.py"
)
hc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(hc)


def _git(repo: Path, *args: str) -> str:
    out = subprocess.run(["git", "-C", str(repo), *args],
                         capture_output=True, text=True, check=True)
    return out.stdout.strip()


def _init(repo: Path) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "t")


def _commit(repo: Path, text: str, msg: str, path: str = "a.txt") -> str:
    f = repo / path
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(text)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", msg)
    return _git(repo, "rev-parse", "HEAD")


class CommitsBehindTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.upstream = self.root / "upstream"
        _init(self.upstream)
        _commit(self.upstream, "one\n", "first")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _clone(self, shallow: bool) -> Path:
        clone = self.root / ("shallow" if shallow else "full")
        args = ["clone", "-q", str(self.upstream), str(clone)]
        if shallow:
            args[2:2] = ["--depth", "1"]
        subprocess.run(["git", *args], capture_output=True, text=True, check=True)
        _git(clone, "config", "user.email", "t@example.com")
        _git(clone, "config", "user.name", "t")
        return clone

    # --- shared history: the count is real and must still be produced --------

    def test_counts_normally_when_history_is_shared(self) -> None:
        """Control: without this the None-return proves nothing."""
        clone = self._clone(shallow=False)
        _commit(self.upstream, "two\n", "second")
        _commit(self.upstream, "three\n", "third")
        _git(clone, "fetch", "-q", "origin")
        self.assertEqual(hc._commits_behind(clone, "main"), 2)

    def test_zero_when_current(self) -> None:
        clone = self._clone(shallow=False)
        self.assertEqual(hc._commits_behind(clone, "main"), 0)

    # --- the bug -------------------------------------------------------------

    def test_returns_none_when_no_common_ancestor(self) -> None:
        """No common ancestor: the count would be a number without a meaning."""
        clone = self._clone(shallow=True)
        # Rebuild upstream history so nothing the clone holds is reachable.
        orphan = self.upstream / "orphan.txt"
        _git(self.upstream, "checkout", "-q", "--orphan", "rebuilt")
        orphan.write_text("rebuilt\n")
        _git(self.upstream, "add", "-A")
        _git(self.upstream, "commit", "-qm", "rebuilt root")
        _git(self.upstream, "branch", "-qM", "rebuilt", "main")
        _git(clone, "fetch", "-q", "origin", "main")
        _git(clone, "update-ref", "refs/remotes/origin/main", "FETCH_HEAD")

        # Precondition: git itself says there is no merge-base.
        base = subprocess.run(["git", "-C", str(clone), "merge-base",
                               "HEAD", "origin/main"], capture_output=True, text=True)
        self.assertNotEqual(base.returncode, 0,
                            "fixture failed to create disconnected histories")

        self.assertIsNone(hc._commits_behind(clone, "main"),
                          "a count across a graft boundary is not a distance")

    def test_probe_says_unknown_distance_not_none(self) -> None:
        """The caller must not render 'None commit(s)' nor prescribe ff-only."""
        clone = self._clone(shallow=True)
        _commit(self.upstream, "x\n", "skill change", path="skills/demo/SKILL.md")
        _git(self.upstream, "checkout", "-q", "--orphan", "rebuilt")
        (self.upstream / "skills" / "demo" / "SKILL.md").write_text("different\n")
        _git(self.upstream, "add", "-A")
        _git(self.upstream, "commit", "-qm", "rebuilt with skills")
        _git(self.upstream, "branch", "-qM", "rebuilt", "main")
        _git(clone, "fetch", "-q", "origin", "main")
        _git(clone, "update-ref", "refs/remotes/origin/main", "FETCH_HEAD")

        r = hc.check_live_checkout_branch(repo_dir=clone)
        detail = r["detail"]
        # Asserted unconditionally: a guarded assert can pass without running.
        self.assertEqual(r["status"], "warn", detail)
        self.assertIn("skills/", detail)
        self.assertNotIn("None commit", detail)
        self.assertIn("unknown distance", detail)
        self.assertIn("no common ancestor", detail)
        # The PRESCRIPTION must become unshallow; the text may still name ff-only.
        self.assertIn("fetch --unshallow", detail)
        self.assertIn("cannot apply without a shared history", detail)

    def test_returns_none_when_remote_ref_absent(self) -> None:
        """Pre-existing contract preserved: unanswerable stays None."""
        clone = self._clone(shallow=False)
        _git(clone, "update-ref", "-d", "refs/remotes/origin/main")
        self.assertIsNone(hc._commits_behind(clone, "main"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
