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

    def test_dict_human_does_not_raise(self):
        """The defect: a structured human record crashed the send."""
        self._cmd({"discord": "123", "username": "someone"})

    def test_dict_human_is_not_ccd(self):
        """A dict has no room-addressable handle -- skip it, never stringify it."""
        self.assertNotIn("cc", " ".join(self._cmd({"discord": "123"})))

    def test_string_human_is_still_ccd(self):
        self.assertIn("(cc @who:ag2.space)", " ".join(self._cmd("@who:ag2.space")))

    def test_string_human_not_duplicated_when_already_present(self):
        cmd = self.mod.command_for(
            {"name": "x", "stand": "@s:ag2.space", "room": "!r:ag2.space",
             "human": "@who:ag2.space"}, "hi @who:ag2.space")
        self.assertEqual(" ".join(cmd).count("@who:ag2.space"), 1)

    def test_missing_human_is_fine(self):
        self.assertNotIn("cc", " ".join(self._cmd(None)))


if __name__ == "__main__":
    unittest.main(verbosity=2)
