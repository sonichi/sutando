#!/usr/bin/env python3
"""skills/install.sh links the owner's own skills from <workspace>/skills/ — the folder
that survives an engine update — and a shipped skill wins a name collision. The workspace
is the one `scripts/sutando-config.sh workspace` resolves (redirected here through the
loader's test-only SUTANDO_TEST_MODE hatch), never a private env var of the script's own.

Run: python3 tests/skills-install-workspace-skills.test.py
"""
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def run_install(config_dir: Path, ws: Path, extra_env: dict | None = None) -> str:
    env = {**os.environ, "CLAUDE_CONFIG_DIR": str(config_dir),
           "SUTANDO_TEST_MODE": "1", "SUTANDO_WORKSPACE": str(ws), **(extra_env or {})}
    env.pop("SUTANDO_WORKSPACE_DIR", None)
    out = subprocess.run(["bash", str(REPO / "skills" / "install.sh")], env=env,
                         capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stderr
    return out.stdout


class WorkspaceSkillsAreLinked(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.ccd = root / "ccd"
        self.ws = root / "workspace"
        (self.ws / "skills" / "apartment-finder").mkdir(parents=True)
        (self.ws / "skills" / "apartment-finder" / "SKILL.md").write_text("# mine\n")
        (self.ws / "skills" / "no-skill-md").mkdir()
        # The same name as a shipped skill: the shipped one wins.
        (self.ws / "skills" / "task-progress").mkdir()
        (self.ws / "skills" / "task-progress" / "SKILL.md").write_text("# shadow\n")

    def tearDown(self):
        self.tmp.cleanup()

    def test_a_workspace_skill_is_linked_and_a_shadow_is_refused(self):
        out = run_install(self.ccd, self.ws)
        skills = self.ccd / "skills"
        link = skills / "apartment-finder"
        self.assertTrue(link.is_symlink(), out)
        # The resolver hands back a real path (/private/var on macOS), so compare resolved forms.
        self.assertEqual(Path(os.readlink(link)).resolve(), (self.ws / "skills" / "apartment-finder").resolve())
        self.assertIn("apartment-finder (workspace skill)", out)
        self.assertFalse((skills / "no-skill-md").exists(), "a folder without SKILL.md is not a skill")
        self.assertEqual(Path(os.readlink(skills / "task-progress")), REPO / "skills" / "task-progress")
        self.assertIn("task-progress (workspace copy shadowed", out)
        # Idempotent: a second run reports the existing link and changes nothing.
        out2 = run_install(self.ccd, self.ws)
        self.assertIn("apartment-finder (workspace skill, symlink exists)", out2)

    def test_the_workspace_is_the_canonical_one_not_a_private_env_var(self):
        # A worker-pool seat carries SUTANDO_WORKSPACE_DIR; install.sh must still link from the
        # workspace every other reader resolves, or skills would come from a different tree.
        other = Path(self.tmp.name) / "other-workspace"
        (other / "skills" / "elsewhere-only").mkdir(parents=True)
        (other / "skills" / "elsewhere-only" / "SKILL.md").write_text("# elsewhere\n")
        out = run_install(self.ccd, self.ws, {"SUTANDO_WORKSPACE_DIR": str(other)})
        self.assertTrue((self.ccd / "skills" / "apartment-finder").is_symlink(), out)
        self.assertFalse((self.ccd / "skills" / "elsewhere-only").exists(), out)

    def test_no_workspace_skills_folder_is_fine(self):
        import shutil
        shutil.rmtree(self.ws / "skills")
        out = run_install(self.ccd, self.ws)
        self.assertIn("Installed.", out)


if __name__ == "__main__":
    unittest.main(verbosity=1)
