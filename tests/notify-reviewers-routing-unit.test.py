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
import datetime
import json
import os
import pathlib
import sys
import shutil
import subprocess
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

    def test_an_allowfrom_miss_is_unverified_not_an_absence(self):
        # allowFrom is inbound authorization, never membership, so an
        # omission proves nothing about whether the person is reachable.
        os.environ["CLAUDE_CONFIG_DIR"] = config_with({"groups": {"222": {"allowFrom": ["9"]}}})
        present, why = nr.discord_reachable(self._target())
        self.assertTrue(present)
        self.assertIn("inbound authorization", why)

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


_LEDGER_ISOLATION = None


def setUpModule():
    """Point the ask ledger at scratch for every case in this file.

    A case that reads the real ledger is not a unit test: an earlier case's park
    row makes a later one refuse, and the failure surfaces as an unrelated
    assertion about missing output.
    """
    global _LEDGER_ISOLATION
    _LEDGER_ISOLATION = os.environ.get("SUTANDO_REVIEW_ASKS_LEDGER")
    os.environ["SUTANDO_REVIEW_ASKS_LEDGER"] = tempfile.mkdtemp() + "/asks.jsonl"


def tearDownModule():
    if _LEDGER_ISOLATION is None:
        os.environ.pop("SUTANDO_REVIEW_ASKS_LEDGER", None)
    else:
        os.environ["SUTANDO_REVIEW_ASKS_LEDGER"] = _LEDGER_ISOLATION


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
        sys.argv[:] = ["notify_reviewers.py", "--reviewers", "d", "--message", "re-review https://github.com/sonichi/sutando/pull/3509"]
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

    def test_an_allowfrom_miss_plans_the_send_and_says_it_is_unchecked(self):
        # Refusing here would silence a reviewer who is in fact reachable.
        cfg = config_with({"groups": {"222": {"allowFrom": ["999"]}}})
        rc, out, err = self._run({"d": DISCORD}, cfg)
        self.assertNotIn("ABSENT from channel", err)
        self.assertIn("UNVERIFIED", err)
        self.assertIn("PLAN:", out)

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
        self._led = os.environ.get("SUTANDO_REVIEW_ASKS_LEDGER")
        os.environ["SUTANDO_REVIEW_ASKS_LEDGER"] = tempfile.mkdtemp() + "/asks.jsonl"

    def tearDown(self):
        sys.argv[:] = self._argv
        nr.subprocess.run = self._run
        for k, v in (("CLAUDE_CONFIG_DIR", self._cfg), ("SUTANDO_SCI_ROSTER", self._roster),
                     ("SUTANDO_REVIEW_ASKS_LEDGER", self._led)):
            os.environ.pop(k, None) if v is None else os.environ.update({k: v})

    def _send(self, rc_out, stdout=""):
        class _R:
            returncode, stderr = rc_out, "boom" if rc_out else ""
        _R.stdout = stdout
        nr.subprocess.run = lambda *a, **k: _R()
        fd, path = tempfile.mkstemp(suffix=".json")
        with os.fdopen(fd, "w") as f:
            json.dump({"d": DISCORD}, f)
        os.environ["SUTANDO_SCI_ROSTER"] = path
        os.environ["CLAUDE_CONFIG_DIR"] = config_with({"groups": {"222": {"allowFrom": ["111"]}}})
        # #3515's two-reviewer minimum landed after these cases; they test the
        # SEND path, so they take the documented escape rather than weakening.
        sys.argv[:] = ["notify_reviewers.py", "--reviewers", "d", "--message", "re-review https://github.com/sonichi/sutando/pull/3509", "--send",
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

    def test_a_successful_send_names_the_message_it_created(self):
        # Without the id there is no artifact naming what landed, so a live
        # delivery cannot be checked against the channel afterwards.
        rc, out, _ = self._send(0, stdout="m-123")
        self.assertIn("as message m-123", out)

    def test_a_failed_send_surfaces_stderr_and_is_not_silent(self):
        # rc 10 is proven NOT_DELIVERED: no post exists, so this is retryable.
        rc, _, err = self._send(10)
        self.assertIn("SEND FAILED rc=10", err)
        self.assertIn("boom", err)
        self.assertNotEqual(rc, 0)

    def test_a_landed_post_that_missed_its_target_is_not_a_retryable_failure(self):
        # rc 3 carries a CONFIRMED receipt and a message id: the post EXISTS,
        # so calling it SEND FAILED invites the repeat that duplicates it.
        rc, _, err = self._send(3)
        self.assertIn("LANDED BUT DID NOT TRIGGER", err)
        self.assertNotIn("SEND FAILED", err)
        self.assertEqual(rc, 4, "unsafe-to-repeat outranks a plain failure")


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
        # #3515's two-reviewer minimum landed after these cases; they test the
        # SEND path, so they take the documented escape rather than weakening.
        sys.argv[:] = ["notify_reviewers.py", "--reviewers", "d", "--message", "re-review https://github.com/sonichi/sutando/pull/3509", "--send",
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




class OneRecipientIsNotTwoPeople(unittest.TestCase):
    """The at-least-two gate counted ROUTES, so one person in two channels
    satisfied it and was pinged twice."""

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

    def _run_with(self, roster, names):
        os.environ["CLAUDE_CONFIG_DIR"] = config_with({"groups": {
            "222": {"allowFrom": ["111"]}, "333": {"allowFrom": ["111"]},
            "444": {"allowFrom": ["999"]}}})
        fd, path = tempfile.mkstemp(suffix=".json")
        with os.fdopen(fd, "w") as f:
            json.dump(roster, f)
        os.environ["SUTANDO_SCI_ROSTER"] = path
        sys.argv[:] = ["notify_reviewers.py", "--reviewers", names, "--message",
                       "re-review https://github.com/sonichi/sutando/pull/3509", "--send"]
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = nr.main()
        os.unlink(path)
        return rc, err.getvalue()

    def test_one_id_in_two_channels_is_one_person(self):
        rc, err = self._run_with(
            {"a1": {"discord_id": "111", "home_channel": "222"},
             "a2": {"discord_id": "111", "home_channel": "333"}}, "a1,a2")
        self.assertEqual(len(self.seen), 0, f"pinged one person twice: {self.seen}")
        self.assertEqual(rc, 5, err)

    def test_the_control_two_real_people_still_send(self):
        # Without this, a dedup that collapsed EVERYTHING would pass the case
        # above while silently refusing every legitimate pair.
        rc, err = self._run_with(
            {"a1": {"discord_id": "111", "home_channel": "222"},
             "b1": {"discord_id": "999", "home_channel": "444"}}, "a1,b1")
        self.assertEqual(len(self.seen), 2, f"two distinct people did not both send: {err}")
        self.assertEqual(rc, 0, err)


class MalformedAccessMapNeverAnswers(unittest.TestCase):
    """Unusable shapes are unverified, not verdicts.

    A scalar allowFrom ITERATES: "111" answers per character, which is a
    definite verdict computed from garbage.
    """

    def _reach(self, blob):
        cfg = config_with(blob)
        prev = os.environ.get("CLAUDE_CONFIG_DIR")
        os.environ["CLAUDE_CONFIG_DIR"] = cfg
        try:
            return nr.discord_reachable({"channel": "222", "discord_id": "111"})
        finally:
            os.environ.pop("CLAUDE_CONFIG_DIR", None) if prev is None else os.environ.update({"CLAUDE_CONFIG_DIR": prev})

    def test_truthy_non_object_section_is_unverified(self):
        ok, why = self._reach({"groups": "not-an-object"})
        self.assertTrue(ok)
        self.assertIn("not an object", why)

    def test_scalar_allow_from_is_unverified(self):
        ok, why = self._reach({"groups": {"222": {"allowFrom": 1}}})
        self.assertTrue(ok)
        self.assertIn("not a list", why)

    def test_string_allow_from_is_unverified_rather_than_per_character(self):
        ok, why = self._reach({"groups": {"222": {"allowFrom": "111"}}})
        self.assertTrue(ok)
        self.assertIn("not a list", why)

    def test_a_good_list_still_answers(self):
        ok, why = self._reach({"groups": {"222": {"allowFrom": ["111"]}}})
        self.assertTrue(ok)
        self.assertIn("listed in", why)


class DiscordLedgerBranchesRun(unittest.TestCase):
    """Execute the ledger branch, rather than asserting it exists in the source.

    Coverage measures execution: `assertIn("record_asks(", src)` passes on a
    line that never runs, which is how these branches reached CI uncovered.
    """

    def _send(self, rc_out, record_effect):
        class _R:
            returncode, stdout, stderr = rc_out, "", "boom" if rc_out else ""
        prev_run, prev_rec = nr.subprocess.run, nr.record_asks
        nr.subprocess.run = lambda *a, **k: _R()
        nr.record_asks = record_effect
        fd, path = tempfile.mkstemp(suffix=".json")
        with os.fdopen(fd, "w") as f:
            json.dump({"d": DISCORD}, f)
        prev_env = {k: os.environ.get(k) for k in ("SUTANDO_SCI_ROSTER", "CLAUDE_CONFIG_DIR")}
        os.environ["SUTANDO_SCI_ROSTER"] = path
        os.environ["CLAUDE_CONFIG_DIR"] = config_with({"groups": {"222": {"allowFrom": ["111"]}}})
        sys.argv[:] = ["notify_reviewers.py", "--reviewers", "d", "--message",
                       "see https://github.com/o/r/pull/1", "--send",
                       "--allow-single", "ledger-branch test"]
        out, err = io.StringIO(), io.StringIO()
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                rc = nr.main()
        finally:
            nr.subprocess.run, nr.record_asks = prev_run, prev_rec
            os.unlink(path)
            for k, v in prev_env.items():
                os.environ.pop(k, None) if v is None else os.environ.update({k: v})
        return rc, out.getvalue(), err.getvalue()

    def test_a_logged_ask_reports_the_count(self):
        rc, out, err = self._send(0, lambda msg, who, outcome='confirmed', actor=None, detail=None, endpoint=None: 2)
        self.assertIn("SENT to channel", out)
        self.assertIn("logged 2 PR ask(s)", err)

    def test_a_writer_that_cannot_reserve_refuses_before_sending(self):
        # The park is reserved BEFORE the post. If that write fails there is no
        # safe way to send: the post would be unrepeatable and unrecorded.
        def boom(msg, who, outcome='confirmed', actor=None, detail=None, endpoint=None):
            raise OSError("disk full")
        rc, out, err = self._send(0, boom)
        self.assertIn("REFUSED", err)
        self.assertIn("could not reserve", err)
        self.assertNotIn("SENT to channel", out)

    def test_a_write_failure_after_a_successful_reserve_is_loud_but_not_fatal(self):
        # The ask already happened; losing the record makes pr-unattended
        # report it as never asked, so this must warn rather than swallow.
        def boom(msg, who, outcome='confirmed', actor=None, detail=None, endpoint=None):
            if outcome == "pending":
                return 1
            raise OSError("disk full")
        rc, out, err = self._send(0, boom)
        self.assertIn("SENT to channel", out)
        self.assertIn("SUCCEEDED but was NOT recorded", err)
        self.assertIn("under-report", err)

    def test_unknown_outcome_is_not_reported_as_a_plain_failure(self):
        rc, _, err = self._send(4, lambda msg, who, outcome='confirmed', actor=None, detail=None, endpoint=None: 1)
        self.assertIn("OUTCOME UNKNOWN", err)
        self.assertNotIn("SEND FAILED", err)



class AllowFromIsNotMembership(unittest.TestCase):
    """An allowFrom miss is UNVERIFIED, never a positive absence.

    allowFrom is inbound authorization -- who may send -- and the bridge also
    grants via a global superset this file cannot see, so an omission is not
    evidence the reviewer is absent.
    """

    def _reach(self, blob):
        prev = os.environ.get("CLAUDE_CONFIG_DIR")
        os.environ["CLAUDE_CONFIG_DIR"] = config_with(blob)
        try:
            return nr.discord_reachable({"channel": "222", "discord_id": "111"})
        finally:
            os.environ.pop("CLAUDE_CONFIG_DIR", None) if prev is None else os.environ.update({"CLAUDE_CONFIG_DIR": prev})

    def test_globally_allowed_but_missing_from_the_channel_list_is_unverified(self):
        ok, why = self._reach({"allowFrom": ["111"],
                               "groups": {"222": {"allowFrom": ["999"]}}})
        self.assertTrue(ok, "reported a positive absence for a reachable reviewer")
        self.assertIn("inbound authorization", why)

    def test_being_listed_still_reads_as_listed(self):
        ok, why = self._reach({"groups": {"222": {"allowFrom": ["111"]}}})
        self.assertTrue(ok)
        self.assertIn("listed in", why)

    def test_non_scalar_elements_are_unverified(self):
        for bad in ([{"id": "111"}], [["111"]], [None], [True]):
            ok, why = self._reach({"groups": {"222": {"allowFrom": bad}}})
            self.assertTrue(ok, f"{bad!r} produced a verdict")
            self.assertIn("non-scalar", why, f"{bad!r} -> {why}")


class UnknownOutcomeIsParkedNotFailed(unittest.TestCase):
    """A send that may have landed is recorded, so a repeat cannot duplicate it."""

    def _run(self, rc_out):
        led = pathlib.Path(tempfile.mkdtemp()) / "review-asks.jsonl"
        prev_run, prev_led = nr.subprocess.run, nr.ledger_path
        class _R:
            returncode, stdout, stderr = rc_out, "", "OUTCOME UNKNOWN"
        nr.subprocess.run = lambda *a, **k: _R()
        nr.ledger_path = lambda: led
        fd, path = tempfile.mkstemp(suffix=".json")
        with os.fdopen(fd, "w") as f:
            json.dump({"d": DISCORD}, f)
        prev_env = {k: os.environ.get(k) for k in ("SUTANDO_SCI_ROSTER", "CLAUDE_CONFIG_DIR")}
        os.environ["SUTANDO_SCI_ROSTER"] = path
        os.environ["CLAUDE_CONFIG_DIR"] = config_with({"groups": {"222": {"allowFrom": ["111"]}}})
        sys.argv[:] = ["notify_reviewers.py", "--reviewers", "d", "--message",
                       "see https://github.com/o/r/pull/1", "--send",
                       "--allow-single", "unknown-outcome test"]
        out, err = io.StringIO(), io.StringIO()
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                rc = nr.main()
        finally:
            nr.subprocess.run, nr.ledger_path = prev_run, prev_led
            os.unlink(path)
            for k, v in prev_env.items():
                os.environ.pop(k, None) if v is None else os.environ.update({k: v})
        rows = [json.loads(l) for l in led.read_text().splitlines()] if led.exists() else []
        return rc, err.getvalue(), rows

    def test_an_unknown_outcome_is_recorded(self):
        rc, err, rows = self._run(4)
        self.assertTrue(rows, "nothing recorded; a repeat could duplicate the ping")
        # The ledger is append-only: the reservation is superseded, not rewritten,
        # so the verdict is the LAST row and the history stays readable.
        self.assertEqual([r["outcome"] for r in rows], ["pending", "unknown"])
        self.assertIn("may have landed", err)

    def test_an_unknown_outcome_has_its_own_exit_code(self):
        # Not 0 and not 1: a caller must not read it as sent OR as failed.
        self.assertEqual(self._run(4)[0], 4)

    def test_a_real_failure_is_still_a_failure_and_does_not_park(self):
        # rc 10 is proven NOT_DELIVERED, so the reservation is released. "Not
        # parked" is the property, not an empty file.
        rc, _, rows = self._run(10)
        self.assertEqual(rc, 1)
        self.assertEqual([r["outcome"] for r in rows], ["pending", "failed"])

    def test_an_ambiguous_child_exit_parks_rather_than_releasing(self):
        # A crash or a signal can happen AFTER the POST. Only a code the
        # interpreter cannot produce accidentally proves non-delivery.
        for rc_out in (1, -9, 137, 99):
            rc, err, rows = self._run(rc_out)
            self.assertEqual(rc, 4, f"rc={rc_out} released the park")
            self.assertIn("AMBIGUOUS EXIT", err)
            self.assertEqual(rows[-1]["outcome"], "unknown")

    def test_a_landed_post_missing_its_mention_parks_instead_of_failing(self):
        # rc 3 holds a CONFIRMED receipt: the post exists, so a repeat duplicates.
        rc, err, rows = self._run(3)
        self.assertEqual(rc, 4)
        self.assertIn("LANDED BUT DID NOT TRIGGER", err)
        self.assertEqual(rows[-1]["outcome"], "unknown")



class UnknownIsParkedPerTargetImmediately(unittest.TestCase):
    """An UNSAFE receipt must block the retry now, not after an age window."""

    MSG = "re-review https://github.com/sonichi/sutando/pull/3509"

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="park-")
        self.led = pathlib.Path(self.tmp) / "asks.jsonl"
        self._orig = nr.ledger_path
        nr.ledger_path = lambda: self.led

    def tearDown(self):
        nr.ledger_path = self._orig
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_an_unknown_row_parks_that_target(self):
        nr.record_asks(self.MSG, "keweichen", outcome="unknown")
        self.assertTrue(nr.unknown_parked(self.MSG, "keweichen"))

    def test_it_does_not_park_a_different_target(self):
        # Without this the park would silence the whole batch, which is the
        # opposite failure and just as expensive.
        nr.record_asks(self.MSG, "keweichen", outcome="unknown")
        self.assertFalse(nr.unknown_parked(self.MSG, "qingyun-wu"))

    def test_a_confirmed_row_does_not_park(self):
        nr.record_asks(self.MSG, "keweichen", outcome="confirmed")
        self.assertFalse(nr.unknown_parked(self.MSG, "keweichen"))

    def test_it_does_not_park_a_different_pr(self):
        nr.record_asks(self.MSG, "keweichen", outcome="unknown")
        other = "re-review https://github.com/sonichi/sutando/pull/3499"
        self.assertFalse(nr.unknown_parked(other, "keweichen"))

    def test_a_short_reference_records_nothing_and_says_so(self):
        # record_asks matches full URLs only, so this writes no row; the caller
        # must report that rather than claim the unknown was parked.
        self.assertEqual(0, nr.record_asks("re-review #3303", "keweichen",
                                           outcome="unknown"))
        self.assertFalse(self.led.exists() and self.led.read_text().strip())


class UnknownOutranksFailureInAMixedBatch(unittest.TestCase):
    def test_the_unknown_check_precedes_the_failure_check(self):
        # A failure is safe to retry and an unknown is not, so collapsing a
        # mixed batch to rc 1 invites exactly the duplicate the park prevents.
        src = pathlib.Path(nr.__file__).read_text()
        self.assertLess(src.index("    if unknowns:\n        return 4"),
                        src.index("    if failures or unlogged:\n        return 1"))



class UnknownBranchesActuallyRun(unittest.TestCase):
    """Drives the park and timeout paths in main(), not their source text."""

    MSG = "re-review https://github.com/sonichi/sutando/pull/3509"

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ub-")
        self.led = pathlib.Path(self.tmp) / "asks.jsonl"
        self._led, self._run = nr.ledger_path, nr.subprocess.run
        nr.ledger_path = lambda: self.led
        fd, self.roster = tempfile.mkstemp(suffix=".json")
        with os.fdopen(fd, "w") as f:
            json.dump({"d": DISCORD}, f)
        os.environ["SUTANDO_SCI_ROSTER"] = self.roster
        os.environ["CLAUDE_CONFIG_DIR"] = config_with(
            {"groups": {"222": {"allowFrom": ["111"]}}})

    def tearDown(self):
        nr.ledger_path, nr.subprocess.run = self._led, self._run
        os.unlink(self.roster)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run_main(self, message=None):
        sys.argv[:] = ["notify_reviewers.py", "--reviewers", "d",
                       "--message", message or self.MSG, "--send",
                       "--allow-single", "single-target send-path test"]
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = nr.main()
        return rc, out.getvalue(), err.getvalue()

    def test_a_prior_unknown_parks_the_send_and_never_calls_the_sender(self):
        nr.record_asks(self.MSG, "d", outcome="unknown")
        called = []
        nr.subprocess.run = lambda *a, **k: called.append(1)
        rc, _, err = self._run_main()
        self.assertIn("PARKED", err)
        self.assertEqual(rc, 4)
        self.assertEqual(called, [], "a parked target must not be re-sent")

    def test_a_timeout_is_unknown_not_failed_and_is_recorded(self):
        def boom(*a, **k):
            raise subprocess.TimeoutExpired(cmd="x", timeout=60)
        nr.subprocess.run = boom
        rc, _, err = self._run_main()
        self.assertIn("UNKNOWN outcome (TimeoutExpired)", err)
        self.assertIn("may have landed", err)
        self.assertEqual(rc, 4)
        self.assertIn("unknown", self.led.read_text())

    def test_a_spawn_failure_is_not_delivered_rather_than_unknown(self):
        # A spawn failure means no child ran and no POST was possible, so it is
        # definitely not delivered; parking it strands an ask that never started.
        def boom(*a, **k):
            raise OSError("exec failed")
        nr.subprocess.run = boom
        rc, _, err = self._run_main()
        self.assertIn("SEND FAILED before spawn", err)
        self.assertIn("safe to retry", err)
        self.assertNotIn("UNKNOWN outcome", err)
        self.assertEqual(rc, 1)
        self.assertFalse(nr.unknown_parked(self.MSG, "d"),
                         "a send that never started must not be parked")

    def test_an_unrecordable_message_is_refused_before_any_send(self):
        # Unreachable by construction: a message with no full PR URL cannot
        # record an unknown, so it never sends.
        called = []
        nr.subprocess.run = lambda *a, **k: called.append(1)
        rc, _, err = self._run_main(message="re-review #3303")
        self.assertIn("REFUSED", err)
        self.assertIn("no full PR URL", err)
        self.assertEqual(called, [], "must not send what it could not park")
        self.assertEqual(rc, 1)

    def test_a_ledger_write_failure_is_reported_not_swallowed(self):
        def boom(*a, **k):
            raise subprocess.TimeoutExpired(cmd="x", timeout=60)
        nr.subprocess.run = boom
        orig = nr.record_asks
        def bad(msg, who, outcome='confirmed', actor=None, detail=None, endpoint=None):
            if outcome == "pending":
                return orig(msg, who, outcome=outcome, actor=actor, detail=detail)
            raise OSError("disk full")
        nr.record_asks = bad
        try:
            rc, _, err = self._run_main()
        finally:
            nr.record_asks = orig
        self.assertIn("stayed PENDING", err)
        self.assertIn("a repeat is blocked", err)
        self.assertEqual(rc, 4)

    def test_the_absent_guard_still_refuses_if_reachability_ever_says_no(self):
        # Every current return from discord_reachable is True, so this branch is
        # unreachable today; the guard stays, and this pins what it must do.
        orig, nr.discord_reachable = nr.discord_reachable, lambda t: (False, "forced")
        called = []
        nr.subprocess.run = lambda *a, **k: called.append(1)
        try:
            rc, _, err = self._run_main()
        finally:
            nr.discord_reachable = orig
        self.assertIn("ABSENT from channel", err)
        self.assertEqual(called, [], "an absent target must not be sent to")
        self.assertNotEqual(rc, 0)

    def test_a_ledger_oserror_on_an_rc4_send_is_reported(self):
        class _R:
            returncode, stdout, stderr = 4, "", ""
        nr.subprocess.run = lambda *a, **k: _R()
        orig = nr.record_asks
        def bad(msg, who, outcome='confirmed', actor=None, detail=None, endpoint=None):
            if outcome == "pending":
                return orig(msg, who, outcome=outcome, actor=actor, detail=detail)
            raise OSError("disk full")
        nr.record_asks = bad
        try:
            rc, _, err = self._run_main()
        finally:
            nr.record_asks = orig
        # The settle write failed, so the reservation stands. The message must
        # say which way that fails: the park holds and the next run refuses.
        self.assertIn("stayed PENDING", err)
        self.assertIn("OUTCOME UNKNOWN", err)
        self.assertEqual(rc, 4)
        self.assertTrue(nr.unknown_parked(self.MSG, "d"),
                        "a failed settle must leave the park in force")

    def test_a_malformed_ledger_line_does_not_crash_the_park_check(self):
        self.led.write_text("not json\n" + json.dumps(
            {"repo": "sonichi/sutando", "pr": 3509, "reviewer": "d",
             "outcome": "unknown"}) + "\n")
        self.assertTrue(nr.unknown_parked(self.MSG, "d"))

    def test_an_unreadable_ledger_fails_CLOSED(self):
        # It cannot prove the target was NOT parked, and a refused send is
        # recoverable while a duplicated unsafe post is not.
        self.led.mkdir()
        self.assertTrue(nr.unknown_parked(self.MSG, "d"))




class DiscordAskIsRecorded(unittest.TestCase):
    """A delivered Discord ask must reach the ledger the Matrix path writes.

    Behavioural, not source-text: these used to grep notify_reviewers.py for a
    substring, which passes for a call that is present and never reached.
    """

    MSG = "re-review https://github.com/sonichi/sutando/pull/3509"

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.led = pathlib.Path(self.tmp) / "asks.jsonl"
        self._env = {k: os.environ.get(k) for k in
                     ("SUTANDO_SCI_ROSTER", "CLAUDE_CONFIG_DIR",
                      "SUTANDO_REVIEW_ASKS_LEDGER")}
        self._run, self._argv = nr.subprocess.run, sys.argv[:]
        os.environ["SUTANDO_REVIEW_ASKS_LEDGER"] = str(self.led)
        os.environ["CLAUDE_CONFIG_DIR"] = config_with(
            {"groups": {"222": {"allowFrom": ["111"]}}})
        fd, self.roster = tempfile.mkstemp(suffix=".json")
        with os.fdopen(fd, "w") as f:
            json.dump({"d": DISCORD}, f)
        os.environ["SUTANDO_SCI_ROSTER"] = self.roster

    def tearDown(self):
        nr.subprocess.run, sys.argv[:] = self._run, self._argv
        os.unlink(self.roster)
        shutil.rmtree(self.tmp, ignore_errors=True)
        for k, v in self._env.items():
            os.environ.pop(k, None) if v is None else os.environ.update({k: v})

    def _run_main(self, rc_out):
        class _R:
            returncode, stdout, stderr = rc_out, "", ""
        nr.subprocess.run = lambda *a, **k: _R()
        sys.argv[:] = ["notify_reviewers.py", "--reviewers", "d", "--message",
                       self.MSG, "--send", "--allow-single", "ledger behaviour"]
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = nr.main()
        rows = ([json.loads(l) for l in self.led.read_text().splitlines()]
                if self.led.exists() else [])
        return rc, err.getvalue(), rows

    def test_a_delivered_discord_ask_reaches_the_ledger(self):
        rc, _, rows = self._run_main(0)
        self.assertEqual(rc, 0)
        self.assertEqual(rows[-1]["outcome"], "confirmed",
                         "pr-unattended would read this delivered ask as unasked")
        self.assertEqual(rows[-1]["pr"], 3509)

    def test_the_sender_is_given_the_target_id_as_its_own_argument(self):
        # allowed_mentions can only be narrowed to a target the sender was
        # handed separately from the message body.
        argv = nr.discord_command_for({"discord_id": "111", "channel": "222"}, "hi")
        self.assertIn("111", argv, f"the id is not a discrete argument: {argv}")
        self.assertEqual(argv[-1], "<@111> hi")

    def test_an_unknown_outcome_is_not_folded_into_send_failed(self):
        rc, err, rows = self._run_main(4)
        self.assertEqual(rc, 4)
        self.assertIn("OUTCOME UNKNOWN", err)
        self.assertNotIn("SEND FAILED", err)
        self.assertEqual(rows[-1]["outcome"], "unknown")


class RepeatsAreMechanicallySuppressed(unittest.TestCase):
    """The composed controls: two invocations, not two halves tested apart.

    Each half passed alone while the composition resent, so these run main()
    twice against one ledger and count what the second run actually sends.
    """

    MSG = "re-review https://github.com/sonichi/sutando/pull/3509"

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.led = pathlib.Path(self.tmp) / "asks.jsonl"
        self._env = {k: os.environ.get(k) for k in
                     ("SUTANDO_SCI_ROSTER", "CLAUDE_CONFIG_DIR",
                      "SUTANDO_REVIEW_ASKS_LEDGER")}
        self._run, self._argv = nr.subprocess.run, sys.argv[:]
        os.environ["SUTANDO_REVIEW_ASKS_LEDGER"] = str(self.led)
        os.environ["CLAUDE_CONFIG_DIR"] = config_with(
            {"groups": {"222": {"allowFrom": ["111"]}}})

    def tearDown(self):
        nr.subprocess.run, sys.argv[:] = self._run, self._argv
        shutil.rmtree(self.tmp, ignore_errors=True)
        for k, v in self._env.items():
            os.environ.pop(k, None) if v is None else os.environ.update({k: v})

    def _roster(self, doc):
        fd, path = tempfile.mkstemp(suffix=".json")
        with os.fdopen(fd, "w") as f:
            json.dump(doc, f)
        os.environ["SUTANDO_SCI_ROSTER"] = path

    def _invoke(self, who="d"):
        sys.argv[:] = ["notify_reviewers.py", "--reviewers", who, "--message",
                       self.MSG, "--send", "--allow-single", "composed control",
                       "--widen-override", "the control re-runs on purpose"]
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = nr.main()
        return rc, err.getvalue()

    def _stub(self, rc_out):
        sends = []

        class _R:
            returncode, stdout, stderr = rc_out, "", ""

        def run(*a, **k):
            sends.append(a[0] if a else None)
            return _R()
        nr.subprocess.run = run
        return sends

    def test_a_landed_post_missing_its_mention_is_not_resent(self):
        # rc 3 already holds a message id, so a second post duplicates channel
        # content and still proves nothing about whether the Stand triggered.
        self._roster({"d": DISCORD})
        sends = self._stub(3)
        rc1, err1 = self._invoke()
        rc2, err2 = self._invoke()
        self.assertEqual((rc1, rc2), (4, 4))
        self.assertIn("LANDED BUT DID NOT TRIGGER", err1)
        self.assertIn("PARKED", err2)
        self.assertEqual(len(sends), 1, "the second invocation resent a landed post")

    def test_an_unknown_outcome_is_not_resent(self):
        self._roster({"d": DISCORD})
        sends = self._stub(4)
        rc1, _ = self._invoke()
        rc2, err2 = self._invoke()
        self.assertEqual((rc1, rc2), (4, 4))
        self.assertIn("PARKED", err2)
        self.assertEqual(len(sends), 1)

    def test_an_alias_of_a_parked_actor_is_also_parked(self):
        # Two roster spellings, one person, one Discord endpoint. Keying the
        # park by spelling let the second name resend to the same channel.
        self._roster({"d": DISCORD, "d_alias": dict(DISCORD, same_actor_as="d")})
        sends = self._stub(4)
        rc1, _ = self._invoke("d")
        rc2, err2 = self._invoke("d_alias")
        self.assertEqual((rc1, rc2), (4, 4))
        self.assertIn("PARKED", err2)
        self.assertEqual(len(sends), 1, "an alias resent to the same endpoint")

    def test_a_definite_failure_stays_retryable_across_invocations(self):
        # The negative control. Without it a park that refuses everything looks
        # identical to one that suppresses only the unsafe repeats.
        self._roster({"d": DISCORD})
        sends = self._stub(10)
        rc1, _ = self._invoke()
        rc2, err2 = self._invoke()
        self.assertEqual((rc1, rc2), (1, 1))
        self.assertNotIn("PARKED", err2)
        self.assertEqual(len(sends), 2, "a send that never landed must be retryable")


class ALegacyRowParksEverySpellingOfOneActor(unittest.TestCase):
    """A park keyed by spelling lets a resend out through an alias."""

    MSG = "re-review https://github.com/o/r/pull/7"
    ROSTER = {"alpha": {"discord_id": "1", "home_channel": "2"},
              "beta": {"discord_id": "1", "home_channel": "2",
                       "same_actor_as": "alpha"}}

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.led = pathlib.Path(self.tmp) / "asks.jsonl"
        self.prev = os.environ.get("SUTANDO_REVIEW_ASKS_LEDGER")
        os.environ["SUTANDO_REVIEW_ASKS_LEDGER"] = str(self.led)
        # A row predating the actor field, written under the OTHER spelling.
        self.led.write_text(json.dumps({
            "repo": "o/r", "pr": 7, "reviewer": "beta",
            "ts": "2026-08-29T11:00:00Z", "channel": "room",
            "outcome": "unknown"}) + "\n")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)
        if self.prev is None:
            os.environ.pop("SUTANDO_REVIEW_ASKS_LEDGER", None)
        else:
            os.environ["SUTANDO_REVIEW_ASKS_LEDGER"] = self.prev

    def _canon(self):
        m = nr._actor_map(self.ROSTER)
        return lambda w: m.get(w, w)

    def test_an_alias_is_parked_by_the_other_spellings_row(self):
        self.assertTrue(
            nr.unknown_parked(self.MSG, "alpha", "alpha", canonical=self._canon()),
            "a possibly-landed post can be resent through another spelling")

    def test_one_aliass_failure_does_not_settle_the_others_unknown(self):
        # beta posted with an unknown outcome, then alpha retried and failed.
        # One collapsed stream lets alpha's failure clear beta's.
        self.led.write_text("".join(json.dumps(r) + "\n" for r in (
            {"repo": "o/r", "pr": 7, "reviewer": "beta",
             "ts": "2026-08-29T10:00:00Z", "channel": "room", "outcome": "unknown"},
            {"repo": "o/r", "pr": 7, "reviewer": "alpha", "actor": "alpha",
             "ts": "2026-08-29T11:00:00Z", "channel": "room", "outcome": "pending"},
            {"repo": "o/r", "pr": 7, "reviewer": "alpha", "actor": "alpha",
             "ts": "2026-08-29T11:00:05Z", "channel": "room", "outcome": "failed"})))
        self.assertTrue(
            nr.unknown_parked(self.MSG, "alpha", "alpha", canonical=self._canon()),
            "alpha's failure cannot prove beta's post did not land")

    def test_an_actors_own_release_still_clears_its_own_park(self):
        # The safe negative control: without it, ORing across spellings could
        # park forever and pass the case above for the wrong reason.
        self.led.write_text("".join(json.dumps(r) + "\n" for r in (
            {"repo": "o/r", "pr": 7, "reviewer": "alpha", "actor": "alpha",
             "ts": "2026-08-29T11:00:00Z", "channel": "room", "outcome": "pending"},
            {"repo": "o/r", "pr": 7, "reviewer": "alpha", "actor": "alpha",
             "ts": "2026-08-29T11:00:05Z", "channel": "room", "outcome": "failed"})))
        self.assertFalse(
            nr.unknown_parked(self.MSG, "alpha", "alpha", canonical=self._canon()))

    def test_an_unrelated_actor_is_not_parked_by_it(self):
        # The negative control: canonicalization must not park everybody.
        roster = dict(self.ROSTER, gamma={"discord_id": "9", "home_channel": "8"})
        m = nr._actor_map(roster)
        self.assertFalse(
            nr.unknown_parked(self.MSG, "gamma", "gamma", canonical=lambda w: m.get(w, w)))


