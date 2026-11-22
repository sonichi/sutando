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

    def test_a_card_that_would_not_render_or_has_no_room_is_refused_at_the_queue(self):
        body, card = model_card()
        for room, line, bad in (("", body, card), ("not-a-room", body, card), (ROOM, "", card),
                                (ROOM, "two\nlines", card), (ROOM, body, dict(card, kind="other"))):
            with self.assertRaises(ValueError):
                cc.queue(self.state, room, line, bad)
        self.assertEqual(self.poster().sweep(), 0)

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


if __name__ == "__main__":
    unittest.main()
