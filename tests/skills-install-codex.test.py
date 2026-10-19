#!/usr/bin/env python3
"""skills/install.sh also links every skill into Codex's skills root when the configured
core runtime is Codex. The root comes from `sutando-config.sh codex-home-path skills` (the
core_config_dirs codex value the launcher exports); Codex's bundled `.system/` and any
same-named entry already there are left alone, and a second run changes nothing.

Run: python3 tests/skills-install-codex.test.py
"""
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
COPIED = ("skills/install.sh", "scripts/sutando-config.sh", "scripts/python-binary.sh",
          "src/sutando_config.py")


class CodexSkillsAreLinked(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        t = Path(self.tmp.name)
        self.root, self.ccd, self.codex = t / "repo", t / "ccd", t / "codex-home"
        self.ws = self.root / "workspace"
        for rel in COPIED:
            (self.root / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(REPO / rel, self.root / rel)
        (self.root / "src" / "__init__.py").touch()
        for name in ("room-commons-like", "task-progress"):
            (self.root / "skills" / name).mkdir()
            (self.root / "skills" / name / "SKILL.md").write_text(f"---\nname: {name}\n---\n")
        (self.ws / "skills" / "owner-skill").mkdir(parents=True)
        (self.ws / "skills" / "owner-skill" / "SKILL.md").write_text("---\nname: owner-skill\n---\n")
        (self.codex / "skills" / ".system" / "imagegen").mkdir(parents=True)
        (self.codex / "skills" / "task-progress").mkdir()
        (self.codex / "skills" / "task-progress" / "SKILL.md").write_text("user copy\n")
        self.write_config("codex")

    def tearDown(self):
        self.tmp.cleanup()

    def write_config(self, runtime):
        (self.root / "sutando.config.json").write_text(json.dumps({
            "core": {"runtime": runtime},
            "workspace": {"path": str(self.ws)},
            "core_config_dirs": [
                {"id": "c", "type": "claude", "env_name": "CLAUDE_CONFIG_DIR",
                 "synced": False, "value": str(self.ccd)},
                {"id": "x", "type": "codex", "env_name": "CODEX_HOME",
                 "synced": False, "value": str(self.codex)},
            ],
        }))

    def run_install(self):
        env = {k: v for k, v in os.environ.items()
               if not k.startswith(("SUTANDO_", "CODEX_", "CLAUDE_"))}
        env.update({"CLAUDE_CONFIG_DIR": str(self.ccd), "HOME": self.tmp.name})
        out = subprocess.run(["bash", str(self.root / "skills" / "install.sh")], env=env,
                             capture_output=True, text=True, timeout=120)
        self.assertEqual(out.returncode, 0, out.stderr)
        return out.stdout

    def test_codex_runtime_links_into_the_codex_skills_root(self):
        out = self.run_install()
        skills = self.codex / "skills"
        self.assertEqual(Path(os.readlink(skills / "room-commons-like")),
                         self.root / "skills" / "room-commons-like")
        self.assertTrue((skills / "owner-skill").is_symlink(), out)
        self.assertFalse((skills / "task-progress").is_symlink(), "a same-named entry is kept")
        self.assertEqual((skills / "task-progress" / "SKILL.md").read_text(), "user copy\n")
        self.assertEqual(os.listdir(skills / ".system"), ["imagegen"])
        self.assertTrue((self.ccd / "skills" / "room-commons-like").is_symlink(), "Claude too")
        self.assertIn("Codex core runtime", out)
        out2 = self.run_install()
        codex_part = out2.split("Codex core runtime", 1)[1]
        self.assertNotIn("✓", codex_part, "a second run links nothing new")

    def test_claude_runtime_leaves_codex_alone(self):
        self.write_config("claude")
        out = self.run_install()
        self.assertNotIn("Codex core runtime", out)
        self.assertEqual(sorted(os.listdir(self.codex / "skills")), [".system", "task-progress"])
        self.assertTrue((self.ccd / "skills" / "room-commons-like").is_symlink())


if __name__ == "__main__":
    unittest.main(verbosity=1)
