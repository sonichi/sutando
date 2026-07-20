#!/usr/bin/env python3
"""Regression test: a DANGLING skill symlink must not count as linked (#2213).

The original condition was:

    if not dst.exists() and not dst.is_symlink():

`exists()` follows the link, so it is False for a dangling one — but
`is_symlink()` is True, making the whole condition False. A dangling entry was
therefore reported as linked. Claude Code cannot load such a skill, so it was
simultaneously invisible and green: the exact "reports healthy while doing
nothing" shape.

Measured on a live host before the fix: 14 dangling links out of 67 entries,
health-check reporting "all 51 skills linked".

Second layer: even once detected, `--fix` could not repair it. The dangling
link occupies the name, so `symlink_to()` raises FileExistsError. Verified
empirically before writing this.

Run: python3 tests/health-check-dangling-symlinks.test.py
Exit: 0 on pass, 1 on fail.
"""
from __future__ import annotations
import importlib.util
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def _load():
    spec = importlib.util.spec_from_file_location(
        "health_check_symlink_test", REPO / "src" / "health-check.py"
    )
    hc = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(hc)
    return hc


class TestDanglingSkillSymlinks(unittest.TestCase):
    def setUp(self):
        self.hc = _load()
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.repo = root / "repo"
        self.src = self.repo / "skills"
        self.dst = root / "home" / ".claude" / "skills"
        self.src.mkdir(parents=True)
        self.dst.mkdir(parents=True)
        self.hc.REPO_DIR = self.repo
        # check_skill_symlinks hardcodes Path.home(); redirect it.
        self._home = root / "home"
        self.hc.Path.home = staticmethod(lambda: self._home)

    def tearDown(self):
        self._tmp.cleanup()

    def _skill(self, name: str) -> Path:
        d = self.src / name
        d.mkdir()
        (d / "SKILL.md").write_text("# x\n")
        return d

    def test_dangling_link_is_not_reported_as_linked(self):
        """The bug. Before the fix this returned ok/'all 1 skills linked'."""
        self._skill("alpha")
        (self.dst / "alpha").symlink_to(self.src / "gone-elsewhere")
        r = self.hc.check_skill_symlinks()
        self.assertEqual(r["status"], "warn", r["detail"])
        self.assertIn("dangling", r["detail"])
        self.assertEqual(r["_broken"], ["alpha"])

    def test_healthy_link_still_ok(self):
        self._skill("alpha")
        (self.dst / "alpha").symlink_to(self.src / "alpha")
        r = self.hc.check_skill_symlinks()
        self.assertEqual(r["status"], "ok", r["detail"])

    def test_missing_and_dangling_are_reported_separately(self):
        """They need different remediation — one needs unlinking first — so
        collapsing them into one bucket would make --fix wrong for half."""
        self._skill("alpha")
        self._skill("beta")
        (self.dst / "alpha").symlink_to(self.src / "gone")
        r = self.hc.check_skill_symlinks()
        self.assertEqual(r["_broken"], ["alpha"])
        self.assertEqual(r["_unlinked"], ["beta"])

    def test_dangling_link_for_a_skill_not_in_this_repo_is_still_flagged(self):
        """The repo-driven loop only walks repo skills, so a dead entry from
        another checkout would be missed entirely."""
        self._skill("alpha")
        (self.dst / "alpha").symlink_to(self.src / "alpha")
        (self.dst / "from-another-clone").symlink_to(self.src / "nowhere")
        r = self.hc.check_skill_symlinks()
        self.assertEqual(r["status"], "warn")
        self.assertEqual(r["_orphaned"], ["from-another-clone"])

    def test_fix_repairs_a_dangling_link(self):
        """--fix must unlink first; symlink_to() alone raises FileExistsError,
        so before this the fix could not repair the case it existed for."""
        self._skill("alpha")
        (self.dst / "alpha").symlink_to(self.src / "gone")
        check = self.hc.check_skill_symlinks()
        res = self.hc.fix_skill_symlinks(check)
        self.assertEqual(res["status"], "ok", res["detail"])
        self.assertTrue((self.dst / "alpha").exists())
        self.assertEqual((self.dst / "alpha").resolve(), (self.src / "alpha").resolve())

    def test_fix_never_removes_a_real_directory(self):
        """The unlink guard must be is_symlink()-gated — a health check must
        not delete real content under any circumstances."""
        self._skill("alpha")
        real = self.dst / "alpha"
        real.mkdir()
        (real / "keep.txt").write_text("important")
        check = self.hc.check_skill_symlinks()
        self.hc.fix_skill_symlinks(check)
        self.assertTrue((real / "keep.txt").exists(), "real dir must survive --fix")


if __name__ == "__main__":
    unittest.main(verbosity=2)
