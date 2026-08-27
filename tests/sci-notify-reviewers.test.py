#!/usr/bin/env python3
"""notify_reviewers refuses everything rule 9 forbids, and plans correctly.

Run: python3 tests/sci-notify-reviewers.test.py   (stdlib only)
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPT = (REPO / "skills" / "collaboration-intelligence" / "scripts"
          / "notify_reviewers.py")


def run(roster: "dict | None", *args):
    env = {**os.environ}
    tmp = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
    if roster is not None:
        json.dump(roster, tmp)
        tmp.flush()
        env["SUTANDO_SCI_ROSTER"] = tmp.name
    else:
        env["SUTANDO_SCI_ROSTER"] = tmp.name + ".missing"
    tmp.close()
    return subprocess.run([sys.executable, str(SCRIPT), *args],
                          capture_output=True, text=True, timeout=30, env=env)


GOOD = {"rui": {"human": "@rui:x", "stand": "@sutando-rui:x",
                "room": "!triage:x", "allowlisted": True}}


class NotifyReviewers(unittest.TestCase):
    def test_plan_mode_builds_a_stand_mention(self):
        p = run(GOOD, "--reviewers", "rui", "--message", "re-review #1")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("mention @sutando-rui:x", p.stdout)
        self.assertIn("!triage:x", p.stdout)
        self.assertIn("cc @rui:x", p.stdout)

    def test_unknown_reviewer_refused_exit_2(self):
        p = run(GOOD, "--reviewers", "ghost", "--message", "m")
        self.assertEqual(p.returncode, 2)
        self.assertIn("do not guess", p.stderr)

    def test_human_only_entry_refused_exit_3(self):
        p = run({"kim": {"human": "@kim:x", "room": "!r:x"}},
                "--reviewers", "kim", "--message", "m")
        self.assertEqual(p.returncode, 3)
        self.assertIn("not Stand addressing", p.stderr)

    def test_known_off_allowlist_refused_exit_4(self):
        p = run({"mini": {"stand": "@mini:x", "room": "!r:x",
                          "allowlisted": False}},
                "--reviewers", "mini", "--message", "m")
        self.assertEqual(p.returncode, 4)
        self.assertIn("route through the owner", p.stderr)

    def test_missing_roster_names_the_path_and_refuses(self):
        p = run(None, "--reviewers", "rui", "--message", "m")
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("never guess", p.stderr)

    def test_one_bad_entry_never_starves_the_batch(self):
        roster = dict(GOOD)
        roster["mini"] = {"stand": "@mini:x", "room": "!r:x",
                          "allowlisted": False}
        p = run(roster, "--reviewers", "rui,mini", "--message", "m")
        self.assertEqual(p.returncode, 4)          # refusal still visible
        self.assertIn("mention @sutando-rui:x", p.stdout)  # rui still planned
        self.assertIn("OFF-ALLOWLIST 'mini'", p.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)
