#!/usr/bin/env python3
"""connector_cards: a connector queues a card, the bridge posts it, and only the owner's tap on that
posted card, in the DM it was posted in, comes back as a command."""
import importlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.append(str(Path(__file__).resolve().parents[3] / "tests"))
from _helpers.hermetic_env import isolate_os_environ  # noqa: E402

# Before the bridge is loaded below: no outer credential selector survives.
isolate_os_environ()

from ag2_sparrow import connector_cards as cc  # noqa: E402

ROOM, OWNER = "!dm:s.space", "@owner:s.space"


def model_card():
    ac, _ = cc._core()
    buttons = [ac.button(label, "reply", reply="Switch to %s" % label, command=ac.reply("model", "agent", alias))
               for label, alias in (("Opus", "opus"), ("Sonnet", "sonnet"))]
    return ac.build("model-switch", "pending", title="Model", body="Pick a model", text="Now: sonnet", buttons=buttons)


def headers(said, event="$card1", **over):
    base = {"access_tier": "owner", "user_id": OWNER, "channel_id": ROOM, "channel_kind": "dm",
            "reply_to_event": event, "card_click": json.dumps({"card_id": "model-switch", "action": said})}
    base.update(over)
    return {k: v for k, v in base.items() if v is not None}


class ConnectorCards(unittest.TestCase):
    def setUp(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        self.state, self.root = Path(d.name) / "bridge-state", Path(d.name) / "work"
        self.root.mkdir()
        self.sent, self.logs = [], []

    def poster(self, answer=None, clock=lambda: 1000.0):
        def post(payload):
            self.sent.append(payload)
            if isinstance(answer, Exception):
                raise answer
            return {"ok": True, "event_id": "$card1"} if answer is None else answer
        return cc.Poster(self.state, post, log=self.logs.append, clock=clock)

    def posted(self):
        body, card = model_card()
        cc.queue(self.state, ROOM, body, card, now=1000.0)
        self.assertEqual(self.poster().sweep(), 1)
        self.assertEqual(cc.absorb(self.state, self.root), 1)

    def test_a_queued_card_is_posted_once_with_the_card_beside_its_plain_line(self):
        body, card = model_card()
        qid = cc.queue(self.state, ROOM, body, card, now=1000.0)
        self.assertEqual(self.poster().sweep(), 1)
        (sent,) = self.sent
        self.assertEqual((sent["op"], sent["room_id"], sent["body"], sent["dedupe_key"]),
                         ("message", ROOM, "Pick a model", "agent-card:" + qid))
        self.assertEqual(sent["extra_content"], {"space.ag2.card": card})
        self.assertEqual(self.poster().sweep(), 0, "nothing is posted twice")
        self.assertEqual(len(self.sent), 1)

    def test_cards_queued_between_two_sweeps_are_posted_in_the_order_they_were_queued(self):
        ac, _ = cc._core()
        for n in range(12):
            cc.queue(self.state, ROOM, "card %d" % n,
                     ac.build("model-switch", "info", title="Model", body="b", text="step %d" % n)[1])
        self.assertEqual(self.poster().sweep(), 12)
        self.assertEqual([sent["body"] for sent in self.sent], ["card %d" % n for n in range(12)])

    def test_a_card_waits_behind_an_earlier_unposted_one_of_the_same_card_and_others_do_not(self):
        ac, _ = cc._core()

        def card(card_id, text):
            return ac.build(card_id, "info", title="T", body="b", text=text)[1]

        cc.queue(self.state, ROOM, "outcome", card("model-switch", "the outcome"))
        cc.queue(self.state, ROOM, "menu", card("model-switch", "the menu"))
        cc.queue(self.state, ROOM, "other card", card("account-switch", "another card"))
        cc.queue(self.state, "!other:s.space", "other room", card("model-switch", "another room"))
        fails = {"outcome"}

        def post(payload):
            self.sent.append(payload["body"])
            if payload["body"] in fails:
                raise OSError("down")
            return {"ok": True, "event_id": "$e%d" % len(self.sent)}

        poster = cc.Poster(self.state, post, log=self.logs.append, clock=lambda: 1000.0)
        self.assertEqual(poster.sweep(), 2)
        self.assertEqual(self.sent, ["outcome", "other card", "other room"], "the menu waits behind the outcome")
        fails.clear()
        self.assertEqual(poster.sweep(), 2)
        self.assertEqual(self.sent[3:], ["outcome", "menu"], "then both, in the order queued")
        # A post that returns no event id holds what is behind it the same way.
        cc.queue(self.state, ROOM, "first", card("model-switch", "1"))
        cc.queue(self.state, ROOM, "second", card("model-switch", "2"))
        self.assertEqual(cc.Poster(self.state, lambda p: {"ok": True}, log=self.logs.append, clock=lambda: 1000.0).sweep(), 0)
        self.assertEqual(len(list((self.state / "agent-cards" / "outbox").iterdir())), 2)

    def test_a_keyed_card_queued_again_is_one_card_before_and_after_it_is_posted(self):
        body, card = model_card()
        first = cc.queue(self.state, ROOM, body, card, key="notice-1", at_ns=1234)
        self.assertEqual(cc.queue(self.state, ROOM, body, card, key="notice-1", at_ns=1234), first)
        self.assertEqual(len(list((self.state / "agent-cards" / "outbox").iterdir())), 1, "queued once")
        self.assertEqual(self.poster().sweep(), 1)
        self.assertEqual(cc.absorb(self.state, self.root), 1)
        # After it was posted and recorded, the same call is still not a second card.
        self.assertEqual(cc.queue(self.state, ROOM, body, card, key="notice-1", at_ns=1234), first)
        self.assertEqual((self.poster().sweep(), len(self.sent)), (0, 1))
        # Another key is another card, and keyed cards keep the order of the moment they were decided.
        later = cc.queue(self.state, ROOM, body, card, key="notice-2", at_ns=5000)
        earlier = cc.queue(self.state, ROOM, body, card, key="notice-0", at_ns=100)
        self.assertLess(earlier, later)
        self.assertEqual(self.poster().sweep(), 2)
        # A marker older than a week is forgotten the next time a keyed card is queued.
        old = self.state / "agent-cards" / "keys" / first
        os.utime(old, (1.0, 1.0))
        cc.queue(self.state, ROOM, body, card, key="notice-3", at_ns=6000)
        self.assertFalse(old.exists())
        with mock.patch.object(Path, "iterdir", side_effect=OSError("gone")):
            cc._forget_old_keys(self.state / "agent-cards" / "keys")

    def test_a_card_queued_as_an_edit_is_shown_in_place_of_the_tapped_card_and_recorded_under_it(self):
        ac, _ = cc._core()
        self.posted()                                   # the menu, as $card1
        settled = ac.build("model-switch-status", "done", title="Model", body="Model: now Opus", text="Switched.")
        qid = cc.queue(self.state, ROOM, *settled, edit="$card1")
        self.assertEqual(self.poster({"ok": True, "event_id": "$edit1"}).sweep(), 1)
        sent = self.sent[-1]
        self.assertEqual((sent["op"], sent["event_id"], sent["room_id"], sent["dedupe_key"]),
                         ("edit", "$card1", ROOM, "agent-card-edit:" + qid))
        self.assertEqual(sent["extra_content"], {"space.ag2.card": settled[1]})
        self.assertEqual(cc.absorb(self.state, self.root), 1)
        # The menu's buttons no longer act: the event now shows the settled card.
        verdict = cc.click(headers("Switch to Opus"), "Switch to Opus", self.root)
        self.assertEqual(verdict.verdict, cc._core()[0].FORGED)
        with self.assertRaises(ValueError):
            cc.queue(self.state, ROOM, *settled, edit="not-an-event")

    def test_an_edit_the_server_refuses_is_posted_as_a_new_card_and_a_network_failure_is_retried(self):
        ac, _ = cc._core()
        body, card = ac.build("model-switch-status", "done", title="Model", body="Model: now Opus", text="Switched.")

        class Refused(Exception):
            code = 404

        cc.queue(self.state, ROOM, body, card, edit="$card1")
        self.assertEqual(self.poster(OSError("down")).sweep(), 0, "no answer: kept for the next sweep")
        calls = []

        def post(payload):
            calls.append(payload["op"])
            if payload["op"] == "edit":
                raise Refused()
            return {"ok": True, "event_id": "$new1"}

        self.assertEqual(cc.Poster(self.state, post, log=self.logs.append, clock=lambda: 1000.0).sweep(), 1)
        self.assertEqual(calls, ["edit", "message"])
        cc.queue(self.state, ROOM, body, card, edit="$card1")
        calls.clear()
        self.assertEqual(cc.Poster(self.state, lambda p: calls.append(p["op"]) or (
            {"is_error": True} if p["op"] == "edit" else {"event_id": "$new2"}),
            log=self.logs.append, clock=lambda: 1000.0).sweep(), 1)
        self.assertEqual(calls, ["edit", "message"], "an edit not accepted is a new card")
        posted = sorted((self.state / "agent-cards" / "posted").iterdir())
        self.assertEqual([json.loads(p.read_text())["event_id"] for p in posted], ["$new1", "$new2"])

    def test_what_became_of_a_queued_card_is_recorded_and_outlives_its_receipt(self):
        outbox = self.state.joinpath(*cc.OUTBOX)
        posted = cc.queue(self.state, ROOM, *model_card(), now=1000.0)
        self.assertEqual(cc.outcome(self.state, posted), "", "queued is not an outcome")
        self.assertEqual(self.poster(OSError("down")).sweep(), 0)
        self.assertEqual(cc.outcome(self.state, posted), "", "nor is a post that failed")
        self.assertEqual(self.poster().sweep(), 1)
        self.assertEqual(cc.outcome(self.state, posted), "posted")
        self.assertEqual(cc.absorb(self.state, self.root), 1)
        self.assertEqual(cc.outcome(self.state, posted), "posted", "the receipt is consumed; the outcome stays")
        # Dropped for age, and dropped for being unreadable: both are recorded before the entry goes.
        aged = cc.queue(self.state, ROOM, *model_card(), now=1000.0)
        self.assertEqual(self.poster(clock=lambda: 1000.0 + cc.MAX_QUEUED_S + 1).sweep(), 0)
        broken = "0" * 31 + "f"
        outbox.joinpath(broken + ".json").write_text("{not json", encoding="utf-8")
        self.assertEqual(self.poster().sweep(), 0)
        self.assertEqual((cc.outcome(self.state, aged), cc.outcome(self.state, broken), list(outbox.iterdir())),
                         ("dropped", "dropped", []))
        # Only a queue id is looked up, only the two words are an outcome, and an old one is forgotten.
        folder = self.state.joinpath(*cc.OUTCOMES)
        folder.joinpath("1" * 32).write_text("maybe", encoding="utf-8")
        self.assertEqual([cc.outcome(self.state, q) for q in ("1" * 32, "2" * 32, "../keys/x", "", None)], [""] * 5)
        os.utime(folder / aged, (0, 0))
        self.poster().sweep()
        cc._settle(self.state, "3" * 32, "posted")
        self.assertEqual((cc.outcome(self.state, aged), cc.outcome(self.state, posted)), ("", "posted"))

    def test_a_card_posted_before_a_local_failure_is_never_expired_as_dropped(self):
        outbox = self.state.joinpath(*cc.OUTBOX)
        late = lambda: 1000.0 + cc.MAX_QUEUED_S + 60  # noqa: E731
        # Posted and its receipt written; then recording the outcome fails, and the entry stays queued.
        qid = cc.queue(self.state, ROOM, *model_card(), now=1000.0)
        with mock.patch.object(cc, "_settle", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self.poster().sweep()
        self.assertEqual((len(self.sent), cc.outcome(self.state, qid)), (1, ""))
        self.assertEqual(self.poster(clock=late).sweep(), 0, "past the deadline: not posted again, not dropped")
        self.assertEqual((len(self.sent), cc.outcome(self.state, qid), list(outbox.iterdir())), (1, "posted", []))
        # The outcome was recorded but the entry could not be removed: the next sweep, late, keeps it posted.
        qid = cc.queue(self.state, ROOM, *model_card(), now=1000.0)
        with mock.patch.object(Path, "unlink", side_effect=OSError("busy")):
            with self.assertRaises(OSError):
                self.poster().sweep()
        cc.absorb(self.state, self.root)
        self.assertEqual(self.poster(clock=late).sweep(), 0)
        self.assertEqual((len(self.sent), cc.outcome(self.state, qid), list(outbox.iterdir())), (2, "posted", []))

    def test_an_expiry_after_a_send_that_failed_is_unsure_and_only_that_may_be_queued_again_under_its_key(self):
        late = lambda: 1000.0 + cc.MAX_QUEUED_S + 60  # noqa: E731
        body, card = model_card()
        keyed = dict(key="k1", at_ns=5)
        qid = cc.queue(self.state, ROOM, body, card, now=1000.0, **keyed)
        self.assertEqual(self.poster(OSError("timed out")).sweep(), 0)     # it may have reached the server
        self.assertEqual(self.poster(clock=late).sweep(), 0)
        self.assertEqual(cc.outcome(self.state, qid), "unsure", "not a confirmed drop")
        self.assertEqual(cc.queue(self.state, ROOM, body, card, **keyed), qid)
        self.assertEqual(list(self.state.joinpath(*cc.OUTBOX).iterdir()), [], "a plain repeat is still one card")
        self.assertEqual(cc.queue(self.state, ROOM, body, card, again=True, **keyed), qid, "the same id: the same dedupe key")
        self.assertEqual(cc.outcome(self.state, qid), "")
        self.sent.clear()
        self.assertEqual(self.poster(clock=__import__("time").time).sweep(), 1)
        self.assertEqual((self.sent[0]["dedupe_key"], cc.outcome(self.state, qid)), ("agent-card:" + qid, "posted"))
        self.assertEqual(cc.queue(self.state, ROOM, body, card, again=True, **keyed), qid)
        self.assertEqual(list(self.state.joinpath(*cc.OUTBOX).iterdir()), [], "a posted card is not queued again")
        # Never tried at all (the bridge was not running): a confirmed drop.
        idle = cc.queue(self.state, ROOM, body, card, now=1000.0, key="k2", at_ns=6)
        self.assertEqual(self.poster(clock=late).sweep(), 0)
        self.assertEqual(cc.outcome(self.state, idle), "dropped")

    def test_an_outcome_that_cannot_be_recorded_leaves_the_card_queued(self):
        qid = cc.queue(self.state, ROOM, *model_card(), now=1000.0)
        with mock.patch.object(cc, "_settle", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self.poster().sweep()
        self.assertTrue((self.state.joinpath(*cc.OUTBOX) / (qid + ".json")).is_file(), "never gone without a reason")
        self.assertEqual(self.poster().sweep(), 0, "its receipt is there: settled, not posted a second time")
        self.assertEqual((cc.outcome(self.state, qid), len(self.sent)), ("posted", 1))

    def test_a_card_that_would_not_render_or_has_no_room_is_refused_at_the_queue(self):
        body, card = model_card()
        for room, line, bad in (("", body, card), ("not-a-room", body, card), (ROOM, "", card),
                                (ROOM, "two\nlines", card), (ROOM, body, dict(card, kind="other"))):
            with self.assertRaises(ValueError):
                cc.queue(self.state, room, line, bad)
        self.assertEqual(self.poster().sweep(), 0)

    def test_a_room_id_without_a_server_is_a_room_at_the_queue_and_at_the_post(self):
        body, card = model_card()
        cc.queue(self.state, "!31hneApxJ_1o-63DmFrpeqnkFfWppnzWso1JvH3ogLM", body, card)
        self.assertEqual(self.poster().sweep(), 1)
        self.assertEqual(self.sent[0]["room_id"], "!31hneApxJ_1o-63DmFrpeqnkFfWppnzWso1JvH3ogLM")

    def test_a_failed_post_stays_queued_and_an_old_one_is_dropped(self):
        body, card = model_card()
        cc.queue(self.state, ROOM, body, card, now=1000.0)
        for answer in (OSError("down"), {"ok": True}, {"event_id": "not-an-event"}, "text"):
            self.assertEqual(self.poster(answer).sweep(), 0)
        self.assertEqual(cc.absorb(self.state, self.root), 0, "an unposted card is in no ledger")
        self.assertEqual(self.poster().sweep(), 1, "the retry posts it")
        cc.queue(self.state, ROOM, body, card, now=1000.0)
        self.assertEqual(self.poster(clock=lambda: 1000.0 + cc.MAX_QUEUED_S + 1).sweep(), 0)
        self.assertTrue(any("dropped" in line for line in self.logs))
        self.assertEqual(len(self.sent), 5)

    def test_files_that_are_not_queue_entries_are_left_or_removed_without_a_post(self):
        box = self.state.joinpath(*cc.OUTBOX)
        box.mkdir(parents=True)
        (box / "note.txt").write_text("not ours")
        (box / ("a" * 32 + ".json")).write_text("[")
        (box / ("b" * 32 + ".json")).write_text(json.dumps({"room": ROOM, "card": "x"}))
        self.assertEqual(self.poster().sweep(), 0)
        self.assertEqual((self.sent, sorted(p.name for p in box.iterdir())), ([], ["note.txt"]))

    def test_the_owners_tap_on_the_posted_card_in_its_dm_is_the_buttons_command(self):
        self.posted()
        verdict = cc.click(headers("Switch to Opus"), "Switch to Opus", self.root)
        self.assertEqual((verdict.verdict, verdict.command.kind, verdict.command.target, verdict.command.verb),
                         ("command", "model", "agent", "opus"))

    def test_any_other_click_is_refused(self):
        said = "Switch to Opus"
        before = cc.click(headers(said), said, self.root)
        self.assertEqual(before.verdict, "forged_or_stale_click", "a card the bridge never posted")
        self.posted()
        cases = {
            "another sender tier": headers(said, access_tier="team"),
            "a collaborator": headers(said, access_tier="team", collaborator="true"),
            "another room": headers(said, channel_id="!other:s.space"),
            "a shared room": headers(said, channel_kind="room"),
            "another card's event": headers(said, event="$other"),
            "the text without a tap": headers(said, card_click=None),
            "an unreadable click": headers(said, card_click="invalid"),
            "another card id": headers(said, card_click=json.dumps({"card_id": "x", "action": said})),
        }
        for name, heads in cases.items():
            self.assertEqual(cc.click(heads, said, self.root).verdict, "forged_or_stale_click", name)
        offered = cc.click(headers("Switch to Fable"), "Switch to Fable", self.root)
        self.assertEqual(offered.verdict, "forged_or_stale_click", "a button the card did not have")
        self.assertEqual(cc.click({"access_tier": "owner", "user_id": OWNER, "channel_id": ROOM, "channel_kind": "dm"},
                                  "hello", self.root).verdict, "not_a_card", "ordinary chat")


class BridgeWiring(unittest.TestCase):
    def test_the_bridge_posts_queued_cards_through_its_own_room_call_on_a_thread_of_its_own(self):
        with tempfile.TemporaryDirectory() as d:
            os.environ["AGENT_CONNECT_STATE_DIR"] = d
            os.environ.setdefault("REMOTE_TASK_URL", "https://gw.example/relay")
            os.environ.setdefault("REMOTE_TASK_TOKEN", "dummy-secret")
            m = importlib.reload(importlib.import_module("ag2_sparrow.remote_gateway_bridge"))
            body, card = model_card()
            cc.queue(m._STATE, ROOM, body, card)
            calls, threads = [], []

            class Thread:
                def __init__(self, target=None, name=None, daemon=None):
                    threads.append((target, name, daemon))

                def start(self):
                    pass

            class Timer:
                """The loop's own wait: two sweeps, then stop. The waits it was asked for are kept."""
                waits = []

                def __init__(self):
                    self.left = 2

                def wait(self, seconds):
                    Timer.waits.append(seconds)
                    self.left -= 1
                    return self.left < 0

            def req(method, path, payload, timeout=None):
                calls.append((method, path, payload["extra_content"], timeout))
                return {"ok": True, "event_id": "$e1"}

            slept = []
            with mock.patch.object(m.threading, "Thread", Thread), mock.patch.object(m.threading, "Event", Timer), \
                    mock.patch.object(m, "_req", req), mock.patch.object(m.time, "sleep", slept.append):
                m._start_agent_cards()
                (target, name, daemon), = threads
                self.assertEqual((name, daemon), ("sparrow-agent-cards", True))
                target()
            self.assertEqual(calls, [("POST", "/v1/room", {"space.ag2.card": card}, m._ROOM_POST_TIMEOUT_S)])
            self.assertEqual((slept, Timer.waits), ([], [m.AGENT_CARD_SWEEP_S] * 3),
                             "the sweep never calls time.sleep, which the poll loop's stall accounting counts")
            self.assertEqual(cc.absorb(m._STATE, Path(d) / "work"), 1)
            # A sweep that raises is logged and the loop goes on.
            logs = []
            with mock.patch.object(m.threading, "Thread", Thread), mock.patch.object(m.threading, "Event", Timer), \
                    mock.patch.object(m, "_log", logs.append), \
                    mock.patch.object(cc.Poster, "sweep", side_effect=RuntimeError("x")):
                m._start_agent_cards()
                threads[-1][0]()
            self.assertEqual(len([line for line in logs if "sweep error" in line]), 2)
            # A sweep that cannot even be set up is logged, and no thread is started.
            before, logs = len(threads), []
            with mock.patch.object(m.threading, "Thread", Thread), mock.patch.object(m, "_log", logs.append), \
                    mock.patch.object(cc, "Poster", side_effect=RuntimeError("boom")):
                m._start_agent_cards()
            self.assertEqual((len(threads), ["sweep not started" in line for line in logs]), (before, [True]))


if __name__ == "__main__":
    unittest.main()
