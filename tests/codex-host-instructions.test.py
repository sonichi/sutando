#!/usr/bin/env python3
"""A desktop-hosted Codex core gets the host's AGENTS.md through its own Codex home.

The helper only writes into an app-private Codex home whose every existing component is a
real directory, and never into the owner's ~/.codex.

Run: python3 tests/codex-host-instructions.test.py
"""
import contextlib
import errno
import io
import json
import os
import shutil
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
DARWIN_MAXSYMLINKS = 32


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.home = self.root / "home"
        self.home.mkdir()
        self.owner = self.home / ".codex"
        self.engine = self.root / "Application Support" / "engine"
        self.repo = self.engine / "sutando"
        self.repo.mkdir(parents=True)
        (self.repo / "AGENTS.md").write_text("checkout\n")
        self.host = self.engine / "AGENTS.md"
        self.host.write_text("host\n")
        self.codex_home = self.root / "workspace" / ".codex-sutando"
        self.target = self.codex_home / "AGENTS.md"

    def tearDown(self):
        for top, dirs, _ in os.walk(self.root):
            for d in dirs:
                p = os.path.join(top, d)
                if not os.path.islink(p):
                    os.chmod(p, 0o700)
        self.tmp.cleanup()

    def tree(self):
        """Every path under the temp root, links included, as a sorted list."""
        found = []
        for top, dirs, files in os.walk(self.root):
            for name in dirs + files:
                found.append(os.path.relpath(os.path.join(top, name), self.root))
        return sorted(found)

    def link(self, codex_home=None, repo=None):
        return chi.ensure_linked(codex_home or self.codex_home, repo or self.repo, self.home)

    def assert_refused_unchanged(self, codex_home, expected):
        before = self.tree()
        self.assertEqual(self.link(codex_home=codex_home), expected)
        self.assertTrue(chi.is_owner_codex_home(codex_home, self.home))
        self.assertEqual(self.tree(), before)


class Linking(Base):
    def test_a_fresh_codex_home_gets_the_host_file_and_a_rerun_changes_nothing(self):
        self.assertFalse(chi.is_owner_codex_home(self.codex_home, self.home))
        self.assertEqual(self.link(), "linked")
        self.assertEqual(Path(os.readlink(self.target)), self.host)
        self.assertEqual(self.target.read_text(), "host\n")
        self.assertEqual(self.link(), "already-set")
        self.assertFalse(chi.is_owner_codex_home(self.codex_home, self.home))

    def test_a_checkout_outside_a_desktop_engine_dir_is_left_alone(self):
        repo = self.root / "code" / "sutando"
        repo.mkdir(parents=True)
        (repo.parent / "AGENTS.md").write_text("the owner's own\n")
        self.assertEqual(self.link(repo=repo), "no-host")
        self.assertFalse(self.codex_home.exists())

    def test_no_host_file_links_nothing(self):
        self.host.unlink()
        self.assertEqual(self.link(), "no-host")
        self.assertFalse(self.target.exists())

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
        self.assertEqual(Path(os.readlink(self.target)), gone)

    def test_a_dangling_link_swapped_for_an_owners_file_after_the_check_survives(self):
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
        self.assertEqual(self.target.read_text(), "owner's file\n")

    def test_a_missing_target_that_an_owner_creates_before_the_link_survives(self):
        real_symlink = os.symlink

        def owner_first(src, dst, *a, **k):
            self.target.write_text("owner's file\n")
            return real_symlink(src, dst, *a, **k)
        with patch.object(os, "symlink", owner_first):
            self.assertEqual(self.link(), "foreign")
        self.assertEqual(self.target.read_text(), "owner's file\n")

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

        def vanished(src, dst, *a, **k):
            raise FileExistsError(dst)
        self.target.unlink()
        with patch.object(os, "symlink", vanished):
            self.assertEqual(self.link(), "foreign")

    def test_a_directory_created_concurrently_is_opened(self):
        real_mkdir = os.mkdir

        def raced(name, *a, **k):
            real_mkdir(name, *a, **k)
            raise FileExistsError(name)
        with patch.object(os, "mkdir", raced):
            self.assertEqual(self.link(), "linked")
        self.assertTrue(self.target.is_symlink())

    def test_an_unexpected_open_error_is_raised(self):
        real_open = os.open

        def denied(path, flags, *a, **k):
            if path == "workspace":
                raise PermissionError(errno.EACCES, "denied", path)
            return real_open(path, flags, *a, **k)
        with patch.object(os, "open", denied):
            with self.assertRaises(PermissionError):
                self.link()
            self.assertTrue(chi.is_owner_codex_home(self.codex_home, self.home))


