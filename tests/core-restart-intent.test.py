#!/usr/bin/env python3
"""Tests for src/core_restart_intent.py — the easy-restart intent hand-off
(sonichi#2401): owner chat command parsing (exact-match, no prose triggers),
atomic write, consume-before-act semantics, and the stale/malformed drops
that keep an ancient or corrupt intent from firing a surprise restart.

Run: python3 tests/core-restart-intent.test.py
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
import threading
import time
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_HERE, "..", "src"))  # sibling workspace_default import
_SRC = os.path.join(_HERE, "..", "src", "core_restart_intent.py")
_spec = importlib.util.spec_from_file_location("core_restart_intent", _SRC)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)


class TestParseRestartCommand(unittest.TestCase):
    def test_commands_match(self):
        for text, action in [("restart core", "restart"), ("Restart Core", "restart"),
                             ("  restart the core  ", "restart"), ("core restart", "restart"),
                             ("restart core!", "restart"), ("stop core", "stop"),
                             ("Stop the core.", "stop"), ("core stop", "stop")]:
            self.assertEqual(_mod.parse_restart_command(text), action, text)

    def test_prose_never_triggers(self):
        for text in ["we should restart core tomorrow", "the restart core question",
                     "restart", "core", "restart the core when convenient",
                     "please restart core now", "", None]:
            self.assertIsNone(_mod.parse_restart_command(text), repr(text))


class TestWriteConsume(unittest.TestCase):
    def test_roundtrip_restart(self):
        with tempfile.TemporaryDirectory() as ws:
            p = _mod.write_intent(ws, "restart", "test")
            self.assertTrue(os.path.exists(p))
            self.assertEqual(_mod.consume_intent(ws), "restart")
            self.assertFalse(os.path.exists(p))  # consumed = deleted

    def test_consume_is_once(self):
        with tempfile.TemporaryDirectory() as ws:
            _mod.write_intent(ws, "stop", "test")
            self.assertEqual(_mod.consume_intent(ws), "stop")
            self.assertIsNone(_mod.consume_intent(ws))  # replay impossible

    def test_no_file_is_none(self):
        with tempfile.TemporaryDirectory() as ws:
            self.assertIsNone(_mod.consume_intent(ws))

    def test_unknown_action_rejected_at_write(self):
        with tempfile.TemporaryDirectory() as ws:
            with self.assertRaises(ValueError):
                _mod.write_intent(ws, "reboot-the-universe", "test")

    def test_stale_intent_dropped_and_consumed(self):
        with tempfile.TemporaryDirectory() as ws:
            p = _mod.write_intent(ws, "restart", "test")
            self.assertIsNone(_mod.consume_intent(ws, now=time.time() + _mod.STALE_SEC + 1))
            self.assertFalse(os.path.exists(p))  # stale file still consumed

    def test_malformed_json_dropped_and_consumed(self):
        with tempfile.TemporaryDirectory() as ws:
            p = _mod.intent_path(ws)
            os.makedirs(os.path.dirname(p))
            with open(p, "w") as f:
                f.write("{not json")
            self.assertIsNone(_mod.consume_intent(ws))
            self.assertFalse(os.path.exists(p))

    def test_unknown_action_in_file_dropped(self):
        with tempfile.TemporaryDirectory() as ws:
            p = _mod.intent_path(ws)
            os.makedirs(os.path.dirname(p))
            with open(p, "w") as f:
                json.dump({"action": "explode", "requested_at": time.time()}, f)
            self.assertIsNone(_mod.consume_intent(ws))

    def test_non_dict_payload_dropped(self):
        with tempfile.TemporaryDirectory() as ws:
            p = _mod.intent_path(ws)
            os.makedirs(os.path.dirname(p))
            with open(p, "w") as f:
                json.dump(["restart"], f)
            self.assertIsNone(_mod.consume_intent(ws))

    def test_unlink_failure_fails_closed(self):
        # qingyun #2408 P1: if the delete fails, the file survives and the
        # next 5s poll would replay the same action — a restart LOOP. No
        # positive consume → NO action, ever.
        with tempfile.TemporaryDirectory() as ws:
            p = _mod.write_intent(ws, "restart", "test")
            orig = os.unlink
            os.unlink = lambda pth: (_ for _ in ()).throw(OSError("locked"))
            try:
                self.assertIsNone(_mod.consume_intent(ws))   # fail closed
                self.assertIsNone(_mod.consume_intent(ws))   # and stays closed
            finally:
                os.unlink = orig
            self.assertTrue(os.path.exists(p))  # file intact for manual removal
            # once deletable again, the (non-stale) intent acts exactly once
            self.assertEqual(_mod.consume_intent(ws), "restart")
            self.assertIsNone(_mod.consume_intent(ws))

    def test_missing_requested_at_is_stale(self):
        with tempfile.TemporaryDirectory() as ws:
            p = _mod.intent_path(ws)
            os.makedirs(os.path.dirname(p))
            with open(p, "w") as f:
                json.dump({"action": "restart"}, f)  # requested_at absent → epoch 0 → stale
            self.assertIsNone(_mod.consume_intent(ws))



class TestAwaitConsumption(unittest.TestCase):
    """Ack-on-consumption (#3183): the bridge must not promise a restart that
    no executor will perform. Consumption is proven by the file disappearing —
    not by probing for one named consumer implementation."""

    def setUp(self):
        self.ws = tempfile.mkdtemp()

    def test_returns_true_when_executor_consumes(self):
        _mod.write_intent(self.ws, "restart", "test")
        calls = {"n": 0}

        def fake_sleep(_):
            calls["n"] += 1
            if calls["n"] == 2:  # an executor claims it on the 2nd poll
                _mod.consume_intent(self.ws)

        self.assertTrue(_mod.await_consumption(
            self.ws, timeout_sec=10, poll_sec=0, sleep=fake_sleep,
            now=lambda: 0.0))

    def test_returns_false_when_nothing_consumes(self):
        _mod.write_intent(self.ws, "restart", "test")
        t = {"v": 0.0}

        def fake_sleep(_):
            t["v"] += 1.0

        # File is never consumed -> must time out False, not hang or lie.
        self.assertFalse(_mod.await_consumption(
            self.ws, timeout_sec=3, poll_sec=0, sleep=fake_sleep,
            now=lambda: t["v"]))
        # ...and the intent is still on disk for a late executor / expiry.
        self.assertTrue(os.path.exists(_mod.intent_path(self.ws)))

    def test_already_consumed_before_first_poll_returns_immediately(self):
        # No intent on disk at all: return True without ever sleeping.
        def boom(_):
            raise AssertionError("must not sleep when already consumed")

        self.assertTrue(_mod.await_consumption(
            self.ws, timeout_sec=5, poll_sec=0, sleep=boom, now=lambda: 0.0))

class TestExclusiveIntent(unittest.TestCase):
    """One pending intent at a time (#3191 review). `await_consumption` reads a
    single deletion as "my request was taken", so two live intents would let one
    executor action satisfy two waiters expecting OPPOSITE actions."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ws = self.tmp.name
        os.makedirs(os.path.join(self.ws, "state"), exist_ok=True)

    def tearDown(self):
        self.tmp.cleanup()

    def test_superseding_opposite_action_is_rejected(self):
        _mod.write_intent(self.ws, "restart", "test")
        with self.assertRaises(_mod.IntentPending) as caught:
            _mod.write_intent(self.ws, "stop", "test")
        # The rejection names the SURVIVING action, so the ack can be accurate.
        self.assertEqual(caught.exception.action, "restart")
        # The first intent is untouched — the loser never clobbers the winner.
        self.assertEqual(_mod.peek_intent(self.ws)["action"], "restart")

    def test_rejection_leaves_exactly_one_consumable_intent(self):
        # The scenario from the review: restart, then stop, then one executor.
        _mod.write_intent(self.ws, "restart", "test")
        with self.assertRaises(_mod.IntentPending):
            _mod.write_intent(self.ws, "stop", "test")
        self.assertEqual(_mod.consume_intent(self.ws), "restart")
        self.assertIsNone(_mod.consume_intent(self.ws))

    def test_same_action_repeat_is_also_rejected(self):
        _mod.write_intent(self.ws, "stop", "test")
        with self.assertRaises(_mod.IntentPending):
            _mod.write_intent(self.ws, "stop", "test")

    def test_stale_intent_is_replaced_not_rejected(self):
        path = _mod.write_intent(self.ws, "restart", "test")
        with open(path) as f:
            d = json.load(f)
        d["requested_at"] = time.time() - (_mod.STALE_SEC + 60)
        with open(path, "w") as f:
            json.dump(d, f)
        # An abandoned intent must not wedge the command forever.
        _mod.write_intent(self.ws, "stop", "test")
        self.assertEqual(_mod.peek_intent(self.ws)["action"], "stop")

    def test_peek_never_consumes(self):
        _mod.write_intent(self.ws, "restart", "test")
        self.assertEqual(_mod.peek_intent(self.ws)["action"], "restart")
        self.assertEqual(_mod.peek_intent(self.ws)["action"], "restart")
        self.assertEqual(_mod.consume_intent(self.ws), "restart")

    def test_no_temp_files_left_behind(self):
        _mod.write_intent(self.ws, "restart", "test")
        with self.assertRaises(_mod.IntentPending):
            _mod.write_intent(self.ws, "stop", "test")
        leftovers = [f for f in os.listdir(os.path.join(self.ws, "state"))
                     if f.endswith(".tmp")]
        self.assertEqual(leftovers, [])



class TestExclusiveIntentEdges(unittest.TestCase):
    """The refusal paths of the exclusive claim. Each one decides whether the
    owner is told a request was queued, so none of them may be inferred."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ws = self.tmp.name
        self.state = os.path.join(self.ws, "state")
        os.makedirs(self.state, exist_ok=True)

    def tearDown(self):
        self.tmp.cleanup()

    def test_peek_returns_none_when_absent_or_unreadable(self):
        self.assertIsNone(_mod.peek_intent(self.ws))
        with open(_mod.intent_path(self.ws), "w") as f:
            f.write("{not json")
        self.assertIsNone(_mod.peek_intent(self.ws))

    def test_peek_returns_none_for_unknown_action(self):
        for payload in ({"action": "self-destruct", "requested_at": time.time()},
                        ["not", "a", "dict"]):
            with open(_mod.intent_path(self.ws), "w") as f:
                json.dump(payload, f)
            self.assertIsNone(_mod.peek_intent(self.ws))

    def test_racing_writer_refill_loses(self):
        # Both attempts collide and peek finds nothing live: someone else is
        # writing. Refuse rather than clobber a claim we cannot read.
        real_link = os.link

        def always_taken(src, dst):
            raise FileExistsError(dst)

        os.link = always_taken
        try:
            with self.assertRaises(_mod.IntentPending):
                _mod.write_intent(self.ws, "restart", "test")
        finally:
            os.link = real_link

    def test_undeletable_stale_intent_refuses_rather_than_clobbers(self):
        _mod.write_intent(self.ws, "restart", "test")
        path = _mod.intent_path(self.ws)
        with open(path) as f:
            d = json.load(f)
        d["requested_at"] = time.time() - (_mod.STALE_SEC + 60)
        with open(path, "w") as f:
            json.dump(d, f)
        real_unlink = os.unlink

        def refuse_intent(target):
            if str(target) == path:
                raise OSError("read-only")
            return real_unlink(target)

        os.unlink = refuse_intent
        try:
            with self.assertRaises(_mod.IntentPending):
                _mod.write_intent(self.ws, "stop", "test")
        finally:
            os.unlink = real_unlink

    def test_stale_intent_consumed_mid_clear_still_succeeds(self):
        # An executor consumes the stale intent between our peek and unlink:
        # the path is FREE, so claim it — don't report a request nobody made.
        path = _mod.write_intent(self.ws, "restart", "test")
        with open(path) as f:
            d = json.load(f)
        d["requested_at"] = time.time() - (_mod.STALE_SEC + 60)
        with open(path, "w") as f:
            json.dump(d, f)
        real_unlink = os.unlink

        def raced(target):
            if str(target) == path:
                real_unlink(target)          # the "executor" got there first
                raise FileNotFoundError(target)
            return real_unlink(target)

        os.unlink = raced
        try:
            self.assertEqual(_mod.write_intent(self.ws, "stop", "test"), path)
        finally:
            os.unlink = real_unlink
        self.assertEqual(_mod.peek_intent(self.ws)["action"], "stop")

    def test_tmp_cleanup_failure_never_masks_the_result(self):
        # The temp file is bookkeeping; failing to remove it must not turn a
        # successful claim into an error the owner sees.
        real_unlink = os.unlink

        def refuse_tmp(target):
            if str(target).endswith(".tmp"):
                raise OSError("gone already")
            return real_unlink(target)

        os.unlink = refuse_tmp
        try:
            self.assertEqual(_mod.write_intent(self.ws, "restart", "test"),
                             _mod.intent_path(self.ws))
        finally:
            os.unlink = real_unlink
        self.assertEqual(_mod.consume_intent(self.ws), "restart")



class TestConcurrentStaleReplacement(unittest.TestCase):
    """Two writers observing the SAME stale intent (qingyun review, #3191).
    Unserialized, each could unlink what the other just installed, so a waiter
    would see its own intent disappear and report a consumption that never
    happened. Exactly one writer may win, and its file must survive until
    `consume_intent` takes it."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ws = self.tmp.name
        os.makedirs(os.path.join(self.ws, "state"), exist_ok=True)
        path = _mod.write_intent(self.ws, "restart", "seed")
        with open(path) as f:
            d = json.load(f)
        d["requested_at"] = time.time() - (_mod.STALE_SEC + 60)
        with open(path, "w") as f:
            json.dump(d, f)

    def tearDown(self):
        self.tmp.cleanup()

    def _race(self, n=2):
        start = threading.Barrier(n)
        won, refused = [], []
        lock = threading.Lock()

        def writer(i):
            start.wait()
            try:
                p = _mod.write_intent(self.ws, "stop" if i else "restart", f"w{i}")
                with lock:
                    won.append((i, p))
            except _mod.IntentPending:
                with lock:
                    refused.append(i)

        threads = [threading.Thread(target=writer, args=(i,)) for i in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)
        self.assertFalse([t for t in threads if t.is_alive()], "writer deadlocked")
        return won, refused

    def test_writer_blocks_while_the_transition_lock_is_held(self):
        # Deterministic proof of serialization: hold the writer lock from
        # outside and the production function must not proceed past inspect.
        import fcntl
        done = threading.Event()

        def writer():
            try:
                _mod.write_intent(self.ws, "stop", "blocked")
            except _mod.IntentPending:
                pass
            done.set()

        with open(_mod.intent_path(self.ws) + ".lock", "a") as held:
            fcntl.flock(held.fileno(), fcntl.LOCK_EX)
            threading.Thread(target=writer, daemon=True).start()
            self.assertFalse(done.wait(timeout=1.0),
                             "write_intent did not serialize on the lock")
            fcntl.flock(held.fileno(), fcntl.LOCK_UN)
        self.assertTrue(done.wait(timeout=10), "writer never resumed")

    def test_exactly_one_writer_wins(self):
        # Repeated: a single race rarely lands in the peek/unlink window, so
        # one round passes even unserialized and proves nothing.
        for _ in range(60):
            self.setUp()
            won, refused = self._race()
            self.assertEqual(len(won), 1, f"won={won} refused={refused}")
            self.assertEqual(len(refused), 1)
            self.assertTrue(os.path.exists(_mod.intent_path(self.ws)),
                            "the winner's intent was deleted by the loser")

    def test_winners_intent_survives_until_consumed(self):
        # The whole point: the loser must not delete the winner's live file,
        # or the winner's waiter reads that deletion as executor consumption.
        won, _ = self._race()
        self.assertTrue(os.path.exists(_mod.intent_path(self.ws)))
        self.assertIsNotNone(_mod.peek_intent(self.ws))
        # No executor has run, so a zero-timeout wait must NOT claim success.
        self.assertFalse(_mod.await_consumption(
            self.ws, timeout_sec=0, poll_sec=0, sleep=lambda _: None, now=lambda: 0.0))
        self.assertIsNotNone(_mod.consume_intent(self.ws))

    def test_no_temp_files_survive_the_race(self):
        self._race()
        leftovers = [f for f in os.listdir(os.path.join(self.ws, "state"))
                     if f.endswith(".tmp")]
        self.assertEqual(leftovers, [])

    def test_eight_writers_still_yield_one_intent(self):
        won, refused = self._race(n=8)
        self.assertEqual(len(won), 1, f"won={won} refused={refused}")
        self.assertEqual(len(refused), 7)
        self.assertIn(_mod.consume_intent(self.ws), _mod._ACTIONS)



class TestWriterVersusConsumer(unittest.TestCase):
    """A consumer that reads, then deletes, can delete an intent it never read
    (qingyun review, #3191): writer replaces the stale file between the two
    steps, consumer removes the NEW one and discards its stale payload. Nobody
    acts, the file is gone, and the writer's waiter reads that as consumption."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ws = self.tmp.name
        os.makedirs(os.path.join(self.ws, "state"), exist_ok=True)

    def tearDown(self):
        self.tmp.cleanup()

    def _seed_stale(self):
        path = _mod.write_intent(self.ws, "restart", "seed")
        with open(path) as f:
            d = json.load(f)
        d["requested_at"] = time.time() - (_mod.STALE_SEC + 60)
        with open(path, "w") as f:
            json.dump(d, f)

    def test_consumer_blocks_while_the_intent_lock_is_held(self):
        import fcntl
        self._seed_stale()
        done = threading.Event()

        def consumer():
            _mod.consume_intent(self.ws)
            done.set()

        with open(_mod.intent_path(self.ws) + ".lock", "a") as held:
            fcntl.flock(held.fileno(), fcntl.LOCK_EX)
            threading.Thread(target=consumer, daemon=True).start()
            self.assertFalse(done.wait(timeout=1.0),
                             "consume_intent deleted without holding the lock")
            fcntl.flock(held.fileno(), fcntl.LOCK_UN)
        self.assertTrue(done.wait(timeout=10), "consumer never resumed")

    def test_consumer_never_destroys_an_intent_it_did_not_act_on(self):
        # The invariant: after any writer/consumer interleaving, either the
        # consumer acted, or a live intent is still on disk for the next poll.
        for _ in range(60):
            self.setUp()
            self._seed_stale()
            got = {}
            start = threading.Barrier(2)

            def writer():
                start.wait()
                try:
                    _mod.write_intent(self.ws, "stop", "w")
                    got["written"] = True
                except _mod.IntentPending:
                    got["written"] = False

            def consumer():
                start.wait()
                got["action"] = _mod.consume_intent(self.ws)

            ts = [threading.Thread(target=writer), threading.Thread(target=consumer)]
            for t in ts:
                t.start()
            for t in ts:
                t.join(timeout=10)
            if got.get("written") and got.get("action") is None:
                self.assertTrue(
                    os.path.exists(_mod.intent_path(self.ws)),
                    "writer succeeded and nobody acted, yet the intent is gone — "
                    "its waiter would report a consumption that never happened")


if __name__ == "__main__":
    unittest.main(verbosity=2)
