#!/usr/bin/env python3
"""src/installed_skill_roots.py: the ordered skill-root set, and its shell entry
`scripts/sutando-config.sh skill-roots`.

Run: python3 tests/installed-skill-roots.test.py
"""
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))
from installed_skill_roots import installed_skill_roots as skill_roots  # noqa: E402


class SkillRoots(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.engine = self.root / "engine" / "sutando"
        self.ws = self.root / "workspace"
        for d in (self.engine / "skills", self.ws / "skills", self.root / "mem" / "skills",
                  self.root / "plugin" / "skills", self.root / "engine" / "b-sib" / "skills",
                  self.root / "engine" / "a-sib" / "skills", self.root / "engine" / "no-skills"):
            d.mkdir(parents=True)

    def tearDown(self):
        self.tmp.cleanup()

    def test_an_unreadable_sibling_is_skipped_and_the_rest_still_listed(self):
        locked = self.root / "engine" / "locked"
        (locked / "skills").mkdir(parents=True)
        locked.chmod(0)
        try:
            roots = skill_roots(self.engine, self.ws, {})
        finally:
            locked.chmod(0o755)
        self.assertIn(self.ws / "skills", roots)
        self.assertNotIn(locked / "skills", roots)

    def test_a_plain_clone_scans_no_siblings(self):
        clone = self.root / "code" / "sutando"
        (clone / "skills").mkdir(parents=True)
        (self.root / "code" / "unrelated" / "skills").mkdir(parents=True)
        self.assertEqual(skill_roots(clone, self.ws, {}), [clone / "skills", self.ws / "skills"])
        opted = {"SUTANDO_EXTERNAL_PLUGIN_DIRS": str(self.root / "code" / "unrelated")}
        self.assertEqual(skill_roots(clone, self.ws, opted),
                         [clone / "skills", self.ws / "skills", self.root / "code" / "unrelated" / "skills"])

    def test_order_is_shipped_workspace_memory_external_then_sorted_siblings(self):
        env = {"SUTANDO_MEMORY_DIR": str(self.root / "mem"),
               "SUTANDO_EXTERNAL_PLUGIN_DIRS": os.pathsep.join(["", str(self.root / "plugin"),
                                                                 str(self.root / "absent")])}
        self.assertEqual(skill_roots(self.engine, self.ws, env), [
            self.engine / "skills", self.ws / "skills", self.root / "mem" / "skills",
            self.root / "plugin" / "skills", self.root / "engine" / "a-sib" / "skills",
            self.root / "engine" / "b-sib" / "skills"])

    def test_legacy_memory_alias_and_each_dir_once(self):
        # The external dir names the memory dir again: it is listed once, at its first position.
        env = {"SUTANDO_PRIVATE_DIR": str(self.root / "mem"),
               "SUTANDO_EXTERNAL_PLUGIN_DIRS": str(self.root / "mem")}
        roots = skill_roots(self.engine, self.ws, env)
        self.assertEqual(roots[2], self.root / "mem" / "skills")
        self.assertEqual(roots.count(self.root / "mem" / "skills"), 1)

    def test_an_unreadable_siblings_dir_contributes_nothing(self):
        gone = self.root / "missing-parent" / "sutando"
        self.assertEqual(skill_roots(gone, self.ws, {}), [self.ws / "skills"])

    def test_shell_entry_prints_the_same_roots(self):
        env = {**os.environ, "SUTANDO_TEST_MODE": "1", "SUTANDO_WORKSPACE": str(self.ws),
               "SUTANDO_EXTERNAL_PLUGIN_DIRS": str(self.root / "plugin")}
        for k in ("SUTANDO_MEMORY_DIR", "SUTANDO_PRIVATE_DIR"):
            env.pop(k, None)
        out = subprocess.run(["bash", str(REPO / "scripts" / "sutando-config.sh"), "skill-roots", str(self.ws)],
                             env=env, capture_output=True, text=True, timeout=60)
        self.assertEqual(out.returncode, 0, out.stderr)
        lines = out.stdout.splitlines()
        self.assertEqual(lines[:3], [str(REPO / "skills"), str(self.ws / "skills"),
                                     str(self.root / "plugin" / "skills")])


if __name__ == "__main__":
    unittest.main(verbosity=1)