class OwnerGuard(Base):
    def test_the_owners_codex_home_and_paths_in_it_are_never_written(self):
        (self.owner / "sub").mkdir(parents=True)
        for spelled in (self.owner, self.owner / "sub", self.owner / "sub" / "new"):
            with self.subTest(path=str(spelled)):
                self.assert_refused_unchanged(spelled, "owner-home")

    def test_an_absent_owners_codex_home_is_refused_before_it_is_created(self):
        for spelled in (self.owner, self.owner / "sub", self.home / ".CODEX" / "sub"):
            with self.subTest(path=str(spelled)):
                self.assert_refused_unchanged(spelled, "owner-home")
                self.assertFalse(os.path.lexists(self.owner))

    def test_a_case_variant_of_an_existing_owners_codex_home_is_never_written(self):
        self.owner.mkdir()
        variant = self.home / ".CODEX"
        if not variant.exists():
            self.skipTest("case-sensitive filesystem: .CODEX is a different directory")
        self.assert_refused_unchanged(variant, "owner-home")

    def test_an_owners_codex_home_reached_through_its_own_symlink_is_refused(self):
        real = self.root / "real-codex"
        real.mkdir()
        self.owner.symlink_to(real)
        self.assert_refused_unchanged(real / "sub", "owner-home")

    def test_an_unreadable_owner_identity_fails_closed_without_any_mutation(self):
        self.owner.mkdir()
        real_stat = os.stat

        def eio_for_owner(path, *a, **k):
            if os.fspath(path) == os.fspath(self.owner):
                raise OSError(errno.EIO, "I/O error", os.fspath(path))
            return real_stat(path, *a, **k)
        mutation = AssertionError("mutated the filesystem")
        before = self.tree()
        with patch.object(os, "stat", eio_for_owner), \
                patch.object(os, "mkdir", side_effect=mutation) as mkdir, \
                patch.object(os, "open", side_effect=mutation) as opened, \
                patch.object(os, "symlink", side_effect=mutation) as symlink:
            self.assertEqual(self.link(codex_home=self.owner), "owner-home")
            self.assertEqual(self.link(), "owner-home")
            self.assertTrue(chi.is_owner_codex_home(self.codex_home, self.home))
        for call in (mkdir, opened, symlink):
            call.assert_not_called()
        self.assertEqual(self.tree(), before)

    def test_an_unreadable_directory_identity_during_the_walk_fails_closed(self):
        with patch.object(os, "fstat", side_effect=OSError(errno.EIO, "I/O error")):
            self.assert_refused_unchanged(self.codex_home, "owner-home")

    def race(self, codex_home, home):
        """Replace ~/.codex with a fresh directory right after its identity is read."""
        real_anchor, replaced = chi._owner_anchor, []

        def replace_after_snapshot(h):
            answer = real_anchor(h)
            real = Path(os.path.realpath(self.owner))
            shutil.rmtree(real)
            real.mkdir()
            replaced.append(True)
            return answer
        before = self.tree()
        with patch.object(chi, "_owner_anchor", replace_after_snapshot):
            result = chi.ensure_linked(codex_home, self.repo, home)
        return result, before

    def test_an_owners_codex_home_replaced_after_its_identity_is_read_is_still_refused(self):
        # Refused by the lexical guard, so inode reuse on the replacement cannot matter.
        self.owner.mkdir()
        result, before = self.race(self.owner, self.home)
        self.assertEqual(result, "owner-home")
        self.assertEqual(os.listdir(self.owner), [])
        self.assertEqual(self.tree(), before)

    def test_a_case_variant_of_a_replaced_owners_codex_home_is_still_refused(self):
        self.owner.mkdir()
        result, _ = self.race(self.home / ".CODEX" / "sub", self.home)
        self.assertEqual(result, "owner-home")
        self.assertEqual(os.listdir(self.owner), [])

    def test_the_owners_codex_home_under_a_symlinked_home_is_still_refused(self):
        self.owner.mkdir()
        home_link = self.root / "home-link"
        home_link.symlink_to(self.home)
        result, _ = self.race(self.owner, home_link)
        self.assertEqual(result, "owner-home")
        self.assertEqual(os.listdir(self.owner), [])
        self.assertTrue(chi.is_owner_codex_home(self.owner / "x", home_link))

    def test_the_lexical_guard_matches_only_the_owners_codex_home(self):
        parts = chi._parts(self.home / ".codexes" / "x")
        self.assertFalse(chi._lexically_owner(parts, self.home))
        self.assertTrue(chi._lexically_owner(chi._parts(self.home / ".Codex"), self.home))

    def retarget(self, candidate):
        """~/.codex is a link elsewhere when its identity is read, then retargeted to `candidate`."""
        elsewhere = self.root / "elsewhere"
        elsewhere.mkdir()
        self.owner.symlink_to(elsewhere)
        real_anchor, calls = chi._owner_anchor, []

        def retarget_after_snapshot(h):
            answer = real_anchor(h)
            if not calls:
                self.owner.unlink()
                self.owner.symlink_to(candidate)
            calls.append(answer)
            return answer
        with patch.object(chi, "_owner_anchor", retarget_after_snapshot):
            return chi.ensure_linked(candidate, self.repo, self.home)

    def test_an_owner_link_retargeted_to_an_existing_candidate_before_the_link_is_refused(self):
        candidate = self.root / "candidate"
        candidate.mkdir()
        self.assertEqual(self.retarget(candidate), "owner-home")
        self.assertEqual(os.listdir(candidate), [])

    def test_an_owner_link_retargeted_to_a_missing_candidate_before_its_mkdir_is_refused(self):
        candidate = self.root / "new-candidate"
        self.assertEqual(self.retarget(candidate), "owner-home")
        self.assertFalse(os.path.lexists(candidate))

    def test_an_unreadable_owner_identity_on_the_recheck_fails_closed(self):
        real_anchor, calls = chi._owner_anchor, []

        def fails_second_time(h):
            calls.append(h)
            if len(calls) > 1:
                raise OSError(errno.EIO, "I/O error")
            return real_anchor(h)
        with patch.object(chi, "_owner_anchor", fails_second_time):
            self.assertEqual(self.link(), "owner-home")
        self.assertFalse(os.path.lexists(self.codex_home))

    def at_pre_symlink(self, transition, codex_home=None):
        """Run `transition` after the walk and override probe, just before the final recheck."""
        real_probe = chi._active_override

        def probe_then_transition(fd):
            answer = real_probe(fd)
            transition()
            return answer
        with patch.object(chi, "_active_override", probe_then_transition):
            return self.link(codex_home=codex_home)

    def reset_owner(self, start, elsewhere):
        if os.path.islink(self.owner) or self.owner.is_file():
            self.owner.unlink()
        elif self.owner.exists():
            shutil.rmtree(self.owner)
        if start == "dir":
            self.owner.mkdir()
        elif start == "link":
            self.owner.symlink_to(elsewhere)

    def test_any_detectable_owner_transition_just_before_the_symlink_refuses_it(self):
        elsewhere = self.root / "elsewhere"
        elsewhere.mkdir()
        other = self.root / "other"
        other.mkdir()

        def created():
            self.owner.mkdir()

        def removed():
            self.owner.rmdir()

        def replaced_old_kept():
            aside = self.root / "owner-aside"
            if aside.exists():
                aside.rmdir()
            self.owner.rename(aside)
            self.owner.mkdir()

        def replaced_by_a_file():
            self.owner.rename(self.root / f"owner-aside-file-{len(os.listdir(self.root))}")
            self.owner.write_text("not a directory\n")

        def retargeted():
            self.owner.unlink()
            self.owner.symlink_to(other)
        # Each new owner inode is guaranteed distinct: the old one is still in use.
        cases = [("created", None, created), ("removed", "dir", removed),
                 ("replaced-old-kept", "dir", replaced_old_kept),
                 ("replaced-by-a-file", "dir", replaced_by_a_file),
                 ("retargeted", "link", retargeted)]
        for name, start, transition in cases:
            with self.subTest(transition=name):
                self.reset_owner(start, elsewhere)
                home = self.root / f"app-home-{name}"
                self.assertEqual(self.at_pre_symlink(transition, home), "owner-home")
                self.assertFalse(os.path.lexists(home / "AGENTS.md"), "linked despite the transition")

    def test_an_owner_recreated_in_place_is_never_written_into(self):
        # rmdir+mkdir may reuse the inode (Linux does), so the record may not change; the
        # property that must hold everywhere is that nothing lands in ~/.codex.
        real_anchor = chi._owner_anchor
        for name, reuse in (("as-the-os-allocates", False), ("inode-reused", True)):
            with self.subTest(case=name):
                self.reset_owner("dir", None)
                snapshot = real_anchor(self.home)

                def recreate():
                    self.owner.rmdir()
                    self.owner.mkdir()
                home = self.root / f"app-home-recreated-{name}"
                patches = [patch.object(chi, "_owner_anchor", lambda h: snapshot)] if reuse else []
                with contextlib.ExitStack() as stack:
                    for p in patches:
                        stack.enter_context(p)
                    result = self.at_pre_symlink(recreate, home)
                self.assertIn(result, ("owner-home", "linked"))
                self.assertEqual(os.listdir(self.owner), [])
                if reuse:
                    self.assertEqual(result, "linked")
                if result == "linked":
                    self.assertEqual(Path(os.readlink(home / "AGENTS.md")), self.host)

    def test_an_unchanged_owner_lets_the_same_path_link(self):
        """Positive control for the transition tests: no transition, same hook, a link."""
        self.owner.mkdir()
        self.assertEqual(self.at_pre_symlink(lambda: None), "linked")
        self.assertEqual(Path(os.readlink(self.target)), self.host)

    def test_an_owner_link_to_the_missing_app_home_is_refused_before_any_mkdir(self):
        app_home = self.root / "app" / "workspace" / ".codex-sutando"
        self.owner.symlink_to(app_home)
        self.assert_refused_unchanged(app_home, "owner-home")
        self.assertFalse(os.path.lexists(self.root / "app"))

    def test_an_owner_link_chain_relative_and_too_long_is_handled(self):
        (self.root / "app").mkdir()
        self.owner.symlink_to("../hop")
        (self.root / "hop").symlink_to("app/.codex-sutando")
        self.assert_refused_unchanged(self.root / "app" / ".codex-sutando" / "x", "owner-home")
        self.owner.unlink()
        self.owner.symlink_to(self.owner.name)
        self.assert_refused_unchanged(self.codex_home, "owner-home")

    def test_an_owner_link_chain_of_32_hops_resolves_and_33_refuses(self):
        target = self.root / "app" / ".codex-sutando"
        (self.root / "app").mkdir()
        for hops, expected in ((32, "owner-home"), (33, "owner-home")):
            with self.subTest(hops=hops):
                if os.path.lexists(self.owner):
                    self.owner.unlink()
                for old in self.root.glob("chain-hop-*"):
                    old.unlink()
                prev = target
                for i in range(hops - 1):
                    link = self.root / f"chain-hop-{i}"
                    link.symlink_to(prev)
                    prev = link
                self.owner.symlink_to(prev)
                spellings = None
                try:
                    spellings = chi._owner_spellings(self.home)
                except ValueError:
                    pass
                if hops == 32:
                    self.assertIn(str(target), spellings, "32 hops must resolve")
                else:
                    self.assertIsNone(spellings, "33 hops must refuse")
                self.assert_refused_unchanged(target, expected)

    def test_a_stable_owner_link_to_the_candidate_is_refused(self):
        candidate = self.root / "candidate"
        candidate.mkdir()
        self.owner.symlink_to(candidate)
        self.assert_refused_unchanged(candidate, "owner-home")

    def test_a_sibling_of_the_owners_codex_home_is_allowed(self):
        self.owner.mkdir()
        for name in (".codex-other", ".codexx", "codex"):
            with self.subTest(sibling=name):
                sibling = self.home / name
                self.assertFalse(chi.is_owner_codex_home(sibling, self.home))
                self.assertEqual(self.link(codex_home=sibling), "linked")
        self.assertEqual(os.listdir(self.owner), [])

    def test_the_lexical_guard_matches_another_unicode_form_of_the_owners_codex_home(self):
        nfc, nfd = "caf\u00e9", "cafe\u0301"
        for configured, aliased in ((nfc, nfd), (nfd, nfc)):
            with self.subTest(configured=ascii(configured)):
                if os.path.lexists(self.owner):
                    self.owner.unlink()
                self.owner.symlink_to(self.root / aliased / "app" / "skills" / "tutorial")
                for below in ([], ["skills"], ["skills", "tutorial", "x"]):
                    parts = chi._parts(self.root / configured / "app") + below
                    self.assertTrue(chi._lexically_owner(parts, self.home), below)

    def test_the_identity_layer_matches_a_missing_owner_in_another_unicode_form(self):
        missing_owner = self.root / "cafe\u0301" / ".codex"
        anchor, missing = chi._owner_anchor(missing_owner.parent.parent / "cafe\u0301")
        fd = os.open(self.root, os.O_RDONLY)
        try:
            self.assertTrue(chi._at_owner(fd, anchor, missing, ["caf\u00e9", ".codex", "sub"]))
            self.assertFalse(chi._at_owner(fd, anchor, missing, ["cafe", ".codex"]))
        finally:
            os.close(fd)

    def test_the_identity_layer_matches_a_missing_owner_by_case_folded_suffix(self):
        """The identity layer alone: reached when a path names ~/.codex by another route."""
        anchor, missing = chi._owner_anchor(self.home)
        fd = os.open(self.home, os.O_RDONLY)
        try:
            self.assertTrue(chi._at_owner(fd, anchor, missing, [".CODEX", "sub"]))
            self.assertFalse(chi._at_owner(fd, anchor, missing, [".codex-other"]))
        finally:
            os.close(fd)

    def test_the_owner_anchor_walks_up_to_an_existing_ancestor(self):
        anchor, missing = chi._owner_anchor(self.root / "no" / "such-home")
        st = os.stat(self.root)
        self.assertEqual((anchor, missing), ((st.st_dev, st.st_ino), ["no", "such-home", ".codex"]))
        with patch.object(os, "stat", side_effect=FileNotFoundError(errno.ENOENT, "gone")):
            with self.assertRaises(FileNotFoundError):
                chi._owner_anchor(self.home)


