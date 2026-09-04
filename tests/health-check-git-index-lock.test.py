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
        # git reports the fully resolved gitdir (/var -> /private/var on macOS),
        # so the advice names the unambiguous path; compare resolved to resolved.
        self.assertIn(f"rm {lock.resolve()}", r["detail"])
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

    def test_a_REAL_worktree_probes_its_own_gitdir_not_the_common_dir(self):
        # Built with `git worktree add`, not a hand-written pointer: the probe
        # asks git, so only a checkout git recognises resolves at all.
        host = Path(self.tmp.name) / "wt"
        _git(self.repo, "worktree", "add", "-q", "--detach", str(host))
        gitdir = hc._git_dir(host)
        self.assertIsNotNone(gitdir)
        self.assertNotEqual(gitdir, (self.repo / ".git").resolve(),
                            "a worktree must not resolve to the common dir")
        self.assertEqual(hc.check_git_index_lock(host)["status"], "ok")
        lock = gitdir / "index.lock"
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

    # ---- malformed `gitdir:` pointers ---------------------------------------

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

    # ---- the answer must belong to THIS checkout ------------------------------

    def _stale(self, lock):
        lock.write_text("")
        old = time.time() - 9 * 3600
        os.utime(lock, (old, old))

    def test_an_inherited_GIT_DIR_does_not_make_git_answer_for_another_repo(self):
        """`git -C` does not neutralise GIT_DIR: inherited, it points the whole
        resolver at another repository. False-clean AND wrong-target."""
        a, b = self._repo("a"), self._repo("b")
        self._stale(a / ".git" / "index.lock")
        with unittest.mock.patch.dict(os.environ, {"GIT_DIR": str(b / ".git")}):
            r = hc.check_git_index_lock(a)
        self.assertEqual(r["status"], "warn")
        self.assertIn(str(a / ".git" / "index.lock"), r["detail"])
        os.unlink(a / ".git" / "index.lock")
        self._stale(b / ".git" / "index.lock")
        with unittest.mock.patch.dict(os.environ, {"GIT_DIR": str(b / ".git")}):
            r = hc.check_git_index_lock(a)
        self.assertEqual(r["status"], "ok")
        self.assertNotIn(str(b), r["detail"])

    def test_the_stripped_set_covers_gits_own_local_env_list(self):
        """Git enumerates its repository-selection variables itself; the
        shipped set must be a superset of what the installed git reports."""
        out = _git(self.repo, "rev-parse", "--local-env-vars")
        self.assertEqual(out.returncode, 0, out.stderr)
        theirs = set(out.stdout.split())
        self.assertTrue(theirs)
        self.assertLessEqual(theirs, hc.GIT_REPO_SELECTION_ENV,
                             f"git names variables the probe does not strip: "
                             f"{sorted(theirs - hc.GIT_REPO_SELECTION_ENV)}")

    def test_a_gitdir_with_a_trailing_space_is_probed_where_git_says_it_is(self):
        """Git returns the significant trailing space; `.strip()` turned it into
        the stripped SIBLING, which is a different existing directory."""
        wt = Path(self.tmp.name) / "wt"
        wt.mkdir()
        real = Path(self.tmp.name) / "gitdir "
        r = _git(wt, "init", "-q", "-b", "main", "--separate-git-dir", str(real))
        self.assertEqual(r.returncode, 0, r.stderr)
        (wt / "f.txt").write_text("x")
        _git(wt, "add", "f.txt")
        sibling = Path(self.tmp.name) / "gitdir"
        sibling.mkdir()
        self._stale(sibling / "index.lock")          # unrelated: wrong-target bait
        r = hc.check_git_index_lock(wt)
        self.assertEqual(r["status"], "ok", r["detail"])
        self._stale(real / "index.lock")             # the real lock: false-clean bait
        r = hc.check_git_index_lock(wt)
        self.assertEqual(r["status"], "warn")
        # git reports the realpath (/private/var on macOS); the space survives.
        self.assertIn(shlex.quote(str(real.resolve() / "index.lock")), r["detail"])
        self.assertNotIn(str(sibling.resolve() / "index.lock"), r["detail"])

    # ---- unknown stat result is not a measured absence -----------------------

    def test_a_stat_error_that_is_not_absence_is_reported_as_unmeasured(self):
        repo = self._repo("stat-eacces")
        real_stat = Path.stat

        def boom(self_path, *a, **k):
            if self_path.name == "index.lock":
                raise PermissionError(13, "Permission denied")
            return real_stat(self_path, *a, **k)

        with unittest.mock.patch.object(Path, "lstat", boom):
            r = hc.check_git_index_lock(repo)
        self.assertEqual(r["status"], "warn")
        self.assertIn("UNMEASURED", r["detail"])
        self.assertNotIn("unblocked", r["detail"])

    def test_every_caught_stat_error_class_is_reported_not_raised(self):
        """The handler catches ValueError and RuntimeError too; formatting via
        the OSError-only `.strerror` raised AttributeError out of the sweep."""
        repo = self._repo("stat-classes")
        real_stat = Path.lstat
        for exc in (RuntimeError("symlink loop"), ValueError("embedded null"),
                    PermissionError(13, "Permission denied")):
            def boom(self_path, *a, _e=exc, **k):
                if self_path.name == "index.lock":
                    raise _e
                return real_stat(self_path, *a, **k)
            with unittest.mock.patch.object(Path, "lstat", boom):
                r = hc.check_git_index_lock(repo)
            self.assertEqual(r["status"], "warn", type(exc).__name__)
            self.assertIn("UNMEASURED", r["detail"])
            self.assertIn(type(exc).__name__, r["detail"])
            self.assertIn(str(exc.args[-1]), r["detail"])

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
            self.assertEqual(argv, [str(lock.resolve())],
                             f"{verb} would act on {len(argv)} operands, not the lock")

    def test_the_probe_is_wired_into_the_run(self):
        src = (ROOT / "src" / "health-check.py").read_text()
        self.assertIn("checks.append(check_git_index_lock())", src,
                      "a probe nobody calls reports nothing")



