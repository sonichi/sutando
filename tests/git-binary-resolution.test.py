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

    src/health-check.py   _file_unchanged_since()  (git log / git diff)
    src/agent-api.py      GET /activity            (git log --oneline)

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
import subprocess
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SRC = REPO / "src"

_spec = importlib.util.spec_from_file_location("git_binary", SRC / "git_binary.py")
git_binary = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(git_binary)

SYSTEM_GIT = git_binary.SYSTEM_GIT


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
            imports_resolver = "from git_binary import resolve_git" in path.read_text(
                encoding="utf-8"
            )
            with self.subTest(source=name):
                self.assertTrue(
                    imports_resolver,
                    f"{name} does not import resolve_git from src/git_binary.py",
                )


class SelectGitOrdering(unittest.TestCase):
    def _never_called(self):
        self.fail("clt_installed probed when a non-shim git was already found")

    def test_nothing_on_path_is_none(self):
        self.assertIsNone(
            git_binary.select_git(None, is_darwin=True, clt_installed=lambda: True)
        )

    def test_non_darwin_uses_path_result_directly(self):
        # No shim outside macOS, so the system path is a real git there.
        self.assertEqual(
            git_binary.select_git(
                SYSTEM_GIT, is_darwin=False, clt_installed=self._never_called
            ),
            SYSTEM_GIT,
        )

    def test_non_shim_git_wins_without_probing(self):
        # A Homebrew/standalone git short-circuits: xcode-select is never spawned.
        brew_git = str(REPO / "fixture-bin" / "git")
        self.assertEqual(
            git_binary.select_git(
                brew_git, is_darwin=True, clt_installed=self._never_called
            ),
            brew_git,
        )

    def test_shim_without_developer_tools_degrades_to_none(self):
        """The clean-VM case — the whole point of the fix."""
        self.assertIsNone(
            git_binary.select_git(
                SYSTEM_GIT, is_darwin=True, clt_installed=lambda: False
            )
        )

    def test_shim_with_developer_tools_is_usable(self):
        self.assertEqual(
            git_binary.select_git(
                SYSTEM_GIT, is_darwin=True, clt_installed=lambda: True
            ),
            SYSTEM_GIT,
        )


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


if __name__ == "__main__":
    unittest.main(verbosity=2)