class NoSymlinkScope(Base):
    def test_any_symlink_component_is_invalid_and_creates_nothing(self):
        (self.owner / "sub").mkdir(parents=True)
        (self.root / "real-parent" / "child").mkdir(parents=True)
        (self.root / "real-parent" / "codex-home").mkdir()
        (self.root / "a-file").write_text("x\n")
        cases = {
            "to-owner": self.owner,
            "to-owner-sub": self.owner / "sub",
            "to-absent": self.root / "nowhere",
            "to-plain-dir": self.root / "real-parent" / "codex-home",
            "through-missing": "missing/../real-parent",
            "through-file": "a-file/../real-parent",
            "file-dot": "a-file/.",
        }
        for name, dest in cases.items():
            alias = self.root / name
            alias.symlink_to(dest)
            for spelled in (alias, alias / "deeper"):
                with self.subTest(alias=name, path=str(spelled)):
                    self.assert_refused_unchanged(spelled, "invalid-home")
        self.assertEqual(os.listdir(self.owner), ["sub"])
        self.assertFalse(os.path.lexists(self.root / "real-parent" / "codex-home" / "AGENTS.md"))

    def test_a_symlinked_intermediate_component_is_invalid(self):
        (self.root / "real" / "mid").mkdir(parents=True)
        (self.root / "via").symlink_to(self.root / "real")
        self.assert_refused_unchanged(self.root / "via" / "mid" / "home", "invalid-home")
        self.assertEqual(os.listdir(self.root / "real" / "mid"), [])

    def test_a_chain_longer_than_darwins_limit_is_invalid(self):
        (self.root / "end").mkdir()
        prev = self.root / "end"
        for i in range(DARWIN_MAXSYMLINKS + 1):
            link = self.root / f"chain-{i}"
            link.symlink_to(prev)
            prev = link
        if sys.platform == "darwin":
            with self.assertRaises(OSError) as raised:
                os.stat(prev)
            self.assertEqual(raised.exception.errno, errno.ELOOP)
        self.assert_refused_unchanged(prev, "invalid-home")
        self.assertEqual(os.listdir(self.root / "end"), [])

    def test_a_dotdot_or_a_relative_path_is_invalid(self):
        (self.root / "real").mkdir()
        for spelled in (f"{self.root}/real/../real", f"{self.codex_home}/missing/../x", "rel-home"):
            with self.subTest(path=spelled):
                self.assert_refused_unchanged(spelled, "invalid-home")
        self.assertFalse(os.path.lexists("rel-home"))

    def test_a_home_below_a_regular_file_is_invalid(self):
        (self.root / "a-file").write_text("x\n")
        self.assert_refused_unchanged(self.root / "a-file" / "home", "invalid-home")

    def test_eloop_and_enotdir_from_a_component_open_are_both_invalid(self):
        real_open = os.open
        for code in (errno.ELOOP, errno.ENOTDIR):
            def fail(path, flags, *a, _code=code, **k):
                if path == "workspace":
                    raise OSError(_code, os.strerror(_code), path)
                return real_open(path, flags, *a, **k)
            with self.subTest(errno=errno.errorcode[code]):
                with patch.object(os, "open", fail):
                    self.assert_refused_unchanged(self.codex_home, "invalid-home")

    def test_an_intermediate_swapped_to_the_owners_home_mid_walk_creates_nothing_there(self):
        """`mid` becomes a link to ~/.codex after the checks, before the first directory open."""
        (self.owner / "deep").mkdir(parents=True)
        mid = self.root / "safe" / "mid"
        (mid / "deep").mkdir(parents=True)
        real_open, swapped = os.open, []

        def swap_first(path, flags, *a, **k):
            if not swapped:
                swapped.append(True)
                (mid / "deep").rmdir()
                mid.rmdir()
                mid.symlink_to(self.owner)
            return real_open(path, flags, *a, **k)
        with patch.object(os, "open", swap_first):
            self.assertEqual(self.link(codex_home=mid / "deep" / "new"), "invalid-home")
        self.assertEqual(sorted(p.name for p in self.owner.rglob("*")), ["deep"])
        self.assertTrue(mid.is_symlink())


