#!/usr/bin/env python3
"""claude-local's quota notice: the account card sent to the owner's DM, unasked, when the session's pane
shows Claude Code's usage-limit banner (quota_card)."""
import json
import sys
import types
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.append(str(Path(__file__).resolve().parents[3] / "tests"))
from _helpers.hermetic_env import isolate_os_environ  # noqa: E402

isolate_os_environ()

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_claude_code_model_card import Base, DM  # noqa: E402
from ag2_sparrow import account_switch, connector_cards  # noqa: E402

from ag2_sparrow.adapters.claude_code import account_card, cli, detector, launcher, quota_card  # noqa: E402

# Claude Code 2.1.x at its limit, as the pane shows it.
LIMITED = "  ⎿  You've hit your usage limit · resets 3pm (America/Los_Angeles)\n\n❯ \n"
CREDITS = "You're out of credits. Add more to keep going.\n"
QUIET = "❯ \n  ? for shortcuts\n"


class Notice(Base):
    def setUp(self):
        super().setUp()
        self.cfg = cli.read_config(cli.config_path())
        self.at = [1000.0]
        self.logs = []
        p = mock.patch.object(account_card, "label", lambda cfg, env=None, run=None: "s…@example.com (max)")
        p.start()
        self.addCleanup(p.stop)

    def tick(self, text, advance=0.0):
        self.at[0] += advance
        return quota_card.notice(self.root, self.cfg, detector.abnormal_names(text), text,
                                 lambda *parts: self.logs.append(" ".join(map(str, parts))), now=lambda: self.at[0])

    def test_the_limit_banner_sends_the_account_card_to_the_owners_dm_once(self):
        quota_card.remember_room(self.root, DM)
        self.assertEqual(self.tick(QUIET), "")
        self.assertEqual(self.tick(LIMITED), "quota-card")
        (queued,) = self.queued()
        self.assertEqual(queued["room"], DM)
        card = queued["card"]
        self.assertEqual(card["id"], account_switch.CARD)
        self.assertIn("Claude Code has reached its usage limit (resets 3pm (America/Los_Angeles))", card["text"])
        self.assertIn("I'm signed in as s…@example.com (max).", card["text"])
        self.assertEqual(card[account_switch.CONTROLS], {"runtime": "claude"}, "the client draws the controls in it")
        for later in (5.0, 120.0, 3600.0):
            self.assertEqual(self.tick(LIMITED, later), "", "one card per limit")
        self.assertEqual(len(self.queued()), 1)

    def test_out_of_credits_says_so_and_a_banner_with_no_reset_time_says_that(self):
        quota_card.remember_room(self.root, DM)
        self.assertEqual(self.tick(CREDITS), "quota-card")
        text = self.queued()[0]["card"]["text"]
        self.assertIn("Claude Code is out of credits (it does not say when that resets)", text)

    def test_the_limit_over_then_back_is_a_new_card_and_a_banner_that_flickers_is_not(self):
        quota_card.remember_room(self.root, DM)
        self.assertEqual(self.tick(LIMITED), "quota-card")
        self.assertEqual(self.tick(QUIET, 10.0), "")
        self.assertEqual(self.tick(LIMITED, 30.0), "", "gone for half a minute: the same limit")
        self.assertEqual(self.tick(QUIET, 10.0), "")
        self.assertEqual(self.tick(QUIET, quota_card.CLEAR_S - 1), "")
        self.assertEqual(self.tick(QUIET, 2.0), "quota-cleared")
        self.assertEqual(quota_card.load(self.root), {"room": DM})
        self.assertEqual(self.tick(QUIET, 5.0), "")
        self.assertEqual(self.tick(LIMITED, 5.0), "quota-card")
        self.assertEqual(len(self.queued()), 2)

    def test_with_no_dm_seen_nothing_is_sent_until_the_owner_writes_there(self):
        self.assertEqual(self.tick(LIMITED), "quota-no-room")
        self.assertEqual(self.queued(), [])
        self.assertEqual(self.tick(LIMITED, 5.0), "", "asked again no sooner than a minute")
        # The owner's own message in their DM is claimed (and cannot be answered): its room is kept.
        self.claim(self.task("task-q1", "are you there?"))
        self.assertEqual(quota_card.load(self.root)["room"], DM)
        self.assertEqual(self.tick(LIMITED, quota_card.RETRY_S), "quota-card")
        self.assertEqual(self.queued()[0]["room"], DM)

    def test_only_the_owner_in_their_dm_names_the_room(self):
        for n, (tier, kind) in enumerate((("team", "dm"), ("owner", "room"), ("guest", "dm"))):
            self.claim(self.task("task-n%d" % n, "hello", tier=tier, kind=kind))
        self.assertEqual(quota_card.load(self.root), {})
        quota_card.remember_room(self.root, "not a room")
        quota_card.remember_room(self.root, None)
        self.assertEqual(quota_card.load(self.root), {})
        with mock.patch.object(quota_card, "remember_room", side_effect=RuntimeError("broken")):
            rc, out = self.claim(self.task("task-n9", "hello"))
        self.assertEqual((rc, out.get("admission")), (0, "owner"), "a room that cannot be kept never holds a task")

    def test_a_card_that_cannot_be_queued_is_tried_again_and_is_the_same_card(self):
        quota_card.remember_room(self.root, DM)
        with mock.patch.object(connector_cards, "queue", side_effect=OSError("disk full")):
            self.assertEqual(self.tick(LIMITED), "quota-not-posted")
        kept = quota_card.load(self.root)
        self.assertFalse(kept.get("told"))
        self.assertEqual(self.tick(LIMITED, 1.0), "")
        self.assertEqual(self.tick(LIMITED, quota_card.RETRY_S), "quota-card")
        self.assertEqual(quota_card.load(self.root)["key"], kept["key"], "queued under the key saved first")
        # A stop after the queue and before `told` was saved queues it again under that key: still one card.
        again = dict(quota_card.load(self.root), told=False, tried=0)
        quota_card.save(self.root, again)
        self.assertEqual(self.tick(LIMITED, 1.0), "quota-card")
        self.assertEqual(len(self.queued()), 1)

    def test_a_state_that_cannot_be_written_or_read_sends_nothing(self):
        quota_card.remember_room(self.root, DM)
        with mock.patch.object(quota_card.records, "write_durable", side_effect=quota_card.records.RecordError("ro")):
            self.assertEqual(self.tick(LIMITED), "quota-not-posted")
        self.assertEqual(self.queued(), [])
        (Path(self.root) / "state" / quota_card.FILE).write_text("[1, 2]")
        self.assertEqual(quota_card.load(self.root), {})
        (Path(self.root) / "state" / quota_card.FILE).write_text("not json")
        self.assertEqual(quota_card.load(self.root), {})

    def test_the_reset_time_is_read_from_the_banner_line_only(self):
        self.assertEqual(quota_card.reset_words(LIMITED), "resets 3pm (America/Los_Angeles)")
        self.assertEqual(quota_card.reset_words("Weekly limit reached │ resets Nov 28, 9am. │ other"), "resets Nov 28, 9am")
        self.assertEqual(quota_card.reset_words("You've hit your usage limit"), "")
        self.assertEqual(quota_card.reset_words(None), "")

    def test_the_agents_state_dir_still_passes_the_start_check_with_the_record_in_it(self):
        from ag2_sparrow import task_lifecycle
        quota_card.remember_room(self.root, DM)
        self.tick(LIMITED)
        self.assertTrue((Path(self.root) / "state" / quota_card.FILE).is_file())
        self.assertIsNone(task_lifecycle.prepare_work_dir(self.root, check_only=True))


