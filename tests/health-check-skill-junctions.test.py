"""Exercise skill-link diagnostics with real Windows directory junctions."""
from __future__ import annotations

import importlib.util
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


REPO = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("health_check_junctions", REPO / "src" / "health-check.py")
hc = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = hc
spec.loader.exec_module(hc)


@unittest.skipUnless(os.name == "nt", "requires Windows directory junctions")
class SkillJunctions(unittest.TestCase):
    def setUp(self):
        fixture = tempfile.TemporaryDirectory(prefix="skill junctions ")
        self.root = Path(fixture.name).resolve()
        self.assertTrue(self.root.is_relative_to(Path(tempfile.gettempdir()).resolve()))
        self.addCleanup(fixture.cleanup)
        self.repo = self.root / "durable repo"
        self.src = self.repo / "skills"
        self.dst = self.root / "claude home" / "skills"
        self.ephemeral = self.root / "Temp"
        self.src.mkdir(parents=True)
        self.dst.mkdir(parents=True)
        self.ephemeral.mkdir()
        for patcher in (
            mock.patch.object(hc, "REPO_DIR", self.repo),
            mock.patch.object(hc, "claude_home_path", lambda *parts: self.dst.parent.joinpath(*parts)),
            mock.patch.object(hc, "_ephemeral_roots", return_value=(str(self.ephemeral),)),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def skill(self, name="alpha"):
        path = self.src / name
        path.mkdir()
        (path / "SKILL.md").write_text("# fixture\n", encoding="utf-8")
        return path

    def junction(self, name, target):
        link = self.dst / name
        self.assertTrue(link.parent.resolve().is_relative_to(self.root))
        self.assertTrue(target.resolve().is_relative_to(self.root))
        env = dict(os.environ, SUTANDO_TEST_JUNCTION_LINK=str(link), SUTANDO_TEST_JUNCTION_TARGET=str(target))
        subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
             "New-Item -ItemType Junction -Path $env:SUTANDO_TEST_JUNCTION_LINK "
             "-Target $env:SUTANDO_TEST_JUNCTION_TARGET -ErrorAction Stop | Out-Null"],
            env=env, check=True, capture_output=True, text=True,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        self.addCleanup(os.rmdir, link)
        self.assertEqual(link.lstat().st_reparse_tag, stat.IO_REPARSE_TAG_MOUNT_POINT)
        self.assertFalse(link.is_symlink())
        return link

    def test_valid_junction_is_healthy(self):
        source = self.skill()
        link = self.junction("alpha", source)
        self.assertEqual((link / "SKILL.md").read_bytes(), (source / "SKILL.md").read_bytes())
        result = hc.check_skill_symlinks()
        self.assertEqual(result["status"], "ok", result)

    def test_dangling_junction_is_broken(self):
        self.skill()
        target = self.root / "gone"
        target.mkdir()
        link = self.junction("alpha", target)
        target.rmdir()
        self.assertFalse(link.exists())
        result = hc.check_skill_symlinks()
        self.assertEqual(result["status"], "warn", result)
        self.assertEqual(result.get("_broken"), ["alpha"], result)
        self.assertEqual(result.get("_unlinked"), [], result)

    def test_dangling_external_junction_is_reported(self):
        self.junction("alpha", self.skill())
        target = self.root / "external gone"
        target.mkdir()
        self.junction("external", target)
        target.rmdir()
        result = hc.check_skill_symlinks()
        self.assertEqual(result.get("_orphaned"), ["external"], result)
        self.assertEqual(result.get("_shadowed"), [], result)

    def test_fix_preserves_broken_junction_and_skill_source(self):
        source = self.skill()
        target = self.root / "gone"
        target.mkdir()
        link = self.junction("alpha", target)
        target.rmdir()
        original = (source / "SKILL.md").read_bytes()
        result = hc.fix_skill_symlinks(hc.check_skill_symlinks())
        self.assertEqual(result["status"], "warn", result)
        self.assertIn("errors", result["detail"])
        self.assertEqual(link.lstat().st_reparse_tag, stat.IO_REPARSE_TAG_MOUNT_POINT)
        self.assertEqual((source / "SKILL.md").read_bytes(), original)

    def test_directory_copy_still_warns(self):
        self.skill()
        (self.dst / "alpha").mkdir()
        result = hc.check_skill_symlinks()
        self.assertEqual(result["status"], "warn", result)
        self.assertEqual(result.get("_shadowed"), ["alpha"], result)

    def test_missing_link_still_warns(self):
        self.skill()
        result = hc.check_skill_symlinks()
        self.assertEqual(result.get("_unlinked"), ["alpha"], result)

    def test_junction_into_temporary_directory_warns(self):
        self.skill()
        target = self.ephemeral / "other skill"
        target.mkdir()
        self.junction("alpha", target)
        result = hc.check_skill_symlinks()
        self.assertEqual(result["status"], "warn", result)
        self.assertIn("loaded from temp", result["detail"])
        self.assertEqual(result.get("_shadowed"), [], result)

    def test_temporary_root_sibling_is_supported_durable_target(self):
        self.skill()
        target = self.root / "TempOther" / "other skill"
        target.mkdir(parents=True)
        self.junction("alpha", target)
        result = hc.check_skill_symlinks()
        self.assertEqual(result["status"], "ok", result)

    def test_ephemeral_containment_handles_windows_paths(self):
        self.assertTrue(hc._is_ephemeral(str(self.ephemeral)))
        self.assertTrue(hc._is_ephemeral(str(self.ephemeral / "other skill")))
        self.assertTrue(hc._is_ephemeral(str(self.ephemeral / "other skill").upper()))
        self.assertFalse(hc._is_ephemeral(str(self.root / "TempOther")))


if __name__ == "__main__":
    unittest.main(verbosity=2)