class Overrides(Base):
    def make_home(self, name):
        home = self.root / f"home-{name}"
        home.mkdir()
        return home, home / "AGENTS.override.md"

    def shape(self, path):
        st = os.lstat(path)
        return (st.st_mode, st.st_ino, os.readlink(path) if os.path.islink(path) else None)

    def check(self, name, make, expected):
        home, override = self.make_home(name)
        make(override)
        before = self.shape(override)
        self.assertEqual(self.link(codex_home=home), expected)
        self.assertEqual(self.shape(override), before, "the override was touched")
        if expected == "linked":
            self.assertEqual(Path(os.readlink(home / "AGENTS.md")), self.host)
        else:
            self.assertFalse(os.path.lexists(home / "AGENTS.md"))

    def test_a_readable_regular_file_overrides(self):
        self.check("regular", lambda p: p.write_text("owner\n"), "overridden")

    def test_a_link_to_a_readable_regular_file_overrides(self):
        def make(p):
            (p.parent / "real.md").write_text("owner\n")
            p.symlink_to(p.parent / "real.md")
        self.check("link-to-regular", make, "overridden")

    def test_overrides_codex_ignores_do_not_block_the_link(self):
        def directory(p):
            p.mkdir()

        def dangling(p):
            p.symlink_to(p.parent / "gone.md")

        def link_to_directory(p):
            (p.parent / "real-dir").mkdir()
            p.symlink_to(p.parent / "real-dir")

        def self_loop(p):
            p.symlink_to(p.name)

        def long_chain(p):
            (p.parent / "real.md").write_text("owner\n")
            prev = p.parent / "real.md"
            for i in range(DARWIN_MAXSYMLINKS + 8):
                link = p.parent / f"hop-{i}"
                link.symlink_to(prev)
                prev = link
            p.symlink_to(prev)

        def fifo(p):
            os.mkfifo(p)
        cases = [("directory", directory), ("dangling", dangling),
                 ("link-to-directory", link_to_directory), ("self-loop", self_loop),
                 ("long-chain", long_chain), ("fifo", fifo)]
        for name, make in cases:
            with self.subTest(override=name):
                self.check(name, make, "linked")

    @unittest.skipIf(hasattr(os, "geteuid") and os.geteuid() == 0, "root can read mode 000")
    def test_an_unreadable_regular_override_does_not_block_the_link(self):
        def make(p):
            p.write_text("owner\n")
            os.chmod(p, 0)
        self.check("mode-000", make, "linked")

    def test_an_empty_or_whitespace_only_override_does_not_block_the_link(self):
        for name, body in (("empty", b""), ("newline", b"\n"), ("spaces", b"   "),
                           ("tabs", b"\t\t"), ("cr", b"\r"), ("vt", b"\x0b"), ("ff", b"\x0c"),
                           ("nbsp", "\u00a0".encode()), ("em-space", "\u2003".encode()),
                           ("mixed", " \t\r\n\x0b\x0c\u00a0\u2003\n".encode())):
            with self.subTest(override=name):
                self.check(f"ws-{name}", lambda p, b=body: p.write_bytes(b), "linked")

    def test_text_and_invalid_utf8_overrides_block_the_link(self):
        for name, body in (("text", b"  \n# owner rules\n"), ("invalid-utf8", b"\xff\xfe\x80"),
                           ("nul", b"\x00"), ("nul-in-whitespace", b" \x00 \n"),
                           ("unicode-ws-and-nul", "\u00a0\u2003\x00".encode()),
                           ("ws-prefix-then-text", " \t\u00a0\n rules".encode())):
            with self.subTest(override=name):
                self.check(f"content-{name}", lambda p, b=body: p.write_bytes(b), "overridden")

    def test_a_hardlinked_override_blocks_the_link(self):
        def make(p):
            (p.parent / "real.md").write_text("owner\n")
            os.link(p.parent / "real.md", p)
        self.check("hardlink", make, "overridden")

    def test_the_whole_override_is_read_with_no_cap(self):
        # Codex falls back to AGENTS.md for a blank override of any size, as the link does.
        self.check("ws-1mib-plus-1", lambda p: p.write_bytes(b" " * (1024 * 1024 + 1)), "linked")
        self.check("late", lambda p: p.write_bytes(b" " * (1024 * 1024 + 1) + b"x"), "overridden")
        split = "\u00a0".encode()  # a multi-byte character straddling a read boundary
        self.check("split", lambda p: p.write_bytes(b" " * 65535 + split + b"\n"), "linked")
        self.check("truncated", lambda p: p.write_bytes(b" " + split[:1]), "overridden")

    def test_only_unicode_white_space_is_blank(self):
        for code in range(0x1C, 0x20):
            with self.subTest(char=f"U+{code:04X}"):
                self.check(f"sep-{code:x}", lambda p, c=code: p.write_bytes(chr(c).encode()), "overridden")
        for char in ("\u0085", "\u1680", "\u2000", "\u200a", "\u2028", "\u2029", "\u202f",
                     "\u205f", "\u3000"):
            with self.subTest(char=f"U+{ord(char):04X}"):
                self.check(f"ws-{ord(char):x}", lambda p, c=char: p.write_bytes(c.encode()), "linked")
        self.check("zwsp", lambda p: p.write_bytes("\u200b".encode()), "overridden")

    def test_content_then_a_read_error_does_not_block_the_link(self):
        """Codex reads the whole file; a later error falls back to AGENTS.md."""
        real_read = os.read
        for name, first in (("text", b"x"), ("invalid-utf8", b"\xff")):
            with self.subTest(first=name):
                home, override = self.make_home(f"late-eio-{name}")
                override.write_bytes(first + b" " * 70000)
                reads = []

                def then_eio(fd, n):
                    reads.append(fd)
                    if len(reads) > 1:
                        raise OSError(errno.EIO, "I/O error")
                    return real_read(fd, n)
                with patch.object(os, "read", then_eio):
                    self.assertEqual(self.link(codex_home=home), "linked")
                self.assertEqual(len(reads), 2, "stopped reading at the first content")

    def test_content_is_read_through_to_eof(self):
        self.check("content-then-ws", lambda p: p.write_bytes(b"x" + b" " * 200000), "overridden")
        self.check("invalid-then-ws", lambda p: p.write_bytes(b"\xff" + b" " * 200000), "overridden")

    def test_a_read_error_on_the_override_does_not_block_the_link(self):
        home, override = self.make_home("eio-read")
        override.write_text("owner\n")
        with patch.object(os, "read", side_effect=OSError(errno.EIO, "I/O error")):
            self.assertEqual(self.link(codex_home=home), "linked")

    def test_an_override_open_or_stat_error_does_not_block_the_link(self):
        home, override = self.make_home("eacces")
        override.write_text("owner\n")
        real_open = os.open

        def eacces(path, flags, *a, **k):
            if path == "AGENTS.override.md":
                raise PermissionError(errno.EACCES, "denied", path)
            return real_open(path, flags, *a, **k)
        with patch.object(os, "open", eacces):
            self.assertEqual(self.link(codex_home=home), "linked")
        (home / "AGENTS.md").unlink()
        real_fstat, probes = os.fstat, []

        def record(path, flags, *a, **k):
            fd = real_open(path, flags, *a, **k)
            if path == "AGENTS.override.md":
                probes.append(fd)
            return fd

        def fstat_fails_on_probe(fd):
            if fd in probes:
                raise OSError(errno.EIO, "I/O error")
            return real_fstat(fd)
        with patch.object(os, "open", record), patch.object(os, "fstat", fstat_fails_on_probe):
            self.assertEqual(self.link(codex_home=home), "linked")
        self.assertEqual(len(probes), 1)
        self.assertEqual(override.read_text(), "owner\n")


