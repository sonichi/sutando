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
# The live limit menu Claude Code opens under its banner: only that menu is the limit (detector.limit_menu).
MENU = ("\n What do you want to do?\n\n ❯ 1. Stop and wait for limit to reset\n"
        "   2. Wait here, then continue automatically\n   3. Switch to usage credits\n\n Enter to confirm · Esc to cancel\n")
LIMITED = "  ⎿  You've hit your usage limit · resets 3pm (America/Los_Angeles)\n" + MENU
CREDITS = "You're out of credits. Add more to keep going.\n"
QUIET = "❯ \n  ? for shortcuts\n"


class NoticeBase(Base):
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

    def sweep(self, at, post=None):
        """The bridge's own sweep of the outbox, at its clock `at`; `post` answers its room call."""
        sent = []
        answer = post or (lambda payload: sent.append(payload) or {"ok": True, "event_id": "$ev%d" % len(sent)})
        connector_cards.Poster(self.bridge, answer, log=self.logs.append, clock=lambda: at).sweep()
        return sent

    def queued_at(self, qid):
        return json.loads((self.bridge.joinpath(*connector_cards.OUTBOX) / (qid + ".json")).read_text())["queued_at"]


class Notice(NoticeBase):
    def test_the_limit_banner_sends_the_account_card_to_the_owners_dm_once(self):
        quota_card.remember_room(self.root, DM)
        self.assertEqual(self.tick(QUIET), "")
        self.assertEqual(self.tick(LIMITED), "quota-card")
        (queued,) = self.queued()
        self.assertEqual(queued["room"], DM)
        card = queued["card"]
        self.assertEqual(card["id"], account_switch.CARD)
        self.assertIn("sarah@example.com (Claude Code) has hit its usage limit; resets 3pm (America/Los_Angeles).", card["text"])
        self.assertTrue(card["text"].endswith(account_switch.BELOW), card["text"])
        self.assertEqual(account_switch.BELOW, "Switch this agent's account below.")
        self.assertEqual(card[account_switch.CONTROLS], {"runtime": "claude"}, "the client draws the controls in it")
        for later in (5.0, 120.0, 3600.0):
            self.assertEqual(self.tick(LIMITED, later), "", "one card per limit")
        self.assertEqual(len(self.queued()), 1)

    def test_out_of_credits_says_so_and_a_banner_with_no_reset_time_says_that(self):
        quota_card.remember_room(self.root, DM)
        self.assertEqual(self.tick(CREDITS), "quota-card")
        text = self.queued()[0]["card"]["text"]
        self.assertIn("sarah@example.com (Claude Code) is out of credits; it does not say when that resets.", text)

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
        # That message has no answer: it carries the card, and no unasked one is sent besides.
        self.assertEqual(self.tick(LIMITED, quota_card.RETRY_S), "quota-task-card x1")
        (queued,) = self.queued()
        self.assertEqual(queued["room"], DM)

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
        # The line under the banner ("/usage-credits to finish ...") is a limit line too, with no time: the card
        # said the limit "does not say when that resets" with the time one line up.
        two = "  ⎿  You've hit your weekly limit · resets Nov 28 at 1pm (UTC)\n     /usage-credits to finish what you’re working on.\n\n❯ \n"
        self.assertEqual(quota_card.reset_words(two), "resets Nov 28, 1pm (UTC)")
        # A banner the pane wrapped inside its "resets ..." is read on to the line under it.
        wrapped = "  ⎿  You've hit your weekly limit · resets Nov 28 at\n     1pm (UTC)\n\n❯ \n"
        self.assertEqual(quota_card.reset_words(wrapped), "resets Nov 28, 1pm (UTC)")
        self.assertEqual(quota_card.reset_words("You've hit your weekly limit · resets\nNov 28 at 1pm\n"), "resets Nov 28, 1pm")
        # Inside the banner line, only time-like characters are taken: nothing after an odd one.
        self.assertEqual(quota_card.reset_words("You've hit your usage limit · resets 3pm <b>x</b> $(rm)"), "resets 3pm")

    def test_the_agents_state_dir_still_passes_the_start_check_with_the_record_in_it(self):
        from ag2_sparrow import task_lifecycle
        quota_card.remember_room(self.root, DM)
        self.tick(LIMITED)
        for name in (quota_card.FILE, quota_card.ROOM_FILE):
            self.assertTrue((Path(self.root) / "state" / name).is_file(), name)
        self.assertIsNone(task_lifecycle.prepare_work_dir(self.root, check_only=True))


