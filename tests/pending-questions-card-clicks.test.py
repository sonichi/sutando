#!/usr/bin/env python3
"""A pending question's decision-card click is handed to exactly one consumer.

- every pass runs under one exclusive lock; the pass that claims a click closes its row (the
  owner's terminal Status refuses that close in the same guarded write) and records the claim
  `handed` before it returns the choice, with its exact action id and consequence;
- a later pass, by any process, never offers it again until `ack`; a claim left `claiming` by a
  pass that died is re-offered under the lock (no pid is probed);
- crash and concurrency boundaries are exercised by real processes (os._exit) against the
  production stores: the HITL store and the room database through the adapter's own serve.
Cards here are made by a test-only path; the ask side does not create them yet.
"""
from _helpers.hermetic_env import hermetic_env, isolate_os_environ

# Before anything reads the environment: no outer credential selector survives.
isolate_os_environ()

import contextlib
import importlib.util
import io
import json
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
PQ = REPO / "skills" / "pending-questions" / "scripts"
for p in (REPO / "src", PQ):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

import pending_questions_clicks as cards
import pending_questions_lease as lease_mod
import pending_questions_reader as reader
import pending_questions_room_db as room_db
from hitl.manager import HitlManager, HitlStore
from hitl.schema import (Action, ActionReply, HumanRequirement, STATUS_CANCELLED, STATUS_IN_PROGRESS,
                         STATUS_RESOLVED)

ADAPTER = str(PQ / "pending_questions_room_db.py")
OPTIONS = [("approve", "Approve", "merge it"), ("option-1", "Hold", "wait for the CLA")]


