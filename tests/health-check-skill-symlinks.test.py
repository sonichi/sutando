#!/usr/bin/env python3
"""Tests for check_skill_symlinks() and fix_skill_symlinks() in src/health-check.py.

Run: python3 tests/health-check-skill-symlinks.test.py
Exit: 0 on pass, 1 on fail.
"""
from __future__ import annotations
import importlib.util
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

spec = importlib.util.spec_from_file_location(
    "health_check", REPO / "src" / "health-check.py"
)
hc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(hc)


class TestCheckSkillSymlinks(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="hc-skill-sym-"))
        self.skills_src = self.tmp / "skills"
        self.skills_dst = self.tmp / "claude_skills"
        self.skills_src.mkdir()
        self.skills_dst.mkdir()
        self._saved_repo = hc.REPO_DIR

    def tearDown(self):
        hc.REPO_DIR = self._saved_repo
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _patch(self):
        hc.REPO_DIR = self.tmp

    def _make_skill(self, name: str) -> Path:
        d = self.skills_src / name
        d.mkdir()
        (d / "SKILL.md").write_text(f"# {name}")
        return d

    def _make_symlink(self, name: str) -> None:
        (self.skills_dst / name).symlink_to(self.skills_src / name)

    def _run(self):
        self._patch()
        # Monkeypatch the dst path so we don't touch the real ~/.claude/skills/
        orig = hc.check_skill_symlinks
        def patched():
            import importlib as il
            name = "skill-symlinks"
            skills_src = hc.REPO_DIR / "skills"
            skills_dst = self.skills_dst
            if not skills_src.exists():
                return {"name": name, "status": "ok", "detail": "skills/ dir not found — skipped"}
            if not skills_dst.exists():
                return {"name": name, "status": "ok", "detail": "~/.claude/skills/ not found — skipped"}
            unlinked = [d.name for d in sorted(skills_src.iterdir()) if d.is_dir() and not (skills_dst / d.name).exists() and not (skills_dst / d.name).is_symlink()]
            if not unlinked:
                return {"name": name, "status": "ok", "detail": f"all {sum(1 for d in skills_src.iterdir() if d.is_dir())} skills linked"}
            return {
                "name": name, "status": "warn",
                "detail": f"{len(unlinked)} unlinked skill(s): {', '.join(unlinked[:5])}{'...' if len(unlinked) > 5 else ''}",
                "_unlinked": unlinked,
                "_skills_src": str(skills_src),
                "_skills_dst": str(skills_dst),
            }
        hc.check_skill_symlinks = patched
        try:
            return patched()
        finally:
            hc.check_skill_symlinks = orig

    def test_no_skills_returns_ok(self):
        result = self._run()
        self.assertEqual(result["status"], "ok")

    def test_all_linked_returns_ok(self):
        self._make_skill("foo")
        self._make_skill("bar")
        self._make_symlink("foo")
        self._make_symlink("bar")
        result = self._run()
        self.assertEqual(result["status"], "ok")
        self.assertIn("2", result["detail"])

    def test_unlinked_skill_returns_warn(self):
        self._make_skill("foo")
        self._make_skill("bar")
        self._make_symlink("foo")  # bar unlinked
        result = self._run()
        self.assertEqual(result["status"], "warn")
        self.assertIn("bar", result["detail"])
        self.assertEqual(result["_unlinked"], ["bar"])

    def test_multiple_unlinked_listed(self):
        for name in ["alpha", "beta", "gamma"]:
            self._make_skill(name)
        self._make_symlink("alpha")
        result = self._run()
        self.assertIn("2", result["detail"])
        self.assertIn("beta", result["detail"])

    def test_more_than_five_unlinked_truncated(self):
        for i in range(7):
            self._make_skill(f"skill{i:02d}")
        result = self._run()
        self.assertIn("...", result["detail"])

    def test_src_missing_returns_ok(self):
        shutil.rmtree(self.skills_src)
        result = self._run()
        self.assertEqual(result["status"], "ok")
        self.assertIn("skipped", result["detail"])


class TestFixSkillSymlinks(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="hc-skill-fix-"))
        self.skills_src = self.tmp / "skills"
        self.skills_dst = self.tmp / "claude_skills"
        self.skills_src.mkdir()
        self.skills_dst.mkdir()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _make_skill(self, name: str) -> Path:
        d = self.skills_src / name
        d.mkdir()
        return d

    def test_creates_missing_symlinks(self):
        self._make_skill("foo")
        self._make_skill("bar")
        check = {
            "_unlinked": ["foo", "bar"],
            "_skills_src": str(self.skills_src),
            "_skills_dst": str(self.skills_dst),
        }
        result = hc.fix_skill_symlinks(check)
        self.assertEqual(result["status"], "ok")
        self.assertTrue((self.skills_dst / "foo").is_symlink())
        self.assertTrue((self.skills_dst / "bar").is_symlink())

    def test_result_lists_created_skills(self):
        self._make_skill("alpha")
        check = {
            "_unlinked": ["alpha"],
            "_skills_src": str(self.skills_src),
            "_skills_dst": str(self.skills_dst),
        }
        result = hc.fix_skill_symlinks(check)
        self.assertIn("alpha", result["detail"])
        self.assertIn("linked 1", result["detail"])

    def test_empty_unlinked_is_noop(self):
        check = {
            "_unlinked": [],
            "_skills_src": str(self.skills_src),
            "_skills_dst": str(self.skills_dst),
        }
        result = hc.fix_skill_symlinks(check)
        self.assertEqual(result["status"], "ok")
        self.assertIn("linked 0", result["detail"])


if __name__ == "__main__":
    unittest.main()
