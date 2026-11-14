#!/usr/bin/env python3
"""Open PRs and Repo Stats query Sutando's GitHub repo only from a git-attached checkout.

Run: python3 tests/session-handoff-repo-queries-dev-only.test.py

Source-tied: the DEV_CHECKOUT assignment and both sections are extracted from the
live script and executed with a stub `gh`, because running the whole script would
overwrite the real session-state.md. The packaged engine ships without .git, so a
regular owner's agent must not query or be handed Sutando repo data at session start.
"""
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "src" / "session-handoff.sh"
BASH = shutil.which("bash") or "/bin/bash"


def _extract(pattern: str, what: str, flags: int = re.DOTALL) -> str:
    m = re.search(pattern, SCRIPT.read_text(encoding="utf-8"), flags)
    assert m, f"{what} not found in session-handoff.sh"
    return m.group(1)


def _program() -> str:
    flag = _extract(r"^(DEV_CHECKOUT=.*)$", "DEV_CHECKOUT assignment", re.MULTILINE)
    prs = _extract(r"\n(  # Open PRs.*?)\n  # Pending questions", "Open PRs section")
    stats = _extract(r"\n(  # Stars.*?)\n  # Relay notes", "Repo Stats section")
    return "\n".join([flag, prs, stats])


def _run(*, git: bool) -> tuple[str, int]:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        repo = root / "repo"
        repo.mkdir()
        if git:
            (repo / ".git").mkdir()
        bindir = root / "bin"
        bindir.mkdir()
        calls = root / "gh-calls"
        gh = bindir / "gh"
        gh.write_text(
            f'#!/bin/sh\necho "$1" >> "{calls}"\n'
            'case "$1" in pr) echo "#1 stub-pr";; api) printf "7\\n3\\n";; esac\n'
        )
        gh.chmod(0o755)
        env = dict(os.environ, PATH=f"{bindir}:{os.environ.get('PATH', '')}")
        r = subprocess.run([BASH, "-c", f'REPO="{repo}"\n{_program()}'],
                           capture_output=True, text=True, env=env, timeout=30)
        assert r.returncode == 0, r.stderr
        n = len(calls.read_text().splitlines()) if calls.exists() else 0
        return r.stdout, n


class RepoQueriesDevOnly(unittest.TestCase):
    def test_packaged_install_queries_nothing(self):
        out, calls = _run(git=False)
        self.assertNotIn("## Open PRs", out)
        self.assertNotIn("## Repo Stats", out)
        self.assertEqual(calls, 0, "gh must not be called on a non-developer install")

    def test_developer_mode_checkout_keeps_both_sections(self):
        out, calls = _run(git=True)
        self.assertIn("## Open PRs", out)
        self.assertIn("stub-pr", out)
        self.assertIn("## Repo Stats", out)
        self.assertIn("7 stars, 3 forks", out)
        self.assertEqual(calls, 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