class TheReachabilityProbeResolvesTheClaudeHomeAndNeverRaises(unittest.TestCase):
    """Replacing the helper with the old private fallback left 84 tests green."""

    def _probe(self, **env):
        # HOME too: without it a miss falls through to the real ~/.claude and
        # the probe answers from this machine rather than the fixture.
        env.setdefault("HOME", tempfile.mkdtemp(prefix="home-iso-"))
        prev = {k: os.environ.get(k) for k in ("CLAUDE_CONFIG_DIR", "CLAUDE_HOME", "HOME")}
        for k in prev:
            os.environ.pop(k, None)
        os.environ.update({k: v for k, v in env.items() if v is not None})
        try:
            return nr.discord_reachable({"discord_id": "111", "channel": "222"})
        finally:
            for k, v in prev.items():
                os.environ.pop(k, None) if v is None else os.environ.update({k: v})

    def _home(self, allow):
        d = tempfile.mkdtemp(prefix="home-")
        chan = pathlib.Path(d, "channels", "discord")
        chan.mkdir(parents=True)
        (chan / "access.json").write_text(json.dumps({"groups": {"222": {"allowFrom": allow}}}))
        return d

    def test_it_honours_CLAUDE_HOME(self):
        # The tier a private `CLAUDE_CONFIG_DIR or ~/.claude` rederivation drops.
        ok, why = self._probe(CLAUDE_HOME=self._home(["111"]))
        self.assertTrue(ok)
        self.assertIn("allowFrom", why)

    def test_CLAUDE_CONFIG_DIR_takes_precedence(self):
        # Both maps mention allowFrom, so the old assertion passed either way.
        # The listed map and the EMPTY one give different reasons; pin which.
        ok, why = self._probe(CLAUDE_CONFIG_DIR=self._home(["111"]),
                              CLAUDE_HOME=self._home([]))
        self.assertIn("listed in groups allowFrom", why,
                      f"the CLAUDE_HOME map was read instead: {why}")
        ok2, why2 = self._probe(CLAUDE_CONFIG_DIR=self._home([]),
                                CLAUDE_HOME=self._home(["111"]))
        self.assertIn("no allowFrom", why2,
                      f"precedence did not hold in the other direction: {why2}")

    def test_a_missing_helper_is_unverified_not_an_exception(self):
        # The probe's contract is that it never raises; a tree without src/
        # must answer UNVERIFIED like any other unreadable map.
        orig = sys.modules.pop("util_paths", None)
        path_backup = sys.path[:]
        sys.path[:] = [p for p in sys.path if not p.endswith("/src")]
        sys.modules["util_paths"] = None       # force ModuleNotFoundError
        try:
            ok, why = self._probe()
            self.assertTrue(ok)
            self.assertTrue(why.startswith("unverified"), why)
        finally:
            sys.path[:] = path_backup
            if orig is not None:
                sys.modules["util_paths"] = orig
            else:
                sys.modules.pop("util_paths", None)


