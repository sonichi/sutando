#!/usr/bin/env python3
"""session-launch.sh trusts the dir claude starts in, so a detached core never waits on
Claude Code's folder-trust dialog: the explicit SUTANDO_CLAUDE_WORKING_DIR when set,
else the launcher's own checkout by its physical path (getcwd() resolves symlinks).

2026-10-22: a PR checkout's core restarted into that dialog because only an explicit
working dir was ever seeded. Run: python3 tests/start-cli-claude-trust-seed.test.py
"""
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "src" / "agent" / "claude" / "cli" / "session-launch.sh"


class ClaudeTrustSeed(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name).resolve()
        self.real = root / "real" / "checkout"
        (self.real / "src").mkdir(parents=True)
        (root / "link").symlink_to(root / "real")
        self.logical = root / "link" / "checkout"
        self.ccd = root / "ccd"
        # Stub resolver: the seed itself is what is under test.
        (self.real / "src" / "claude_config_dir.sh").write_text(
            f'resolve_claude_config_dir() {{ echo "{self.ccd}"; }}\n')
        self.home = root / "home"
        self.home.mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def seed(self, working_dir=None):
        env = {"PATH": os.environ["PATH"], "HOME": str(self.home)}
        if working_dir is not None:
            env["SUTANDO_CLAUDE_WORKING_DIR"] = str(working_dir)
        script = (f'REPO="{self.logical}"; PY=python3; . "{SCRIPT}"; '
                  'resolve_claude_cwd_args; resolve_claude_config_dir_and_seed')
        r = subprocess.run(["bash", "-c", script], env=env, capture_output=True, text=True,
                           timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        projects = json.loads((self.ccd / ".claude.json").read_text()).get("projects", {})
        return {k for k, v in projects.items() if v.get("hasTrustDialogAccepted") is True}

    def test_no_override_trusts_the_checkout_by_its_physical_path(self):
        self.assertEqual(self.seed(), {str(self.real)})

    def test_an_explicit_working_dir_is_the_only_dir_trusted(self):
        chosen = Path(self.tmp.name).resolve() / "chosen"
        self.assertEqual(self.seed(chosen), {str(chosen)})


if __name__ == "__main__":
    unittest.main()
