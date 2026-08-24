#!/usr/bin/env python3
"""landed_24h must be None, not 0, where merging rewrites the SHA.
Rationale and before/after evidence are in the PR body."""
import importlib.util
import pathlib
import subprocess
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent


def _load():
    spec = importlib.util.spec_from_file_location("di", ROOT / "src" / "daily-insight.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _tmpdir():
    # Teardown is not under test: rmtree of a git worktree can race to ENOTEMPTY on
    # '.git'. Suppression is deliberate and UNBOUNDED — a later fd leak goes quiet too.
    return tempfile.TemporaryDirectory(ignore_cleanup_errors=True)


def _git(repo):
    def g(*a):
        # check=True raises with the exit code only, so a failing fixture reports
        # "returned non-zero exit status 128" and discards git's reason for it.
        p = subprocess.run(["git", "-C", str(repo), *a], capture_output=True, text=True)
        if p.returncode != 0:
            raise AssertionError(
                f"git {' '.join(a)} exited {p.returncode} in {repo}\n"
                f"  stderr: {(p.stderr or '').strip() or '(empty)'}\n"
                f"  stdout: {(p.stdout or '').strip() or '(empty)'}")
    return g


def _repo(td, *, merges, main_commits=30):
    r = pathlib.Path(td) / "r"
    r.mkdir()
    g = _git(r)
    g("init", "-q", "-b", "main")
    g("config", "user.email", "t@example.com")
    g("config", "user.name", "t")
    # Empty commits: these fixtures need a COUNT and whether merge commits exist,
    # never content — and 30 rapid write+add+commit cycles proved fragile on CI.
    for i in range(main_commits):
        g("commit", "-q", "--allow-empty", "-m", f"c{i}")
    if merges:
        # A repo that preserves SHAs on merge: at least one real merge commit.
        g("checkout", "-q", "-b", "side")
        (r / "side").write_text("s")
        g("add", "-A")
        g("commit", "-q", "-m", "side")
        g("checkout", "-q", "main")
        g("merge", "-q", "--no-ff", "-m", "merge side", "side")
    g("update-ref", "refs/remotes/origin/main", "main")
    # Local work that is NOT reachable from origin/main by SHA — exactly what a
    # squash-merged commit looks like, and also what unmerged work looks like.
    g("checkout", "-q", "-b", "wip")
    (r / "w").write_text("w")
    g("add", "-A")
    g("commit", "-q", "-m", "wip")
    return r


def _all_work_on_a_branch(td, main_commits=30):
    """The production shape: every last-24h local commit lives off origin/main."""
    r = pathlib.Path(td) / "r"
    r.mkdir()
    g = _git(r)
    g("init", "-q", "-b", "main")
    g("config", "user.email", "t@example.com")
    g("config", "user.name", "t")
    # One tracked file, because the orphan step below runs `git rm --cached .`
    # and that exits 128 with an empty index; the rest are empty commits.
    (r / "f0").write_text("0")
    g("add", "-A")
    g("commit", "-q", "-m", "c0")
    for i in range(1, main_commits):
        g("commit", "-q", "--allow-empty", "-m", f"c{i}")
    g("update-ref", "refs/remotes/origin/main", "main")
    g("checkout", "-q", "--orphan", "wip")
    g("rm", "-rq", "--cached", ".")
    (r / "w").write_text("w")
    g("add", "-A")
    g("commit", "-q", "-m", "wip")
    g("branch", "-q", "-D", "main")
    return r


class TestLandedUnderSquash(unittest.TestCase):
    def test_the_reported_symptom_none_landed_on_a_squash_repo(self):
        """The reported symptom: "none landed yet" on a day work did land."""
        mod = _load()
        with _tmpdir() as td:
            dev = mod.analyze_dev_activity(repo_root=_all_work_on_a_branch(td))
        self.assertIsNone(dev["landed_24h"], dev)
        line = mod.dev_activity_insight(dev)
        self.assertNotIn("none landed yet", line, line)
        self.assertNotIn("velocity is in review", line, line)

    def test_squash_repo_yields_unknown_not_zero(self):
        mod = _load()
        with _tmpdir() as td:
            repo = _repo(td, merges=False)
            dev = mod.analyze_dev_activity(repo_root=repo)
        self.assertIsNotNone(dev)
        self.assertIsNone(
            dev["landed_24h"],
            "no merge commit in 30 => SHAs are rewritten on merge, so reachability "
            "cannot tell merged from unmerged; the honest answer is unknown")
        line = mod.dev_activity_insight(dev)
        self.assertNotIn("none landed", line, line)
        self.assertIn("landed count unavailable", line, line)

    def test_merge_commit_repo_still_reports_a_real_zero(self):
        """The suppression must not swallow the case reachability CAN answer."""
        mod = _load()
        with _tmpdir() as td:
            repo = _repo(td, merges=True)
            dev = mod.analyze_dev_activity(repo_root=repo)
        self.assertIsNotNone(dev)
        # Exact because only the wip commit is off origin/main — and a measurement
        # because a real merge commit proves reachability tracks landing here.
        self.assertEqual(dev["landed_24h"], dev["commits_24h"] - 1, dev)

    def test_too_little_history_is_unknown_not_proof_of_no_rewriting(self):
        """A depth-1 clone of a squash repo looks identical to a young one here."""
        mod = _load()
        with _tmpdir() as td:
            repo = _repo(td, merges=False, main_commits=2)
            dev = mod.analyze_dev_activity(repo_root=repo)
        self.assertIsNone(dev["landed_24h"],
                          "insufficient history is not positive evidence that the repo "
                          "preserves SHAs — a shallow squash clone reads the same")

    def test_a_shallow_clone_of_a_squash_repo_is_not_read_as_no_rewriting(self):
        """The reviewer's case: depth-1 hides the merge history entirely."""
        mod = _load()
        with _tmpdir() as td:
            src = pathlib.Path(td) / "src"
            src.mkdir()
            g = _git(src)
            g("init", "-q", "-b", "main")
            g("config", "user.email", "t@example.com")
            g("config", "user.name", "t")
            for i in range(30):
                g("commit", "-q", "--allow-empty", "-m", f"c{i}")
            dst = pathlib.Path(td) / "shallow"
            c = subprocess.run(["git", "clone", "-q", "--depth", "1",
                                f"file://{src}", str(dst)], capture_output=True, text=True)
            if c.returncode != 0:
                raise AssertionError(f"git clone exited {c.returncode}\n"
                                     f"  stderr: {(c.stderr or '').strip() or '(empty)'}")
            d = _git(dst)
            d("config", "user.email", "t@example.com")
            d("config", "user.name", "t")
            d("checkout", "-q", "-b", "wip")
            (dst / "w").write_text("w")
            d("add", "-A")
            d("commit", "-q", "-m", "wip")
            dev = mod.analyze_dev_activity(repo_root=dst)
        self.assertIsNone(dev["landed_24h"], dev)
        line = mod.dev_activity_insight(dev)
        self.assertIn("landed count unavailable", line, line)
        self.assertNotIn("none landed", line, line)


class TestDiscriminatorFailsSafe(unittest.TestCase):
    """A failed probe must yield unknown, never a value the caller can act on.
    `assertFalse` cannot pin it — None is falsy too; argument in the PR body."""

    def setUp(self):
        self.mod = _load()

    def _with_run(self, fake):
        real = self.mod.subprocess.run
        self.mod.subprocess.run = fake
        self.addCleanup(lambda: setattr(self.mod.subprocess, "run", real))

    def test_oserror_is_not_read_as_squash(self):
        def raiser(*a, **k):
            raise OSError("git missing")
        self._with_run(raiser)
        self.assertIsNone(self.mod._rewrites_shas_on_merge("/nope", "origin/main"))

    def test_subprocess_error_is_not_read_as_squash(self):
        def raiser(*a, **k):
            raise subprocess.SubprocessError("boom")
        self._with_run(raiser)
        self.assertIsNone(self.mod._rewrites_shas_on_merge("/nope", "origin/main"))

    def test_nonzero_git_is_not_read_as_squash(self):
        class R:
            returncode = 128
            stdout = ""
        self._with_run(lambda *a, **k: R())
        self.assertIsNone(self.mod._rewrites_shas_on_merge("/nope", "origin/main"))

    def test_unparsable_count_is_not_read_as_squash(self):
        class R:
            returncode = 0
            stdout = "not-a-number"
        self._with_run(lambda *a, **k: R())
        self.assertIsNone(self.mod._rewrites_shas_on_merge("/nope", "origin/main"))

    def test_a_probe_failure_yields_unknown_not_a_confident_count(self):
        """End-to-end: a probe that cannot run must not license stating a count."""
        real = self.mod.subprocess.run

        def only_the_probe_fails(cmd, *a, **k):
            if "--count" in cmd and "rev-list" in cmd:
                raise OSError("probe unavailable")
            return real(cmd, *a, **k)
        self._with_run(only_the_probe_fails)
        with _tmpdir() as td:
            dev = self.mod.analyze_dev_activity(repo_root=_all_work_on_a_branch(td))
        self.assertIsNone(dev["landed_24h"],
                          "git errored, so whether reachability tracks landing here is "
                          "unknown; 0 would assert 'none landed' on no evidence")


if __name__ == "__main__":
    unittest.main(verbosity=2)
