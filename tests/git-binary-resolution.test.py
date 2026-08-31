#!/usr/bin/env python3
"""Regression test: git must be RESOLVED, never hardcoded to /usr/bin/git.

Why this exists
---------------
On macOS `/usr/bin/git` is the Xcode Command Line Tools shim, not git. The file
exists on every Mac whether or not the tools are installed; invoking it without
them pops the modal "install command line developer tools" dialog and returns
nothing. It is one inode hardlinked across git / python3 / swiftc / clang / gcc
/ make.

Two call sites invoked it by ABSOLUTE path:

    src/health-check.py   _file_unchanged_since()          (git log / git diff)
    src/health-check.py   check_live_checkout_branch()     (git branch, bare `git`)
    src/agent-api.py      GET /activity                    (git log --oneline)

Absolute means PATH cannot shadow it, so a user who installs a real git
(Homebrew, git-scm.com, a static build) still gets the dialog. And health-check
runs on a timer, so the dialog re-appears indefinitely on a clean machine.

What is asserted
----------------
1. Source-tied: neither call site still carries the `/usr/bin/git` literal.
   This is the fails-before / passes-after guard — it fails at the parent
   commit and passes at HEAD.
2. `select_git` ordering, including the case that matters: on Darwin, when the
   only git on PATH IS the shim and the tools are absent, the answer is None
   (degrade) rather than the shim (dialog).
3. `developer_tools_installed` maps the `xcode-select -p` exit status, and
   fails closed when the probe itself cannot run.

Run: python3 tests/git-binary-resolution.test.py
"""
from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

REPO = Path(__file__).resolve().parent.parent
SRC = REPO / "src"

_spec = importlib.util.spec_from_file_location("git_binary", SRC / "git_binary.py")
git_binary = importlib.util.module_from_spec(_spec)
sys.modules["git_binary"] = git_binary
_spec.loader.exec_module(git_binary)

SYSTEM_GIT = git_binary.SYSTEM_GIT

# health-check.py is imported for the integration case below (the caller whose
# degradation path this fix relies on). Banner suppressed so the suite output
# stays readable; CLAUDE_CONFIG_DIR isolated so importing a src/ module never
# reads or writes the host's real config (the rule scripts/lint-hermetic-bridge-
# tests enforces, #2429).
os.environ.setdefault("SUTANDO_SUPPRESS_CCD_FALLBACK_BANNER", "1")
os.environ.setdefault("CLAUDE_CONFIG_DIR", str(REPO / "workspace" / ".claude-sutando"))

_hc_spec = importlib.util.spec_from_file_location("health_check", SRC / "health-check.py")
health_check = importlib.util.module_from_spec(_hc_spec)
sys.modules["health_check"] = health_check
_hc_spec.loader.exec_module(health_check)


class _Proc:
    """Minimal stand-in for a CompletedProcess (only returncode is read)."""

    def __init__(self, returncode: int) -> None:
        self.returncode = returncode


class NoHardcodedSystemGit(unittest.TestCase):
    """The regression itself. Fails at the parent commit, passes at HEAD."""

    CALL_SITES = ("health-check.py", "agent-api.py")

    def test_call_sites_do_not_hardcode_the_clt_shim(self):
        for name in self.CALL_SITES:
            path = SRC / name
            # Report line numbers, not the file body: assertNotIn on a whole
            # source file prints the entire container on failure (~650KB here),
            # which buries the finding it is supposed to surface.
            hits = [
                f"  {name}:{n}: {line.strip()}"
                for n, line in enumerate(
                    path.read_text(encoding="utf-8").splitlines(), start=1
                )
                if f'"{SYSTEM_GIT}"' in line
            ]
            with self.subTest(source=name):
                self.assertEqual(
                    hits,
                    [],
                    f"\n{name} still hardcodes {SYSTEM_GIT} — that is the CLT "
                    "shim, and an absolute path PATH cannot shadow.\n"
                    "Use resolve_git() from src/git_binary.py.\n"
                    + "\n".join(hits),
                )

    def test_call_sites_import_the_resolver(self):
        for name in self.CALL_SITES:
            path = SRC / name
            # assertTrue on a precomputed bool, not assertIn on the file body —
            # same reason as above: keep the failure message readable.
            imports_resolver = "from git_binary import git_argv" in path.read_text(
                encoding="utf-8"
            )
            with self.subTest(source=name):
                self.assertTrue(
                    imports_resolver,
                    f"{name} does not import git_argv from src/git_binary.py",
                )


