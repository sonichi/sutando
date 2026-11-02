#!/usr/bin/env python3
"""A desktop-hosted Codex core gets the host's AGENTS.md through its own Codex home.

Run: python3 tests/codex-host-instructions.test.py
"""
import contextlib
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
import codex_host_instructions as chi  # noqa: E402

SCRIPT = REPO / "src/codex_host_instructions.py"


class EnsureLinked(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name).resolve()
        self.home = root / "home"
        self.engine = root / "Application Support" / "engine"
        self.repo = self.engine / "sutando"
        self.repo.mkdir(parents=True)
        (self.repo / "AGENTS.md").write_text("checkout\n")
        self.host = self.engine / "AGENTS.md"
        self.host.write_text("host\n")
        self.codex_home = root / "workspace" / ".codex-sutando"
        self.target = self.codex_home / "AGENTS.md"

    def tearDown(self):
        self.tmp.cleanup()

    def link(self, codex_home=None, repo=None):
        return chi.ensure_linked(codex_home or self.codex_home, repo or self.repo, self.home)

    def test_a_fresh_codex_home_gets_the_host_file_and_a_rerun_changes_nothing(self):
        self.assertEqual(self.link(), "linked")
        self.assertEqual(Path(os.readlink(self.target)), self.host)
        self.assertEqual(self.target.read_text(), "host\n")
        self.assertEqual(self.link(), "already-set")

    def test_a_checkout_outside_a_desktop_engine_dir_is_left_alone(self):
        repo = Path(self.tmp.name).resolve() / "code" / "sutando"
        repo.mkdir(parents=True)
        (repo.parent / "AGENTS.md").write_text("the owner's own\n")
        self.assertEqual(self.link(repo=repo), "no-host")
        self.assertFalse(self.codex_home.exists())

    def test_no_host_file_links_nothing(self):
        self.host.unlink()
        self.assertEqual(self.link(), "no-host")
        self.assertFalse(self.target.exists())

    def test_the_owners_own_codex_home_is_never_written(self):
        owner = self.home / ".codex"
        self.assertEqual(self.link(codex_home=owner), "owner-home")
        self.assertFalse((owner / "AGENTS.md").exists())

    def test_an_existing_file_or_other_link_is_kept(self):
        self.codex_home.mkdir(parents=True)
        self.target.write_text("mine\n")
        self.assertEqual(self.link(), "foreign")
        self.assertEqual(self.target.read_text(), "mine\n")
        self.target.unlink()
        other = self.codex_home / "other.md"
        other.write_text("other\n")
        self.target.symlink_to(other)
        self.assertEqual(self.link(), "foreign")
        self.assertEqual(Path(os.readlink(self.target)), other)

    def test_a_dangling_link_is_reported_and_never_touched(self):
        self.codex_home.mkdir(parents=True)
        gone = self.codex_home / "gone.md"
        self.target.symlink_to(gone)
        self.assertEqual(self.link(), "dangling")
        self.assertTrue(self.target.is_symlink())
        self.assertEqual(Path(os.readlink(self.target)), gone)

    def test_a_dangling_link_swapped_for_an_owners_file_after_the_check_survives(self):
        """The check sees a dangling link; the owner's file lands before any mutation."""
        self.codex_home.mkdir(parents=True)
        self.target.symlink_to(self.codex_home / "gone.md")
        real_exists = Path.exists

        def swap_after_answering(path, *a, **k):
            answer = real_exists(path, *a, **k)
            if path == self.target and path.is_symlink():
                os.unlink(self.target)
                self.target.write_text("owner's file\n")
            return answer
        with patch.object(Path, "exists", swap_after_answering):
            self.assertEqual(self.link(), "dangling")
        self.assertFalse(self.target.is_symlink())
        self.assertEqual(self.target.read_text(), "owner's file\n")

    def test_a_missing_target_that_an_owner_creates_before_the_link_survives(self):
        real_symlink = Path.symlink_to

        def owner_first(path, src, *a, **k):
            path.write_text("owner's file\n")
            return real_symlink(path, src, *a, **k)
        with patch.object(Path, "symlink_to", owner_first):
            self.assertEqual(self.link(), "foreign")
        self.assertEqual(self.target.read_text(), "owner's file\n")

    def test_a_symlinked_alias_of_the_owners_codex_home_is_never_written(self):
        owner = self.home / ".codex"
        owner.mkdir(parents=True)
        alias = Path(self.tmp.name).resolve() / "alias-codex"
        alias.symlink_to(owner)
        self.assertEqual(self.link(codex_home=alias), "owner-home")
        self.assertEqual(self.link(codex_home=alias / "sub"), "owner-home")
        self.assertEqual(os.listdir(owner), [])

    def test_a_case_variant_of_the_owners_codex_home_is_never_written(self):
        owner = self.home / ".codex"
        owner.mkdir(parents=True)
        variant = self.home / ".CODEX"
        if not variant.exists():
            self.skipTest("case-sensitive filesystem: .CODEX is a different directory")
        self.assertEqual(self.link(codex_home=variant), "owner-home")
        self.assertFalse((owner / "AGENTS.md").exists())

    def test_a_case_variant_is_refused_before_the_owners_home_exists(self):
        self.home.mkdir(parents=True)
        self.assertEqual(self.link(codex_home=self.home / ".CODEX"), "owner-home")
        self.assertFalse((self.home / ".CODEX").exists())

    def test_an_owners_override_is_left_alone_and_reported(self):
        self.codex_home.mkdir(parents=True)
        override = self.codex_home / "AGENTS.override.md"
        override.write_text("owner override\n")
        self.assertEqual(self.link(), "overridden")
        self.assertFalse(self.target.exists() or self.target.is_symlink())
        self.assertEqual(override.read_text(), "owner override\n")

    def test_a_concurrent_launch_that_links_first_wins(self):
        real_symlink = Path.symlink_to

        def raced(path, src, *a, **k):
            real_symlink(path, src)
            raise FileExistsError(str(path))
        with patch.object(Path, "symlink_to", raced):
            self.assertEqual(self.link(), "already-set")
        other = self.codex_home / "other.md"
        other.write_text("other\n")
        self.target.unlink()

        def other_won(path, src, *a, **k):
            real_symlink(path, other)
            raise FileExistsError(str(path))
        with patch.object(Path, "symlink_to", other_won):
            self.assertEqual(self.link(), "foreign")
        self.assertEqual(Path(os.readlink(self.target)), other)

    def main(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with patch.object(Path, "home", return_value=self.home), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = chi.main([str(a) for a in argv])
        return rc, out.getvalue(), err.getvalue()

    def test_main_prints_the_outcome_and_returns_0(self):
        rc, out, _ = self.main(self.codex_home, self.repo)
        self.assertEqual((rc, out.strip()), (0, "linked"))
        rc, out, _ = self.main(self.codex_home, self.repo)
        self.assertEqual((rc, out.strip()), (0, "already-set"))

    def test_main_returns_1_and_reports_an_os_error(self):
        self.codex_home.parent.mkdir(parents=True)
        self.codex_home.write_text("a file where the Codex home should be\n")
        rc, out, err = self.main(self.codex_home, self.repo)
        self.assertEqual((rc, out), (1, ""))
        self.assertIn("codex host instructions:", err)

    def test_cli_prints_the_outcome(self):
        out = subprocess.run([sys.executable, str(SCRIPT), str(self.codex_home), str(self.repo)],
                             capture_output=True, text=True, env={**os.environ, "HOME": str(self.home)})
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(out.stdout.strip(), "linked")
        self.assertTrue(self.target.is_symlink())


if __name__ == "__main__":
    unittest.main(verbosity=1)