# Captured on a VM (Claude Code restarted after an account switch onto an account at its weekly limit):
# at startup, before any turn, the pane is the CLI's own limit menu, with no banner line of its own.
STARTUP_MENU = (
    "╭───────────────────────────────────────────────────╮\n"
    "│ ✻ Welcome to Claude Code!                         │\n"
    "│                                                   │\n"
    "│   /help for help, /status for your current setup  │\n"
    "╰───────────────────────────────────────────────────╯\n"
    "\n"
    " What do you want to do?\n"
    "\n"
    " ❯ 1. Stop and wait for limit to reset\n"
    "   2. Wait here, then continue automatically at Nov 28 at 1pm\n"
    "   3. Switch to usage credits\n"
    "\n"
    " Enter to confirm · Esc to cancel\n")
# The same menu as a turn that hits the limit shows it, under the CLI's banner.
TURN_MENU = (
    "> summarize the thread\n"
    "  ⎿  You've hit your weekly limit · resets Nov 28 at 1pm\n"
    "\n"
    " What do you want to do?\n"
    "\n"
    " ❯ 1. Stop and wait for limit to reset\n"
    "   2. Wait here, then continue automatically at Nov 28 at 1pm\n"
    "   3. Switch to usage credits\n"
    "\n"
    " Enter to confirm · Esc to cancel\n")
# A limit long over: its banner is still in view, far above the work the session did after it.
OLD_BANNER = ("  ⎿  You've hit your usage limit · resets 3pm (America/Los_Angeles)\n"
              + "".join("● step %d of the next answer\n" % n for n in range(12)) + "❯ \n")


