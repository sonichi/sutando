#!/usr/bin/env python3
"""`command_for` must tolerate both roster shapes for a reviewer's "human" field.

reviewer-stands.json carries `human` as a room-addressable handle for some
entries ("@ruiwangwarm:ag2.space") and as a structured record for others
({"discord": ..., "username": ...}). The dict shape reached `x not in body`
and raised `TypeError: 'in <string>' requires string as left operand, not dict`,
so notifying that reviewer was impossible -- and the PR notification contract
requires reaching every reviewer's Stand.
"""

import importlib.util
import unittest
from pathlib import Path

SCRIPT = (Path(__file__).resolve().parent.parent / "skills"
          / "collaboration-intelligence" / "scripts" / "notify_reviewers.py")


def _load():
    spec = importlib.util.spec_from_file_location("_notify_reviewers", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class HumanShapeTests(unittest.TestCase):
    def setUp(self):
        self.mod = _load()

    def _cmd(self, human):
        return self.mod.command_for(
            {"name": "x", "stand": "@stand:ag2.space", "room": "!r:ag2.space",
             "human": human}, "body")

    @staticmethod
    def _body(cmd):
        """The message argument. Asserting over the joined argv puts the repo path
        in the subject, so a checkout under /tmp/tmp.jrjxccG0z6 fails on "cc"."""
        return cmd[-2]

    def test_dict_human_does_not_raise(self):
        """The defect: a structured human record crashed the send."""
        self._cmd({"discord": "123", "username": "someone"})

    def test_dict_human_is_not_ccd(self):
        """A dict has no room-addressable handle -- skip it, never stringify it."""
        self.assertEqual(self._body(self._cmd({"discord": "123"})), "body")

    def test_string_human_is_still_ccd(self):
        self.assertEqual(self._body(self._cmd("@who:ag2.space")),
                         "body (cc @who:ag2.space)")

    def test_string_human_not_duplicated_when_already_present(self):
        cmd = self.mod.command_for(
            {"name": "x", "stand": "@s:ag2.space", "room": "!r:ag2.space",
             "human": "@who:ag2.space"}, "hi @who:ag2.space")
        self.assertEqual(self._body(cmd).count("@who:ag2.space"), 1)

    def test_missing_human_is_fine(self):
        self.assertEqual(self._body(self._cmd(None)), "body")


class IdentityCaveatSurfaces(unittest.TestCase):
    """`identity_caveat` is data the operator must see, not data we act on.

    A roster can record that two agents share one GitHub login; before this
    fired, nothing read the field, so the note existed and never reached
    anyone. Populating it is per-host, which is why it is pinned here.
    """

    ROOM = "!r:ag2.space"

    def _resolve(self, roster):
        import contextlib
        import io
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            targets, rc = self.mod.resolve(sorted(roster), roster)
        return targets, rc, buf.getvalue()

    def setUp(self):
        self.mod = _load()

    def test_a_caveat_entry_prints_before_the_send(self):
        roster = {"a": {"stand": "@a:ag2.space", "room": self.ROOM,
                        "identity_caveat": "SHARED LOGIN: a and b push as X"}}
        targets, rc, err = self._resolve(roster)
        self.assertIn("IDENTITY CAVEAT 'a'", err)
        self.assertIn("SHARED LOGIN", err)
        self.assertEqual((len(targets), rc), (1, 0))

    def test_an_entry_without_one_is_silent(self):
        # Control: without this, a hook that printed for every entry would
        # look identical on the affected one.
        roster = {"b": {"stand": "@b:ag2.space", "room": self.ROOM}}
        targets, rc, err = self._resolve(roster)
        self.assertNotIn("IDENTITY CAVEAT", err)
        self.assertEqual((len(targets), rc), (1, 0))

    def test_the_caveat_does_not_refuse_the_target(self):
        # A shared login is a reason to check WHICH person, never a reason to
        # skip them: the entry must still resolve to a real target.
        roster = {"a": {"stand": "@a:ag2.space", "room": self.ROOM,
                        "identity_caveat": "x"},
                  "b": {"stand": "@b:ag2.space", "room": self.ROOM}}
        targets, rc, _ = self._resolve(roster)
        self.assertEqual(rc, 0)
        self.assertEqual({t["name"] for t in targets}, {"a", "b"})

    def test_authority_caveat_reaches_the_operator(self):
        """The field that reached nobody (#3965).

        It records a Stand's self-stated refusal to cast a formal vote on a
        collaborator-routed ask — a routing constraint, and the one thing that
        stops an unauthorized request going out.
        """
        roster = {"a": {"stand": "@a:ag2.space", "room": self.ROOM,
                        "authority_caveat": "DECLINES a collaborator-routed vote"}}
        _, rc, err = self._resolve(roster)
        self.assertIn("AUTHORITY CAVEAT 'a'", err)
        self.assertIn("DECLINES a collaborator-routed vote", err)
        self.assertEqual(rc, 0)

    def test_a_caveat_field_nobody_has_named_yet_still_prints(self):
        """THE discriminating arm: this is what makes it derived rather than
        enumerated. A hardcoded ('identity','authority') pair passes every
        other arm here and fails only this one — which is precisely how
        `authority_caveat` came to exist unread beside a field that was read.
        """
        roster = {"a": {"stand": "@a:ag2.space", "room": self.ROOM,
                        "jurisdiction_caveat": "invented for this test"}}
        _, rc, err = self._resolve(roster)
        self.assertIn("JURISDICTION CAVEAT 'a'", err)
        self.assertEqual(rc, 0)

    def test_every_caveat_on_one_entry_prints(self):
        roster = {"a": {"stand": "@a:ag2.space", "room": self.ROOM,
                        "identity_caveat": "shared login",
                        "authority_caveat": "declines the vote",
                        "room_caveat": "wrong room"}}
        _, _, err = self._resolve(roster)
        for label in ("IDENTITY CAVEAT", "AUTHORITY CAVEAT", "ROOM CAVEAT"):
            self.assertIn(label, err)

    def test_a_non_caveat_field_is_not_printed(self):
        """Control for the loop: matching too widely would dump the whole
        entry to stderr, and 'everything is surfaced' is indistinguishable
        from 'nothing is' once the operator stops reading it.
        """
        roster = {"a": {"stand": "@a:ag2.space", "room": self.ROOM,
                        "note": "ordinary prose", "github": "someone"}}
        _, _, err = self._resolve(roster)
        self.assertNotIn("CAVEAT", err)

    def test_an_empty_caveat_is_not_printed(self):
        # An empty string is absence, not a caveat worth a line.
        roster = {"a": {"stand": "@a:ag2.space", "room": self.ROOM,
                        "authority_caveat": ""}}
        _, _, err = self._resolve(roster)
        self.assertNotIn("CAVEAT", err)


if __name__ == "__main__":
    unittest.main(verbosity=2)
