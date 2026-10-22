#!/usr/bin/env python3
"""The Codex launcher records its own checkout as trusted, and nothing else."""
import os
import subprocess
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))
import codex_project_trust as cpt  # noqa: E402


class EnsureTrusted(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name).resolve()
        self.home = root / "codex-home"
        self.repo = root / "check out"  # a space, as in "Application Support"
        self.repo.mkdir()
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True)
        self.config = self.home / "config.toml"

    def tearDown(self):
        self.tmp.cleanup()

    def projects(self):
        return tomllib.loads(self.config.read_text()).get("projects", {})

    def test_records_the_checkout_and_keeps_existing_settings(self):
        self.home.mkdir()
        self.config.write_text('model = "gpt-x"\n[features]\nfoo = true')
        os.chmod(self.config, 0o600)
        self.assertEqual(cpt.ensure_trusted(self.home, self.repo / ".", self.repo), "trusted")
        body = tomllib.loads(self.config.read_text())
        self.assertEqual(body["model"], "gpt-x")
        self.assertEqual(body["features"], {"foo": True})
        self.assertEqual(self.projects(), {str(self.repo): {"trust_level": "trusted"}})
        self.assertEqual(self.config.stat().st_mode & 0o777, 0o600)
        self.assertEqual(sorted(p.name for p in self.home.iterdir()), ["config.toml"])
        self.assertEqual(cpt.ensure_trusted(self.home, self.repo, self.repo), "already-set")

    def test_subdirectory_trusts_the_repository_root(self):
        sub = self.repo / "src"
        sub.mkdir()
        self.assertEqual(cpt.ensure_trusted(self.home, sub, self.repo), "trusted")
        self.assertIn(str(self.repo), self.projects())
        self.assertEqual(self.config.stat().st_mode & 0o777, 0o600)

    def test_an_explicit_untrusted_choice_is_left_alone(self):
        self.home.mkdir()
        original = f'[projects."{self.repo}"]\ntrust_level = "untrusted"\n'
        self.config.write_text(original)
        self.assertEqual(cpt.ensure_trusted(self.home, self.repo, self.repo), "already-set")
        self.assertEqual(self.config.read_text(), original)

    def test_never_trusts_a_directory_outside_the_launchers_checkout(self):
        other = Path(self.tmp.name).resolve() / "other"
        other.mkdir()
        subprocess.run(["git", "init", "-q", str(other)], check=True)
        self.assertEqual(cpt.ensure_trusted(self.home, other, self.repo), "foreign")
        plain = Path(self.tmp.name).resolve() / "plain"
        plain.mkdir()
        self.assertEqual(cpt.ensure_trusted(self.home, plain, self.repo), "not-git")
        self.assertFalse(self.config.exists())

    def test_an_unparseable_config_is_refused_unchanged(self):
        self.home.mkdir()
        self.config.write_text("model = \n")
        self.assertEqual(cpt.main([str(self.home), str(self.repo), str(self.repo)]), 1)
        self.assertEqual(self.config.read_text(), "model = \n")

    def test_failed_replace_keeps_the_old_config(self):
        self.home.mkdir()
        self.config.write_text('model = "gpt-x"\n')
        with patch.object(cpt.os, "replace", side_effect=OSError("disk")):
            self.assertEqual(cpt.main([str(self.home), str(self.repo), str(self.repo)]), 1)
        self.assertEqual(self.config.read_text(), 'model = "gpt-x"\n')
        self.assertEqual(sorted(p.name for p in self.home.iterdir()), ["config.toml"])

    def test_cli_prints_the_outcome(self):
        out = subprocess.run([sys.executable, str(REPO / "src/codex_project_trust.py"),
                              str(self.home), str(self.repo), str(self.repo)],
                             capture_output=True, text=True)
        self.assertEqual((out.returncode, out.stdout.strip()), (0, "trusted"))


if __name__ == "__main__":
    unittest.main()