class LimitMenu(NoticeBase):
    """Claude Code's limit menu is a limit: every owner message left unanswered gets the account card."""

    def text_of(self, queued):
        return queued["card"]["text"]

    def test_the_startup_menu_is_read_as_the_limit_with_its_reset_time(self):
        self.assertEqual(detector.abnormal_names(STARTUP_MENU), ["quota-limit"])
        self.assertEqual(detector.abnormal_names(TURN_MENU), ["quota-limit"])
        self.assertEqual(quota_card.reset_words(STARTUP_MENU), "resets Nov 28, 1pm")
        self.assertEqual(quota_card.reset_words(TURN_MENU), "resets Nov 28, 1pm")
        self.assertTrue(quota_card.current(STARTUP_MENU))
        self.assertFalse(quota_card.current(OLD_BANNER), "an old banner above later work is not the limit now")
        self.assertTrue(quota_card.current(LIMITED))
        # Only the menu's own option line gives the time, and only its time-like part.
        self.assertEqual(quota_card.reset_words(STARTUP_MENU.replace("at Nov 28 at 1pm", "whenever, key sk-1")), "")
        self.assertEqual(quota_card.reset_words("2. Wait here, then continue automatically at 1pm\n"), "",
                         "the option line alone, with no menu, is not read")

    def test_every_queued_owner_task_at_the_startup_menu_gets_the_card_and_nothing_is_pressed(self):
        for n in range(3):
            self.task("task-s%d" % n, "message %d" % n)
        # No DM room was ever remembered (the Mod never claimed anything behind the menu): the tasks name it.
        self.assertEqual(quota_card.room(self.root), "")
        self.assertEqual(self.tick(STARTUP_MENU), "quota-task-card x3")
        queued = self.queued()
        self.assertEqual(len(queued), 3, "one card per message, and no unasked card besides")
        for q in queued:
            self.assertEqual(q["room"], DM)
            self.assertEqual(q["card"]["id"], account_switch.CARD)
            self.assertEqual(q["card"][account_switch.CONTROLS], {"runtime": "claude"}, "the switch dropdown")
            # Said once where to switch: the card showed two sentences pointing below.
            self.assertEqual(self.text_of(q), "sarah@example.com (Claude Code) has hit its usage limit; resets Nov 28, "
                             "1pm. I can't answer until then. Switch this agent's account below.")
            self.assertNotIn("API key", self.text_of(q), "Claude Code is not offered an API key")
            self.assertNotIn("usage credits", self.text_of(q))
        self.assertEqual(quota_card.room(self.root), DM)
        # The messages stay queued for the session once the account is switched: none was answered here.
        self.assertEqual(sorted(p.name for p in self.inbox.iterdir()), ["task-s0.txt", "task-s1.txt", "task-s2.txt"])
        self.assertEqual(sorted(launcher.inbox.unanswered(self.root)), ["task-s0", "task-s1", "task-s2"])
        for later in (5.0, 120.0, 3600.0):
            self.assertEqual(self.tick(STARTUP_MENU, later), "", "each message is told once")
        self.assertEqual(len(self.queued()), 3)
        # A new message while the menu is still up is told too.
        self.task("task-s3", "still there?")
        self.assertEqual(self.tick(STARTUP_MENU, 5.0), "quota-task-card x1")
        self.assertEqual(len(self.queued()), 4)

    def test_a_limit_told_before_still_tells_each_new_message(self):
        quota_card.remember_room(self.root, DM)
        self.assertEqual(self.tick(TURN_MENU), "quota-card")
        quota_card.save(self.root, dict(quota_card.load(self.root), told=True))
        # A message already waiting is known a moment after the limit: the card just posted answers it. Each
        # limit used to put two identical cards in the owner's DM, the unasked one and this message's.
        self.task("task-t1", "hello?")
        self.assertEqual(self.tick(TURN_MENU, 30.0), "")
        self.assertEqual(len(self.queued()), 1)
        self.assertEqual(self.tick(TURN_MENU, 600.0), "", "and it is not told later either")
        # A message long after that card is answered with its own.
        self.task("task-t2", "still limited?")
        self.assertEqual(self.tick(TURN_MENU, 5.0), "quota-task-card x1")
        self.assertEqual(len(self.queued()), 2)

    def test_a_tasks_own_words_on_the_pane_are_not_the_limit(self):
        # An account with quota left was said to have hit its limit: the owner's message quoted the card, and the
        # terminal wrapped it so a line began "hit its usage limit".
        quota_card.remember_room(self.root, DM)
        self.task("task-q", "why does it say sarah@example.com has hit its usage limit; resets Nov 28? "
                            "then read with room_ops.py read --limit 30")
        pane = ("> why does it say sarah@example.com has\n  hit its usage limit; resets Nov 28? then read with\n"
                "  room_ops.py read --limit 30\n\n● Looking at the room now.\n❯ \n")
        self.assertEqual(detector.abnormal_names(pane), ["quota-limit"], "the pattern alone is fooled")
        for later in (0.0, 30.0, 120.0):
            self.assertEqual(self.tick(pane, later), "")
        self.assertEqual(self.queued(), [])
        # Claude Code's own banner under the same task is still the limit.
        self.assertEqual(self.tick(pane.replace("● Looking at the room now.", LIMITED.strip("\n")), 5.0),
                         "quota-task-card x1")
        # Nor a stall: the launcher raised "abnormal text on the pane" cards for the same wrapped words.
        own, plugin = quota_card.own_ui(self.root, pane), {"inflight": {}, "stuck": {}}
        seen = detector.derive({}, pane, plugin, 0.0, own=own)
        self.assertEqual((seen["abnormal"], detector.derive(seen, pane, plugin, 600.0, own=own)["stall"]), ([], None))
        fooled = detector.derive(detector.derive({}, pane, plugin, 0.0), pane, plugin, 600.0)
        self.assertEqual(fooled["stall"]["reason"], "abnormal:quota-limit", "the raw pane, as before")
        # The pane is left as it is when no task is waiting or its text cannot be read.
        self.assertEqual(quota_card.own_ui(str(self.root) + "-nowhere", pane), pane)

    def test_a_banner_a_new_session_redraws_from_the_conversation_before_is_history(self):
        # After a restart Claude Code shows the old transcript, its limit banner included: the card fired again.
        quota_card.remember_room(self.root, DM)

        def tick(text, advance, session):
            self.at[0] += advance
            return quota_card.notice(self.root, self.cfg, detector.abnormal_names(text), text, lambda *parts: None,
                                     now=lambda: self.at[0], session=session)
        self.assertEqual(tick(QUIET, 0.0, "s1"), "")                 # nothing drawn yet
        banner = LIMITED[:LIMITED.index(MENU)] + "\n❯ \n"
        for later in (5.0, 60.0, 600.0):
            self.assertEqual(tick(CREDITS, later, "s1"), "", "the banner was on the pane when the session started")
        # A usage-limit banner with no menu under it is words, redrawn or new: only the live menu is that limit.
        self.assertEqual(tick(banner + "● next answer\n" + banner, 5.0, "s1"), "")
        self.assertEqual(self.queued(), [])
        # The same out-of-credits banner again under the old one is a limit now.
        self.assertEqual(tick(CREDITS + "● next answer\n" + CREDITS, 5.0, "s1"), "quota-card")
        # The limit menu is never history, however early in a session.
        quota_card.new_session(self.root)
        self.assertEqual(tick(STARTUP_MENU, 1.0, "s2"), "quota-card")
        # A caller that names no session sets nothing aside.
        self.assertFalse(quota_card._history(self.root, LIMITED, 0.0, ""))
        # What it keeps is a file the launcher knows: any other name in state/ refuses the next start, as
        # "state/quota-seen.json is not this adapter's state" did on a test machine.
        kept = sorted(p.name for p in (Path(self.root) / "state").iterdir() if p.name.startswith("quota-"))
        self.assertIn(quota_card.SEEN_FILE, kept)
        from ag2_sparrow import task_lifecycle
        self.assertEqual([name for name in kept if name not in task_lifecycle.LOCAL_FEATURE_FILES], [])
        self.assertIsNone(task_lifecycle.prepare_work_dir(self.root, check_only=True), "the next start is not refused")

    def test_a_message_from_another_room_or_before_the_unasked_card_gets_its_own_card(self):
        self.assertTrue(quota_card._just_carded({"qid": "q", "queued": 100.0}, DM, DM, 130.0), "queued, not yet posted")
        for rec, to, at, why in (({"told": True, "queued": 100.0}, "!other:s.space", 130.0, "another room"),
                                 ({"told": True, "queued": 100.0}, DM, 100.0 + quota_card.SAME_CARD_S, "long after"),
                                 ({"told": True, "by_tasks": True}, DM, 130.0, "no unasked card was sent"),
                                 ({"told": True, "unknown": True, "queued": 100.0}, DM, 130.0, "it may never have shown"),
                                 ({}, DM, 130.0, "nothing sent yet")):
            self.assertFalse(quota_card._just_carded(rec, to, DM, at), why)

    def test_a_new_session_is_a_new_incident(self):
        quota_card.remember_room(self.root, DM)
        self.assertEqual(self.tick(STARTUP_MENU), "quota-card")
        quota_card.save(self.root, dict(quota_card.load(self.root), told=True))
        self.assertEqual(self.tick(STARTUP_MENU, 30.0), "")
        quota_card.new_session(self.root)    # the account switch restarted the session, onto a limited account
        self.assertEqual(quota_card.room(self.root), DM, "the DM room is kept")
        self.assertEqual(self.tick(STARTUP_MENU, 5.0), "quota-card")
        self.assertEqual(len(self.queued()), 2)

    def test_only_the_owners_own_unanswered_messages_get_it(self):
        quota_card.remember_room(self.root, DM)
        self.task("task-g", "hi", tier="guest")
        self.task("task-c", "Switch account", click={"card_id": account_switch.CARD, "action": "switch"})
        self.task("task-done", "answered already")
        (self.results / "task-done.txt").write_text("[s.space] done\n")
        self.task("task-r", "from a room", kind="room")
        self.assertEqual(self.tick(STARTUP_MENU), "quota-task-card x1")
        (only,) = self.queued()
        self.assertEqual(only["room"], DM, "the owner's message in a room is told in their DM")
        self.assertEqual(sorted(quota_card.load(self.root)["tasks"]), ["task-c", "task-g", "task-r"])
        self.assertEqual(self.tick(STARTUP_MENU, 5.0), "")
        self.assertEqual(len(self.queued()), 1)

    def test_an_account_that_cannot_be_read_is_named_as_claude_code_and_said_so(self):
        quota_card.remember_room(self.root, DM)
        self.task("task-u", "hello")
        with mock.patch.object(account_card, "label", side_effect=RuntimeError("no answer")):
            self.assertEqual(self.tick(STARTUP_MENU), "quota-task-card x1")
        text = self.text_of(self.queued()[0])
        self.assertTrue(text.startswith("Claude Code has hit its usage limit; resets Nov 28, 1pm."), text)
        self.assertIn("I could not read which account I'm signed in with.", text)

    def test_a_task_that_cannot_be_read_or_sent_is_not_told_and_tried_again_later(self):
        (self.inbox / "task-dir.txt").mkdir()       # unreadable: never told, never retried
        self.task("task-r", "from a room", kind="room")
        self.assertEqual(self.tick(STARTUP_MENU), "quota-no-room", "no DM room known: nowhere to tell it")
        self.assertEqual(self.queued(), [])
        self.assertEqual(quota_card.load(self.root)["tasks"], {"task-dir": {"skip": True}})
        quota_card.remember_room(self.root, DM)
        gone = self.bridge.with_name("bridge-gone")
        self.bridge.rename(gone)
        self.assertEqual(self.tick(STARTUP_MENU, quota_card.RETRY_S), "quota-not-posted", "no bridge to post it")
        gone.rename(self.bridge)
        with mock.patch.object(quota_card.records, "write_durable", side_effect=quota_card.records.RecordError("ro")):
            self.assertEqual(self.tick(STARTUP_MENU, quota_card.RETRY_S), "quota-not-posted", "its key not kept: not sent")
        self.assertEqual(self.queued(), [])
        self.assertEqual(self.tick(STARTUP_MENU, quota_card.RETRY_S), "quota-task-card x1")
        self.assertEqual(self.tick(STARTUP_MENU, 1.0), "")
        self.assertEqual(len(self.queued()), 1)

    def test_a_task_card_whose_send_was_unsure_is_queued_again_under_its_own_id(self):
        quota_card.remember_room(self.root, DM)
        self.task("task-n", "hello")
        self.assertEqual(self.tick(STARTUP_MENU), "quota-task-card x1")
        qid = quota_card.load(self.root)["tasks"]["task-n"]["qid"]

        def down(payload):
            raise OSError("no route")
        self.sweep(self.queued_at(qid) + 1, down)
        self.sweep(self.queued_at(qid) + connector_cards.MAX_QUEUED_S + 1)
        self.assertEqual((connector_cards.outcome(self.bridge, qid), self.queued()), ("unsure", []))
        self.assertEqual(self.tick(STARTUP_MENU, 5.0), "quota-task-card x1")
        self.assertEqual(quota_card.load(self.root)["tasks"]["task-n"]["qid"], qid, "one message at the server")
        self.assertEqual(len(self.queued()), 1)

    def test_a_root_with_no_tasks_folder_has_nothing_unanswered(self):
        self.assertEqual(launcher.inbox.unanswered(str(Path(self.root) / "missing")), [])
        (self.inbox / ".task-tmp.txt").write_text("x")
        (self.inbox / "notes.json").write_text("{}")
        self.assertEqual(launcher.inbox.unanswered(self.root), [])

    def test_words_that_read_like_the_limit_or_its_menu_send_nothing(self):
        quota_card.remember_room(self.root, DM)
        self.task("task-w", "what does the limit menu look like?")
        said = ("⏺ You've hit your usage limit is what Claude Code prints. Its menu reads:\n"
                "  1. Stop and wait for limit to reset\n  2. Wait here, then continue automatically\n"
                "  3. Switch to usage credits\n\n❯ \n")
        banner = "  ⎿  You've hit your usage limit · resets 3pm\n\n❯ \n"
        split = " ❯ 1. Stop and wait for limit to reset\n\nsome other text\n   2. Switch to usage credits\n"
        for text in (said, banner, split, "You've hit your usage limit\n" + said):
            self.assertFalse(detector.limit_menu(text), text)
            self.assertEqual(self.tick(text, 5.0), "", text)
        self.assertEqual(self.queued(), [])
        self.assertTrue(detector.limit_menu(STARTUP_MENU) and detector.limit_menu(TURN_MENU) and detector.limit_menu(LIMITED))

    def test_the_reset_time_is_the_current_limits_and_a_late_first_banner_is_not_history(self):
        older = "  ⎿  You've hit your usage limit · resets 3pm (UTC)\n● work done after it\n"
        untimed = "  ⎿  You've hit your weekly limit\n     /usage-credits to finish what you’re working on.\n\n❯ \n"
        self.assertEqual(quota_card.reset_words(older + untimed), "", "an older limit's time is not this one's")
        self.assertEqual(quota_card.reset_words(older + TURN_MENU), "resets Nov 28, 1pm", "the live menu's own time")
        # A session that ran quietly for a long time: its first banner is a limit, not startup history.
        quota_card.remember_room(self.root, DM)

        def tick(text, advance):
            self.at[0] += advance
            return quota_card.notice(self.root, self.cfg, detector.abnormal_names(text), text, lambda *parts: None,
                                     now=lambda: self.at[0], session="s9")
        self.assertEqual(tick(QUIET, 0.0), "")
        self.assertEqual(tick(CREDITS, 3600.0), "quota-card")

    def test_an_old_banner_in_view_tells_no_message(self):
        quota_card.remember_room(self.root, DM)
        self.task("task-o", "next question")
        self.assertEqual(self.tick(OLD_BANNER), "", "an old banner is not the limit")
        self.assertEqual(self.queued(), [], "and the message is not told")
        self.assertEqual(quota_card.load(self.root).get("tasks") or {}, {})

    def test_a_task_card_the_bridge_dropped_is_queued_again_once(self):
        quota_card.remember_room(self.root, DM)
        self.task("task-d", "hello")
        self.assertEqual(self.tick(STARTUP_MENU), "quota-task-card x1")
        first = quota_card.load(self.root)["tasks"]["task-d"]["qid"]
        born = self.queued_at(first)
        self.assertEqual(self.sweep(born + connector_cards.MAX_QUEUED_S + 1), [])
        self.assertEqual(connector_cards.outcome(self.bridge, first), "dropped")
        self.assertEqual(self.tick(STARTUP_MENU, quota_card.RETRY_S), "quota-task-card x1")
        second = quota_card.load(self.root)["tasks"]["task-d"]["qid"]
        self.assertNotEqual(second, first)
        (sent,) = self.sweep(self.queued_at(second) + 1)
        self.assertEqual(sent["room_id"], DM)
        self.assertEqual(self.tick(STARTUP_MENU, 5.0), "")
        self.assertEqual(self.queued(), [])

    def test_a_queued_card_covers_nothing_until_it_is_posted_and_a_drop_restores_the_notice(self):
        quota_card.remember_room(self.root, DM)
        # A message's card is queued, the message is answered, then the bridge drops the card unposted:
        # nothing reached the owner, so the incident's own card is sent.
        self.task("task-g", "hello")
        self.assertEqual(self.tick(STARTUP_MENU), "quota-task-card x1")
        first = quota_card.load(self.root)["tasks"]["task-g"]["qid"]
        (Path(self.root) / "tasks" / "task-g.txt").unlink()
        self.assertEqual(self.tick(STARTUP_MENU, 5.0), "", "queued, not posted: it waits, and is not told")
        self.assertFalse(quota_card.load(self.root).get("told"))
        self.assertEqual(self.sweep(self.queued_at(first) + connector_cards.MAX_QUEUED_S + 1), [])
        self.assertEqual(connector_cards.outcome(self.bridge, first), "dropped")
        self.assertEqual(self.tick(STARTUP_MENU, quota_card.RETRY_S), "quota-card", "the drop restores the notice")
        own = quota_card.load(self.root)["qid"]
        # A message that arrives while the incident's card is only queued is not skipped for good: when that
        # card is dropped too, the message still gets its card.
        self.task("task-h", "anyone there?")
        self.assertEqual(self.tick(STARTUP_MENU, 5.0), "")
        self.assertNotIn("skip", quota_card.load(self.root).get("tasks", {}).get("task-h", {}))
        self.assertEqual(self.sweep(self.queued_at(own) + connector_cards.MAX_QUEUED_S + 1), [])
        self.assertEqual(self.tick(STARTUP_MENU, connector_cards.MAX_QUEUED_S), "quota-card-dropped; quota-task-card x1")
        # Posted, it covers the incident: no unasked card besides.
        (sent,) = self.sweep(self.queued_at(quota_card.load(self.root)["tasks"]["task-h"]["qid"]) + 1)
        self.assertEqual(self.tick(STARTUP_MENU, 5.0), "")
        rec = quota_card.load(self.root)
        self.assertEqual((rec.get("told"), rec.get("by_tasks"), "covers" in rec), (True, True, False))
        self.assertEqual(self.queued(), [])

    def test_a_restarted_supervisor_never_tells_a_message_twice(self):
        quota_card.remember_room(self.root, DM)
        self.task("task-k", "hello")
        # A stop after the key was kept and before the queue id was: the same card, under the same key.
        with mock.patch.object(connector_cards, "queue", side_effect=OSError("stopped")):
            self.assertEqual(self.tick(STARTUP_MENU), "quota-not-posted")
        self.assertEqual(self.queued(), [])
        key = quota_card.load(self.root)["tasks"]["task-k"]["key"]
        self.assertEqual(self.tick(STARTUP_MENU, 1.0), "", "tried again no sooner than a minute")
        self.assertEqual(self.queued(), [])
        self.assertEqual(self.tick(STARTUP_MENU, quota_card.RETRY_S), "quota-task-card x1")
        self.assertEqual(quota_card.load(self.root)["tasks"]["task-k"]["key"], key)
        self.assertEqual(len(self.queued()), 1)


