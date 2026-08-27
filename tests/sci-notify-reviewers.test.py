#!/usr/bin/env python3
"""notify_reviewers refuses everything rule 9 forbids, and plans correctly.

Run: python3 tests/sci-notify-reviewers.test.py   (stdlib only)
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import pathlib
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPT = (REPO / "skills" / "collaboration-intelligence" / "scripts"
          / "notify_reviewers.py")


# One managed root for every fixture; NamedTemporaryFile(delete=False) leaked
# one roster JSON per call, six per run, for the lifetime of the machine.
_TMP = tempfile.TemporaryDirectory()


def run(roster: "dict | None", *args):
    env = {**os.environ}
    path = pathlib.Path(tempfile.mkdtemp(dir=_TMP.name)) / "roster.json"
    if roster is not None:
        path.write_text(json.dumps(roster))
        env["SUTANDO_SCI_ROSTER"] = str(path)
    else:
        env["SUTANDO_SCI_ROSTER"] = str(path) + ".missing"
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

def run_send(stub_payload, roster=None):
    """Drive --send against a STUB room_ops. The script resolves room_ops as
    parents[3] of its own path, so the copy must sit in a matching tree."""
    root = pathlib.Path(tempfile.mkdtemp(dir=_TMP.name))
    (root / "skills" / "collaboration-intelligence" / "scripts").mkdir(parents=True)
    (root / "skills" / "agent-room-ops").mkdir(parents=True)
    copy = root / "skills/collaboration-intelligence/scripts/notify_reviewers.py"
    copy.write_text(SCRIPT.read_text())
    (root / "skills/agent-room-ops/room_ops.py").write_text(
        "import sys\n"
        f"sys.stdout.write({stub_payload!r})\n"
        "sys.exit(0)\n")          # rc 0 + empty stderr: the real refusal shape
    rp = root / "roster.json"
    rp.write_text(json.dumps(roster or GOOD))
    env = {**os.environ, "SUTANDO_SCI_ROSTER": str(rp)}
    return subprocess.run([sys.executable, str(copy), "--send",
                           "--reviewers", "rui", "--message", "m"],
                          capture_output=True, text=True, timeout=30, env=env)


class SilentRefusal(unittest.TestCase):
    """room_ops reports refusals IN-BAND: rc 0, empty stderr, ok:false+reason.
    Printing stderr alone renders every such refusal as a blank line."""

    def test_in_band_reason_is_surfaced_not_swallowed(self):
        p = run_send('{"ok": false, "members": [], "reason": "no gateway configured"}')
        self.assertEqual(p.returncode, 1)
        self.assertIn("no gateway configured", p.stderr)
        self.assertNotIn("STDERR=\n", p.stderr)

    def test_the_gateway_reason_is_surfaced_verbatim(self):
        # Surfaced, not interpreted: the producer emits this whenever the base
        # URL is empty and says nothing about why, so we must not name a cause.
        p = run_send('{"ok": false, "reason": "no gateway configured"}')
        self.assertIn("reason=no gateway configured", p.stderr)
        self.assertNotIn("env is not loaded", p.stderr)

    def test_a_reasonless_failure_still_says_something(self):
        p = run_send('{"ok": false}')
        self.assertEqual(p.returncode, 1)
        self.assertIn("no reason reported", p.stderr)

    def test_unparseable_output_is_not_reported_as_success(self):
        p = run_send('not json at all')
        self.assertEqual(p.returncode, 1)
        self.assertIn("unparseable", p.stderr)

    def test_non_object_payloads_do_not_crash_the_notifier(self):
        # room_ops should never emit these; a notifier that dies on one reports
        # nothing at all, which is the failure this PR exists to remove.
        for payload in ('[]', '"hello"', 'null', '{"reason": 1}'):
            with self.subTest(payload=payload):
                p = run_send(payload)
                self.assertEqual(p.returncode, 1, p.stderr)
                self.assertIn("ok=False", p.stderr)
                self.assertNotIn("Traceback", p.stderr)

    def test_a_non_string_reason_is_still_reported(self):
        # The bare "reason=1" assertion passes even unfixed: the line prints
        # before the substring test raises. Only the exit + traceback discriminate.
        p = run_send('{"ok": false, "reason": 1}')
        self.assertIn("reason=1", p.stderr)
        self.assertEqual(p.returncode, 1, p.stderr)
        self.assertNotIn("Traceback", p.stderr)

    def test_success_still_reports_the_event_id(self):
        p = run_send('{"ok": true, "event_id": "$abc123"}')
        self.assertEqual(p.returncode, 0)
        self.assertIn("ok=True", p.stdout)
        self.assertIn("$abc123", p.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)