class OneBadRosterRowDoesNotStarveTheBatch(unittest.TestCase):
    """A malformed `same_actor_as` on an UNRELATED row raised for everyone."""

    def test_a_non_string_same_actor_as_is_skipped_not_fatal(self):
        roster = {"d": {"discord_id": "111", "home_channel": "222"},
                  "junk": {"discord_id": "9", "same_actor_as": ["a"]}}
        m = nr._actor_map(roster)       # must not raise
        self.assertEqual(m.get("d", "d"), "d")

    def test_a_valid_alias_still_unions(self):
        # The negative control: skipping bad values must not skip good ones.
        roster = {"d": {"discord_id": "111"},
                  "e": {"discord_id": "111", "same_actor_as": "d"}}
        m = nr._actor_map(roster)
        self.assertEqual(m["d"], m["e"])


class TheTwoReviewerGateCountsPeopleNotRows(unittest.TestCase):
    """"At least TWO" must mean two endpoints, or one Stand is pinged twice."""

    D = {"discord_id": "111", "home_channel": "222"}

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._env = {k: os.environ.get(k) for k in
                     ("SUTANDO_SCI_ROSTER", "CLAUDE_CONFIG_DIR",
                      "SUTANDO_REVIEW_ASKS_LEDGER")}
        self._run, self._argv = nr.subprocess.run, sys.argv[:]
        os.environ["SUTANDO_REVIEW_ASKS_LEDGER"] = str(pathlib.Path(self.tmp) / "a.jsonl")
        os.environ["CLAUDE_CONFIG_DIR"] = config_with(
            {"groups": {"222": {"allowFrom": ["111"]}}})

    def tearDown(self):
        nr.subprocess.run, sys.argv[:] = self._run, self._argv
        shutil.rmtree(self.tmp, ignore_errors=True)
        for k, v in self._env.items():
            os.environ.pop(k, None) if v is None else os.environ.update({k: v})

    def _run_main(self, roster, reviewers):
        rp = pathlib.Path(self.tmp) / "r.json"
        rp.write_text(json.dumps(roster))
        os.environ["SUTANDO_SCI_ROSTER"] = str(rp)
        sends = []

        class _R:
            returncode, stdout, stderr = 0, "m1", ""
        nr.subprocess.run = lambda *a, **k: (sends.append(1), _R())[1]
        sys.argv[:] = ["n", "--reviewers", reviewers, "--message",
                       "see https://github.com/o/r/pull/7", "--send"]
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                rc = nr.main()
            except SystemExit as e:
                rc = e.code
        return rc, len(sends), err.getvalue()

    def test_the_same_name_twice_does_not_satisfy_the_minimum(self):
        rc, sends, err = self._run_main({"d": self.D}, "d,d")
        self.assertIn("at least TWO", err)
        self.assertEqual(sends, 0, "one Stand was pinged twice")

    def test_two_aliases_of_one_actor_do_not_satisfy_it_either(self):
        roster = {"d": self.D, "e": dict(self.D, same_actor_as="d")}
        rc, sends, err = self._run_main(roster, "d,e")
        self.assertIn("at least TWO", err)
        self.assertEqual(sends, 0, "two spellings of one person passed the gate")

    def test_one_axis_colliding_is_enough_to_be_one_person(self):
        # A composite (actor, endpoint) key collapses only when BOTH match,
        # which is the easy case; either axis alone is still one person.
        same_actor = {"d": self.D,
                      "e": {"discord_id": "999", "home_channel": "888",
                            "same_actor_as": "d"}}
        rc, sends, err = self._run_main(same_actor, "d,e")
        self.assertIn("at least TWO", err)
        self.assertEqual(sends, 0, "same actor, different endpoints, passed the gate")

        same_endpoint = {"d": self.D, "g": dict(self.D)}
        rc, sends, err = self._run_main(same_endpoint, "d,g")
        self.assertIn("at least TWO", err)
        self.assertEqual(sends, 0, "different actors, same endpoint, passed the gate")

    def test_two_real_people_still_pass(self):
        # The negative control: dedup must not refuse a legitimate pair.
        roster = {"d": self.D, "f": {"discord_id": "999", "home_channel": "222"}}
        rc, sends, err = self._run_main(roster, "d,f")
        self.assertNotIn("at least TWO", err)
        self.assertEqual(sends, 2)