class SelectGitOrdering(unittest.TestCase):
    def _never_called(self):
        self.fail("clt_installed probed when a non-shim git was already found")

    def test_nothing_on_path_is_none(self):
        self.assertIsNone(
            git_binary.select_git([], is_darwin=True, clt_installed=lambda: True)
        )

    def test_non_darwin_uses_path_result_directly(self):
        # No shim outside macOS, so the system path is a real git there.
        self.assertEqual(
            git_binary.select_git(
                [SYSTEM_GIT], is_darwin=False, clt_installed=self._never_called
            ),
            SYSTEM_GIT,
        )

    def test_non_shim_git_wins_without_probing(self):
        # A Homebrew/standalone git short-circuits: xcode-select is never spawned.
        brew_git = str(REPO / "fixture-bin" / "git")
        self.assertEqual(
            git_binary.select_git(
                [brew_git], is_darwin=True, clt_installed=self._never_called
            ),
            brew_git,
        )

    def test_shim_without_developer_tools_degrades_to_none(self):
        """The clean-VM case — the whole point of the fix."""
        self.assertIsNone(
            git_binary.select_git(
                [SYSTEM_GIT], is_darwin=True, clt_installed=lambda: False
            )
        )

    def test_a_later_real_git_beats_a_stub_first_path(self):
        """A stub-first PATH must not hide a real git further along.

        `shutil.which` returns only the FIRST match, so the previous
        implementation handed back the stub and returned None on a no-CLT host —
        contradicting this module's own stated order (@john-the-dev, #2469).
        Service PATHs routinely put /usr/bin ahead of a real install.
        """
        real = "/usr/fake/brew/bin/git"
        picked = git_binary.select_git(
            [SYSTEM_GIT, real],
            is_darwin=True,
            clt_installed=self._never_called,   # must not even be consulted
            realpath=lambda p: p,
        )
        self.assertEqual(picked, real)

    def test_stub_is_remembered_as_a_fallback_not_discarded(self):
        """Stub first, no other candidate, CLT present -> the stub is usable."""
        picked = git_binary.select_git(
            [SYSTEM_GIT], is_darwin=True, clt_installed=lambda: True,
            realpath=lambda p: p,
        )
        self.assertEqual(picked, SYSTEM_GIT)

    def test_shim_with_developer_tools_is_usable(self):
        self.assertEqual(
            git_binary.select_git(
                [SYSTEM_GIT], is_darwin=True, clt_installed=lambda: True
            ),
            SYSTEM_GIT,
        )


class PathCandidates(unittest.TestCase):
    def test_returns_every_executable_in_path_order(self):
        found = git_binary.path_candidates(
            "git", path_env="/usr/fake/a:/usr/fake/b",
            is_exec=lambda p: p in ("/usr/fake/a/git", "/usr/fake/b/git"),
        )
        self.assertEqual(found, ["/usr/fake/a/git", "/usr/fake/b/git"])

    def test_skips_empty_path_entries(self):
        self.assertEqual(
            git_binary.path_candidates("git", path_env="::", is_exec=lambda p: False),
            [],
        )

    def test_skips_non_executables(self):
        found = git_binary.path_candidates(
            "git", path_env="/usr/fake/a:/usr/fake/b",
            is_exec=lambda p: p == "/usr/fake/b/git",
        )
        self.assertEqual(found, ["/usr/fake/b/git"])


class DeveloperToolsProbe(unittest.TestCase):
    def test_zero_exit_means_installed(self):
        self.assertTrue(
            git_binary.developer_tools_installed(run=lambda *a, **k: _Proc(0))
        )

    def test_nonzero_exit_means_absent(self):
        self.assertFalse(
            git_binary.developer_tools_installed(run=lambda *a, **k: _Proc(1))
        )

    def test_probe_failure_fails_closed(self):
        def _raise(*a, **k):
            raise OSError("xcode-select missing")

        self.assertFalse(git_binary.developer_tools_installed(run=_raise))

    def test_probe_timeout_fails_closed(self):
        def _timeout(*a, **k):
            raise subprocess.TimeoutExpired(cmd="xcode-select", timeout=5)

        self.assertFalse(git_binary.developer_tools_installed(run=_timeout))


class ResolveGit(unittest.TestCase):
    """`resolve_git` wires PATH lookup + the probe together (and caches)."""

    def setUp(self):
        git_binary.reset_cache_for_tests()

    def tearDown(self):
        git_binary.reset_cache_for_tests()

    def test_returns_path_result_when_a_real_git_exists(self):
        real = str(REPO / "fixture-bin" / "git")
        with patch.object(git_binary, "path_candidates", return_value=[real]):
            self.assertEqual(git_binary.resolve_git(), real)

    def test_returns_none_when_path_has_nothing(self):
        with patch.object(git_binary, "path_candidates", return_value=[]):
            self.assertIsNone(git_binary.resolve_git())

    def test_a_positive_answer_is_cached(self):
        real = str(REPO / "fixture-bin" / "git")
        with patch.object(git_binary, "path_candidates", return_value=[real]) as which:
            git_binary.resolve_git()
            git_binary.resolve_git()
        self.assertEqual(which.call_count, 1, "resolve_git re-probed PATH")

    def test_a_negative_answer_is_NOT_cached(self):
        """agent-api.py is a long-lived serve_forever() process.

        If a None were memoised, a user installing the developer tools mid-run
        would leave GET /activity permanently empty until someone restarted the
        service — while health-check on the same host, being re-exec'd each run,
        reported git as fine. (@sonichi, reviewing #2469.)
        """
        with patch.object(git_binary, "path_candidates", return_value=[]) as which:
            self.assertIsNone(git_binary.resolve_git())
            self.assertIsNone(git_binary.resolve_git())
        self.assertEqual(which.call_count, 2, "a negative answer was cached")

    def test_install_after_start_is_picked_up(self):
        """The exact install-after-start ordering a post-restart test cannot reach."""
        real = str(REPO / "fixture-bin" / "git")
        with patch.object(git_binary, "path_candidates", return_value=[]):
            self.assertIsNone(git_binary.resolve_git())
        # ...user installs the tools; no restart.
        with patch.object(git_binary, "path_candidates", return_value=[real]):
            self.assertEqual(git_binary.resolve_git(), real)


