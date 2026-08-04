#!/usr/bin/env python3
"""dev-activity must count THIS instance's commits — not the peer's.

Both Sutando instances commit under the owner's GH-mapped email (CLA requires
it), so `--author` carries zero information about which bot shipped what. The
`Stand:` trailer is the only discriminator. Measured on Mini 2026-08-01, a
local-branch scan returned 17 `Echo Act IV Mini` commits beside 16
`Echo Act IV Pro` ones — the peer's arrived purely because worktrees had been
created at their PR heads.

Run: python3 tests/daily-insight-stand-attribution.test.py
"""
from __future__ import annotations

import importlib.util
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("di", ROOT / "src" / "daily-insight.py")
di = importlib.util.module_from_spec(_spec)
sys.modules["di"] = di
try:
    _spec.loader.exec_module(di)
except SystemExit:
    pass

EMAIL = "4250911+sonichi@users.noreply.github.com"


def build_repo(commits) -> Path:
    """commits = [(filename, stand_or_None), ...] on a NON-default local branch."""
    d = Path(tempfile.mkdtemp())
    run = lambda *a: subprocess.run(["git", "-C", str(d), *a], capture_output=True, text=True)
    subprocess.run(["git", "init", "-q", str(d)], capture_output=True)
    run("config", "user.email", EMAIL)
    run("config", "user.name", "Chi Wang")
    for name, stand in commits:
        p = d / "src"; p.mkdir(exist_ok=True)
        (p / name).write_text(name)
        run("add", "-A")
        msg = f"feat: {name}" + (f"\n\nStand: {stand}" if stand else "")
        run("commit", "-q", "-m", msg)
    return d


class TestStandValueResolution(unittest.TestCase):
    def test_explicit_override_wins(self):
        self.assertEqual(di._own_stand_value({"SUTANDO_STAND": "Echo Act IV Mini"}),
                         "Echo Act IV Mini")

    def test_host_label_is_NOT_mapped_to_an_owner_stand(self):
        """Installation-specific policy must not live in shared code.

        An earlier revision mapped any label containing `mac-mini`/`macbook` to
        THIS owner's Stand values — on another user's machine that assigns a
        foreign identity and filters out all their commits (john-the-dev, #2484).
        """
        for label in ("Chis-Mac-mini", "Chis-MacBook-Pro", "Someone-Else-MacBook"):
            with self.subTest(label=label):
                got = di._own_stand_value({"SUTANDO_HOST_LABEL": label},
                                          repo_root=Path(tempfile.mkdtemp()))
                self.assertEqual(got, "", f"{label} must not resolve a Stand by host name")

    def test_no_env_and_no_config_returns_empty(self):
        """The REAL cron shape: neither env var exported, no config key."""
        self.assertEqual(di._own_stand_value({}, repo_root=Path(tempfile.mkdtemp())), "")

    def test_git_root_resolution_failure_returns_empty(self):
        with patch.object(di.subprocess, "run", side_effect=OSError("git missing")):
            self.assertEqual(di._own_stand_value({}, repo_root=Path("/tmp/inside")), "")

    def test_config_command_failure_returns_empty(self):
        repo = Path(tempfile.mkdtemp())
        (repo / "scripts").mkdir()
        (repo / "scripts" / "sutando-config.sh").write_text("#!/bin/bash\n")
        top = subprocess.CompletedProcess(
            args=["git"], returncode=0, stdout=f"{repo}\n", stderr=""
        )
        with patch.object(di.subprocess, "run", side_effect=[top, OSError("bash failed")]):
            self.assertEqual(di._own_stand_value({}, repo_root=repo), "")

    def test_config_resolves_when_activated_call_starts_inside_src(self):
        """The scheduled default passes <repo>/src, not the checkout root."""
        repo = Path(tempfile.mkdtemp())
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        (repo / "src").mkdir()
        (repo / "scripts").mkdir()
        shutil.copy2(ROOT / "src" / "sutando_config.py", repo / "src")
        shutil.copy2(ROOT / "scripts" / "sutando-config.sh", repo / "scripts")
        # sutando-config.sh sources this helper (#2599)
        shutil.copy2(ROOT / "scripts" / "python-binary.sh", repo / "scripts")
        (repo / "sutando.config.json").write_text("{}\n")
        (repo / "sutando.config.local.json").write_text(
            '{"stand":"Echo Act IV Mini"}\n'
        )
        self.assertEqual(
            di._own_stand_value({}, repo_root=repo / "src"),
            "Echo Act IV Mini",
        )


class TestUnknownIdentityDeclinesRatherThanMisattributing(unittest.TestCase):
    """The activated-path blocker: "" must mean DECLINE, never count-everything."""

    def test_no_stand_returns_None_not_a_count(self):
        import os
        repo = build_repo([("a.py", "Echo Act IV Mini"), ("b.py", "Echo Act IV Pro")])
        for k in ("SUTANDO_STAND", "SUTANDO_HOST_LABEL"):
            os.environ.pop(k, None)
        got = di.analyze_dev_activity(repo)
        self.assertIsNone(got, "unknown identity must decline, not credit the peer")


class TestCountsOnlyOwnCommits(unittest.TestCase):
    def setUp(self):
        self.repo = build_repo([
            ("a.py", "Echo Act IV Mini"),
            ("b.py", "Echo Act IV Pro"),
            ("c.py", "Echo Act IV Mini"),
            ("d.py", None),
        ])

    def test_filters_to_this_instance(self):
        import os
        os.environ["SUTANDO_STAND"] = "Echo Act IV Mini"
        try:
            got = di.analyze_dev_activity(self.repo)
        finally:
            os.environ.pop("SUTANDO_STAND", None)
        self.assertIsNotNone(got)
        self.assertEqual(got["commits_24h"], 2, got)

    def test_peer_only_repo_reports_nothing(self):
        """The bug this fixes in reverse: never credit the peer's day as yours."""
        import os
        repo = build_repo([("x.py", "Echo Act IV Pro"), ("y.py", "Echo Act IV Pro")])
        os.environ["SUTANDO_STAND"] = "Echo Act IV Mini"
        try:
            self.assertIsNone(di.analyze_dev_activity(repo))
        finally:
            os.environ.pop("SUTANDO_STAND", None)

    def test_unknown_stand_declines(self):
        """Was 'counts everything' — inverted after #2484: that credited the peer."""
        import os
        for k in ("SUTANDO_STAND", "SUTANDO_HOST_LABEL"):
            os.environ.pop(k, None)
        self.assertIsNone(di.analyze_dev_activity(self.repo))


class TestSeesWorkOnBranches(unittest.TestCase):
    def test_counts_commits_not_reachable_from_HEAD(self):
        """The original defect: `git log` without --branches saw 0 on a 2-PR day."""
        import os
        repo = build_repo([("a.py", "Echo Act IV Mini")])
        subprocess.run(["git", "-C", str(repo), "checkout", "-q", "-b", "feature"],
                       capture_output=True)
        (repo / "src" / "e.py").write_text("e")
        subprocess.run(["git", "-C", str(repo), "add", "-A"], capture_output=True)
        subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m",
                        "feat: on a branch\n\nStand: Echo Act IV Mini"], capture_output=True)
        subprocess.run(["git", "-C", str(repo), "checkout", "-q", "-"], capture_output=True)
        os.environ["SUTANDO_STAND"] = "Echo Act IV Mini"
        try:
            got = di.analyze_dev_activity(repo)
        finally:
            os.environ.pop("SUTANDO_STAND", None)
        self.assertEqual(got["commits_24h"], 2, "branch commit must be counted")


if __name__ == "__main__":
    unittest.main(verbosity=2)
