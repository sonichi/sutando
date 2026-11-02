#!/usr/bin/env python3
"""A desktop-hosted Codex core gets the host's AGENTS.md through its own Codex home.

Run: python3 tests/codex-host-instructions.test.py
"""
import contextlib
import errno
import io
import types
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

    def tree(self):
        """Every path under the temp root, links included, as a sorted list."""
        root = Path(self.tmp.name).resolve()
        found = []
        for top, dirs, files in os.walk(root):
            for name in dirs + files:
                found.append(os.path.relpath(os.path.join(top, name), root))
        return sorted(found)

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
        real_readlink = os.readlink

        def swap_after_answering(path, *a, **k):
            answer = real_readlink(path, *a, **k)
            if os.path.islink(self.target):
                os.unlink(self.target)
                self.target.write_text("owner's file\n")
            return answer
        with patch.object(os, "readlink", swap_after_answering):
            self.assertIn(self.link(), ("dangling", "foreign"))
        self.assertFalse(self.target.is_symlink())
        self.assertEqual(self.target.read_text(), "owner's file\n")

    def test_a_missing_target_that_an_owner_creates_before_the_link_survives(self):
        real_symlink = os.symlink

        def owner_first(src, dst, *a, **k):
            self.target.write_text("owner's file\n")
            return real_symlink(src, dst, *a, **k)
        with patch.object(os, "symlink", owner_first):
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
        real_symlink = os.symlink

        def raced(src, dst, *a, **k):
            real_symlink(src, dst, *a, **k)
            raise FileExistsError(dst)
        with patch.object(os, "symlink", raced):
            self.assertEqual(self.link(), "already-set")
        other = self.codex_home / "other.md"
        other.write_text("other\n")
        self.target.unlink()

        def other_won(src, dst, *a, **k):
            real_symlink(other, dst, *a, **k)
            raise FileExistsError(dst)
        with patch.object(os, "symlink", other_won):
            self.assertEqual(self.link(), "foreign")
        self.assertEqual(Path(os.readlink(self.target)), other)

    def test_an_alias_to_a_directory_inside_the_owners_codex_home_is_refused(self):
        sub = self.home / ".codex" / "sub"
        sub.mkdir(parents=True)
        alias = Path(self.tmp.name).resolve() / "alias-sub"
        alias.symlink_to(sub)
        self.assertEqual(self.link(codex_home=alias), "owner-home")
        self.assertEqual(self.link(codex_home=alias / "deeper"), "owner-home")
        self.assertEqual(sorted(p.name for p in (self.home / ".codex").rglob("*")), ["sub"])

    def test_an_alias_retargeted_to_the_owners_codex_home_after_the_check_writes_nothing_there(self):
        owner = self.home / ".codex"
        owner.mkdir(parents=True)
        safe = Path(self.tmp.name).resolve() / "safe-home"
        safe.mkdir()
        alias = Path(self.tmp.name).resolve() / "alias-home"
        alias.symlink_to(safe)
        real_check = chi.is_owner_codex_home

        def retarget_after_check(*a, **k):
            answer = real_check(*a, **k)
            if os.readlink(alias) != str(owner):
                alias.unlink()
                alias.symlink_to(owner)
            return answer
        with patch.object(chi, "is_owner_codex_home", retarget_after_check):
            result = self.link(codex_home=alias)
        self.assertEqual(os.listdir(owner), [], result)
        self.assertEqual(result, "linked")
        self.assertEqual(Path(os.readlink(safe / "AGENTS.md")), self.host)

    def test_an_alias_through_dotdot_into_the_owners_codex_home_is_refused(self):
        sub = self.home / ".codex" / "sub"
        sub.mkdir(parents=True)
        detour = Path(self.tmp.name).resolve() / "x"
        detour.mkdir()
        alias = Path(self.tmp.name).resolve() / "alias-dotdot"
        alias.symlink_to(f"{detour}/../{self.home.name}/.codex/sub")
        self.assertEqual(self.link(codex_home=alias), "owner-home")
        self.assertEqual(sorted(p.name for p in (self.home / ".codex").rglob("*")), ["sub"])

    def test_aliases_into_an_absent_owner_codex_home_are_refused_and_create_nothing(self):
        self.home.mkdir(parents=True)
        owner = self.home / ".codex"
        root = Path(self.tmp.name).resolve()
        for name, dest in (("to-owner", owner), ("to-owner-sub", owner / "sub"),
                           ("to-dotdot", f"{root}/x/../{self.home.name}/.codex/sub")):
            with self.subTest(alias=name):
                alias = root / name
                alias.symlink_to(dest)
                self.assertEqual(self.link(codex_home=alias), "owner-home")
                self.assertEqual(self.link(codex_home=alias / "deeper"), "owner-home")
                self.assertFalse(os.path.lexists(owner), f"{name} created the owner's home")

    def test_an_alias_then_dotdot_to_the_owners_codex_home_is_refused(self):
        """alias/../.codex resolves the alias first, as the OS does: it is ~/.codex."""
        (self.home / ".codex").mkdir(parents=True)
        (self.home / "child").mkdir()
        elsewhere = Path(self.tmp.name).resolve() / "elsewhere"
        elsewhere.mkdir()
        alias = elsewhere / "alias"
        alias.symlink_to(self.home / "child")
        spelled = f"{alias}/../.codex"
        resolved = Path(os.path.realpath(spelled))
        self.assertTrue(os.path.samefile(resolved, self.home / ".codex"))
        before = self.tree()
        self.assertEqual(self.link(codex_home=spelled), "owner-home")
        self.assertEqual(self.tree(), before)
        self.assertEqual(os.listdir(resolved), [])
        self.assertFalse(os.path.lexists(elsewhere / ".codex"), "lexical location created")

    def test_an_alias_then_dotdot_links_into_the_directory_the_os_resolves(self):
        root = Path(self.tmp.name).resolve()
        real_parent = root / "real-parent"
        (real_parent / "child").mkdir(parents=True)
        (real_parent / "codex-home").mkdir()
        elsewhere = root / "elsewhere"
        elsewhere.mkdir()
        alias = elsewhere / "alias"
        alias.symlink_to(real_parent / "child")
        spelled = f"{alias}/../codex-home"
        resolved = Path(os.path.realpath(spelled))
        self.assertEqual(resolved, real_parent / "codex-home")
        self.assertEqual(self.link(codex_home=spelled), "linked")
        self.assertTrue((resolved / "AGENTS.md").is_symlink())
        self.assertEqual(Path(os.readlink(resolved / "AGENTS.md")), self.host)
        self.assertEqual((resolved / "AGENTS.md").read_text(), "host\n")
        self.assertFalse(os.path.lexists(elsewhere / "codex-home"), "lexical location created")
        self.assertEqual(sorted(os.listdir(elsewhere)), ["alias"])

    def test_a_relative_codex_home_is_refused_and_creates_nothing(self):
        before = self.tree()
        self.assertEqual(self.link(codex_home="rel-home"), "invalid-home")
        self.assertTrue(chi.is_owner_codex_home("rel-home", self.home))
        self.assertEqual(self.tree(), before)
        self.assertFalse(os.path.lexists("rel-home"))

    def test_dotdot_below_a_missing_directory_is_refused_before_anything_is_created(self):
        spelled = f"{self.codex_home}/missing/../x"
        before = self.tree()
        self.assertEqual(self.link(codex_home=spelled), "invalid-home")
        self.assertTrue(chi.is_owner_codex_home(spelled, self.home))
        self.assertEqual(self.tree(), before)
        self.assertFalse(os.path.lexists(self.codex_home.parent))

    def test_fd_path_reads_f_getpath(self):
        fake = types.SimpleNamespace(F_GETPATH=50, fcntl=lambda fd, op, buf: b"/some/dir\0junk")
        with patch.dict(sys.modules, {"fcntl": fake}):
            self.assertEqual(chi._fd_path(3), Path("/some/dir"))

        def broken(fd, op, buf):
            raise OSError(errno.EBADF, "bad fd")
        with patch.dict(sys.modules, {"fcntl": types.SimpleNamespace(F_GETPATH=50, fcntl=broken)}):
            self.assertIsNone(chi._fd_path(3))

    def test_fd_path_falls_back_to_proc_then_to_none(self):
        with patch.dict(sys.modules, {"fcntl": types.SimpleNamespace()}):
            with patch.object(os, "readlink", return_value="/proc/dir"):
                self.assertEqual(chi._fd_path(3), Path("/proc/dir"))
            with patch.object(os, "readlink", side_effect=OSError(errno.ENOENT, "no proc")):
                self.assertIsNone(chi._fd_path(3))

    def test_a_directory_created_concurrently_is_opened(self):
        real_mkdir = os.mkdir

        def raced(name, *a, **k):
            real_mkdir(name, *a, **k)
            raise FileExistsError(name)
        with patch.object(os, "mkdir", raced):
            self.assertEqual(self.link(), "linked")
        self.assertTrue(self.target.is_symlink())

    def test_a_failed_open_closes_the_parent_and_raises(self):
        real_open, real_close, closed = os.open, os.close, []

        def fail_child(path, flags, *a, **k):
            if k.get("dir_fd") is not None:
                raise PermissionError(errno.EACCES, "denied")
            return real_open(path, flags, *a, **k)

        def track(fd):
            closed.append(fd)
            real_close(fd)
        with patch.object(os, "open", fail_child), patch.object(os, "close", track):
            with self.assertRaises(PermissionError):
                self.link()
        self.assertEqual(len(closed), 1)

    def test_a_component_that_became_a_symlink_is_foreign_and_other_errors_raise(self):
        with patch.object(chi, "_open_home", side_effect=OSError(errno.ELOOP, "loop")):
            self.assertEqual(self.link(), "foreign")
        with patch.object(chi, "_open_home", side_effect=OSError(errno.EACCES, "denied")):
            with self.assertRaises(OSError):
                self.link()

    def test_the_opened_directory_is_rechecked_against_the_owners_home(self):
        owner = self.home / ".codex"
        owner.mkdir(parents=True)
        with patch.object(chi, "_fd_path", return_value=owner):
            self.assertEqual(self.link(), "owner-home")
        self.assertEqual(os.listdir(owner), [])
        self.assertFalse(self.target.exists())

    def test_without_an_fd_path_the_opened_directory_must_match_by_identity(self):
        with patch.object(chi, "_fd_path", return_value=None):
            with patch.object(os.path, "samestat", return_value=False):
                self.assertEqual(self.link(), "foreign")
            self.assertFalse(self.target.exists())
            self.assertEqual(self.link(), "linked")

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
