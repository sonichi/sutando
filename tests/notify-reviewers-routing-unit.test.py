#!/usr/bin/env python3
"""In-process cover for notify_reviewers' routing decisions.

The sibling routing test drives the CLI as a subprocess, which is the right
shape for exit codes but is invisible to coverage instrumentation. These call
the same functions directly.
"""
from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import pathlib
import sys
import tempfile
import unittest

REPO = pathlib.Path(__file__).resolve().parents[1]
SCRIPT = REPO / "skills" / "collaboration-intelligence" / "scripts" / "notify_reviewers.py"

_spec = importlib.util.spec_from_file_location("notify_reviewers_mod", SCRIPT)
nr = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(nr)

MATRIX = {"stand": "@sutando-x:ag2.space", "room": "!r:ag2.space"}
DISCORD = {"discord_id": "111", "home_channel": "222"}


def config_with(payload) -> str:
    d = tempfile.mkdtemp(prefix="cfg-unit-")
    chan = pathlib.Path(d, "channels", "discord")
    chan.mkdir(parents=True)
    (chan / "access.json").write_text(payload if isinstance(payload, str) else json.dumps(payload))
    return d


class Resolve(unittest.TestCase):
    def test_transport_is_classified_per_row(self):
        targets, rc = nr.resolve(["m", "d"], {"m": MATRIX, "d": DISCORD})
        self.assertEqual(rc, 0)
        self.assertEqual([t["transport"] for t in targets], ["matrix", "discord"])

    def test_each_missing_field_gets_its_own_reason(self):
        # stand-without-room, id-without-channel, and neither
        roster = {"a": {"stand": "@s:x"}, "b": {"discord_id": "9"},
                  "c": {"human": "someone"}}
        targets, rc = nr.resolve(["a", "b", "c"], roster)
        self.assertEqual(targets, [])
        self.assertEqual(rc, 3)

    def test_unknown_reviewer_is_rc_2_and_never_guessed(self):
        targets, rc = nr.resolve(["ghost"], {})
        self.assertEqual((targets, rc), ([], 2))

    def test_off_allowlist_is_rc_4_on_either_transport(self):
        for row in ({**MATRIX, "allowlisted": False}, {**DISCORD, "allowlisted": False}):
            targets, rc = nr.resolve(["x"], {"x": row})
            self.assertEqual((targets, rc), ([], 4))

    def test_one_bad_entry_does_not_starve_the_batch(self):
        targets, rc = nr.resolve(["ok", "bad"], {"ok": DISCORD, "bad": {"human": "h"}})
        self.assertEqual(len(targets), 1)
        self.assertEqual(rc, 3)


class DiscordReachability(unittest.TestCase):
    def setUp(self):
        self._saved = os.environ.get("CLAUDE_CONFIG_DIR")

    def tearDown(self):
        if self._saved is None:
            os.environ.pop("CLAUDE_CONFIG_DIR", None)
        else:
            os.environ["CLAUDE_CONFIG_DIR"] = self._saved

    def _target(self):
        return {"name": "d", "transport": "discord", "discord_id": "111",
                "channel": "222", "stand": None, "room": None, "human": None}

    def test_present_on_allowfrom(self):
        os.environ["CLAUDE_CONFIG_DIR"] = config_with({"groups": {"222": {"allowFrom": ["111", "9"]}}})
        present, why = nr.discord_reachable(self._target())
        self.assertTrue(present)
        self.assertIn("allowFrom", why)

    def test_absent_is_a_positive_absence(self):
        os.environ["CLAUDE_CONFIG_DIR"] = config_with({"groups": {"222": {"allowFrom": ["9"]}}})
        present, _ = nr.discord_reachable(self._target())
        self.assertFalse(present)

    def test_channels_section_is_read_too(self):
        os.environ["CLAUDE_CONFIG_DIR"] = config_with({"channels": {"222": {"allowFrom": ["111"]}}})
        self.assertTrue(nr.discord_reachable(self._target())[0])

    # A broken instrument must never report a reachable person as absent:
    # every unusable-config shape below fails toward UNVERIFIED, not absence.
    def test_unreadable_config_is_unverified_not_absent(self):
        os.environ["CLAUDE_CONFIG_DIR"] = tempfile.mkdtemp(prefix="cfg-empty-")
        present, why = nr.discord_reachable(self._target())
        self.assertTrue(present)
        self.assertTrue(why.startswith("unverified"))

    def test_unparseable_config_is_unverified(self):
        os.environ["CLAUDE_CONFIG_DIR"] = config_with("{not json")
        self.assertTrue(nr.discord_reachable(self._target())[1].startswith("unverified"))

    def test_non_object_map_is_unverified(self):
        os.environ["CLAUDE_CONFIG_DIR"] = config_with("[1,2,3]")
        self.assertTrue(nr.discord_reachable(self._target())[1].startswith("unverified"))

    def test_empty_allowfrom_is_unverified_not_absent(self):
        os.environ["CLAUDE_CONFIG_DIR"] = config_with({"groups": {"222": {"allowFrom": []}}})
        present, why = nr.discord_reachable(self._target())
        self.assertTrue(present)
        self.assertTrue(why.startswith("unverified"))

    def test_channel_missing_from_map_is_unverified(self):
        os.environ["CLAUDE_CONFIG_DIR"] = config_with({"groups": {"999": {"allowFrom": ["1"]}}})
        self.assertTrue(nr.discord_reachable(self._target())[1].startswith("unverified"))


