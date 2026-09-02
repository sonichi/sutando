#!/usr/bin/env python3
"""The roster is per-host and read as a union; a peer may never shadow local.

The vault merges PER-HOST branches — a host merges a peer into its own branch
and never writes to the peer's — so one shared roster would be a single JSON
document with N writers and no merge strategy, where the loser's rows vanish
silently. Per-host files give one writer each and a read-time union.

Run: python3 tests/notify-reviewers-per-host-roster.test.py   (stdlib only)
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "skills" / "collaboration-intelligence" / "scripts" / "notify_reviewers.py"


def _load():
    spec = importlib.util.spec_from_file_location("nr", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(REPO / "src"))
    spec.loader.exec_module(mod)
    return mod


LEAF = Path("data") / "collaboration-intelligence" / "reviewer-stands.json"


class PerHostRoster(unittest.TestCase):
    def setUp(self):
        self.nr = _load()
        self.tmp = Path(tempfile.mkdtemp(prefix="roster-"))
        self._prev = os.environ.pop("SUTANDO_SCI_ROSTER", None)
        self.nr.roster_path = lambda: self.tmp / "hosts" / "LOCAL" / LEAF
        self.nr._host_label = lambda: "LOCAL"
        import workspace_default
        self._real_ws = workspace_default.resolve_workspace
        workspace_default.resolve_workspace = lambda *a, **k: str(self.tmp)
        self._ws_mod = workspace_default

    def tearDown(self):
        self._ws_mod.resolve_workspace = self._real_ws
        if self._prev is not None:
            os.environ["SUTANDO_SCI_ROSTER"] = self._prev

    def _write(self, host, rows):
        p = self.tmp / "hosts" / host / LEAF
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(rows))
        return p

    def test_a_peer_only_reviewer_is_reachable_by_name(self):
        """The whole point of the union: rows a peer observed are usable here."""
        self._write("LOCAL", {"alice": {"stand": "@a:x"}})
        self._write("PEER", {"bob": {"stand": "@b:x"}})
        r = self.nr.load_roster()
        self.assertEqual(r["bob"]["stand"], "@b:x")
        self.assertEqual(r["alice"]["stand"], "@a:x")

    def test_local_WINS_a_collision_and_the_peer_row_is_kept_not_dropped(self):
        """A lost row and a row nobody wrote are indistinguishable afterwards."""
        self._write("LOCAL", {"alice": {"stand": "@local:x"}})
        self._write("PEER", {"alice": {"stand": "@peer:x"}})
        r = self.nr.load_roster()
        self.assertEqual(r["alice"]["stand"], "@local:x", "a peer shadowed the local row")
        self.assertEqual(r["alice@PEER"]["stand"], "@peer:x",
                         "the peer's row was dropped instead of retained under its host")

    def test_an_IDENTICAL_peer_row_does_not_manufacture_a_suffixed_duplicate(self):
        """Agreement is not a conflict; suffixing it would inflate every roster."""
        same = {"alice": {"stand": "@a:x"}}
        self._write("LOCAL", same)
        self._write("PEER", same)
        r = self.nr.load_roster()
        self.assertEqual([k for k in r if k.startswith("alice")], ["alice"])

    def test_CONTROL_an_explicit_override_is_not_widened_by_the_glob(self):
        """A fixture-pinned test must never be answered by a host roster."""
        self._write("LOCAL", {"alice": {"stand": "@local:x"}})
        only = self.tmp / "only.json"
        only.write_text(json.dumps({"zed": {"stand": "@z:x"}}))
        os.environ["SUTANDO_SCI_ROSTER"] = str(only)
        try:
            r = self.nr.load_roster()
            self.assertEqual(list(r), ["zed"], "the glob leaked past an explicit override")
        finally:
            os.environ.pop("SUTANDO_SCI_ROSTER", None)

    def test_CONTROL_an_absent_override_REFUSES_rather_than_unioning(self):
        self._write("LOCAL", {"alice": {"stand": "@local:x"}})
        os.environ["SUTANDO_SCI_ROSTER"] = str(self.tmp / "nope.json")
        try:
            with self.assertRaises(SystemExit) as e:
                self.nr.load_roster()
            self.assertIn("never guess", str(e.exception))
        finally:
            os.environ.pop("SUTANDO_SCI_ROSTER", None)

    def test_roster_path_without_a_host_label_uses_the_shared_file(self):
        """host-label can fail; guessing a per-host path would name a file that
        never exists, turning a resolvable roster into a refusal."""
        nr2 = _load()
        nr2._host_label = lambda: ""
        self.assertEqual(nr2.roster_path(), self.tmp / LEAF)

    def test_roster_path_falls_back_to_the_shared_file_before_the_move(self):
        """A host whose roster has not been moved yet must still find it."""
        nr2 = _load()
        nr2._host_label = lambda: "LOCAL"
        legacy = self.tmp / LEAF
        legacy.parent.mkdir(parents=True, exist_ok=True)
        legacy.write_text(json.dumps({"alice": {"stand": "@a:x"}}))
        self.assertEqual(nr2.roster_path(), legacy)

    def test_the_shared_roster_still_joins_the_union_mid_migration(self):
        """Hosts move one at a time. If the pre-move file left the union the
        moment this host moved, every unmoved peer would go unreachable."""
        self._write("LOCAL", {"alice": {"stand": "@a:x"}})
        legacy = self.tmp / LEAF
        legacy.parent.mkdir(parents=True, exist_ok=True)
        legacy.write_text(json.dumps({"carol": {"stand": "@c:x"}}))
        self.assertIn("", [h for h, _ in self.nr.roster_paths()],
                      "the shared roster was dropped from the union")
        self.assertEqual(self.nr.load_roster()["carol"]["stand"], "@c:x")

    def test_a_roster_that_is_not_an_object_REFUSES_instead_of_being_skipped(self):
        """A JSON list parses fine and yields no rows, so skipping it reads as
        an empty roster — which is exactly when a lookup starts guessing."""
        self._write("LOCAL", {"alice": {"stand": "@a:x"}})
        bad = self.tmp / "hosts" / "PEER" / LEAF
        bad.parent.mkdir(parents=True, exist_ok=True)
        bad.write_text("[1, 2, 3]")
        with self.assertRaises(SystemExit) as caught:
            self.nr.load_roster()
        self.assertIn(str(bad), str(caught.exception))

    def test_BOTH_readers_of_this_store_resolve_a_collision_identically(self):
        """The divergence guard. Two readers that disagree about what a
        collision MEANS are worse than no union: one surfaces `alice@peerbox`,
        the other silently drops it, and nothing records which was intended.
        """
        self._write("LOCAL", {"alice": {"stand": "@local:x"}})
        self._write("peerbox", {"alice": {"stand": "@peer:x"}})

        spec = importlib.util.spec_from_file_location(
            "lk", REPO / "skills" / "collaboration-intelligence" / "scripts" / "lookup.py")
        lk = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(lk)

        via_notify = sorted(self.nr.load_roster())
        via_lookup = sorted(r["entity_id"]
                            for r in lk.load_roster(self.tmp / "data" / "collaboration-intelligence"))
        self.assertEqual(via_notify, via_lookup,
                         "the two readers of reviewer-stands.json disagree on a collision")
        self.assertIn("alice@peerbox", via_notify,
                      "the losing peer row was dropped instead of surfaced")


if __name__ == "__main__":
    unittest.main(verbosity=2)