class AnAllowFromHitIsNotMembership(unittest.TestCase):
    """The positive-hit direction, previously reported as verified reachability."""

    def _probe(self, allow):
        prev = os.environ.get("CLAUDE_CONFIG_DIR")
        os.environ["CLAUDE_CONFIG_DIR"] = config_with(
            {"groups": {"222": {"allowFrom": allow}}})
        try:
            return nr.discord_reachable({"discord_id": "111", "channel": "222"})
        finally:
            if prev is None:
                os.environ.pop("CLAUDE_CONFIG_DIR", None)
            else:
                os.environ["CLAUDE_CONFIG_DIR"] = prev

    def test_a_hit_is_unverified_just_as_a_miss_is(self):
        for allow, label in ((["111"], "hit"), (["999"], "miss")):
            ok, why = self._probe(allow)
            self.assertTrue(ok, f"{label}: must not refuse")
            self.assertTrue(why.startswith("unverified"),
                            f"{label} reported as checked reachability: {why}")


class TheWidenRuleReadsAskHistoryNotRetrySafety(unittest.TestCase):
    """`_stale_repeat_ask` had no caller in this suite, so its rule was unpinned.

    The two questions differ: retry-safety wants the LAST outcome, ask-history
    wants whether a post ever landed. One reduction serving both erases a real
    ask whenever a later attempt fails.
    """

    MSG = "see https://github.com/sonichi/sutando/pull/3509"
    TARGETS = [{"name": "k"}]
    ROSTER = {"k": {"discord_id": "1", "home_channel": "2"}}

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.led = pathlib.Path(self.tmp) / "asks.jsonl"
        self.prev = os.environ.get("SUTANDO_REVIEW_ASKS_LEDGER")
        os.environ["SUTANDO_REVIEW_ASKS_LEDGER"] = str(self.led)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)
        if self.prev is None:
            os.environ.pop("SUTANDO_REVIEW_ASKS_LEDGER", None)
        else:
            os.environ["SUTANDO_REVIEW_ASKS_LEDGER"] = self.prev

    def _rows(self, *pairs):
        now = datetime.datetime.now(datetime.timezone.utc)
        self.led.write_text("".join(json.dumps({
            "repo": "sonichi/sutando", "pr": 3509, "reviewer": "k", "actor": "k",
            "ts": (now - datetime.timedelta(minutes=m)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "channel": "room", "outcome": o}) + "\n" for o, m in pairs))

    def _stale(self):
        return nr._stale_repeat_ask(self.MSG, self.TARGETS, self.ROSTER)[0]

    def _endpoint_rows(self, *pairs):
        """Rows as the CURRENT writer records them: keyed by durable endpoint."""
        now = datetime.datetime.now(datetime.timezone.utc)
        ep = nr.durable_endpoint(self.ROSTER["k"])
        self.led.write_text("".join(json.dumps({
            "repo": "sonichi/sutando", "pr": 3509, "reviewer": "k", "actor": "k",
            "endpoint": ep, "channel": "room", "outcome": o,
            "ts": (now - datetime.timedelta(minutes=m)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        }) + "\n" for o, m in pairs))

    def test_an_upper_cased_url_is_the_same_pull_request(self):
        # The writer lower-cases refs and the guard parsed them raw, so a
        # re-cased URL named a "different" PR and permitted a repeat ask.
        self._endpoint_rows(("confirmed", 31))
        up = self.MSG.replace("sonichi/sutando", "Sonichi/Sutando")
        self.assertNotEqual(up, self.MSG, "fixture did not re-case the URL")
        self.assertTrue(nr._stale_repeat_ask(up, self.TARGETS, self.ROSTER)[0],
                        "an upper-cased URL bypassed the stale-repeat guard")

    def test_the_widen_list_excludes_an_earlier_sorting_alias(self):
        # The reverse sort order alpha/beta cannot reach: the UNASKED alias
        # sorts first, so its own endpoint misses `prior`.
        roster = {"alpha": {"discord_id": "1", "home_channel": "c1",
                            "same_actor_as": "zeta"},
                  "zeta": {"discord_id": "2", "home_channel": "c2"},
                  "gamma": {"discord_id": "9", "home_channel": "c9"}}
        now = datetime.datetime.now(datetime.timezone.utc)
        self.led.write_text(json.dumps({
            "repo": "sonichi/sutando", "pr": 3509, "reviewer": "zeta",
            "actor": "zeta", "endpoint": nr.durable_endpoint(roster["zeta"]),
            "channel": "room", "outcome": "confirmed",
            "ts": (now - datetime.timedelta(minutes=31)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        }) + "\n")
        targets, _ = nr.resolve(["alpha"], roster)
        stale, why = nr._stale_repeat_ask(self.MSG, targets, roster)
        self.assertTrue(stale, "an alias of the asked actor read as a new ask")
        offered = why.split("Not yet asked:")[-1]
        self.assertNotIn("alpha", offered,
                         f"offered the already-asked person: {why}")
        self.assertIn("gamma", offered,
                      f"the unrelated person stopped being offered: {why}")

    def test_another_alias_of_the_same_person_is_not_a_new_ask(self):
        # Two aliases can hold DIFFERENT endpoints; comparing only the selected
        # alias's own let the second route re-ask the same person.
        roster = {"alpha": {"discord_id": "1", "home_channel": "c1"},
                  "beta": {"discord_id": "2", "home_channel": "c2",
                           "same_actor_as": "alpha"},
                  "gamma": {"discord_id": "9", "home_channel": "c9"}}
        now = datetime.datetime.now(datetime.timezone.utc)
        self.led.write_text(json.dumps({
            "repo": "sonichi/sutando", "pr": 3509, "reviewer": "alpha",
            "actor": "alpha", "endpoint": nr.durable_endpoint(roster["alpha"]),
            "channel": "room", "outcome": "confirmed",
            "ts": (now - datetime.timedelta(minutes=31)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        }) + "\n")
        beta, _ = nr.resolve(["beta"], roster)
        gamma, _ = nr.resolve(["gamma"], roster)
        self.assertTrue(nr._stale_repeat_ask(self.MSG, beta, roster)[0],
                        "a second alias of the same person read as a new ask")
        self.assertFalse(nr._stale_repeat_ask(self.MSG, gamma, roster)[0],
                         "an unrelated person was wrongly treated as asked")

    def test_an_endpoint_keyed_ask_still_counts_as_asked(self):
        # Endpoint-keyed rows made the subset test compare endpoints against
        # actor names, so a recorded ask was invisible and the guard failed open.
        self._endpoint_rows(("confirmed", 31))
        self.assertTrue(self._stale(),
                        "an endpoint-keyed ask read as never-asked")

    def test_an_endpoint_keyed_ask_is_not_offered_as_a_widen_target(self):
        # The verdict and the suggestion must share one id axis; otherwise the
        # message says everyone was asked AND names one of them as unasked.
        self._endpoint_rows(("confirmed", 31))
        _, why = nr._stale_repeat_ask(self.MSG, self.TARGETS, self.ROSTER)
        self.assertNotIn("Not yet asked: k", why,
                         f"already-asked target offered as a widen target: {why}")

    def test_the_control_a_name_keyed_ask_is_unchanged(self):
        # Legacy rows carry no endpoint; the fix must not need one to work.
        self._rows(("confirmed", 31))
        self.assertTrue(self._stale(), "a legacy name-keyed ask stopped counting")

    def test_a_later_failed_attempt_does_not_erase_an_ask_that_landed(self):
        self._rows(("confirmed", 90), ("pending", 5), ("failed", 5))
        self.assertTrue(self._stale(), "re-pinging someone asked 90 minutes ago")

    def test_a_landed_ask_alone_still_refuses_the_repeat(self):
        self._rows(("confirmed", 90))
        self.assertTrue(self._stale())

    def test_a_reservation_that_never_posted_leaves_the_retry_open(self):
        # The finding this exclusion was added for: nothing was posted, so the
        # widen rule must not treat the released reservation as an ask.
        self._rows(("pending", 90), ("failed", 90))
        self.assertFalse(self._stale())

    def test_an_unsafe_settle_counts_as_an_ask(self):
        self._rows(("pending", 90), ("unknown", 90))
        self.assertTrue(self._stale())

    def test_a_row_predating_the_outcome_field_still_counts_as_an_ask(self):
        # Every row written before this PR has no `outcome`. Reading absence as
        # "nothing was posted" would silently re-ping everyone asked so far.
        now = datetime.datetime.now(datetime.timezone.utc)
        self.led.write_text(json.dumps({
            "repo": "sonichi/sutando", "pr": 3509, "reviewer": "k",
            "ts": (now - datetime.timedelta(minutes=90)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "channel": "room"}) + "\n")
        self.assertTrue(self._stale())

    def test_every_selected_target_must_clear_the_window(self):
        # One global earliest reported the OLDEST ask as if it were everyone's,
        # so a person asked 5 minutes ago was refused on someone else's 90.
        now = datetime.datetime.now(datetime.timezone.utc)
        self.led.write_text("".join(json.dumps({
            "repo": "sonichi/sutando", "pr": 3509, "reviewer": w, "actor": w,
            "ts": (now - datetime.timedelta(minutes=m)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "channel": "room", "outcome": "confirmed"}) + "\n"
            for w, m in (("A", 90), ("B", 5))))
        roster = {"A": {"discord_id": "1", "home_channel": "2"},
                  "B": {"discord_id": "3", "home_channel": "4"}}
        both = nr._stale_repeat_ask(self.MSG, [{"name": "A"}, {"name": "B"}], roster)[0]
        self.assertFalse(both, "B was asked 5 minutes ago and must not be refused")
        # The positive control on the same ledger: A alone IS past the window.
        self.assertTrue(nr._stale_repeat_ask(self.MSG, [{"name": "A"}], roster)[0])

    def test_a_standing_reservation_carries_its_own_timestamp(self):
        # _first_ask stored "" for a pending-only actor, and "" is falsy, so the
        # age reduction dropped that actor and the whole set read as un-asked.
        now = datetime.datetime.now(datetime.timezone.utc)
        self.led.write_text(json.dumps({
            "repo": "sonichi/sutando", "pr": 3509, "reviewer": "k", "actor": "k",
            "ts": (now - datetime.timedelta(minutes=90)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "channel": "room", "outcome": "pending"}) + "\n")
        self.assertTrue(self._stale(), "a standing reservation was dropped from the age")

    def test_one_aliass_failure_does_not_settle_the_others_standing_ask(self):
        # The park was fixed per raw stream and ask-history was not, so the same
        # collapse still lost beta's 90-minute reservation and its timestamp.
        now = datetime.datetime.now(datetime.timezone.utc)
        def at(m):
            return (now - datetime.timedelta(minutes=m)).strftime("%Y-%m-%dT%H:%M:%SZ")
        self.led.write_text("".join(json.dumps(r) + "\n" for r in (
            {"repo": "sonichi/sutando", "pr": 3509, "reviewer": "beta",
             "ts": at(90), "channel": "room", "outcome": "pending"},
            {"repo": "sonichi/sutando", "pr": 3509, "reviewer": "alpha",
             "actor": "alpha", "ts": at(5), "channel": "room", "outcome": "pending"},
            {"repo": "sonichi/sutando", "pr": 3509, "reviewer": "alpha",
             "actor": "alpha", "ts": at(5), "channel": "room", "outcome": "failed"})))
        roster = {"alpha": {"discord_id": "1", "home_channel": "2"},
                  "beta": {"discord_id": "1", "home_channel": "2",
                           "same_actor_as": "alpha"}}
        for name in ("alpha", "beta"):
            self.assertTrue(
                nr._stale_repeat_ask(self.MSG, [{"name": name}], roster)[0],
                f"{name}'s standing 90m reservation was settled by alpha's failure")

    def test_a_single_streams_release_still_clears_its_own_ask_history(self):
        # The control: without it, never settling a pending would pass the case
        # above by refusing every repeat forever.
        now = datetime.datetime.now(datetime.timezone.utc)
        def at(m):
            return (now - datetime.timedelta(minutes=m)).strftime("%Y-%m-%dT%H:%M:%SZ")
        self.led.write_text("".join(json.dumps(r) + "\n" for r in (
            {"repo": "sonichi/sutando", "pr": 3509, "reviewer": "k", "actor": "k",
             "ts": at(90), "channel": "room", "outcome": "pending"},
            {"repo": "sonichi/sutando", "pr": 3509, "reviewer": "k", "actor": "k",
             "ts": at(5), "channel": "room", "outcome": "failed"})))
        self.assertFalse(self._stale())

    def test_a_recent_pending_does_not_inherit_an_old_confirmed_age(self):
        now = datetime.datetime.now(datetime.timezone.utc)
        def at(m):
            return (now - datetime.timedelta(minutes=m)).strftime("%Y-%m-%dT%H:%M:%SZ")
        self.led.write_text("".join(json.dumps({
            "repo": "sonichi/sutando", "pr": 3509, "reviewer": w, "actor": w,
            "ts": at(m), "channel": "room", "outcome": o}) + "\n"
            for w, o, m in (("A", "confirmed", 90), ("B", "pending", 3))))
        roster = {"A": {"discord_id": "1", "home_channel": "2"},
                  "B": {"discord_id": "3", "home_channel": "4"}}
        self.assertFalse(
            nr._stale_repeat_ask(self.MSG, [{"name": "A"}, {"name": "B"}], roster)[0],
            "B's 3-minute reservation was aged against A's 90 minutes")

    def test_a_recent_ask_is_not_stale_yet(self):
        # The negative control on the CLOCK rather than the outcome: without it
        # a rule that refused every repeat would pass all four cases above.
        self._rows(("confirmed", 5))
        self.assertFalse(self._stale())


class OneOwnerForTheLedgerReadContract(unittest.TestCase):
    """Both readers are projections over `_streams`, not two copies of it."""

    GOOD = {"repo": "o/r", "pr": 7, "reviewer": "k", "ts": "2026-08-29T11:00:00Z", "outcome": "confirmed"}

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.led = pathlib.Path(self.tmp) / "asks.jsonl"

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write(self, *rows):
        self.led.write_text("".join(
            (r if isinstance(r, str) else json.dumps(r)) + "\n" for r in rows))

    def test_malformed_lines_are_skipped_not_fatal(self):
        self._write("not json", self.GOOD)
        self.assertEqual(list(nr._streams(self.led)), [("o/r", "7", "k")])

    def test_a_valid_json_line_that_is_not_a_record_is_skipped(self):
        self._write('["not","a","record"]', '"bare"', "123", "null", self.GOOD)
        self.assertEqual(list(nr._streams(self.led)), [("o/r", "7", "k")])

    def test_identity_prefers_actor_and_falls_back_to_reviewer(self):
        self._write(dict(self.GOOD, reviewer="spelling", actor="canon"),
                    {"repo": "o/r", "pr": 7, "reviewer": "legacy", "ts": "2026-08-29T11:00:02Z",
                     "outcome": "unknown"})
        self.assertEqual(sorted(k[2] for k in nr._streams(self.led)),
                         ["canon", "legacy"])

    def test_an_absent_ledger_is_empty_not_an_error(self):
        self.assertEqual(nr._streams(pathlib.Path(self.tmp) / "nope.jsonl"), {})

    def test_a_malformed_field_value_never_crashes_a_reader(self):
        # Every shape below is a valid JSON object with one field of the wrong
        # type; each used to raise inside a reader or misattribute the stream.
        for bad in ({"repo": []}, {"actor": ["k"]}, {"actor": []},
                    {"outcome": ["unknown"]}, {"ts": 17}, {"pr": {}},
                    {"pr": True}, {"reviewer": None}):
            self._write(dict(self.GOOD, **bad))
            self.assertEqual(nr._streams(self.led), {}, f"{bad} was accepted")
            self.assertEqual(nr._latest_outcomes(self.led), {})
            self.assertEqual(nr._first_ask(self.led), {})

    def test_an_unrecognised_outcome_cannot_settle_a_park(self):
        # A typo'd outcome became the latest one and cleared a possibly-landed
        # post. It is a malformed row, so the earlier unknown still stands.
        self._write(dict(self.GOOD, ts="2026-08-29T11:00:01Z", outcome="unknown"),
                    dict(self.GOOD, ts="2026-08-29T11:00:02Z", outcome="typo"))
        self.assertEqual(nr._latest_outcomes(self.led),
                         {("o/r", "7", "k"): ("unknown", "2026-08-29T11:00:01.000000Z")})

    def test_a_falsy_timestamp_is_not_coerced_into_an_accepted_empty_string(self):
        # `d.get("ts") or ""` turned each of these into "" BEFORE the type
        # check, so the check never saw what was written.
        for bad in (0, False, [], {}):
            self._write(dict(self.GOOD, ts=bad))
            self.assertEqual(nr._streams(self.led), {}, f"ts={bad!r} was accepted")

    def test_a_well_shaped_but_impossible_instant_is_rejected(self):
        # A regex accepts these; they are not instants, and they sort below
        # every real stamp exactly as bare "0000" did.
        for bad in ("0000-00-00T00:00:00Z", "2026-02-30T11:00:00Z",
                    "2026-08-29T25:00:00Z", "2026-08-29T11:00:00+99:99",
                    "2026-08-29T11:00:00"):
            self._write(dict(self.GOOD, ts=bad))
            self.assertEqual(nr._streams(self.led), {}, f"ts={bad!r} was accepted")

    def test_an_offset_stamp_is_normalized_so_it_orders_against_a_Z_stamp(self):
        # Mixed offsets do not sort lexically; every accepted stamp is stored
        # in one fixed-width UTC form so the comparisons downstream hold.
        self._write(dict(self.GOOD, ts="2026-08-29T06:00:00-05:00"))
        self.assertEqual(nr._latest_outcomes(self.led),
                         {("o/r", "7", "k"): ("confirmed", "2026-08-29T11:00:00.000000Z")})

    def test_a_string_that_is_not_a_timestamp_is_rejected(self):
        # "0000" sorts below every real stamp, lending one actor's age to
        # another in the widen comparison.
        self._write(dict(self.GOOD, ts="0000"))
        self.assertEqual(nr._streams(self.led), {})

    def test_a_malformed_row_never_hides_a_later_unsafe_one(self):
        # The consequence that matters: one bad record must not stop a later
        # possibly-landed row from being seen, or the park silently clears.
        self._write({"repo": [], "pr": 7, "reviewer": "k", "ts": "2026-08-29T11:00:01Z"},
                    {"repo": "o/r", "pr": 7, "reviewer": "k", "ts": "2026-08-29T11:00:02Z",
                     "outcome": "unknown"})
        self.assertEqual(nr._latest_outcomes(self.led),
                         {("o/r", "7", "k"): ("unknown", "2026-08-29T11:00:02.000000Z")})

    def test_the_reader_streams_the_file_rather_than_materializing_it(self):
        # Peak traced memory, not a code read: restoring read_text().splitlines()
        # takes this from kilobytes to the size of the file.
        import tracemalloc
        self._write(*[dict(self.GOOD, ts=f"2026-08-29T{i//3600:02d}:{i//60%60:02d}:{i%60:02d}Z")
                      for i in range(20000)])
        size = self.led.stat().st_size
        tracemalloc.start()
        nr._streams(self.led)
        peak = tracemalloc.get_traced_memory()[1]
        tracemalloc.stop()
        self.assertLess(peak, size // 4,
                        f"peak {peak}B against a {size}B file — the read is not streaming")

    def test_state_is_bounded_per_stream_not_per_row(self):
        # The structural bound: 500 rows for one stream must not retain 500
        # anything — the result is per-stream and of fixed size.
        self._write(*[dict(self.GOOD, ts=f"2026-08-29T11:{i//60:02d}:{i%60:02d}Z", outcome="pending")
                      for i in range(500)])
        st = nr._streams(self.led)[("o/r", "7", "k")]
        self.assertEqual(st["n"], 500, "the count is kept")
        self.assertNotIn("rows", st)
        sizes = [len(v) for v in st.values() if isinstance(v, (list, tuple, dict))]
        self.assertTrue(all(n <= 2 for n in sizes), f"unbounded state: {st}")

    def test_the_projections_read_first_and_last_of_the_same_stream(self):
        # Order, without retaining rows: last-wins and earliest-ask must both
        # come out of one line-by-line fold.
        self._write(dict(self.GOOD, ts="2026-08-29T11:00:01Z", outcome="confirmed"),
                    dict(self.GOOD, ts="2026-08-29T11:00:02Z", outcome="pending"),
                    dict(self.GOOD, ts="2026-08-29T11:00:03Z", outcome="failed"))
        self.assertEqual(nr._latest_outcomes(self.led),
                         {("o/r", "7", "k"): ("failed", "2026-08-29T11:00:03.000000Z")})
        self.assertEqual(nr._first_ask(self.led), {("o/r", "7", "k"): "2026-08-29T11:00:01.000000Z"})

    def test_both_readers_derive_their_output_from_the_one_owner(self):
        # A mutant called `_streams`, discarded it, re-read the file and stayed
        # green. Feed a sentinel the file cannot produce, from a missing path.
        sentinel = {("S/S", "99", "sent"): {"last": ("unknown", "2026-08-29T11:00:09Z"),
                                            "first_ask": "2026-08-29T11:00:00Z", "n": 1}}
        missing = pathlib.Path(self.tmp) / "does-not-exist.jsonl"
        orig = nr._streams
        nr._streams = lambda led: sentinel
        try:
            self.assertEqual(nr._latest_outcomes(missing),
                             {("S/S", "99", "sent"): ("unknown", "2026-08-29T11:00:09Z")})
            self.assertEqual(nr._first_ask(missing), {("S/S", "99", "sent"): "2026-08-29T11:00:00Z"})
        finally:
            nr._streams = orig


if __name__ == "__main__":
    unittest.main(verbosity=2)
