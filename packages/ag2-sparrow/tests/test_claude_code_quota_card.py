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
        p = mock.patch.object(account_card, "label", lambda cfg, env=None, run=None: "sarah@example.com")
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
        self.assertIn("I'm using sarah@example.com.", card["text"])
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
        self.assertEqual(quota_card.load(self.root), {})
        self.assertEqual(quota_card.room(self.root), DM, "the room is the claim's record: untouched")
        self.assertEqual(self.tick(QUIET, 5.0), "")
        self.assertEqual(self.tick(LIMITED, 5.0), "quota-card")
        self.assertEqual(len(self.queued()), 2)

    def test_with_no_dm_seen_nothing_is_sent_until_the_owner_writes_there(self):
        self.assertEqual(self.tick(LIMITED), "quota-no-room")
        self.assertEqual(self.queued(), [])
        self.assertEqual(self.tick(LIMITED, 5.0), "", "asked again no sooner than a minute")
        # The owner's own message in their DM is claimed (and cannot be answered): its room is kept.
        self.claim(self.task("task-q1", "are you there?"))
        self.assertEqual(quota_card.room(self.root), DM)
        self.assertEqual(self.tick(LIMITED, quota_card.RETRY_S), "quota-card")
        self.assertEqual(self.queued()[0]["room"], DM)

    def test_only_the_owner_in_their_dm_names_the_room(self):
        for n, (tier, kind) in enumerate((("team", "dm"), ("owner", "room"), ("guest", "dm"))):
            self.claim(self.task("task-n%d" % n, "hello", tier=tier, kind=kind))
        self.assertEqual(quota_card.room(self.root), "")
        quota_card.remember_room(self.root, "not a room")
        quota_card.remember_room(self.root, None)
        self.assertEqual(quota_card.room(self.root), "")
        (Path(self.root) / "state" / quota_card.ROOM_FILE).write_text(json.dumps({"room": "not a room"}))
        self.assertEqual(quota_card.room(self.root), "", "a record that names no room is no room")
        with mock.patch.object(quota_card, "remember_room", side_effect=RuntimeError("broken")):
            rc, out = self.claim(self.task("task-n9", "hello"))
        self.assertEqual((rc, out.get("admission")), (0, "owner"), "a room that cannot be kept never holds a task")

    def test_a_card_that_cannot_be_queued_is_tried_again_and_is_the_same_card(self):
        quota_card.remember_room(self.root, DM)
        with mock.patch.object(connector_cards, "queue", side_effect=OSError("disk full")):
            self.assertEqual(self.tick(LIMITED), "quota-not-posted")
        kept = quota_card.load(self.root)
        self.assertFalse(kept.get("qid"))
        self.assertEqual(self.tick(LIMITED, 1.0), "")
        self.assertEqual(self.tick(LIMITED, quota_card.RETRY_S), "quota-card")
        self.assertEqual(quota_card.load(self.root)["key"], kept["key"], "queued under the key saved first")
        # A stop after the queue and before its id was saved queues it again under that key: still one card.
        again = {k: v for k, v in quota_card.load(self.root).items() if k not in ("qid", "queued")}
        quota_card.save(self.root, dict(again, tried=0))
        self.assertEqual(self.tick(LIMITED, 1.0), "quota-card")
        self.assertEqual(len(self.queued()), 1)
        # With no bridge to post it, nothing is queued and nothing is called told.
        quota_card.save(self.root, {})
        self.bridge.rename(self.bridge.with_name("bridge-gone"))
        self.assertEqual(self.tick(LIMITED, quota_card.RETRY_S), "quota-not-posted")
        self.assertFalse(quota_card.load(self.root).get("told"))

    def sweep(self, at, post=None):
        """The bridge's own sweep of the outbox, at its clock `at`; `post` answers its room call."""
        sent = []
        answer = post or (lambda payload: sent.append(payload) or {"ok": True, "event_id": "$ev%d" % len(sent)})
        connector_cards.Poster(self.bridge, answer, log=self.logs.append, clock=lambda: at).sweep()
        return sent

    def queued_at(self, qid):
        return json.loads((self.bridge.joinpath(*connector_cards.OUTBOX) / (qid + ".json")).read_text())["queued_at"]

    def test_queued_is_not_told_and_a_card_the_bridge_dropped_is_queued_again(self):
        quota_card.remember_room(self.root, DM)
        self.assertEqual(self.tick(LIMITED), "quota-card")
        qid = quota_card.load(self.root)["qid"]
        born = self.queued_at(qid)

        def down(payload):
            raise OSError("no route")
        self.assertEqual(self.tick(LIMITED, 30.0), "", "still in the outbox: waiting, not told")
        self.assertFalse(quota_card.load(self.root).get("told"))
        # No bridge ran until past MAX_QUEUED_S, so no send was ever tried: its sweep records a drop.
        self.assertEqual(self.sweep(born + connector_cards.MAX_QUEUED_S + 1), [])
        self.assertEqual((connector_cards.outcome(self.bridge, qid), self.queued()), ("dropped", []))
        self.assertEqual(self.tick(LIMITED, 5.0), "quota-card-dropped", "seen well inside the deadline: the record decides")
        self.assertEqual(self.tick(LIMITED, quota_card.RETRY_S), "quota-card")
        second = quota_card.load(self.root)["qid"]
        self.assertNotEqual(second, qid)
        # This one's send fails (it may have gone through) and it expires: unsure, so the same card again.
        self.sweep(self.queued_at(second) + 1, down)
        self.sweep(self.queued_at(second) + connector_cards.MAX_QUEUED_S + 1)
        self.assertEqual(connector_cards.outcome(self.bridge, second), "unsure")
        self.assertEqual(self.tick(LIMITED, 5.0), "quota-card-unsure")
        # The requeue fails once (and the supervisor restarts): the next try still queues it, never gives up.
        with mock.patch.object(connector_cards, "_write", side_effect=OSError("disk full")):
            self.assertEqual(self.tick(LIMITED, quota_card.RETRY_S), "quota-not-posted")
        self.assertEqual((connector_cards.outcome(self.bridge, second), self.queued()), ("unsure", []))
        self.assertEqual(self.tick(LIMITED, quota_card.RETRY_S), "quota-card")
        self.assertEqual(len(self.queued()), 1)
        self.assertEqual(connector_cards.outcome(self.bridge, second), "unsure", "stands until the bridge settles it")
        self.assertEqual(self.tick(LIMITED, 30.0), "", "queued again: waiting, not asked for a third time")
        self.assertEqual(quota_card.load(self.root)["qid"], second, "the same queue id: one message at the server")
        self.assertNotIn("again", quota_card.load(self.root))
        # The second is posted by a real sweep: told, once.
        (sent,) = self.sweep(self.queued_at(second) + 1)
        self.assertEqual(sent["room_id"], DM)
        self.assertEqual(self.tick(LIMITED, 5.0), "quota-card-posted")
        self.assertTrue(quota_card.load(self.root)["told"])
        self.assertEqual(self.tick(LIMITED, 3600.0), "", "one card per limit")

    def test_a_posted_card_whose_receipt_was_consumed_is_told_on_either_side_of_the_deadline(self):
        for late in (20.0, connector_cards.MAX_QUEUED_S + 300, 3 * 86400.0):
            with self.subTest(late=late):
                quota_card.save(self.root, {})
                quota_card.remember_room(self.root, DM)
                self.assertEqual(self.tick(LIMITED, quota_card.RETRY_S), "quota-card")
                qid = quota_card.load(self.root)["qid"]
                self.assertEqual(len(self.sweep(self.queued_at(qid) + 1)), 1)
                # Its receipt is taken into the agent's ledger at a claim, before the supervisor looks.
                self.assertEqual(connector_cards.absorb(self.bridge, self.root), 1)
                self.assertEqual(list(self.bridge.joinpath(*connector_cards.POSTED).iterdir()), [])
                self.assertEqual(self.tick(LIMITED, late), "quota-card-posted", "never queued a second time")
                self.assertEqual(self.queued(), [])
                self.assertEqual(self.tick(LIMITED, 60.0), "")

    def test_a_card_gone_with_no_recorded_outcome_is_neither_posted_nor_sent_again(self):
        quota_card.remember_room(self.root, DM)
        self.assertEqual(self.tick(LIMITED), "quota-card")
        qid = quota_card.load(self.root)["qid"]
        # Removed by something that is not the bridge's sweep: no outcome, no receipt.
        self.bridge.joinpath(*connector_cards.OUTBOX, qid + ".json").unlink()
        self.assertEqual(self.tick(LIMITED, 20.0), "", "early absence is not delivery")
        self.assertFalse(quota_card.load(self.root).get("told"))
        self.assertEqual(self.tick(LIMITED, connector_cards.MAX_QUEUED_S), "", "nor is late absence a drop")
        self.assertEqual(self.tick(LIMITED, quota_card.UNKNOWN_S), "quota-card-unknown")
        self.assertEqual(self.queued(), [], "not queued under a new key: it may have been posted")
        self.assertEqual(self.tick(LIMITED, 3600.0), "")
        # An older bridge's receipt, with no outcome record, still counts while it is there.
        quota_card.save(self.root, {})
        self.assertEqual(self.tick(LIMITED, quota_card.RETRY_S), "quota-card")
        again = quota_card.load(self.root)["qid"]
        posted = self.bridge.joinpath(*connector_cards.POSTED)
        posted.mkdir(parents=True, exist_ok=True)
        self.bridge.joinpath(*connector_cards.OUTBOX, again + ".json").rename(posted / (again + ".json"))
        self.assertEqual(self.tick(LIMITED, 5.0), "quota-card-posted")

    def test_the_claim_and_the_supervisor_never_undo_each_others_record(self):
        # The supervisor holds its snapshot of the incident while the claim learns the room, and saves after.
        self.assertEqual(self.tick(LIMITED), "quota-no-room")
        held = quota_card.load(self.root)
        quota_card.remember_room(self.root, DM)
        quota_card.save(self.root, dict(held, tried=0))
        self.assertEqual(quota_card.room(self.root), DM, "the room survives the supervisor's later save")
        self.assertEqual(self.tick(LIMITED, 1.0), "quota-card")
        # And the other way: the claim's write, landing after the incident's key was saved, keeps that key.
        key = quota_card.load(self.root)["key"]
        quota_card.remember_room(self.root, "!other:s.space")
        self.assertEqual(quota_card.load(self.root)["key"], key)
        self.assertEqual(sorted(p.name for p in (Path(self.root) / "state").iterdir() if p.name.startswith("quota-")),
                         ["quota-card.json", "quota-room.json"])

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
        self.assertEqual(quota_card.reset_words("You've hit your usage limit · resets at 9:30 PM (UTC)"), "resets 9:30pm (UTC)")
        self.assertEqual(quota_card.reset_words("You've hit your usage limit · resets soon, see https://x.example"), "",
                         "words that are not a time are not copied")
        self.assertEqual(quota_card.reset_words("You've hit your usage limit · resets 3pm (call 555 now)"), "resets 3pm")
        self.assertEqual(quota_card.reset_words("You've hit your usage limit"), "")
        self.assertEqual(quota_card.reset_words(None), "")
        # Other lines of the pane are never read, whatever they say (a transcript line about resets, a
        # pasted secret): only the recognised banner's own line gives the time.
        pane = ("the token resets tomorrow, key sk-secret-123\n"
                "  ⎿  You've hit your usage limit\n"
                "this counter resets at 9pm and the password is hunter2\n❯ \n")
        self.assertEqual(quota_card.reset_words(pane), "")
        self.assertNotIn("hunter2", quota_card.words(["quota-limit"], pane, "x"))
        self.assertNotIn("sk-secret", quota_card.words(["quota-limit"], pane, "x"))
        # Inside the banner line, only time-like characters are taken: nothing after an odd one.
        self.assertEqual(quota_card.reset_words("You've hit your usage limit · resets 3pm <b>x</b> $(rm)"), "resets 3pm")

    def test_the_agents_state_dir_still_passes_the_start_check_with_the_record_in_it(self):
        from ag2_sparrow import task_lifecycle
        quota_card.remember_room(self.root, DM)
        self.tick(LIMITED)
        for name in (quota_card.FILE, quota_card.ROOM_FILE):
            self.assertTrue((Path(self.root) / "state" / name).is_file(), name)
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
