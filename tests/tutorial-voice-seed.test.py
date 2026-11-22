#!/usr/bin/env python3
"""skills/tutorial/scripts/seed_voice_tutorial.py — the launch hook that gives the voice mode its file.

The voice walkthrough reads <workspace>/notes/first-time-tutorial.md, which nothing else writes. The
hook copies the shipped copy there once and never replaces a file the owner has.

Run: python3 tests/tutorial-voice-seed.test.py
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from _helpers.hermetic_env import isolate_os_environ  # noqa: E402

isolate_os_environ()

REPO = Path(__file__).resolve().parent.parent
SKILL = REPO / "skills" / "tutorial"
sys.path.insert(0, str(SKILL / "scripts"))
sys.path.insert(0, str(REPO / "src"))

import seed_voice_tutorial as svt  # noqa: E402
import skill_roots  # noqa: E402


class Seed(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.ws = Path(tmp.name)

    def test_a_missing_file_is_seeded_from_the_shipped_copy(self):
        self.assertEqual(svt.seed(self.ws), "seeded")
        self.assertEqual(svt.target(self.ws).read_bytes(), svt.SOURCE.read_bytes())
        self.assertEqual([p.name for p in svt.target(self.ws).parent.iterdir()], ["first-time-tutorial.md"])

    def test_an_existing_file_is_never_replaced(self):
        dest = svt.target(self.ws)
        dest.parent.mkdir(parents=True)
        dest.write_text("my own walkthrough\n")
        self.assertEqual(svt.seed(self.ws), "present")
        self.assertEqual(dest.read_text(), "my own walkthrough\n")
        dest.unlink()
        os.symlink(self.ws / "elsewhere.md", dest)  # a dangling link is the owner's too
        self.assertEqual(svt.seed(self.ws), "present")
        self.assertTrue(dest.is_symlink())

    def test_a_file_that_appears_while_seeding_wins(self):
        dest = svt.target(self.ws)
        real_link = os.link

        def racing(src, dst):
            Path(dst).write_text("written meanwhile\n")
            return real_link(src, dst)
        with mock.patch.object(svt.os, "link", racing):
            self.assertEqual(svt.seed(self.ws), "present")
        self.assertEqual(dest.read_text(), "written meanwhile\n")
        self.assertEqual(sorted(p.name for p in dest.parent.iterdir()), ["first-time-tutorial.md"])

    def test_an_unreadable_or_oversized_source_seeds_nothing(self):
        self.assertTrue(svt.seed(self.ws, self.ws / "missing.md").startswith("skipped"))
        big = self.ws / "big.md"
        big.write_bytes(b"x" * (svt.SOURCE_MAX + 1))
        self.assertTrue(svt.seed(self.ws, big).startswith("skipped"))
        self.assertFalse(os.path.lexists(svt.target(self.ws)))

    def test_the_cli_always_exits_zero(self):
        with mock.patch.object(svt, "_workspace", side_effect=RuntimeError("no workspace")):
            self.assertEqual(svt.main(), 0)
        with mock.patch.object(svt, "_workspace", return_value=self.ws):
            self.assertEqual(svt.main(), 0)
        self.assertTrue(svt.target(self.ws).is_file())


class Wiring(unittest.TestCase):
    def test_the_manifest_declares_the_hook_and_the_launcher_finds_it(self):
        manifest = json.loads((SKILL / "manifest.json").read_text())
        self.assertEqual(manifest["on_launch"], "scripts/seed_voice_tutorial.py")
        found = dict(skill_roots.declared_scripts("on_launch", [REPO / "skills"]))
        self.assertEqual(found.get("tutorial"), (SKILL / "scripts" / "seed_voice_tutorial.py").resolve())

    def test_the_voice_mode_reads_the_seeded_file_and_names_the_shipped_copy(self):
        text = (SKILL / "SKILL.md").read_text()
        self.assertIn("<workspace>/notes/first-time-tutorial.md", text)
        self.assertIn("skills/tutorial/voice/first-time-tutorial.md", text)

    def test_the_shipped_walkthrough_has_sections_and_is_small(self):
        text = svt.SOURCE.read_text()
        self.assertGreaterEqual(text.count("\n## "), 10)
        self.assertLess(len(text.encode()), svt.SOURCE_MAX)

    def test_the_script_as_the_launcher_runs_it_seeds_the_resolved_workspace(self):
        with tempfile.TemporaryDirectory() as ws:
            env = {**os.environ, "SUTANDO_WORKSPACE": ws, "SUTANDO_TEST_MODE": "1"}
            done = subprocess.run([sys.executable, str(SKILL / "scripts" / "seed_voice_tutorial.py")],
                                  capture_output=True, text=True, env=env, timeout=60)
            self.assertEqual(done.returncode, 0, done.stderr)
            self.assertIn("seed_voice_tutorial: seeded", done.stdout)
            self.assertTrue(svt.target(Path(ws)).is_file())


if __name__ == "__main__":
    unittest.main(verbosity=1)