class CheckMode(Base):
    def test_check_reports_what_would_be_refused_and_creates_nothing(self):
        app_home = self.root / "app" / ".codex-sutando"
        (self.root / "a-link").symlink_to(self.root)
        before = self.tree()
        self.assertEqual(chi.check(self.codex_home, self.repo, self.home), "ok")
        self.assertEqual(chi.check("rel", self.repo, self.home), "invalid-home")
        self.assertEqual(chi.check(self.root / "a-link" / "x", self.repo, self.home), "invalid-home")
        self.assertEqual(chi.check(self.owner, self.repo, self.home), "owner-home")
        self.assertEqual(chi.check(self.codex_home, self.root / "code" / "x", self.home), "no-host")
        self.owner.symlink_to(app_home)
        self.assertEqual(chi.check(app_home, self.repo, self.home), "owner-home")
        self.assertEqual(self.tree(), before + [os.path.relpath(self.owner, self.root)])

    def test_check_fails_closed_on_an_unreadable_owner_identity(self):
        with patch.object(chi, "_owner_anchor", side_effect=OSError(errno.EIO, "I/O error")):
            self.assertEqual(chi.check(self.codex_home, self.repo, self.home), "owner-home")

    def test_check_refuses_a_walk_that_reaches_the_owner(self):
        with patch.object(chi, "_walk", return_value=("owner-home", None)):
            self.assertEqual(chi.check(self.codex_home, self.repo, self.home), "owner-home")
        self.codex_home.mkdir(parents=True)
        self.assertEqual(chi.check(self.codex_home, self.repo, self.home), "ok")

    def test_the_check_flag_on_the_cli(self):
        out = subprocess.run([sys.executable, str(SCRIPT), "--check", str(self.codex_home),
                              str(self.repo)], capture_output=True, text=True,
                             env={**os.environ, "HOME": str(self.home)})
        self.assertEqual((out.returncode, out.stdout.strip()), (0, "ok"), out.stderr)
        self.assertFalse(os.path.lexists(self.codex_home))