class CommandShape(unittest.TestCase):
    def test_discord_command_mentions_the_id(self):
        argv = nr.discord_command_for({"discord_id": "111", "channel": "222"}, "hello")
        self.assertIn("--to", argv)
        self.assertIn("111", argv)
        self.assertIn("hello", argv)

    def test_matrix_command_still_targets_room_ops(self):
        argv = nr.command_for({"stand": "@s:x", "room": "!r:x", "human": None}, "hi")
        self.assertTrue(any("room_ops.py" in a for a in argv))
        self.assertIn("mention", argv)


class MainDiscordBranch(unittest.TestCase):
    """main()'s discord branch, in-process: the CLI test reaches it in a child
    process, so coverage never attributes these lines to the file."""

    def setUp(self):
        self._argv, self._cfg = sys.argv[:], os.environ.get("CLAUDE_CONFIG_DIR")
        self._roster = os.environ.get("SUTANDO_SCI_ROSTER")

    def tearDown(self):
        sys.argv[:] = self._argv
        for k, v in (("CLAUDE_CONFIG_DIR", self._cfg), ("SUTANDO_SCI_ROSTER", self._roster)):
            os.environ.pop(k, None) if v is None else os.environ.update({k: v})

    def _run(self, roster, cfg):
        fd, path = tempfile.mkstemp(suffix=".json")
        with os.fdopen(fd, "w") as f:
            json.dump(roster, f)
        os.environ["SUTANDO_SCI_ROSTER"] = path
        os.environ["CLAUDE_CONFIG_DIR"] = cfg
        sys.argv[:] = ["notify_reviewers.py", "--reviewers", "d", "--message", "m"]
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = nr.main()
        os.unlink(path)
        return rc, out.getvalue(), err.getvalue()

    def test_plan_mode_prints_the_send_command(self):
        cfg = config_with({"groups": {"222": {"allowFrom": ["111"]}}})
        rc, out, _ = self._run({"d": DISCORD}, cfg)
        self.assertIn("PLAN:", out)
        self.assertIn("--to", out)
        self.assertEqual(rc, 0)

    def test_absent_from_channel_refuses_and_counts_a_failure(self):
        cfg = config_with({"groups": {"222": {"allowFrom": ["999"]}}})
        rc, out, err = self._run({"d": DISCORD}, cfg)
        self.assertIn("ABSENT from channel 222", err)
        self.assertNotIn("PLAN:", out)
        self.assertNotEqual(rc, 0)

    def test_unverified_config_sends_but_says_it_did_not_check(self):
        rc, out, err = self._run({"d": DISCORD}, tempfile.mkdtemp(prefix="cfg-mainunv-"))
        self.assertIn("UNVERIFIED", err)
        self.assertIn("not a confirmation", err)
        self.assertIn("PLAN:", out)


class MainDiscordSend(unittest.TestCase):
    """The --send leg with the transport stubbed: nothing is posted, but the
    success and failure reporting are the lines a real send would take."""

    def setUp(self):
        self._argv, self._cfg = sys.argv[:], os.environ.get("CLAUDE_CONFIG_DIR")
        self._roster, self._run = os.environ.get("SUTANDO_SCI_ROSTER"), nr.subprocess.run

    def tearDown(self):
        sys.argv[:] = self._argv
        nr.subprocess.run = self._run
        for k, v in (("CLAUDE_CONFIG_DIR", self._cfg), ("SUTANDO_SCI_ROSTER", self._roster)):
            os.environ.pop(k, None) if v is None else os.environ.update({k: v})

    def _send(self, rc_out):
        class _R:
            returncode, stdout, stderr = rc_out, "", "boom" if rc_out else ""
        nr.subprocess.run = lambda *a, **k: _R()
        fd, path = tempfile.mkstemp(suffix=".json")
        with os.fdopen(fd, "w") as f:
            json.dump({"d": DISCORD}, f)
        os.environ["SUTANDO_SCI_ROSTER"] = path
        os.environ["CLAUDE_CONFIG_DIR"] = config_with({"groups": {"222": {"allowFrom": ["111"]}}})
        sys.argv[:] = ["notify_reviewers.py", "--reviewers", "d", "--message", "m", "--send"]
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = nr.main()
        os.unlink(path)
        return rc, out.getvalue(), err.getvalue()

    def test_a_successful_send_is_reported_with_its_channel(self):
        rc, out, _ = self._send(0)
        self.assertIn("SENT to channel 222", out)
        self.assertEqual(rc, 0)

    def test_a_failed_send_surfaces_stderr_and_is_not_silent(self):
        rc, _, err = self._send(3)
        self.assertIn("SEND FAILED rc=3", err)
        self.assertIn("boom", err)
        self.assertNotEqual(rc, 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
