#!/usr/bin/env python3
"""The Codex launcher records its own checkout as trusted, and nothing else."""
import importlib
import io
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import codex_project_trust as cpt  # noqa: E402

import toml_fallback  # noqa: E402
from _helpers.old_python import pre_311_python  # noqa: E402

SCRIPT = REPO / "src/codex_project_trust.py"
# Runs the helper as the launcher does, on an interpreter whose stdlib has no tomllib.
WITHOUT_TOMLLIB = ("import runpy, sys; sys.modules['tomllib'] = sys.modules['tomli'] = None; "
                   "sys.path.insert(0, sys.argv[1]); sys.argv = sys.argv[2:]; "
                   "runpy.run_path(sys.argv[0], run_name='__main__')")


def load_without_tomllib():
    """A fresh codex_project_trust imported as if neither tomllib nor tomli existed."""
    saved = sys.modules.pop("codex_project_trust")
    try:
        with patch.dict(sys.modules, {"tomllib": None, "tomli": None}):
            return importlib.import_module("codex_project_trust")
    finally:
        sys.modules["codex_project_trust"] = saved


class EnsureTrusted(unittest.TestCase):
    cpt = cpt

    def cli(self, *args):
        return subprocess.run([sys.executable, str(SCRIPT), *map(str, args)],
                              capture_output=True, text=True)

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
        return self.cpt.tomllib.loads(self.config.read_text()).get("projects", {})

    def test_records_the_checkout_and_keeps_existing_settings(self):
        self.home.mkdir()
        self.config.write_text('model = "gpt-x"\n[features]\nfoo = true')
        os.chmod(self.config, 0o600)
        self.assertEqual(self.cpt.ensure_trusted(self.home, self.repo / ".", self.repo), "trusted")
        body = self.cpt.tomllib.loads(self.config.read_text())
        self.assertEqual(body["model"], "gpt-x")
        self.assertEqual(body["features"], {"foo": True})
        self.assertEqual(self.projects(), {str(self.repo): {"trust_level": "trusted"}})
        self.assertEqual(self.config.stat().st_mode & 0o777, 0o600)
        self.assertEqual(sorted(p.name for p in self.home.iterdir()), ["config.toml"])
        self.assertEqual(self.cpt.ensure_trusted(self.home, self.repo, self.repo), "already-set")

    def test_subdirectory_trusts_the_repository_root(self):
        sub = self.repo / "src"
        sub.mkdir()
        self.assertEqual(self.cpt.ensure_trusted(self.home, sub, self.repo), "trusted")
        self.assertIn(str(self.repo), self.projects())
        self.assertEqual(self.config.stat().st_mode & 0o777, 0o600)

    def test_an_explicit_untrusted_choice_is_left_alone(self):
        self.home.mkdir()
        original = f'[projects."{self.repo}"]\ntrust_level = "untrusted"\n'
        self.config.write_text(original)
        self.assertEqual(self.cpt.ensure_trusted(self.home, self.repo, self.repo), "already-set")
        self.assertEqual(self.config.read_text(), original)

    def test_never_trusts_a_directory_outside_the_launchers_checkout(self):
        other = Path(self.tmp.name).resolve() / "other"
        other.mkdir()
        subprocess.run(["git", "init", "-q", str(other)], check=True)
        self.assertEqual(self.cpt.ensure_trusted(self.home, other, self.repo), "foreign")
        plain = Path(self.tmp.name).resolve() / "plain"
        plain.mkdir()
        self.assertEqual(self.cpt.ensure_trusted(self.home, plain, self.repo), "not-git")
        self.assertFalse(self.config.exists())

    def test_an_unparseable_config_is_refused_unchanged(self):
        self.home.mkdir()
        self.config.write_text("model = \n")
        self.assertEqual(self.cpt.main([str(self.home), str(self.repo), str(self.repo)]), 1)
        self.assertEqual(self.config.read_text(), "model = \n")

    def test_failed_replace_keeps_the_old_config(self):
        self.home.mkdir()
        self.config.write_text('model = "gpt-x"\n')
        with patch.object(self.cpt.os, "replace", side_effect=OSError("disk")):
            self.assertEqual(self.cpt.main([str(self.home), str(self.repo), str(self.repo)]), 1)
        self.assertEqual(self.config.read_text(), 'model = "gpt-x"\n')
        self.assertEqual(sorted(p.name for p in self.home.iterdir()), ["config.toml"])

    def test_cli_prints_the_outcome(self):
        out = self.cli(self.home, self.repo, self.repo)
        self.assertEqual((out.returncode, out.stdout.strip()), (0, "trusted"))
        out = self.cli(self.home, self.repo, self.repo)
        self.assertEqual((out.returncode, out.stdout.strip()), (0, "already-set"))

    def test_main_reports_success(self):
        with patch("sys.stdout", new_callable=io.StringIO) as out:
            self.assertEqual(self.cpt.main([str(self.home), str(self.repo), str(self.repo)]), 0)
        self.assertEqual(out.getvalue().strip(), "trusted")

    def test_an_inline_projects_table_is_refused_unchanged(self):
        """An inline table cannot gain a [projects."x"] section, so appending would break the file."""
        self.home.mkdir()
        self.config.write_text('projects = { "/elsewhere" = { trust_level = "trusted" } }\n')
        self.assertEqual(self.cpt.main([str(self.home), str(self.repo), str(self.repo)]), 1)
        self.assertEqual(self.config.read_text(),
                         'projects = { "/elsewhere" = { trust_level = "trusted" } }\n')

    def test_a_typical_codex_config_is_extended(self):
        self.home.mkdir()
        self.config.write_text(
            'model = "gpt-5"  # default\n'
            '[mcp_servers.docs]\ncommand = "npx"\nargs = [\n  "-y",\n  "pkg@1.2",\n]\n'
            'env = { TOKEN_FILE = \'C:\\tmp\\t\' }\nstartup_timeout_sec = 20.5\n'
            '[projects."/other path"]\ntrust_level = "untrusted"\n')
        self.assertEqual(self.cpt.ensure_trusted(self.home, self.repo, self.repo), "trusted")
        self.assertEqual(self.projects(), {"/other path": {"trust_level": "untrusted"},
                                           str(self.repo): {"trust_level": "trusted"}})
        self.assertEqual(self.cpt.tomllib.loads(self.config.read_text())["mcp_servers"]["docs"]["args"],
                         ["-y", "pkg@1.2"])