class CreateHome(Base):
    def test_a_missing_home_is_created_and_is_ok(self):
        self.assertEqual(chi.create_home(self.codex_home, self.home), "ok")
        self.assertTrue(self.codex_home.is_dir())

    def test_a_relative_or_dotdot_home_is_invalid_and_creates_nothing(self):
        before = self.tree()
        for bad in ("relative/home", f"{self.root}/a/../b"):
            self.assertEqual(chi.create_home(bad, self.home), "invalid-home")
        self.assertEqual(self.tree(), before)

    def test_the_owner_home_and_a_link_component_are_refused_creating_nothing(self):
        (self.root / "workspace").symlink_to(self.root / "elsewhere")
        (self.root / "elsewhere").mkdir()
        before = self.tree()
        self.assertEqual(chi.create_home(self.owner / "x", self.home), "owner-home")
        self.assertEqual(chi.create_home(self.codex_home, self.home), "invalid-home")
        self.assertEqual(self.tree(), before)

    def test_an_unreadable_owner_identity_fails_closed(self):
        with patch.object(chi, "_owner_anchor", side_effect=OSError("denied")):
            self.assertEqual(chi.create_home(self.codex_home, self.home), "owner-home")
        self.assertFalse(self.codex_home.exists())


class OpenHome(Base):
    def test_an_existing_home_opens_as_a_directory_descriptor(self):
        self.codex_home.mkdir(parents=True)
        fd = chi.open_home(self.codex_home, self.home)
        try:
            self.assertTrue(os.path.samestat(os.fstat(fd), self.codex_home.stat()))
        finally:
            os.close(fd)

    def test_the_owner_home_and_an_invalid_home_raise_value_error(self):
        for bad in (self.owner / "x", "relative/home", f"{self.root}/a/../b"):
            with self.assertRaises(ValueError):
                chi.open_home(bad, self.home)

    def test_a_missing_home_raises_and_creates_nothing(self):
        before = self.tree()
        with self.assertRaises(FileNotFoundError):
            chi.open_home(self.codex_home, self.home)
        self.assertEqual(self.tree(), before)

    def test_a_link_component_is_refused(self):
        (self.root / "elsewhere").mkdir()
        (self.root / "workspace").symlink_to(self.root / "elsewhere")
        with self.assertRaises(ValueError):
            chi.open_home(self.codex_home, self.home)

    def test_an_unreadable_owner_identity_raises_value_error(self):
        with patch.object(chi, "_owner_anchor", side_effect=OSError("denied")):
            with self.assertRaisesRegex(ValueError, "owner home unreadable"):
                chi.open_home(self.codex_home, self.home)