class TargetMustBelongToThisCheckout(unittest.TestCase):
    """keweichen's blockers: a pointer git does not recognise must not produce
    `rm` advice, and neither resolution nor a dangling/future lock may read clean."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _worktree(self, pointer: str) -> Path:
        co = self.root / "checkout"
        co.mkdir()
        (co / ".git").write_text(pointer)
        return co

    def test_pointer_to_an_unrelated_directory_gets_no_removal_advice(self):
        unrelated = self.root / "unrelated"
        unrelated.mkdir()
        (unrelated / "index.lock").write_text("")
        os.utime(unrelated / "index.lock", (0, 0))
        r = hc.check_git_index_lock(self._worktree(f"gitdir: {unrelated}"))
        self.assertNotIn("rm ", r["detail"],
                         "advised removing a lock git never associated with this checkout")

    def test_symlink_loop_target_does_not_escape_the_probe(self):
        co = self._worktree("gitdir: loop")
        loop = co / "loop"
        os.symlink(loop, loop)  # self-referential: Path.resolve() raises RuntimeError
        r = hc.check_git_index_lock(co)   # must return, not raise
        self.assertIn(r["status"], ("ok", "warn"))

    def test_a_dangling_lock_symlink_is_not_absent(self):
        co = self.root / "real"
        subprocess.run(["git", "init", "-q", str(co)], check=True, capture_output=True)
        gd = co / ".git"
        link = gd / "index.lock"
        os.symlink(gd / "nonexistent-target", link)
        stale = time.time() - 9.4 * 3600
        os.utime(link, (stale, stale), follow_symlinks=False)
        self.assertTrue(link.is_symlink())
        self.assertFalse(link.exists(), "control: stat() through the link sees it as absent")
        add = subprocess.run(["git", "-C", str(co), "add", "-A"], capture_output=True)
        self.assertNotEqual(add.returncode, 0, "control: git really is blocked by the entry")
        r = hc.check_git_index_lock(co)
        self.assertEqual(r["status"], "warn")
        self.assertNotIn("unblocked", r["detail"],
                         "a dangling entry blocks git; stat() would have called it absent")

    def test_git_unavailable_fails_closed_with_no_remedy(self):
        """keweichen: when association cannot be established, no `rm` advice."""
        import subprocess as sp
        real = sp.run

        def boom(argv, *a, **k):
            if "rev-parse" in argv:
                raise OSError(2, "No such file or directory: git")
            return real(argv, *a, **k)

        co = self.root / "nogit"
        subprocess.run(["git", "init", "-q", str(co)], check=True, capture_output=True)
        (co / ".git" / "index.lock").write_text("")
        with unittest.mock.patch.object(sp, "run", boom):
            r = hc.check_git_index_lock(co)
        self.assertEqual(r["status"], "ok")
        self.assertIn("not a git checkout", r["detail"])
        self.assertNotIn("rm ", r["detail"])

    def test_an_unusable_resolver_answer_fails_closed(self):
        """A path git returns that cannot even be inspected is not a gitdir."""
        real_is_dir = Path.is_dir

        def boom(self_path, *a, **k):
            if self_path.name == ".git":
                raise RuntimeError("Symlink loop")
            return real_is_dir(self_path, *a, **k)

        co = self.root / "loopy"
        subprocess.run(["git", "init", "-q", str(co)], check=True, capture_output=True)
        with unittest.mock.patch.object(Path, "is_dir", boom):
            r = hc.check_git_index_lock(co)
        self.assertEqual(r["status"], "ok")
        self.assertNotIn("rm ", r["detail"])

    def test_a_future_dated_lock_is_unmeasured_not_in_flight(self):
        co = self.root / "future"
        subprocess.run(["git", "init", "-q", str(co)], check=True, capture_output=True)
        lock = co / ".git" / "index.lock"
        lock.write_text("")
        future = time.time() + 86400
        os.utime(lock, (future, future))
        r = hc.check_git_index_lock(co)
        self.assertEqual(r["status"], "warn")
        self.assertIn("FUTURE", r["detail"])
        self.assertNotIn("in flight", r["detail"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