class EnsureTrustedWithoutTomllib(EnsureTrusted):
    """Python 3.9/3.10: the same contract through the bundled reader."""
    cpt = load_without_tomllib()

    def cli(self, *args):
        return subprocess.run([sys.executable, "-c", WITHOUT_TOMLLIB, str(REPO / "src"),
                               str(SCRIPT), *map(str, args)], capture_output=True, text=True)

    def test_the_bundled_reader_is_the_one_in_use(self):
        self.assertIs(self.cpt.tomllib, toml_fallback)

    def test_toml_it_cannot_read_is_refused_unchanged(self):
        self.home.mkdir()
        original = 'model = "x"\nwhen = 2024-13-45\n'
        self.config.write_text(original)
        out = self.cli(self.home, self.repo, self.repo)
        self.assertEqual(out.returncode, 1)
        self.assertIn("codex project trust:", out.stderr)
        self.assertEqual(self.config.read_text(), original)


@unittest.skipUnless(pre_311_python(), "no Python < 3.11 found (set SUTANDO_TEST_OLD_PYTHON)")
class EnsureTrustedOnARealOldInterpreter(EnsureTrusted):
    """The helper on an actual 3.9/3.10, not a simulated missing import."""

    def cli(self, *args):
        return subprocess.run([pre_311_python(), str(SCRIPT), *map(str, args)],
                              capture_output=True, text=True)


if __name__ == "__main__":
    unittest.main()