class EnsureChildDir(Base):
    def test_a_child_the_owners_codex_links_to_is_refused_unchanged(self):
        self.codex_home.mkdir(parents=True)
        self.owner.symlink_to(self.codex_home / "skills")
        for existing in (False, True):
            with self.subTest(skills_exists=existing):
                if existing:
                    (self.codex_home / "skills").mkdir(exist_ok=True)
                before = self.tree()
                with self.assertRaisesRegex(ValueError, "owner's Codex home"):
                    chi.ensure_child_dir(self.codex_home, "skills", self.home)
                self.assertEqual(self.tree(), before)

    def test_a_child_matching_the_owners_identity_is_refused(self):
        (self.codex_home / "skills").mkdir(parents=True)
        anchor = os.stat(self.codex_home / "skills")
        with patch.object(chi, "_owner_anchor", return_value=((anchor.st_dev, anchor.st_ino), [])):
            with self.assertRaisesRegex(ValueError, "owner's Codex home"):
                chi.ensure_child_dir(self.codex_home, "skills", self.home)

    def test_a_missing_child_the_owner_would_become_is_refused_before_mkdir(self):
        self.codex_home.mkdir(parents=True)
        home = os.stat(self.codex_home)
        before = self.tree()
        with patch.object(chi, "_owner_anchor",
                          return_value=((home.st_dev, home.st_ino), ["Skills"])):
            with self.assertRaisesRegex(ValueError, "owner's Codex home"):
                chi.ensure_child_dir(self.codex_home, "skills", self.home)
        self.assertEqual(self.tree(), before)

    def test_an_owner_identity_unreadable_after_the_open_fails_closed(self):
        self.codex_home.mkdir(parents=True)
        real = chi._owner_anchor(self.home)
        before = self.tree()
        with patch.object(chi, "_owner_anchor", side_effect=[real, OSError("denied")]):
            with self.assertRaisesRegex(ValueError, "owner home unreadable"):
                chi.ensure_child_dir(self.codex_home, "skills", self.home)
        self.assertEqual(self.tree(), before)

    def test_a_plain_child_is_created(self):
        self.codex_home.mkdir(parents=True)
        self.assertEqual(chi.ensure_child_dir(self.codex_home, "skills", self.home), "ok")
        self.assertTrue((self.codex_home / "skills").is_dir())


