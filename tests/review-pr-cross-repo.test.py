#!/usr/bin/env python3
"""`review-pr.sh <N>` resolves a bare number against the cwd repo.

Measured 2026-09-04: a peer asked for a review of sutando-skills#657 and
sonichi/sutando#657 is a real MERGED PR with an unrelated title, so running the
bare form from this checkout reviews the wrong object and reports it clean. The
number is not the identity; the repo plus the number is.
"""
from __future__ import annotations

import os
import pathlib
import subprocess
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "skills" / "claude-codex" / "scripts" / "review-pr.sh"

# A `gh` stand-in that records argv and emits an empty diff, so the script exits
# early at its own empty-diff guard and never reaches codex.
FAKE_GH = """#!/usr/bin/env bash
printf '%s\\n' "$*" >> "$GH_ARGV_LOG"
exit 0
"""


def run(args):
    d = tempfile.mkdtemp()
    bindir = pathlib.Path(d) / "bin"
    bindir.mkdir()
    gh = bindir / "gh"
    gh.write_text(FAKE_GH)
    gh.chmod(0o755)
    log = pathlib.Path(d) / "argv.log"
    env = dict(os.environ)
    env["PATH"] = f"{bindir}:{env['PATH']}"
    env["GH_ARGV_LOG"] = str(log)
    p = subprocess.run(["bash", str(SCRIPT), *args],
                       capture_output=True, text=True, env=env, cwd=d)
    return p, (log.read_text() if log.exists() else "")


class CrossRepo(unittest.TestCase):
    def test_qualified_ref_passes_the_repo_to_gh(self):
        _, argv = run(["sonichi/sutando-skills#657"])
        self.assertIn("-R sonichi/sutando-skills 657", argv)

    def test_repo_flag_passes_the_repo_to_gh(self):
        _, argv = run(["657", "--repo", "sonichi/sutando-skills"])
        self.assertIn("-R sonichi/sutando-skills 657", argv)

    def test_bare_number_still_defers_to_cwd(self):
        # The back-compatible path: no -R, so gh resolves the cwd remote as before.
        _, argv = run(["1754"])
        self.assertIn("pr diff 1754", argv)
        self.assertNotIn("-R", argv)

    def test_a_non_numeric_pr_is_refused_rather_than_passed_through(self):
        p, argv = run(["sonichi/sutando-skills#not-a-number"])
        self.assertEqual(p.returncode, 2)
        self.assertIn("is not a PR number", p.stderr)
        self.assertEqual(argv, "")

    def test_the_subject_is_requested_so_the_verdict_can_name_it(self):
        # Without this the caller's assumption is the only record of which PR
        # was read, which is exactly what failed.
        _, argv = run(["sonichi/sutando-skills#657"])
        self.assertIn("pr view", argv)
        self.assertIn("-R sonichi/sutando-skills", argv.split("pr view")[1])


if __name__ == "__main__":
    unittest.main(verbosity=1)
