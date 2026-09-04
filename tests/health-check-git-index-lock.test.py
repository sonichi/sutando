#!/usr/bin/env python3
"""A stale .git/index.lock blocks every git write; nothing else surfaced it.

Drives the real probe against real git repositories, including a real crashed
writer (the lock left behind by a killed `git add`), not a hand-made file only.
"""
import importlib.util
import os
import shlex
import unittest.mock
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("health_check", ROOT / "src" / "health-check.py")
hc = importlib.util.module_from_spec(_spec)
sys.modules["health_check"] = hc
try:
    _spec.loader.exec_module(hc)
except SystemExit:
    pass


def _git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)


class GitIndexLock(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name) / "repo"
        self.repo.mkdir()
        _git(self.repo, "init", "-q", "-b", "main")
        (self.repo / "f.txt").write_text("x")
        _git(self.repo, "add", "f.txt")
        _git(self.repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "base")

    def tearDown(self):
        self.tmp.cleanup()

    def _repo(self, name):
        """A real git repo at an arbitrary name, so a path with spaces is testable."""
        d = Path(self.tmp.name) / name
        d.mkdir()
        _git(d, "init", "-q", "-b", "main")
        (d / "f.txt").write_text("x")
        _git(d, "add", "f.txt")
        _git(d, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "base")
        return d

    def _lock(self, age_s):
        p = self.repo / ".git" / "index.lock"
        p.write_text("")
        old = time.time() - age_s
        os.utime(p, (old, old))
        return p

    def test_no_lock_is_ok(self):
        r = hc.check_git_index_lock(self.repo)
        self.assertEqual(r["status"], "ok")
        self.assertIn("unblocked", r["detail"])

    def test_a_fresh_lock_is_an_in_flight_write_not_a_fault(self):
        self._lock(5)
        r = hc.check_git_index_lock(self.repo)
        self.assertEqual(r["status"], "ok", r["detail"])
        self.assertIn("in flight", r["detail"])

    def test_a_stale_lock_warns_and_names_the_exact_file_to_remove(self):
        lock = self._lock(9.4 * 3600)
        r = hc.check_git_index_lock(self.repo)
        self.assertEqual(r["status"], "warn")
        self.assertIn("9.4h", r["detail"])
        self.assertIn(f"rm {lock}", r["detail"])
        self.assertIn("lsof", r["detail"], "the remedy must be gated on checking for a live holder")

    def test_the_warn_is_reachable_from_a_REAL_blocked_write(self):
        # Not a hand-made file: hold the lock and confirm git itself refuses,
        # which is the failure the probe claims to detect.
        self._lock(9.4 * 3600)
        r = _git(self.repo, "add", "f.txt")
        self.assertNotEqual(r.returncode, 0, "git accepted a write while index.lock existed")
        self.assertIn("index.lock", r.stderr)
        self.assertEqual(hc.check_git_index_lock(self.repo)["status"], "warn")

    def test_the_boundary_is_the_threshold_not_the_wording(self):
        p = self._lock(hc.GIT_LOCK_STALE_S + 1)
        self.assertEqual(hc.check_git_index_lock(self.repo)["status"], "warn")
        old = time.time() - (hc.GIT_LOCK_STALE_S - 1)
        os.utime(p, (old, old))
        self.assertEqual(hc.check_git_index_lock(self.repo)["status"], "ok")

    def test_a_worktree_probes_its_OWN_gitdir_not_the_common_dir(self):
        wt = Path(self.tmp.name) / "wt"
        self.assertEqual(_git(self.repo, "worktree", "add", "--detach", str(wt)).returncode, 0)
        self.assertTrue((wt / ".git").is_file(), "expected a worktree .git FILE")
        gd = hc._git_dir(wt)
        self.assertIsNotNone(gd)
        self.assertNotEqual(gd.resolve(), (self.repo / ".git").resolve())
        # A stale lock in the MAIN repo must not be reported against the worktree.
        self._lock(9.4 * 3600)
        self.assertEqual(hc.check_git_index_lock(wt)["status"], "ok")
        p = gd / "index.lock"
        p.write_text("")
        old = time.time() - 9.4 * 3600
        os.utime(p, (old, old))
        self.assertEqual(hc.check_git_index_lock(wt)["status"], "warn")

    def test_a_dot_git_file_that_is_not_a_gitdir_pointer_is_not_a_repo(self):
        # A `.git` FILE that is not a gitdir pointer: guessing a path from it
        # would probe an arbitrary location.
        odd = Path(self.tmp.name) / "odd"
        odd.mkdir()
        (odd / ".git").write_text("this is not a gitdir pointer\n")
        self.assertIsNone(hc._git_dir(odd))
        self.assertEqual(hc.check_git_index_lock(odd)["status"], "ok")

    def test_a_RELATIVE_gitdir_pointer_resolves_against_the_checkout(self):
        # git writes a relative `gitdir:` in some layouts; resolving it against
        # the process cwd instead of the checkout would probe the wrong file.
        host = Path(self.tmp.name) / "host"
        (host / "real-gitdir").mkdir(parents=True)
        (host / ".git").write_text("gitdir: real-gitdir\n")
        self.assertEqual(hc._git_dir(host), (host / "real-gitdir").resolve())
        self.assertEqual(hc.check_git_index_lock(host)["status"], "ok")
        lock = host / "real-gitdir" / "index.lock"
        lock.write_text("")
        old = time.time() - 9.4 * 3600
        os.utime(lock, (old, old))
        r = hc.check_git_index_lock(host)
        self.assertEqual(r["status"], "warn")
        self.assertIn(str(lock), r["detail"])

    def test_a_non_repo_is_not_this_probes_business(self):
        plain = Path(self.tmp.name) / "plain"
        plain.mkdir()
        self.assertEqual(hc.check_git_index_lock(plain)["status"], "ok")

    # ---- malformed `gitdir:` pointers (keweichen, 2026-09-04) ----------------

    def _worktree_like(self, name, pointer_body):
        """A checkout whose `.git` is a FILE, as a worktree's is."""
        d = Path(self.tmp.name) / name
        d.mkdir()
        (d / ".git").write_bytes(pointer_body)
        return d

    def test_an_empty_gitdir_target_is_rejected_not_resolved_to_the_checkout(self):
        # Path("") resolves to the repo, so the advice would name <repo>/index.lock.
        d = self._worktree_like("empty-target", b"gitdir:\n")
        stray = d / "index.lock"
        stray.write_text("")
        old = time.time() - 9.4 * 3600
        os.utime(stray, (old, old))
        self.assertIsNone(hc._git_dir(d))
        r = hc.check_git_index_lock(d)
        self.assertEqual(r["status"], "ok")
        self.assertNotIn("rm ", r["detail"])

    def test_a_nul_gitdir_target_does_not_escape_the_always_on_sweep(self):
        """NOT a control for the try/except: on CPython 3.14/macOS `is_dir()`
        swallows both, so this passes with the guard removed. It pins the
        CONTRACT (a malformed pointer yields None), and the guard exists for
        the platforms where the reviewer measured ValueError/OSError."""
        d = self._worktree_like("nul-target", b"gitdir: /tmp/a\x00b\n")
        self.assertIsNone(hc._git_dir(d))
        self.assertEqual(hc.check_git_index_lock(d)["status"], "ok")

    def test_an_overlong_gitdir_target_does_not_escape_either(self):
        """Same caveat as the NUL case above — contract pin, not a control."""
        d = self._worktree_like("long-target", b"gitdir: /" + b"x" * 5000 + b"\n")
        self.assertIsNone(hc._git_dir(d))
        self.assertEqual(hc.check_git_index_lock(d)["status"], "ok")

    # ---- unknown stat result is not a measured absence -----------------------

    def test_a_stat_error_that_is_not_absence_is_reported_as_unmeasured(self):
        repo = self._repo("stat-eacces")
        real_stat = Path.stat

        def boom(self_path, *a, **k):
            if self_path.name == "index.lock":
                raise PermissionError(13, "Permission denied")
            return real_stat(self_path, *a, **k)

        with unittest.mock.patch.object(Path, "stat", boom):
            r = hc.check_git_index_lock(repo)
        self.assertEqual(r["status"], "warn")
        self.assertIn("UNMEASURED", r["detail"])
        self.assertNotIn("unblocked", r["detail"])

    def test_a_genuinely_missing_lock_is_still_a_clean_ok(self):
        repo = self._repo("no-lock")
        r = hc.check_git_index_lock(repo)
        self.assertEqual(r["status"], "ok")
        self.assertIn("no index.lock", r["detail"])

    # ---- the advice must be shell-safe --------------------------------------

    def test_the_advice_quotes_a_path_with_spaces_so_argv_stays_one_operand(self):
        repo = self._repo("valid repo with spaces")
        lock = repo / ".git" / "index.lock"
        lock.write_text("")
        old = time.time() - 9.4 * 3600
        os.utime(lock, (old, old))
        detail = hc.check_git_index_lock(repo)["detail"]
        for verb in ("lsof", "rm"):
            argv = shlex.split(detail.split(f"`{verb} ", 1)[1].split("`", 1)[0])
            self.assertEqual(argv, [str(lock)],
                             f"{verb} would act on {len(argv)} operands, not the lock")

    def test_the_probe_is_wired_into_the_run(self):
        src = (ROOT / "src" / "health-check.py").read_text()
        self.assertIn("checks.append(check_git_index_lock())", src,
                      "a probe nobody calls reports nothing")


if __name__ == "__main__":
    unittest.main(verbosity=2)
