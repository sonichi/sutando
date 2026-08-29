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
    def test_the_send_targets_the_channel_that_was_validated(self):
        # The composition defect: discord_reachable validated home_channel and
        # the sender resolved bot2bot, so the check guarded the wrong channel.
        argv = nr.discord_command_for({"discord_id": "111", "channel": "222"}, "hello")
        self.assertIn("222", argv)
        self.assertTrue(any("send_channel_message.py" in a for a in argv))
        self.assertFalse(any("bot2bot-post" in a for a in argv))
        self.assertIn("<@111> hello", argv)

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
        self.assertIn("222", out)          # the validated channel, not bot2bot
        self.assertIn("<@111>", out)
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
        # #3515's two-reviewer minimum landed after these cases; they test the
        # SEND path, so they take the documented escape rather than weakening.
        sys.argv[:] = ["notify_reviewers.py", "--reviewers", "d", "--message", "m", "--send",
                       "--allow-single", "single-target send-path test"]
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


class TwoChannelsDistinct(unittest.TestCase):
    """The control qingyun asked for: home_channel and bot2bot are DIFFERENT.

    Every earlier test used one channel, which is exactly the case that cannot
    show the defect — the notifier validated home_channel and the sender
    resolved bot2bot, so with one channel both halves agreed by accident.
    """

    def setUp(self):
        self._argv, self._cfg = sys.argv[:], os.environ.get("CLAUDE_CONFIG_DIR")
        self._roster, self._run = os.environ.get("SUTANDO_SCI_ROSTER"), nr.subprocess.run
        self.seen = []
        nr.subprocess.run = lambda a, **k: self.seen.append(a) or type(
            "R", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    def tearDown(self):
        sys.argv[:] = self._argv
        nr.subprocess.run = self._run
        for k, v in (("CLAUDE_CONFIG_DIR", self._cfg), ("SUTANDO_SCI_ROSTER", self._roster)):
            os.environ.pop(k, None) if v is None else os.environ.update({k: v})

    def test_the_send_goes_to_home_channel_not_bot2bot(self):
        # HOME=222 holds the reviewer; BOT2BOT=999 does not. Sending to 999
        # would reach nobody, which is the failure this whole PR is about.
        os.environ["CLAUDE_CONFIG_DIR"] = config_with({"groups": {
            "222": {"allowFrom": ["111"]},
            "999": {"role": "bot2bot", "allowFrom": ["someone-else"]}}})
        fd, path = tempfile.mkstemp(suffix=".json")
        with os.fdopen(fd, "w") as f:
            json.dump({"d": DISCORD}, f)
        os.environ["SUTANDO_SCI_ROSTER"] = path
        # #3515 landed a two-reviewer minimum after these cases were written.
        # They exercise the SEND path, not the reviewer-count rule, so they take
        # the documented escape rather than being weakened to accommodate it.
        sys.argv[:] = ["notify_reviewers.py", "--reviewers", "d", "--message", "m", "--send",
                       "--allow-single", "single-target send-path test"]
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = nr.main()
        os.unlink(path)
        self.assertEqual(rc, 0)
        self.assertEqual(len(self.seen), 1)
        argv = self.seen[0]
        self.assertIn("222", argv, f"sent somewhere other than the validated channel: {argv}")
        self.assertNotIn("999", argv, f"sent to bot2bot, which the reviewer is not in: {argv}")


if __name__ == "__main__":
    unittest.main(verbosity=2)


class DiscordAskIsRecorded(unittest.TestCase):
    """A delivered Discord ask must reach the ledger the Matrix path writes.

    The success branch used to `continue` before `record_asks`, so pr-unattended
    read a correctly-delivered ask as NOBODY_EVER_ASKED.
    """

    def test_the_discord_success_branch_calls_record_asks(self):
        src = (REPO / "skills" / "collaboration-intelligence" / "scripts"
               / "notify_reviewers.py").read_text()
        disc = src[src.index('if t["transport"] == "discord":'):]
        disc = disc[:disc.index('if a.room and t["room"] != a.room:')]
        self.assertIn("record_asks(", disc,
                      "the Discord success path does not write the ask ledger")

    def test_it_passes_the_target_id_so_mentions_can_be_pinned(self):
        src = (REPO / "skills" / "collaboration-intelligence" / "scripts"
               / "notify_reviewers.py").read_text()
        cmd = src[src.index("def discord_command_for("):]
        cmd = cmd[:cmd.index("\ndef ", 1)]
        self.assertIn('str(target["discord_id"]),', cmd,
                      "the sender cannot restrict allowed_mentions without the id")

    def test_unknown_outcome_is_not_folded_into_send_failed(self):
        src = (REPO / "skills" / "collaboration-intelligence" / "scripts"
               / "notify_reviewers.py").read_text()
        self.assertIn("OUTCOME UNKNOWN", src)
