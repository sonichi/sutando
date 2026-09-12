#!/usr/bin/env python3
"""One invocation reads the roster ONCE.

Targets froze at the first read while capability, identity, actors, the resolver
and per-target membership each re-read the file. A row changing in between made
the code validate one identity's write access and address a DIFFERENT identity's
endpoint — the two halves of an authorization decision taken from two snapshots.
"""
import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
_TD = tempfile.mkdtemp()
os.environ["SUTANDO_REVIEW_ASKS_LEDGER"] = str(Path(_TD) / "ledger.jsonl")
_spec = importlib.util.spec_from_file_location(
    "nr", ROOT / "skills" / "collaboration-intelligence" / "scripts" / "notify_reviewers.py")
nr = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(nr)

OLD = {"alice": {"stand": "@alice-old:x", "room": "!old:x",
                 "gh": "readonly-login", "allowlisted": True}}
NEW = {"alice": {"stand": "@alice-new:x", "room": "!new:x",
                 "gh": "writer-login", "allowlisted": True}}


class OneInvocationReadsTheRosterOnce(unittest.TestCase):
    def _run(self, mutate: bool):
        """Returns (rc, logins gated, endpoints addressed, roster read count)."""
        calls, gated, sent = {"n": 0}, [], []

        def load():
            calls["n"] += 1
            return NEW if (mutate and calls["n"] > 1) else OLD

        def run(argv, **kw):
            sent.append(argv)

            class R:
                returncode = 0
                stdout = json.dumps({"ok": True, "event_id": "$e", "state": "confirmed"})
                stderr = ""
            return R()

        saved = (nr.load_roster, nr.gate_capability, nr.subprocess.run, sys.argv)
        try:
            nr.load_roster = load
            nr.gate_capability = lambda repo, login: (gated.append(login) or (True, "collaborator"))
            nr.subprocess.run = run
            sys.argv = ["nr", "--reviewers", "alice",
                        "--message", "re-review https://github.com/o/r/pull/7",
                        "--allow-single", "control", "--send"]
            try:
                rc = nr.main()
            except SystemExit as e:
                rc = e.code
        finally:
            nr.load_roster, nr.gate_capability, nr.subprocess.run, sys.argv = saved
        eps = [a for c in sent for a in c if str(a).startswith("@")]
        return rc, gated, eps, calls["n"]

    def test_the_roster_is_read_exactly_once(self):
        _rc, _g, _e, reads = self._run(mutate=False)
        self.assertEqual(reads, 1, "a second read is a second snapshot")

    def test_a_row_changing_mid_invocation_cannot_split_the_decision(self):
        # The defect: gate `writer-login` while addressing the OLD endpoint.
        _rc, gated, eps, _n = self._run(mutate=True)
        self.assertEqual(gated, ["readonly-login"],
                         "the login gated must come from the snapshot the target came from")
        self.assertEqual(eps, ["@alice-old:x"])

    def test_the_control_a_stable_roster_behaves_identically(self):
        # Or the fix could pass by ignoring the roster altogether.
        rc_s, g_s, e_s, _ = self._run(mutate=False)
        rc_m, g_m, e_m, _ = self._run(mutate=True)
        self.assertEqual((rc_s, g_s, e_s), (rc_m, g_m, e_m),
                         "a mid-invocation change must be invisible to the outcome")

    def test_the_control_the_send_still_happens(self):
        # And it must not pass by refusing everything.
        rc, _g, eps, _n = self._run(mutate=False)
        self.assertEqual(rc, 0)
        self.assertEqual(eps, ["@alice-old:x"])


if __name__ == "__main__":
    unittest.main()