class GitArgv(unittest.TestCase):
    def test_builds_argv_when_git_is_available(self):
        real = str(REPO / "fixture-bin" / "git")
        with patch.object(git_binary, "resolve_git", return_value=real):
            self.assertEqual(
                git_binary.git_argv("log", "-1"), [real, "log", "-1"]
            )

    def test_raises_when_git_is_unavailable(self):
        with patch.object(git_binary, "resolve_git", return_value=None):
            with self.assertRaises(git_binary.GitUnavailable):
                git_binary.git_argv("log", "-1")

    def test_unavailable_is_an_oserror(self):
        """Call sites absorb it through the OSError handling they already have.

        If this ever stops being true, both callers silently start propagating
        a hard error out of an optional-provenance path.
        """
        self.assertTrue(issubclass(git_binary.GitUnavailable, OSError))


class HealthCheckDegradesWithoutGit(unittest.TestCase):
    """The caller-side contract: no git ⇒ 'can't tell', not a crash or dialog."""

    def test_returns_false_when_git_is_unavailable(self):
        with patch.object(health_check, "git_argv", side_effect=git_binary.GitUnavailable("no git")):
            self.assertFalse(
                health_check._file_unchanged_since(SRC / "git_binary.py", 0.0)
            )

    def test_live_checkout_branch_degrades_without_git(self):
        """check_live_checkout_branch is registered UNCONDITIONALLY.

        It called bare `git`, which PATH resolves to /usr/bin/git — the stub —
        on a Mac without developer tools, so the modal fired before the
        return-code degradation below could run. It ran on every health pass.
        The source-tied scan above only covers the two originally-changed call
        sites, so it passed while this activated caller stayed broken
        (@john-the-dev, reviewing #2469).
        """
        spawned = []
        real_run = subprocess.run

        def spy(argv, *a, **k):
            spawned.append(argv[0] if isinstance(argv, list) else argv)
            return real_run(argv, *a, **k)

        git_binary.reset_cache_for_tests()
        try:
            with patch.object(git_binary, "path_candidates", return_value=[]), \
                 patch.object(subprocess, "run", spy):
                result = health_check.check_live_checkout_branch()
        finally:
            git_binary.reset_cache_for_tests()
        # Assert the BEHAVIOUR, not the wording. #2471 landed a second
        # degradation path in this probe (resolver returns None -> return before
        # the try:), so pinning one path's detail string made this test fail on a
        # merge that had made the degradation strictly earlier and safer. What
        # must hold is: the probe stays non-fatal AND spawns nothing — a spawn is
        # what raises the CLT modal, and only counting execs can see that.
        self.assertEqual(result["status"], "ok")
        self.assertEqual(spawned, [], "probe spawned a process with no runnable git")
        self.assertRegex(result["detail"], r"(?i)git not runnable|no runnable git")

    def test_checkout_is_canonical_degrades_without_git(self):
        """`_checkout_is_canonical` must resolve git, not invoke a bare `git`."""
        spawned = []
        real_run = subprocess.run

        def spy(argv, *a, **k):
            spawned.append(argv[0] if isinstance(argv, list) else argv)
            return real_run(argv, *a, **k)

        git_binary.reset_cache_for_tests()
        try:
            with patch.object(git_binary, "path_candidates", return_value=[]), \
                 patch.object(subprocess, "run", spy):
                ok, reason = health_check._checkout_is_canonical(REPO)
        finally:
            git_binary.reset_cache_for_tests()
        self.assertEqual(spawned, [], "spawned a process with no runnable git — the CLT modal path")
        self.assertFalse(ok, "must fail closed when git state is unreadable")
        self.assertIn("unreadable", reason)

    def test_runs_against_a_real_git_without_raising(self):
        """Exercises both git invocations on a host that does have git.

        Asserts only the type: the value depends on the checkout's commit times,
        which a test must not pin. The point is that the argv built by git_argv
        is accepted by git and neither call raises.
        """
        if git_binary.resolve_git() is None:
            self.skipTest("no runnable git on this host")
        result = health_check._file_unchanged_since(SRC / "git_binary.py", 0.0)
        self.assertIsInstance(result, bool)


if __name__ == "__main__":
    unittest.main(verbosity=2)