class Supervisor(unittest.TestCase):
    """The launcher's tick hands the pane to the notice, logs each change once, and survives its failure."""

    def lc(self):
        logs = []
        return types.SimpleNamespace(root="/r", cfg={}, clock=lambda: 5.0, quota_said="", log=logs.append), logs

    def test_each_outcome_is_logged_once_and_a_failure_never_raises(self):
        lc, logs = self.lc()
        seen = []
        with mock.patch.object(quota_card, "notice", side_effect=lambda root, cfg, names, text, log, now: (
                seen.append(names), log("a", 1), "quota-card")[-1]):
            launcher.Launcher.quota(lc, LIMITED)
            launcher.Launcher.quota(lc, LIMITED)
        self.assertEqual(seen[0], ["quota-limit"])
        self.assertEqual(logs, ["a 1", "quota: quota-card", "a 1"])
        with mock.patch.object(quota_card, "notice", side_effect=RuntimeError("broken")):
            launcher.Launcher.quota(lc, LIMITED)
        self.assertEqual(logs[-1], "quota: quota-failed (RuntimeError: broken)")
        with mock.patch.object(quota_card, "notice", return_value=""):
            launcher.Launcher.quota(lc, QUIET)
        self.assertEqual(len(logs), 4)

    def test_the_ticks_observation_calls_it_with_the_pane(self):
        source = Path(launcher.__file__).read_text()
        self.assertIn("self.observation = detector.derive(self.observation, text, plugin, self.clock())\n        self.quota(text)", source)


if __name__ == "__main__":
    unittest.main()