class DesktopLayout(Base):
    COPIED = ("scripts/sutando-config.sh", "scripts/python-binary.sh",
              "src/sutando_config.py", "sutando.config.json")

    def test_the_codex_home_the_desktop_configures_is_all_real_directories_and_links(self):
        app = self.root / "app-support"
        engine = app / "engine"
        checkout = engine / "sutando"
        for rel in self.COPIED:
            (checkout / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(REPO / rel, checkout / rel)
        (checkout / "src" / "__init__.py").touch()
        (engine / "AGENTS.md").write_text("host\n")
        workspace = app / "workspace"
        workspace.mkdir()
        (checkout / "workspace").symlink_to("../../workspace")
        # The layer the desktop writes for its app-private Codex home.
        (workspace / "sutando.config.local.json").write_text(json.dumps({"core_config_dirs": [
            {"env_name": "CLAUDE_CONFIG_DIR", "id": "claude-default", "synced": True,
             "type": "claude", "value": "${WORKSPACE_DIR}/.claude-sutando"},
            {"env_name": "CODEX_HOME", "id": "codex-app", "synced": False,
             "type": "codex", "value": "${WORKSPACE_DIR}/.codex-sutando"}]}))
        env = {k: v for k, v in os.environ.items()
               if not k.startswith(("SUTANDO_", "CODEX_", "CLAUDE_"))}
        env.update({"HOME": str(self.home), "SUTANDO_APP_SUPPORT": str(app)})
        out = subprocess.run(["bash", str(checkout / "scripts/sutando-config.sh"),
                              "core-config-dir-value", "codex"],
                             env=env, capture_output=True, text=True, timeout=60)
        self.assertEqual(out.returncode, 0, out.stderr)
        value = Path(out.stdout.strip())
        self.assertEqual(value, workspace / ".codex-sutando")
        value.mkdir()  # as start-cli.sh does before the helper runs
        for prefix in (value, *value.parents):
            self.assertFalse(os.path.islink(prefix), f"{prefix} is a symlink")
        self.assertEqual(chi.ensure_linked(value, checkout, self.home), "linked")
        self.assertEqual((value / "AGENTS.md").read_text(), "host\n")


class Cli(Base):
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
        with patch.object(chi, "ensure_linked", side_effect=OSError(errno.EIO, "I/O error")):
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