class Supervisor(unittest.TestCase):
    """The launcher's tick hands the pane to the notice, logs each change once, and survives its failure."""

    def lc(self):
        logs = []
        return types.SimpleNamespace(root="/r", cfg={}, clock=lambda: 5.0, quota_said="", log=logs.append), logs

    def test_each_outcome_is_logged_once_and_a_failure_never_raises(self):
        lc, logs = self.lc()
        seen = []
        with mock.patch.object(quota_card, "notice", side_effect=lambda root, cfg, names, text, log, now, session: (
                seen.append((names, session)), log("a", 1), "quota-card")[-1]):
            launcher.Launcher.quota(lc, LIMITED, "nonce-1")
            launcher.Launcher.quota(lc, LIMITED)
        self.assertEqual(seen, [(["quota-limit"], "nonce-1"), (["quota-limit"], "")], "the session it reads is named")
        self.assertEqual(logs, ["a 1", "quota: quota-card", "a 1"])
        with mock.patch.object(quota_card, "notice", side_effect=RuntimeError("broken")):
            launcher.Launcher.quota(lc, LIMITED)
        self.assertEqual(logs[-1], "quota: quota-failed (RuntimeError: broken)")
        with mock.patch.object(quota_card, "notice", return_value=""):
            launcher.Launcher.quota(lc, QUIET)
        self.assertEqual(len(logs), 4)

    def test_a_created_session_starts_a_new_incident(self):
        source = Path(launcher.__file__).read_text()
        self.assertIn("rec = self.retire_card()\n        self.new_quota_incident()\n", source)
        lc, logs = self.lc()
        with mock.patch.object(quota_card, "new_session") as new:
            launcher.Launcher.new_quota_incident(lc)
        new.assert_called_once_with("/r")
        self.assertEqual(logs, [])
        with mock.patch.object(quota_card, "new_session", side_effect=OSError("ro")):
            launcher.Launcher.new_quota_incident(lc)
        self.assertEqual(logs, ["quota: new session not recorded (OSError: ro)"], "advisory: never raises")

    def test_the_ticks_observation_calls_it_with_the_pane(self):
        source = Path(launcher.__file__).read_text()
        self.assertIn("own=quota_card.own_ui(self.root, text))\n        self.quota(text, ", source)


if __name__ == "__main__":
    unittest.main()