def _room_db_suite():
    spec = importlib.util.spec_from_file_location("pq_room_db_suite_c", REPO / "tests" / "pending-questions-room-db.test.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


RDBT = _room_db_suite()


def _manager(ws: Path) -> HitlManager:
    return HitlManager(HitlStore(ws / "state" / "hitl" / "requirements"))


def make_card(m: HitlManager, ask_id: str, question="Merge #12?", options=OPTIONS) -> str:
    """The test-only card path: the shape the ask side will create."""
    req = HumanRequirement(kind="choice", runtime=cards.CARD_RUNTIME, message=question, guard=ask_id,
                           actions=[Action(id=i, kind="confirmation", label=lbl) for i, lbl, _ in options],
                           subject={"ask_id": ask_id, "choices": {i: what for i, _, what in options}},
                           turn_on_action=True)
    return m.create(req).id


def token_of(m: HitlManager, card: str) -> str:
    return m.get(card).subject["claim"]["token"]


def lease_of(m: HitlManager, card: str) -> dict:
    return m.get(card).subject["claim"]["lease"]


def set_lease(m: HitlManager, card: str, observed_at: float, elapsed: float) -> None:
    claim = m.get(card).subject["claim"]
    cards.stamp_claim(m, card, {**claim, "lease": {"observed_at": observed_at, "elapsed": elapsed}})


def click(m: HitlManager, card: str, action_id: str) -> None:
    req = m.get(card)
    m.apply_action(ActionReply(hitl_id=req.id, expected_revision=req.revision, action_id=action_id,
                               guard=req.guard))


CHILD = """
import os, sys, json
from pathlib import Path
from unittest import mock
sys.path[:0] = [{src!r}, {pq!r}]
import pending_questions_room_db as rdb, pending_questions_reader as reader
ws = Path({ws!r})
{body}
"""


class _Room(unittest.TestCase):
    """A workspace with the fake room capability on disk, one question row, and its clicked card."""

    def setUp(self):
        self.ws = Path(tempfile.mkdtemp(prefix="pq-clicks-"))
        for d in ("state", "results"):
            (self.ws / d).mkdir()
        RDBT._install_fake_capability(self.ws)
        keys = {"FAKE_ROOM_STATE": str(self.ws / "room.json"), "SUTANDO_HOST_LABEL": RDBT.HOST}
        self.env = hermetic_env(**keys)
        patch = mock.patch.dict("os.environ", keys)
        patch.start()
        self.addCleanup(patch.stop)
        self.store, _ = room_db.room_store(self.ws)
        out = room_db.ask_owner("Merge #12?", urgency="durable", workspace=self.ws, host=RDBT.HOST,
                                store=self.store, default_action="merge it", options=[("Hold", "wait for the CLA")])
        self.ask_id = out["ask_id"]
        self.m = _manager(self.ws)
        self.card = make_card(self.m, self.ask_id)

    def run_clicks(self):
        return room_db.clicks(self.ws, manager=self.m)

    def code(self, body):
        return CHILD.format(src=str(REPO / "src"), pq=str(PQ), ws=str(self.ws), body=body)

    def child(self, body, rc=17):
        r = subprocess.run([sys.executable, "-c", self.code(body)], env=self.env, capture_output=True,
                           text=True, timeout=120)
        self.assertEqual(r.returncode, rc, r.stderr[-2000:])
        return r.stdout


class HandedOnce(_Room):
    def test_a_click_is_actionable_once_then_awaits_ack(self):
        click(self.m, self.card, "option-1")
        self.assertFalse(room_db.ack_click(self.ws, self.ask_id, "x", self.m)[0], "nothing handed yet: no ack")
        [first] = self.run_clicks()
        self.assertEqual((first["actionable"], first["closed"], first["action_id"], first["consequence"]),
                         (True, True, "option-1", "wait for the CLA"))
        self.assertEqual(first["token"], token_of(self.m, self.card))
        self.assertIn(f"ack with --token {first['token']}", reader.click_line(first))
        self.assertEqual(reader.click_line(first)[:len(f"- [{self.ask_id}] chose option-1 'Hold' -> wait")],
                         f"- [{self.ask_id}] chose option-1 'Hold' -> wait")
        self.assertEqual(self.store.status_of(self.ask_id), "Answered")
        [again] = self.run_clicks()
        self.assertEqual((again["actionable"], again["handed_before"]), (False, True))
        self.assertNotIn("chose", reader.click_line(again))
        self.assertIn("awaiting ack", reader.click_line(again))
        self.assertTrue(room_db.ack_click(self.ws, self.ask_id, first["token"], self.m)[0])
        self.assertEqual(self.m.get(self.card).status, STATUS_RESOLVED)
        self.assertEqual(self.run_clicks(), [])
        self.assertFalse(room_db.ack_click(self.ws, self.ask_id, first["token"], self.m)[0], "nothing left to ack")

    def test_the_lock_lives_beside_the_hitl_store(self):
        self.run_clicks()
        self.assertTrue((self.ws / "state" / "hitl" / cards.LOCK_NAME).exists())

    def test_two_concurrent_consumers_get_one_actionable_choice(self):
        click(self.m, self.card, "option-1")
        slow = ("real = rdb._close_for_click\n"
                "def slow(*a):\n    import time\n    r = real(*a)\n    time.sleep(1.5)\n    return r\n"
                "with mock.patch.object(rdb, '_close_for_click', slow):\n"
                f"    reader.main(['clicks', '--workspace', str(ws), '--store-adapter', {ADAPTER!r}])\n"
                "sys.stdout.flush()\nos._exit(17)")
        plain = (f"reader.main(['clicks', '--workspace', str(ws), '--store-adapter', {ADAPTER!r}])\n"
                 "sys.stdout.flush()\nos._exit(17)")
        procs = [subprocess.Popen([sys.executable, "-c", self.code(b)], env=self.env, stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE, text=True) for b in (slow, plain)]
        outs = [p.communicate(timeout=120) for p in procs]
        self.assertEqual([p.returncode for p in procs], [17, 17], "".join(e[-1500:] for _, e in outs))
        text = "".join(o for o, _ in outs)
        self.assertEqual(text.count(" chose option-1 'Hold'"), 1, text)
        self.assertEqual(self.m.get(self.card).status, STATUS_IN_PROGRESS, "claimed until ack, never cancelled")

class CrashBoundaries(_Room):
    def die_in_close(self, after_close: bool):
        call = "    real(*a)\n" if after_close else ""
        self.child("real = rdb._close_for_click\n"
                   f"def close_and_die(*a):\n{call}    os._exit(17)\n"
                   "with mock.patch.object(rdb, '_close_for_click', close_and_die):\n"
                   "    rdb.clicks(ws)")

    def test_death_after_the_claim_before_the_close_reoffers_it(self):
        click(self.m, self.card, "option-1")
        self.die_in_close(after_close=False)
        self.assertEqual(self.m.get(self.card).subject["claim"]["state"], cards.CLAIMING)
        [c] = self.run_clicks()
        self.assertEqual((c["actionable"], c["closed"]), (True, True))

    def test_death_after_the_close_before_the_handoff_reoffers_it_once(self):
        click(self.m, self.card, "option-1")
        self.die_in_close(after_close=True)
        self.assertEqual(self.store.status_of(self.ask_id), "Answered", "the close landed")
        [c] = self.run_clicks()
        self.assertEqual((c["actionable"], c["stale"], c["closed"]), (True, False, True), c["close_message"])
        [again] = self.run_clicks()
        self.assertFalse(again["actionable"])

    def test_death_after_the_handoff_is_reoffered_after_the_lease_and_not_before(self):
        click(self.m, self.card, "option-1")
        self.child("with mock.patch.object(reader, 'click_line', side_effect=lambda c: os._exit(17)):\n"
                   f"    reader.main(['clicks', '--workspace', str(ws), '--store-adapter', {ADAPTER!r}])")
        claim = self.m.get(self.card).subject["claim"]
        self.assertEqual((claim["state"], claim["lease"]["elapsed"]), (cards.HANDED, 0.0))
        t = claim["lease"]["observed_at"]
        for now in (t + cards.LEASE_MAX_STEP_SEC, t + cards.HANDED_LEASE_SEC - 1):  # credited 300, then 599
            [inside] = room_db.clicks(self.ws, manager=self.m, now=now)
            self.assertEqual((inside["actionable"], inside["handed_before"]), (False, True))
        self.assertIn("re-offered if no ack comes within the lease", reader.click_line(inside))
        [after] = room_db.clicks(self.ws, manager=self.m, now=t + cards.HANDED_LEASE_SEC)  # credited 600
        self.assertEqual((after["actionable"], after["reoffered"]), (True, True))
        self.assertIn("chose option-1 'Hold' -> wait for the CLA (re-offered", reader.click_line(after))
        fresh = self.m.get(self.card).subject["claim"]
        self.assertNotEqual(fresh["token"], claim["token"])
        self.assertEqual(fresh["lease"], {"observed_at": t + cards.HANDED_LEASE_SEC, "elapsed": 0.0}, "renewal resets both")
        [next_pass] = room_db.clicks(self.ws, manager=self.m, now=t + cards.HANDED_LEASE_SEC + 1)
        self.assertFalse(next_pass["actionable"], "a re-offer starts a new lease")

    def test_a_second_lease_expiry_gives_no_second_reoffer(self):
        click(self.m, self.card, "option-1")
        [first] = self.run_clicks()
        t0 = lease_of(self.m, self.card)["observed_at"]
        room_db.clicks(self.ws, manager=self.m, now=t0 + cards.LEASE_MAX_STEP_SEC)
        [again] = room_db.clicks(self.ws, manager=self.m, now=t0 + cards.HANDED_LEASE_SEC)
        self.assertTrue(again["reoffered"])
        r = t0 + cards.HANDED_LEASE_SEC
        room_db.clicks(self.ws, manager=self.m, now=r + cards.LEASE_MAX_STEP_SEC)
        for later in (r + cards.HANDED_LEASE_SEC, r + 50 * cards.HANDED_LEASE_SEC):
            [stuck] = room_db.clicks(self.ws, manager=self.m, now=later)
            self.assertEqual((stuck["actionable"], stuck["needs_attention"]), (False, True))
            line = reader.click_line(stuck)
            self.assertNotIn("chose", line)
            self.assertIn("NEEDS ATTENTION", line)
            self.assertIn(f"--token {again['token']}", line)
        self.assertEqual(self.m.get(self.card).subject["claim"]["reoffers"], 1)
        self.assertTrue(room_db.ack_click(self.ws, self.ask_id, again["token"], self.m)[0], "it can still be acked")

    def test_an_ack_inside_the_lease_prevents_any_reoffer(self):
        click(self.m, self.card, "option-1")
        [c] = self.run_clicks()
        t = lease_of(self.m, self.card)["observed_at"]
        self.assertTrue(room_db.ack_click(self.ws, self.ask_id, c["token"], self.m)[0])
        for i in range(1, 6):
            self.assertEqual(room_db.clicks(self.ws, manager=self.m, now=t + i * cards.LEASE_MAX_STEP_SEC), [])
        self.assertEqual(self.m.get(self.card).status, STATUS_RESOLVED)

    def test_two_consumers_after_the_lease_get_exactly_one_reoffer(self):
        click(self.m, self.card, "option-1")
        self.run_clicks()
        set_lease(self.m, self.card, observed_at=time.time(), elapsed=cards.HANDED_LEASE_SEC)  # spent
        slow = ("real = rdb._close_for_click\n"
                "import pending_questions_clicks as pc, time\n"
                "real_cards = pc.answered_cards\n"
                "def slow_cards(m):\n    got = real_cards(m)\n    time.sleep(1.5)\n    return got\n"
                "with mock.patch.object(pc, 'answered_cards', slow_cards):\n"
                f"    reader.main(['clicks', '--workspace', str(ws), '--store-adapter', {ADAPTER!r}])\n"
                "sys.stdout.flush()\nos._exit(17)")
        plain = (f"reader.main(['clicks', '--workspace', str(ws), '--store-adapter', {ADAPTER!r}])\n"
                 "sys.stdout.flush()\nos._exit(17)")
        procs = [subprocess.Popen([sys.executable, "-c", self.code(b)], env=self.env, stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE, text=True) for b in (slow, plain)]
        outs = [p.communicate(timeout=120) for p in procs]
        self.assertEqual([p.returncode for p in procs], [17, 17], "".join(e[-1500:] for _, e in outs))
        text = "".join(o for o, _ in outs)
        self.assertEqual(text.count(" chose option-1 'Hold'"), 1, text)
        self.assertEqual(text.count("(re-offered"), 1, text)


class TokenAndOutage(_Room):
    def _owner_sets(self, status):
        self.store.client.set_cells(room_db.DB_SCHEMA, self.store._rid(self.ask_id), {"status": status})

    def test_a_late_ack_after_a_reoffer_is_refused_as_stale(self):
        click(self.m, self.card, "option-1")
        [first] = self.run_clicks()
        t = lease_of(self.m, self.card)["observed_at"]
        room_db.clicks(self.ws, manager=self.m, now=t + cards.LEASE_MAX_STEP_SEC)
        [again] = room_db.clicks(self.ws, manager=self.m, now=t + cards.HANDED_LEASE_SEC)
        self.assertTrue(again["reoffered"])
        self.assertNotEqual(again["token"], first["token"])
        ok, msg = room_db.ack_click(self.ws, self.ask_id, first["token"], self.m)
        self.assertFalse(ok)
        self.assertIn("stale ack", msg)
        self.assertEqual(self.m.get(self.card).status, STATUS_IN_PROGRESS, "the late ack changed nothing")
        self.assertTrue(room_db.ack_click(self.ws, self.ask_id, again["token"], self.m)[0])
        self.assertEqual(self.m.get(self.card).status, STATUS_RESOLVED)

    def test_a_recovered_claim_never_takes_credit_for_an_unrelated_resolve(self):
        click(self.m, self.card, "option-1")
        cards.stamp_claim(self.m, self.card, {"token": "claimtok", "state": cards.CLAIMING})
        self.child("import pending_questions_clicks as pc\n"
                   "with mock.patch.object(pc, 'retire_cards', side_effect=lambda *a, **k: os._exit(17)):\n"
                   "    rdb.resolve(ws, " + repr(self.ask_id) + ", 'Answered')")
        self.assertEqual(self.store.status_of(self.ask_id), "Answered", "resolve closed the row")
        self.assertEqual(self.m.get(self.card).status, STATUS_IN_PROGRESS, "the crash left the card")
        [c] = self.run_clicks()
        self.assertEqual((c["stale"], c["actionable"]), (True, False), c["close_message"])
        self.assertNotIn("chose", reader.click_line(c))
        self.assertEqual(self.m.get(self.card).status, STATUS_CANCELLED)

    def test_the_claims_own_earlier_close_is_recognised_by_its_token(self):
        click(self.m, self.card, "option-1")
        cards.stamp_claim(self.m, self.card, {"token": "claimtok", "state": cards.CLAIMING})
        self.store.close(self.ask_id, "Answered", require_open=True, token="claimtok")
        [c] = self.run_clicks()
        self.assertEqual((c["actionable"], c["token"]), (True, "claimtok"), c["close_message"])

    def test_the_owners_status_wins_over_a_matching_token_mark_whose_clearing_failed(self):
        click(self.m, self.card, "option-1")
        cards.stamp_claim(self.m, self.card, {"token": "claimtok", "state": cards.CLAIMING})
        self.store.close(self.ask_id, "Answered", require_open=True, token="claimtok")  # this claim's mark
        self._owner_sets("resolved")  # then the owner answered on the row himself
        with mock.patch.object(type(self.store), "clear", side_effect=OSError("stale mark not cleared")):
            [c] = self.run_clicks()
        self.assertEqual((c["actionable"], c["stale"]), (False, True), c["close_message"])
        self.assertNotIn("chose", reader.click_line(c))
        self.assertEqual(self.m.get(self.card).status, STATUS_CANCELLED)

    def test_an_outage_defers_a_click_even_beside_a_terminal_owner_status(self):
        self._owner_sets("resolved")
        click(self.m, self.card, "approve")
        with mock.patch.object(room_db, "room_store", return_value=(None, "room capability unreachable")):
            [c] = self.run_clicks()
        self.assertEqual((c["deferred"], c["actionable"], c["stale"]), (True, False, False), c["close_message"])
        self.assertIn("deferred", reader.click_line(c))
        self.assertNotIn("chose", reader.click_line(c))
        self.assertEqual(self.m.get(self.card).subject["claim"]["state"], cards.CLAIMING, "retryable")
        self.assertFalse((self.ws / "state" / "pending-questions-outbox" / "closed").exists(), "no local close")
        [back] = self.run_clicks()
        self.assertEqual((back["stale"], back["actionable"]), (True, False), "the owner's Status wins")

    def test_an_unreachable_room_or_an_absent_row_defers(self):
        click(self.m, self.card, "approve")
        with mock.patch.object(room_db, "room_store", return_value=(self.store, "room")), \
                mock.patch.object(type(self.store), "close", side_effect=OSError("socket refused")):
            [c] = self.run_clicks()
        self.assertTrue(c["deferred"])
        self.assertIn("unreachable", c["close_message"])
        with mock.patch.object(type(self.store), "close",
                               side_effect=room_db.GuardFailed(self.ask_id, {})):
            [c] = self.run_clicks()
        self.assertTrue(c["deferred"])
        self.assertIn("no row in the room yet", c["close_message"])
        [c] = self.run_clicks()
        self.assertTrue(c["actionable"], "the deferred click is handed over once the room answers")


    def test_a_row_in_view_commits_the_store_history_before_its_click_closes_it(self):
        click(self.m, self.card, "approve")
        with mock.patch.object(room_db, "store_history", return_value=False), \
                mock.patch.object(room_db.Outbox, "commit_history") as ch:
            [c] = self.run_clicks()
        ch.assert_called_once_with(self.ask_id)
        self.assertTrue(c["actionable"])


def _health_check():
    spec = importlib.util.spec_from_file_location("hc_for_card_clicks", REPO / "src" / "health-check.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


HC = _health_check()


class Wakeup(_Room):
    """A deferred click or an expired lease is retried from the periodic health check (run by the
    proactive loop every pass, and by the launchd fallback), with no new task."""

    def loop_pass(self):
        """The health check's warn, then the command it names, run as the core would."""
        warn = HC.check_decision_card_clicks(self.ws)
        if warn is None:
            return None, ""
        cmd = "python3 src/pending_questions_reader.py clicks"
        self.assertIn(f"`{cmd}`", warn["detail"])
        self.assertEqual((warn["name"], warn["status"]), ("decision-card-clicks", "warn"))
        r = subprocess.run(["bash", "-c", f"{cmd} --workspace {self.ws} --store-adapter {ADAPTER}"], cwd=REPO,
                           env=self.env, capture_output=True, text=True, timeout=120)
        return warn, r.stdout

    def test_nothing_owed_raises_no_warn(self):
        self.assertEqual(self.loop_pass(), (None, ""))

    def test_the_health_run_registers_the_check(self):
        self.assertIn("check_decision_card_clicks()", (REPO / "src" / "health-check.py").read_text())

    def test_a_handoff_whose_consumer_died_is_reoffered_by_the_next_loop_pass(self):
        click(self.m, self.card, "option-1")
        self.child("with mock.patch.object(reader, 'click_line', side_effect=lambda c: os._exit(17)):\n"
                   f"    reader.main(['clicks', '--workspace', str(ws), '--store-adapter', {ADAPTER!r}])")
        self.assertEqual(self.loop_pass(), (None, ""), "inside the lease nothing is owed")
        # one lease step short, last seen a full step ago: the health check's own pass spends it
        set_lease(self.m, self.card, observed_at=time.time() - cards.LEASE_MAX_STEP_SEC,
                  elapsed=cards.HANDED_LEASE_SEC - cards.LEASE_MAX_STEP_SEC)
        warn, out = self.loop_pass()
        self.assertIn(self.ask_id, warn["detail"])
        self.assertIn(f"[{self.ask_id}] chose option-1 'Hold' -> wait for the CLA (re-offered", out)
        self.assertEqual(self.loop_pass(), (None, ""), "re-offered: owed nothing more")

    def test_a_click_deferred_in_an_outage_is_handed_over_by_the_next_loop_pass(self):
        click(self.m, self.card, "approve")
        with mock.patch.object(room_db, "room_store", return_value=(None, "room unreachable")):
            [c] = self.run_clicks()
        self.assertTrue(c["deferred"])
        warn, out = self.loop_pass()
        self.assertIn(f"[{self.ask_id}] chose approve 'Approve' -> merge it", out)

    def test_an_unreadable_store_raises_no_warn(self):
        with mock.patch.object(reader, "due_clicks", side_effect=OSError("store gone")):
            self.assertIsNone(HC.check_decision_card_clicks(self.ws))

    def test_the_due_probe(self):
        click(self.m, self.card, "option-1")
        self.assertEqual(room_db.due_clicks(self.ws, self.m), [self.ask_id], "a click never processed")
        [c] = self.run_clicks()
        t = lease_of(self.m, self.card)["observed_at"]
        self.assertEqual(room_db.due_clicks(self.ws, self.m, now=t + cards.LEASE_MAX_STEP_SEC), [], "inside the lease")
        self.assertEqual(lease_of(self.m, self.card)["elapsed"], cards.LEASE_MAX_STEP_SEC, "the probe ages the lease")
        self.assertEqual(room_db.due_clicks(self.ws, self.m, now=t + cards.HANDED_LEASE_SEC), [self.ask_id])
        room_db.clicks(self.ws, manager=self.m, now=t + cards.HANDED_LEASE_SEC)
        r = t + cards.HANDED_LEASE_SEC
        self.assertEqual(room_db.due_clicks(self.ws, self.m, now=r + 1), [], "inside the re-offer's lease")
        room_db.due_clicks(self.ws, self.m, now=r + cards.LEASE_MAX_STEP_SEC)
        self.assertEqual(room_db.due_clicks(self.ws, self.m, now=r + cards.HANDED_LEASE_SEC), [self.ask_id],
                         "its re-offer used and unacked: NEEDS ATTENTION is surfaced, not skipped")
        with mock.patch.object(reader, "_adapter", return_value=(None, "no adapter")):
            self.assertEqual(reader.due_clicks(self.ws), (None, "no store: no adapter"))
        with mock.patch.object(reader, "_adapter", return_value=(object(), "p")):
            self.assertIn("takes no card clicks", reader.due_clicks(self.ws)[1])
        with mock.patch.object(reader, "_adapter", return_value=(mock.Mock(due_clicks=mock.Mock(side_effect=OSError("x"))), "p")):
            self.assertIn("adapter failed", reader.due_clicks(self.ws)[1])
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                self.assertEqual(reader.main(["clicks", "--due", "--workspace", str(self.ws)]), 1)
            self.assertIn("not read", buf.getvalue())
        click_ws_buf = io.StringIO()
        with contextlib.redirect_stdout(click_ws_buf):
            rc = reader.main(["clicks", "--due", "--workspace", str(self.ws), "--store-adapter", ADAPTER])
        self.assertEqual(rc, 0, "the re-offer was stamped ahead of this clock and is unseen: surfaced")
        self.assertIn(f"1 decision-card click(s) owe a pass: {self.ask_id}", click_ws_buf.getvalue())


class LeaseInvariant(unittest.TestCase):
    """pending_questions_lease: a lease expires exactly when credited time reaches its window; a pass
    credits clamp(now - observed_at, 0, max_step) and then sets observed_at = now."""

    W, STEP = 600.0, 300.0

    def run_passes(self, times, start=1000.0, lease=None):
        lease = lease or lease_mod.new(start)
        for t in times:
            lease = lease_mod.advance(lease, t, self.STEP)
        return lease

    def test_table(self):
        T, YEAR = 1000.0, 365 * 86400.0
        cases = [  # (pass times after a lease made at T, credited, expired?, why)
            ([T + 300, T + 599], 599.0, False, 'W - 1'),
            ([T + 300, T + 600], 600.0, True, 'W exactly'),
            ([T + 300, T + 601], 600.0, True, 'W + 1 is capped by the step'),
            ([T + 300, T + 300, T + 300], 300.0, False, 'repeated passes at one time credit 0'),
            ([T + 10**9], 300.0, False, 'a forward jump credits one step only: no single pass expires'),
            ([T + YEAR, T], 300.0, False, 'a year spike, then correction: the correction credits 0'),
            ([T + YEAR, T, T + 300], 600.0, True, '... and normal time resumes: expired 1 pass later, no freeze'),
            ([T - 3600], 0.0, False, 'backward step credits 0: no steal'),
            ([T - 1, T + 299], 300.0, False, 'rollback 1 s: the catch-up over-credits 1 s, within the step'),
            ([T + 300, T - 50, T + 300], 600.0, True, 'rollback then catch-up: over-credit at most one step'),
        ]
        for times, credited, exp, why in cases:
            with self.subTest(why=why, times=times):
                lease = self.run_passes(times, start=T)
                self.assertEqual(lease["elapsed"], credited)
                self.assertEqual(lease_mod.expired(lease, self.W), exp)
                self.assertEqual(lease["observed_at"], times[-1], "observed_at is the last pass's wall time")

    def test_a_spike_of_any_size_is_recovered_within_window_over_step_plus_one_passes(self):
        for spike in (3600.0, 86400.0, 365 * 86400.0, 10**12):
            lease = self.run_passes([1000 + spike, 1000], start=1000)  # spike, then corrected
            passes = 0
            now = 1000.0
            while not lease_mod.expired(lease, self.W):
                now += self.STEP
                lease = lease_mod.advance(lease, now, self.STEP)
                passes += 1
            self.assertLessEqual(passes, self.W / self.STEP + 1, spike)

    def test_a_renewal_resets_both_fields_and_a_missing_lease_starts_now(self):
        old = self.run_passes([1300, 1600], start=1000)
        self.assertTrue(lease_mod.expired(old, self.W))
        self.assertEqual(lease_mod.new(1601), {"observed_at": 1601.0, "elapsed": 0.0})
        self.assertEqual(lease_mod.advance(None, 5, self.STEP), {"observed_at": 5.0, "elapsed": 0.0})
        self.assertFalse(lease_mod.expired(None, self.W))


class LeaseInProduction(_Room):
    def _handed(self):
        click(self.m, self.card, "option-1")
        [c] = self.run_clicks()
        return lease_of(self.m, self.card)["observed_at"], c

    def test_a_one_second_rollback_after_a_late_reoffer_never_raises_attention(self):
        t, _ = self._handed()
        room_db.clicks(self.ws, manager=self.m, now=t + 300)
        [re] = room_db.clicks(self.ws, manager=self.m, now=t + 700)  # a late re-offer
        self.assertTrue(re["reoffered"])
        [back] = room_db.clicks(self.ws, manager=self.m, now=t + 699)  # then the clock steps back 1 s
        self.assertEqual((back["actionable"], back["needs_attention"]), (False, False))
        self.assertEqual(lease_of(self.m, self.card), {"observed_at": t + 699, "elapsed": 0.0})

    def test_a_stamp_just_under_a_window_ahead_expires_one_window_after_first_sight(self):
        t, _ = self._handed()
        set_lease(self.m, self.card, observed_at=t + cards.HANDED_LEASE_SEC - 1, elapsed=0.0)  # stamped W-1 ahead
        for now in (t, t + 300, t + cards.HANDED_LEASE_SEC - 1):  # first sight at t credits 0
            [c] = room_db.clicks(self.ws, manager=self.m, now=now)
            self.assertFalse(c["actionable"])
        [c] = room_db.clicks(self.ws, manager=self.m, now=t + cards.HANDED_LEASE_SEC)
        self.assertTrue(c["reoffered"], "one window after first sight, not nearly two")

    def test_a_forward_spike_then_correction_does_not_freeze_recovery(self):
        t, _ = self._handed()
        year = 365 * 86400
        trace = []
        for now in (t + year, t, t + 300):
            due = room_db.due_clicks(self.ws, self.m, now=now)
            trace.append((lease_of(self.m, self.card)["elapsed"], bool(due)))
        self.assertEqual(trace, [(300.0, False), (300.0, False), (600.0, True)],
                         "the spike credits one step, the correction 0, then normal time expires it")

    def test_a_far_future_stamp_does_not_matter(self):
        t, _ = self._handed()
        set_lease(self.m, self.card, observed_at=t + 365 * 86400, elapsed=0.0)  # stamped a year ahead
        [c] = room_db.clicks(self.ws, manager=self.m, now=t)
        self.assertFalse(c["actionable"], "first sight credits 0: no steal")
        room_db.clicks(self.ws, manager=self.m, now=t + 300)
        [c] = room_db.clicks(self.ws, manager=self.m, now=t + 600)
        self.assertTrue(c["reoffered"], "only observed deltas count")

    def test_a_restart_resumes_from_the_persisted_lease(self):
        t, _ = self._handed()
        self.child("import pending_questions_clicks as pc\n"
                   "real = pc.stamp_claim\n"
                   "def stamp_then_die(*a, **k):\n    real(*a, **k)\n    os._exit(17)\n"
                   "with mock.patch.object(pc, 'stamp_claim', stamp_then_die):\n"
                   f"    rdb.clicks(ws, now={t + 300!r})")
        self.assertEqual(lease_of(self.m, self.card), {"observed_at": t + 300, "elapsed": 300.0},
                         "both fields landed in one atomic save before the death")
        [c] = room_db.clicks(self.ws, manager=self.m, now=t + 600)
        self.assertTrue(c["reoffered"], "the next process resumes from the persisted credit")


class RollbackAndClock(_Room):
    @staticmethod
    def base_effective_status(cells):
        """The reviewed base's reader (main at 20f3042b), verbatim in behaviour: no click tokens."""
        names = {"open": "Open", "answered": "Answered", "resolved": "Resolved"}
        status = names.get(cells.get("status"))
        owner = status if status in ("Answered", "Resolved") else None
        val, _, tag = str(cells.get("closed") or "").partition("@")
        closed = val if val and (tag or None) == (cells.get("host") or None) else None
        return owner or (closed if closed in ("Answered", "Resolved") else None) or "Open"

    def test_a_click_close_reads_closed_to_the_base_reader(self):
        click(self.m, self.card, "option-1")
        [c] = self.run_clicks()
        self.assertTrue(c["actionable"])
        cells = self.store.client.row(room_db.DB_SCHEMA, self.store._rid(self.ask_id))["cells"]
        self.assertEqual(cells["closed"], f"Answered@{RDBT.HOST}", "Closed stays the base's grammar")
        self.assertEqual(cells["recovery"], f"click:{c['token']}@{RDBT.HOST}")
        self.assertEqual(self.base_effective_status(cells), "Answered")
        self.assertEqual(self.store.status_of(self.ask_id), "Answered")
        self.assertEqual(self.store.click_token_of(self.ask_id), c["token"])

    def test_a_click_mark_that_cannot_be_read_defers(self):
        click(self.m, self.card, "option-1")
        cards.stamp_claim(self.m, self.card, {"token": "claimtok", "state": cards.CLAIMING})
        self.store.close(self.ask_id, "Answered", require_open=True, token="claimtok")
        with mock.patch.object(type(self.store), "click_token_of", side_effect=OSError("row unreadable")):
            [c] = self.run_clicks()
        self.assertTrue(c["deferred"])

    def test_the_lease_starts_when_the_handoff_is_persisted_not_before_the_lock(self):
        click(self.m, self.card, "option-1")
        clock = [100.0]
        real = room_db._close_for_click

        def slow_close(*a):
            clock[0] = 701.0  # lock wait and room I/O took ten minutes
            return real(*a)
        with mock.patch.object(cards.time, "time", side_effect=lambda: clock[0]), \
                mock.patch.object(room_db, "_close_for_click", slow_close):
            [c] = room_db.clicks(self.ws, manager=self.m)
        self.assertEqual(lease_of(self.m, self.card), {"observed_at": 701.0, "elapsed": 0.0})
        [nxt] = room_db.clicks(self.ws, manager=self.m, now=702.0)
        self.assertFalse(nxt["actionable"], "a fresh lease is not already expired")


class ParentReaderNeverTextsACard(_Room):
    """Rolling the ask side back to this PR must not bring a text ask back beside a live card."""

    def _rem(self):
        spec = importlib.util.spec_from_file_location("pq_remind_parent", PQ / "pending_questions_remind.py")
        rem = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(rem)
        return rem

    def test_a_live_card_record_is_never_reminded_as_text_force_included(self):
        import pending_questions_ask as ask
        record = f"**Sent:** card {self.card} for ag2space owner-dm (decision card) at 2026-01-01T00:00:00Z"
        self.assertTrue(ask.asked_recently(record, self.ws / "results"))
        rem = self._rem()
        q = {"ask_id": self.ask_id, "title": "Merge #12?", "body": record}
        g = {"waiting": [q], "notes": [], "unavailable": False}
        with mock.patch.object(rem, "gather", return_value=g), \
                mock.patch.object(rem, "presenter_mode_active", return_value=False), \
                mock.patch.object(rem, "deliver") as dv:
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rem.main(["--notify", "--force"], workspace=self.ws)
        dv.assert_not_called()
        self.assertIn("wait as live decision cards; no text sent", buf.getvalue())

    def test_a_gone_card_or_an_unreadable_record_leaves_it_due(self):
        import pending_questions_ask as ask
        self.m.cancel(self.card)
        record = f"**Sent:** card {self.card} for ag2space owner-dm (decision card) at 2026-01-01T00:00:00Z"
        self.assertFalse(ask.asked_recently(record, self.ws / "results"))
        self.assertIsNone(ask.card_sent("**Sent:** card hitl_x for x at 2026-13-99T99:99:99Z"))
        with mock.patch("pending_questions_clicks.manager_for", side_effect=OSError("store gone")):
            self.assertEqual(ask.card_state(self.ws, "hitl_x")[0], "unknown")


class RetirementRace(_Room):
    """A `resolve` that retires the card between the decision and its claim save wins: nothing is
    handed over, and no unsaved token is returned."""

    def _resolve_before_handed_save(self):
        real = cards.stamp_claim

        def resolve_then_save(manager, hitl_id, claim):
            if claim.get("state") == cards.HANDED and not getattr(resolve_then_save, "done", False):
                resolve_then_save.done = True
                room_db.resolve(self.ws, self.ask_id, "Answered", manager=self.m)
            return real(manager, hitl_id, claim)
        return mock.patch.object(cards, "stamp_claim", resolve_then_save)

    def test_resolve_racing_the_handoff_save_hands_nothing_over(self):
        click(self.m, self.card, "option-1")
        with self._resolve_before_handed_save():
            [c] = self.run_clicks()
        self.assertEqual(self.m.get(self.card).status, STATUS_CANCELLED)
        self.assertEqual((c["actionable"], c["token"], c.get("retired")), (False, None, True), c)
        self.assertNotIn("chose", reader.click_line(c))
        self.assertIn("settled meanwhile — not a decision", reader.click_line(c))

    def test_resolve_racing_the_reoffer_save_hands_nothing_over(self):
        click(self.m, self.card, "option-1")
        [first] = self.run_clicks()
        t = lease_of(self.m, self.card)["observed_at"]
        room_db.clicks(self.ws, manager=self.m, now=t + 300)
        with self._resolve_before_handed_save():
            [c] = room_db.clicks(self.ws, manager=self.m, now=t + 600)
        self.assertEqual(self.m.get(self.card).status, STATUS_CANCELLED)
        self.assertEqual((c["actionable"], c["reoffered"], c.get("retired")), (False, False, True), c)
        self.assertEqual(self.m.get(self.card).subject["claim"]["token"], first["token"], "nothing unsaved returned")

    def test_a_card_settled_before_its_first_claim_is_skipped(self):
        click(self.m, self.card, "option-1")
        with mock.patch.object(cards, "stamp_claim", return_value=False):
            [c] = self.run_clicks()
        self.assertEqual((c["actionable"], c.get("retired")), (False, True))


class OwnerStatusWins(_Room):
    def _owner_sets(self, status):
        self.store.client.set_cells(room_db.DB_SCHEMA, self.store._rid(self.ask_id), {"status": status})

    def test_owner_answered_then_a_contradicting_click_is_stale(self):
        self._owner_sets("answered")
        click(self.m, self.card, "option-1")
        [c] = self.run_clicks()
        self.assertEqual((c["stale"], c["actionable"]), (True, False), c["close_message"])
        self.assertNotIn("chose", reader.click_line(c))
        self.assertEqual(self.m.get(self.card).status, STATUS_CANCELLED)

    def test_owner_resolved_then_a_contradicting_click_is_stale(self):
        self._owner_sets("resolved")
        click(self.m, self.card, "approve")
        [c] = self.run_clicks()
        self.assertEqual((c["stale"], c["actionable"]), (True, False), c["close_message"])
        self.assertEqual(self.store.status_of(self.ask_id), "Resolved")

    def test_a_plain_resolve_still_closes_beside_an_owner_status_and_retires_the_card(self):
        self._owner_sets("resolved")
        self.store.close(self.ask_id, "Answered")
        ws2 = self.ws
        ok, msg = room_db.resolve(ws2, self.ask_id, "Answered", manager=self.m)
        self.assertIn("retired", msg)
        self.assertEqual(self.m.get(self.card).status, STATUS_CANCELLED)

    def test_resolve_retires_the_card_so_a_late_click_is_refused(self):
        ok, msg = room_db.resolve(self.ws, self.ask_id, "Answered", manager=self.m)
        self.assertTrue(ok, msg)
        self.assertEqual(self.m.get(self.card).status, STATUS_CANCELLED)
        self.assertEqual(self.run_clicks(), [])

    def test_a_card_that_cannot_be_retired_is_reported_and_the_close_stands(self):
        with mock.patch.object(cards, "retire_cards", side_effect=OSError("store gone")):
            ok, msg = room_db.resolve(self.ws, self.ask_id, "Answered", manager=self.m)
        self.assertTrue(ok)
        self.assertIn("could not be retired", msg)


class ExactChoice(unittest.TestCase):
    def _handed(self, question, options, action_id):
        ws = Path(tempfile.mkdtemp(prefix="pq-clicks-x-"))
        (ws / "state").mkdir()
        m = _manager(ws)
        card = make_card(m, "ask-x", question, options)
        click(m, card, action_id)
        return cards.clicks(ws, lambda *a: (cards.CLOSED, "closed"), m)[0]

    def test_a_long_label_and_a_long_question_report_the_exact_operation(self):
        label = "Ship it to production after the canary has run for two full hours without alerts " * 3
        c = self._handed("Context. " * 60, [("option-0", label.strip(), "deploy build 4411"), ("option-1", "Hold", "wait")],
                         "option-0")
        self.assertIn(f"chose option-0 {label.strip()!r} -> deploy build 4411", reader.click_line(c))

    def test_duplicate_labels_print_distinct_ids_and_consequences(self):
        opts = [("option-0", "Yes", "merge #1"), ("option-1", "Yes", "merge #2")]
        one = reader.click_line(self._handed("Which?", opts, "option-0"))
        two = reader.click_line(self._handed("Which?", opts, "option-1"))
        self.assertIn("option-0 'Yes' -> merge #1", one)
        self.assertIn("option-1 'Yes' -> merge #2", two)

    def test_other_runtimes_cards_are_not_this_skills(self):
        ws = Path(tempfile.mkdtemp(prefix="pq-clicks-o-"))
        (ws / "state").mkdir()
        m = _manager(ws)
        req = m.create(HumanRequirement(kind="choice", runtime="report-feedback", message="q", guard="g",
                                        actions=[Action(id="a", kind="confirmation", label="A")]))
        click(m, req.id, "a")
        self.assertEqual(cards.answered_cards(m), [])


class CoreEntries(_Room):
    def _main(self, argv):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = reader.main(argv)
        return rc, buf.getvalue()

    def test_clicks_then_ack_through_the_reader_cli(self):
        click(self.m, self.card, "approve")
        base = ["--workspace", str(self.ws), "--store-adapter", ADAPTER]
        rc, text = self._main(["clicks", *base])
        self.assertEqual(rc, 0, text)
        self.assertIn(f"[{self.ask_id}] chose approve 'Approve' -> merge it", text)
        rc, text = self._main(["clicks", "--json", *base])
        self.assertEqual(json.loads(text)[0]["handed_before"], True)
        token = token_of(self.m, self.card)
        rc, text = self._main(["ack", self.ask_id, "--token", "superseded", *base])
        self.assertEqual(rc, 1, text)
        self.assertIn("stale ack", text)
        rc, text = self._main(["ack", self.ask_id, "--token", token, *base])
        self.assertEqual(rc, 0, text)
        rc, text = self._main(["clicks", *base])
        self.assertIn("no question-card clicks waiting", text)
        rc, text = self._main(["ack", self.ask_id, "--token", token, *base])
        self.assertEqual(rc, 1)

    def test_refusals_change_nothing(self):
        with self.assertRaises(SystemExit):
            self._main(["ack", "--workspace", str(self.ws)])
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            self._main(["ack", self.ask_id, "--workspace", str(self.ws)])
        with mock.patch.object(reader, "_adapter", return_value=(None, "no adapter")):
            self.assertEqual(reader.ack(self.ws, "a", "t")[0], False)
            self.assertEqual(reader.clicks(self.ws), (None, "no store: no adapter"))
            rc, text = self._main(["clicks", "--workspace", str(self.ws)])
            self.assertEqual(rc, 1)
            self.assertIn("not read", text)
        with mock.patch.object(reader, "_adapter", return_value=(object(), "p")):
            self.assertIn("takes no card clicks", reader.clicks(self.ws)[1])
            self.assertIn("takes no acks", reader.ack(self.ws, "a", "t")[1])
        boom = mock.Mock(clicks=mock.Mock(side_effect=OSError("x")), ack_click=mock.Mock(side_effect=OSError("y")))
        with mock.patch.object(reader, "_adapter", return_value=(boom, "p")):
            self.assertIn("adapter failed", reader.clicks(self.ws)[1])
            self.assertIn("adapter failed", reader.ack(self.ws, "a", "t")[1])

    def test_pq_cli_forwards_clicks_and_ack(self):
        spec = importlib.util.spec_from_file_location("pq_cli_c", PQ / "pq.py")
        pq = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(pq)
        with mock.patch.object(reader, "main", return_value=0) as rm:
            self.assertEqual(pq.main(["clicks", "--json"]), 0)
            self.assertEqual(pq.main(["ack", "ask-1", "--token", "t"]), 0)
        self.assertEqual(rm.call_args_list[0].args[0][:2], ["clicks", "--json"])
        self.assertIn("--store-adapter", rm.call_args_list[1].args[0])

    def test_the_default_store_is_the_workspace_one(self):
        click(self.m, self.card, "approve")
        [c] = room_db.clicks(self.ws)
        self.assertTrue(c["actionable"])
        self.assertTrue(room_db.ack_click(self.ws, self.ask_id, c["token"])[0])


if __name__ == "__main__":
    unittest.main()
