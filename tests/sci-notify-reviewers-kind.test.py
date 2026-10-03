#!/usr/bin/env python3
"""A NOTICE is not an ASK: --kind must gate the ledger and both ask-only gates.

Issue #3544: every message carrying a PR URL was treated as a review request,
so telling two approvers their PR had merged was logged as an ask AND refused
as a repeat-ask — the only way through being --widen-override, which files a
DELIBERATE re-ask and makes the next genuine escalation look like the third.

Each notice assertion is paired with the ask control that must still fire; a
suite where only the notice cases run proves the flag is read, not that the
gates it bypasses were ever armed.

Run: python3 tests/sci-notify-reviewers-kind.test.py   (stdlib only)
"""
import datetime
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

SCRIPT = (Path(__file__).resolve().parent.parent / "skills"
          / "collaboration-intelligence" / "scripts" / "notify_reviewers.py")
PR = "https://github.com/sonichi/sutando/pull/4242"


def _load():
    spec = importlib.util.spec_from_file_location("_nr_kind", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _iso(minutes_ago):
    t = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(minutes=minutes_ago)
    return t.isoformat().replace("+00:00", "Z")


class _Sent:
    """Stands in for the room_ops mention so nothing leaves the machine."""
    returncode = 0
    stdout = json.dumps({"ok": True, "event_id": "$stub"})
    stderr = ""


class Kind(unittest.TestCase):
    def setUp(self):
        self.mod = _load()
        self.tmp = tempfile.TemporaryDirectory()
        d = Path(self.tmp.name)
        self.ledger = d / "review-asks.jsonl"
        roster = {k: {"stand": f"@{k}-stand:x", "room": "!r:x", "human": f"@{k}:x"}
                  for k in ("alice", "bob")}
        self.roster_file = d / "roster.json"
        self.roster_file.write_text(json.dumps(roster))
        self.calls = []

    def tearDown(self):
        self.tmp.cleanup()

    def _run(self, argv):
        def fake_run(args, **kw):
            self.calls.append(args)
            return _Sent()
        with patch.object(self.mod, "ledger_path", lambda: self.ledger), \
             patch.object(self.mod, "roster_path", lambda: self.roster_file), \
             patch.object(self.mod.subprocess, "run", fake_run), \
             patch.object(self.mod.sys, "argv", ["notify_reviewers.py"] + argv):
            return self.mod.main()

    def _ledger_lines(self):
        if not self.ledger.exists():
            return []
        return [x for x in self.ledger.read_text().splitlines() if x.strip()]

    def _seed_stale_asks(self, *reviewers, minutes=45):
        with open(self.ledger, "a") as fh:
            for r in reviewers:
                fh.write(json.dumps({"repo": "sonichi/sutando", "pr": 4242,
                                     "reviewer": r, "ts": _iso(minutes),
                                     "channel": "room"}) + "\n")

    # --- the ledger -------------------------------------------------------
    def test_CONTROL_an_ask_is_recorded(self):
        rc = self._run(["--reviewers", "alice,bob", "--message", f"review {PR}", "--send"])
        self.assertEqual(rc, 0)
        rows = [json.loads(x) for x in self._ledger_lines()]
        # Both transports now reserve then settle, so count the LIFECYCLE, not
        # lines. `actor` is on both rows; `reviewer` only on the settlement.
        self.assertEqual({r["actor"] for r in rows}, {"alice", "bob"},
                         "the ask path must write, or the notice assertion below is vacuous")
        for who in ("alice", "bob"):
            outcomes = [r.get("outcome") for r in rows if r.get("actor") == who]
            self.assertIn("pending", outcomes, f"{who} never reserved a park")
            self.assertIn("confirmed", outcomes, f"{who} never settled its reservation")

    def test_a_notice_is_not_recorded_as_an_ask(self):
        rc = self._run(["--reviewers", "alice,bob", "--message", f"merged {PR}",
                        "--send", "--kind", "notice"])
        self.assertEqual(rc, 0)
        self.assertEqual(self._ledger_lines(), [],
                         "a merge announcement recorded as an ask corrupts pr-unattended")

    def test_a_notice_still_sends(self):
        self._run(["--reviewers", "alice,bob", "--message", f"merged {PR}",
                   "--send", "--kind", "notice"])
        mentions = [c for c in self.calls if "mention" in c]
        self.assertEqual(len(mentions), 2, "the notice must still reach both stands")

    # --- the two-reviewer rule -------------------------------------------
    def test_CONTROL_an_ask_to_one_reviewer_is_refused(self):
        rc = self._run(["--reviewers", "alice", "--message", f"review {PR}", "--send"])
        self.assertEqual(rc, 5)

    def test_a_notice_to_one_reviewer_is_allowed(self):
        rc = self._run(["--reviewers", "alice", "--message", f"merged {PR}",
                        "--send", "--kind", "notice"])
        self.assertEqual(rc, 0, "the rule stops a PR stalling on one person; "
                                "a notice asks for nothing and cannot stall it")

    # --- the repeat-ask gate ---------------------------------------------
    def test_CONTROL_a_repeat_ask_is_refused(self):
        self._seed_stale_asks("alice", "bob")
        rc = self._run(["--reviewers", "alice,bob", "--message", f"re-review {PR}", "--send"])
        self.assertEqual(rc, 6)

    def test_a_notice_to_the_same_people_is_not_a_repeat_ask(self):
        self._seed_stale_asks("alice", "bob")
        rc = self._run(["--reviewers", "alice,bob", "--message", f"merged {PR}",
                        "--send", "--kind", "notice"])
        self.assertEqual(rc, 0, "telling the approvers their PR landed is the exact "
                                "case #3544 reports being refused as spam")

    # --- the default must not move ---------------------------------------
    def test_the_default_kind_is_ask(self):
        self._seed_stale_asks("alice", "bob")
        rc = self._run(["--reviewers", "alice,bob", "--message", f"re-review {PR}", "--send"])
        self.assertEqual(rc, 6, "omitting --kind must behave exactly as before")


if __name__ == "__main__":
    unittest.main(verbosity=2)
