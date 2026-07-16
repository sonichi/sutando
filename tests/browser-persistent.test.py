#!/usr/bin/env python3
"""Smoke tests for Sutando's persistent Playwright browser profile."""
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "src" / "browser.mjs"


class PersistentBrowserTests(unittest.TestCase):
    def test_profile_override_is_reported_without_launching(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = dict(os.environ, SUTANDO_BROWSER_PROFILE=tmp)
            result = subprocess.run(
                ["node", str(SCRIPT), "profile"], cwd=REPO, env=env,
                capture_output=True, text=True, check=True,
            )
            self.assertEqual(result.stdout.strip(), tmp)

    def test_headless_action_uses_persistent_profile(self):
        with tempfile.TemporaryDirectory() as tmp:
            profile = Path(tmp) / "profile"
            env = dict(os.environ, SUTANDO_BROWSER_PROFILE=str(profile))
            result = subprocess.run(
                ["node", str(SCRIPT), "data:text/html,<body>persistent-ok</body>", "text"],
                cwd=REPO, env=env, capture_output=True, text=True, check=True,
            )
            self.assertIn("persistent-ok", result.stdout)
            self.assertTrue(profile.is_dir())
            self.assertTrue(any(profile.iterdir()))


if __name__ == "__main__":
    unittest.main()
