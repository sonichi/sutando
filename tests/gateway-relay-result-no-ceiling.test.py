#!/usr/bin/env python3
"""A voice relay ask's result is delivered with no ceiling, and reclaimed after a downgrade.

For a task served with `ack_first` (its mark is kept), the result's outbox item is retried
with no window and no attempt cap, across restarts, under the same id and payload, until
the broker accepts it: a 401 or 403 waits for the poll loop's credential recovery, a 408,
425, 429, 5xx or network failure keeps the same result, and a definite refusal of the
payload swaps in a minimal `failed` result for the same task, kept and retried the same
way. Every other result keeps today's 10-minute window and is parked on the Mac after it.

A bridge from before the contract parks a relay ask's result it cannot deliver for 10
minutes; on a named instance, or for a re-ask, a restart then settles the task. When an
updated bridge starts, it requeues each such item a mark names, puts the local id the mark
names back in flight, and moves the newest quarantined copy back to that id's name, so the
answer is posted once, under the relay ask's id, with the older bridge's stored payload.

Drives the real drain, orphan sweep and reclaim in process; outages, refusals, restarts and
downgrades run the real poll loop (`main()`) in fresh processes over one workspace, against
a relay on a real socket. The older bridge is replayed by `_helpers/older_bridge.py`.

Run: python3 tests/gateway-relay-result-no-ceiling.test.py
"""
from __future__ import annotations

import contextlib
import errno
import glob
import io
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

from _helpers.hermetic_gateway import assert_hermetic, isolate_then_import  # noqa: E402
_GW, _IMPORT_READS = isolate_then_import()   # before anything else imports the bridge

_REPO = Path(__file__).resolve().parent.parent
_PKG = _REPO / "packages" / "ag2-sparrow"
_TESTS = Path(__file__).resolve().parent
if str(_PKG) not in sys.path:
    sys.path.insert(0, str(_PKG))

sys.path.insert(0, str(_TESTS))
from _helpers.hermetic_env import hermetic_env  # noqa: E402

from _helpers import gateway_relay, older_bridge  # noqa: E402
from ag2_sparrow import gateway_instance, outbox, remote_gateway_bridge as gw  # noqa: E402

Bridges, Relay = gateway_relay.Bridges, gateway_relay.Relay

CONTRACT = ["voice-outcome-v1"]
OWNER = "@owner:ag2.space"
ROOM = "!room:ag2.space"
ASK_A, ASK_B = "ask-5f3c9e0a1b2c", "ask-7d4e2f1a9b8c"
TYPED_A = "task-22d83e59601f3a1fef"
SESSION_END = "voice-end-0123456789abcdef01234567"
EMPTY_HOLDER = "task-0e5d1c2b3a4f5e6d7c"
UNVERIFIED = ("⚠️ The holder's delivery destination or sender could not be verified. "
              "This question needs a direct answer.")
PRIVATE, TEXT = "[dm-only]\nYour 3pm is with Dana.", "Your 3pm is with Dana."
ANSWER = {"id": ASK_A, "body": TEXT, "capabilities": CONTRACT, "dm_only": True}
FAILED = {"id": ASK_A, "body": "", "status": "failed", "capabilities": CONTRACT, "dm_only": False}
DEAD = {"status": "dead", "reason": "send-failed:M_FORBIDDEN", "terminal": True}
# A relay result refused for good parks under the prefix with the refusal's own reason kept.
FOR_GOOD = "refused-for-good:permanent-refusal"
DAY = 86400.0

_LEDGERS = {"INFLIGHT_FILE": "remote-task-inflight", "TASK_ROOMS_FILE": "remote-task-rooms",
            "TASK_MEDIA_FILE": "remote-task-media", "PENDING_ACK_FILE": "remote-task-acks",
            "DEDUP_ALIAS_FILE": "remote-dedup-alias", "ACK_FIRST_FILE": "remote-task-ack-first",
            "ACK_FIRST_MANIFEST_FILE": "remote-task-ack-first-manifest"}


def voice_ask(broker_id: str = ASK_A) -> dict:
    """A relay ask as the broker serves one: no room, the owner as `user_id`."""
    return {"id": broker_id, "task": "What's on my calendar today?", "source": "matrixrtc-voice",
            "user_id": OWNER, "asked_by": OWNER, "access_tier": "owner",
            "lease_id": f"gen-{broker_id}", "ack_first": True}


def typed_task(broker_id: str = TYPED_A) -> dict:
    return {"id": broker_id, "task": "What is AG2Space?", "source": "ag2space",
            "channel_id": ROOM, "user_id": OWNER, "access_tier": "owner"}


def item(root: Path, item_id: str) -> dict:
    return outbox.read_item(root, item_id) or {}


def age_retry(root: Path, item_id: str, seconds: float, failures: int = 20) -> None:
    """As if every send of the item had failed for `seconds` more: the outage on disk."""
    path = outbox._item_path(root, item_id)
    record = json.loads(path.read_text())
    for key in ("started_at", "deadline", "next_attempt_at"):
        record["retry"][key] -= seconds
    record["retry"]["failures"] = max(record["retry"]["failures"], failures)
    path.write_text(json.dumps(record, sort_keys=True))


def refused(code: int, body: "dict | None" = None) -> urllib.error.HTTPError:
    return urllib.error.HTTPError("http://relay/v1/results", code, "refused", {},
                                  io.BytesIO(json.dumps(body or {"error": "refused"}).encode()))


class _Broker:
    """The broker's side of the wire, in process. Acks are accepted; `result` answers each
    result POST: "ok", "down" (no connection) or an HTTP status that refuses it."""

    def __init__(self):
        self.calls: "list[tuple]" = []
        self.result = "ok"
        self.delivery: "dict | None" = None        # every result's delivery state, when readable
        self.delivery_reads = 0

    def request(self, method, path, payload=None, **_kw):
        payload = None if payload is None else json.loads(json.dumps(payload))
        self.calls.append((method, path, payload))
        if path == "/v1/results":
            answer = self.result(payload) if callable(self.result) else self.result
            if answer == "down":
                raise urllib.error.URLError("connection refused")
            if answer != "ok":
                raise refused(int(answer))
        if path == "/v1/agents":
            return {"agents": []}
        if path.startswith("/v1/results/") and path.endswith("/delivery"):
            self.delivery_reads += 1
            if self.delivery is not None:
                return dict(self.delivery)
        return {"ok": True, "delivery_readable": True, "durable": True}

    def results(self) -> "list[dict]":
        return [p for _m, path, p in self.calls if path == "/v1/results"]


class _Bridge(unittest.TestCase):
    """The real bridge over a scratch workspace; `INSTANCE` names its gateway."""

    INSTANCE = ""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="relay-no-ceiling-")
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.broker = _Broker()
        self.lines: "list[str]" = []
        for d in ("results", "tasks", "state"):
            (self.root / d).mkdir()
        suffix = f".{self.INSTANCE}" if self.INSTANCE else ""
        state = self.root / "state"
        values = dict(
            RESULTS_DIR=self.root / "results", ARCHIVE_RESULTS_DIR=self.root / "results" / "archive",
            UNDELIVERABLE_RESULTS_DIR=self.root / "results" / "undelivered",
            TASKS_DIR=self.root / "tasks", _STATE=state,
            GATEWAY_INSTANCE=self.INSTANCE, _INST_SUFFIX=suffix,
            OWNER_ACTIVITY_FILE=state / "last-owner-activity.json",
            _DELIVERY_CORE=None, _RELAY_CORE=None, _req=self.broker.request, _log=self.lines.append,
            _fire_on_task=lambda *a, **k: None,
            _WITHHELD_TASK_OUTPUT={}, _SETTLE_PENDING=set(), _SETTLE_SCANNED=set(),
            _SETTLE_LOADED=False, _REDELIVERED=set(), _HELD_TASKS={}, _HELD_REPORTED=set(),
            _LOG_COUNTS={},
            **{name: state / f"{stem}{suffix}.json" for name, stem in _LEDGERS.items()})
        stack = contextlib.ExitStack()
        for name, value in values.items():
            stack.enter_context(patch.object(gw, name, value))
        stack.enter_context(patch.object(outbox, "_activity_completed", lambda *a, **k: None))
        self.addCleanup(stack.close)
        self.outbox_root = gateway_instance.outbox_root(gw.RESULTS_DIR, self.INSTANCE)

    def serve(self, task: dict) -> str:
        """Queue a served task as the poll loop does: a relay ask acknowledged first."""
        if task.get("ack_first") is True:
            self.assertTrue(gw._take_ack_first(dict(task), set(gw._load_inflight())))
            return gw._local_tid(task["id"])
        tid, _durable = gw._write_task(dict(task))
        gw._save_inflight(set(gw._load_inflight()) | {tid})
        return tid

    def answer(self, tid: str, body: str = PRIVATE) -> None:
        (gw.RESULTS_DIR / f"{tid}.txt").write_text(body)

    def drain(self) -> "set[str]":
        inflight = set(gw._load_inflight())
        gw._post_ready_results(inflight)
        return inflight

    def quarantined(self) -> "list[str]":
        d = gw.UNDELIVERABLE_RESULTS_DIR
        return sorted(p.name for p in d.iterdir() if not p.name.startswith(".")) if d.is_dir() else []

    def backoff_counts(self, *ids: str) -> "list[tuple]":
        return sorted(k for k in gw._LOG_COUNTS if k[0] in ids)

    def drain_while_the_marks_cannot_be_read(self, *tids: str) -> None:
        """One pass that waits under the local id (`deferred`); `tids` limits it to those."""
        marks = gw.ACK_FIRST_FILE.read_text()
        gw.ACK_FIRST_FILE.write_text("{not json")
        if tids:
            gw._post_ready_results(set(tids))
        else:
            self.drain()
        gw.ACK_FIRST_FILE.write_text(marks)

    def delivered_after_waiting(self, body: str = PRIVATE) -> str:
        """A relay result that waited in every way that counts (misses, waits inside the
        backoff, the marks unreadable), then delivered; its counts are checked on the way."""
        ask = self.serve(voice_ask())
        self.answer(ask, body)
        self.broker.result = "down"
        clock = [time.time()]
        gw._delivery_core("relay").backend.clock = lambda: clock[0]
        for _pass in range(3):
            self.drain()                                 # a miss, then two waits inside its backoff
        clock[0] += 60.0
        self.drain()                                     # a second miss
        marks = gw.ACK_FIRST_FILE.read_text()
        gw.ACK_FIRST_FILE.write_text("{not json")
        self.drain()                                     # deferred while the marks cannot be read
        gw.ACK_FIRST_FILE.write_text(marks)
        gw._LOG_COUNTS[("another-task", "pending")] = 3
        self.assertEqual(self.backoff_counts(ASK_A, ask),
                         sorted({(ASK_A, "pending"), (ASK_A, "unconfirmed"), (ask, "deferred")}))
        self.broker.result = "ok"
        clock[0] += 60.0
        self.assertNotIn(ask, self.drain())
        return ask


class TheRule(_Bridge):
    """Which results get the relay rule, and what it does where today's policy parks."""

    def older_window_runs_out(self, failures: int) -> None:
        # The Mac updated while an older bridge's 10-minute window was running: that window
        # is on disk, and it runs out under the updated bridge.
        ask = self.serve(voice_ask())
        self.answer(ask)
        self.broker.result = "down"
        stored = json.dumps({"id": ASK_A, "body": TEXT}).encode()
        old = gw._delivery_core()
        old.backend.publish(ASK_A, stored, source_ready_sha256=outbox.source_digest(PRIVATE))
        old.deliver_one(ASK_A, stored)
        age_retry(self.outbox_root, ASK_A, 700, failures=failures)
        self.drain()
        self.assertEqual(self.quarantined(), [])
        self.assertTrue((gw.RESULTS_DIR / f"{ask}.txt").exists())
        record = item(self.outbox_root, ASK_A)
        self.assertEqual((record["status"], record["requeue_reason"]), ("QUEUED", gw._RELAY_REOPENED))
        self.assertNotIn("retry", record, "the older window was kept")
        self.broker.result = "ok"
        self.assertNotIn(ask, self.drain())
        self.assertEqual(self.broker.results()[-1], {"id": ASK_A, "body": TEXT})
        self.assertEqual(item(self.outbox_root, ASK_A)["status"], "DELIVERED")
        self.assertEqual(gw._load_ack_first(), {})

    def test_a_park_left_by_an_older_window_is_reopened_never_quarantined(self):
        self.older_window_runs_out(failures=20)      # parked before the claim

    def test_a_window_that_runs_out_during_a_send_is_reopened_too(self):
        self.older_window_runs_out(failures=4)       # parked by the failed send itself

    def test_a_failed_refused_too_ends_the_result_and_nothing_revives_it(self):
        ask = self.serve(voice_ask())
        typed = self.serve(typed_task())
        self.answer(ask)
        self.answer(typed, "A collaborative workspace.")
        self.broker.result = "400"
        clock = [time.time()]
        gw._delivery_core("relay").backend.clock = lambda: clock[0]
        for _pass in range(6):
            inflight = self.drain()
            gw._save_inflight(inflight)
            clock[0] += 60.0
        self.assertEqual(item(self.outbox_root, TYPED_A)["reason"], "permanent-refusal",
                         "a typed result no longer parks on a refusal")
        # The answer refused, the `failed` in its place refused too: sent once each, then ended.
        self.assertEqual([p for p in self.broker.results() if p["id"] == ASK_A], [ANSWER, FAILED])
        record = item(self.outbox_root, ASK_A)
        self.assertEqual((record["status"], record["reason"]), ("PARKED", FOR_GOOD))
        self.assertEqual((json.loads(record["payload"]), json.loads(record["refused_payload"])),
                         (FAILED, ANSWER))
        self.assertEqual(record["resend_epoch"], 1, "the `failed` did not get an epoch of its own")
        self.assertEqual(sorted(n.split("-")[0] for n in self.quarantined()), ["ask", "task"])
        self.assertFalse((gw.RESULTS_DIR / f"{ask}.txt").exists())
        self.assertEqual(gw._load_ack_first(), {}, "the mark outlived the end")
        self.assertNotIn(ask, gw._load_inflight(), "the in-flight id outlived the end")
        self.assertEqual(sum(line.startswith(f"ERROR result {ask}: the broker refused") for line in self.lines), 1)
        self.assertIn(f"for good ({FOR_GOOD}); it is quarantined", "".join(self.lines), "the reason was not logged")
        self.assertEqual(self.backoff_counts(ASK_A, ask), [], "counts outlived the end")
        self.assertIn(f"result {ask}: its relay result's end is complete; its task is settled and its relay "
                      "mark retired", self.lines)
        # A restart: the reclaim, the keep pass and the drain revive nothing, and post nothing.
        inflight = set(gw._load_inflight())
        gw._reclaim_parked_relay_results(inflight)
        self.assertFalse(gw._keep_relay_results_in_flight(inflight))
        gw._post_ready_results(inflight)
        self.assertEqual([p for p in self.broker.results() if p["id"] == ASK_A], [ANSWER, FAILED])
        self.assertEqual(item(self.outbox_root, ASK_A)["reason"], FOR_GOOD)

    def test_a_delivered_relay_results_backoff_counts_go_and_its_cadence_restarts(self):
        ask = self.delivered_after_waiting()
        self.assertEqual(self.backoff_counts(ASK_A, ask), [], "counts outlived the delivery")
        self.assertEqual(gw._LOG_COUNTS[("another-task", "pending")], 3, "another task's count went")
        gw._log_backoff((ASK_A, "unconfirmed"), "the next miss")
        self.assertEqual(self.lines[-1], "the next miss", "the cadence did not start over")

    def test_a_closed_relay_asks_backoff_counts_go_too(self):
        ask = self.delivered_after_waiting("[no-send]")  # a skip: the close is the delivery
        self.assertEqual(self.broker.results()[-1]["no_send"], True)
        self.assertEqual(self.backoff_counts(ASK_A, ask), [], "counts outlived the close")

    def test_misses_are_logged_on_a_backoff(self):
        ask = self.serve(voice_ask())
        self.answer(ask)
        self.broker.result = "down"
        clock = [time.time()]
        gw._delivery_core("relay").backend.clock = lambda: clock[0]
        for _pass in range(8):
            self.drain()
            clock[0] += 60.0
        # Eight misses log the 1st, 2nd, 4th and 8th time, with the count; never parked.
        unconfirmed = [line for line in self.lines if line.startswith(f"result POST not confirmed for {ask}")]
        self.assertEqual([line.rsplit("(seen ", 1)[-1] for line in unconfirmed[1:]],
                         ["2 times)", "4 times)", "8 times)"])
        self.assertEqual(len(unconfirmed), 4)
        self.assertNotEqual(item(self.outbox_root, ASK_A)["status"], "PARKED")

    def crash_after_the_end_then_a_start(self, reason: "str | None" = None) -> None:
        ask = self.serve(voice_ask())
        self.answer(ask)
        self.broker.result = "400"
        clock = [time.time()]
        gw._delivery_core("relay").backend.clock = lambda: clock[0]
        with patch.object(gw, "_forget_ack_first"):    # died before the mark went
            for _pass in range(3):
                self.drain()
                clock[0] += 60.0
        self.assertEqual(item(self.outbox_root, ASK_A)["reason"], FOR_GOOD)
        if reason is not None:
            path = outbox._item_path(self.outbox_root, ASK_A)
            path.write_text(json.dumps(dict(json.loads(path.read_text()), reason=reason)))
        self.assertEqual(gw._load_ack_first(), {ASK_A: ask})
        [copy] = self.quarantined()
        gw._reclaim_parked_relay_results(set())
        self.assertEqual(item(self.outbox_root, ASK_A)["status"], "PARKED", "the reclaim requeued it")
        self.assertEqual((self.quarantined(), gw._load_ack_first()), ([copy], {}))
        self.assertFalse((gw.RESULTS_DIR / f"{ask}.txt").exists())

    def test_a_crash_after_the_end_never_lets_the_reclaim_revive_it(self):
        self.crash_after_the_end_then_a_start()

    def test_the_bare_reason_an_earlier_build_parked_with_is_the_end_too(self):
        self.crash_after_the_end_then_a_start(reason="refused-for-good")

    def test_a_close_of_an_accepted_id_keeps_todays_policy(self):
        # The broker already took this task's result: no `failed` may follow it.
        ask = self.serve(voice_ask())
        self.answer(ask, "Your calendar is clear.")
        self.drain()
        gw._record_ack_first(ask)                    # a retirement that did not commit
        self.broker.result = lambda p: "400" if p.get("no_send") else "ok"
        self.answer(ask, "[no-send]")                # a second reply, a suppression
        gw._save_inflight({ask})
        self.drain()
        record = item(self.outbox_root, f"{ASK_A}.lease-close")
        self.assertEqual((record["status"], record["reason"]), ("PARKED", "permanent-refusal"))
        self.assertEqual([p.get("status") for p in self.broker.results()], [None, None])

    def test_unreadable_marks_hold_only_a_result_that_may_be_a_relay_asks(self):
        ask = self.serve(voice_ask())
        typed = self.serve(typed_task())
        # Sutando's session-end note: a voice task too, but never a relay ask.
        end = self.serve({**typed_task(SESSION_END), "source": "matrixrtc-voice", "priority": "low"})
        gw.ACK_FIRST_FILE.write_text("{not json")
        self.answer(ask)
        self.answer(typed, "A collaborative workspace.")
        self.answer(end, "Noted the call.")
        for _pass in range(5):
            self.drain()
        self.assertEqual(sorted(p["id"] for p in self.broker.results()), sorted([SESSION_END, TYPED_A]))
        self.assertIsNone(item(self.outbox_root, ASK_A).get("status"), "published without its rule")
        deferred = [line for line in self.lines if line.startswith(f"delivery deferred for {ask}")]
        self.assertEqual(len(deferred), 3, "the wait is not logged on a backoff (passes 1, 2 and 4)")
        unreadable = [line for line in self.lines if line.startswith("ack_first marks unreadable")]
        self.assertTrue(1 <= len(unreadable) <= 5, f"not on a backoff: {len(unreadable)} lines")
        gw.ACK_FIRST_FILE.write_text(json.dumps({ASK_A: ask}))
        self.drain()
        self.assertEqual(self.broker.results()[-1], ANSWER)

    def test_a_re_asks_answer_outside_the_ledger_goes_back_in_flight(self):
        ask = self.serve(voice_ask())
        gw.ARCHIVE_RESULTS_DIR.mkdir(parents=True, exist_ok=True)
        (gw.ARCHIVE_RESULTS_DIR / f"{EMPTY_HOLDER}-1785976425.txt").write_text("")
        self.answer(ask, f"[deduped: {EMPTY_HOLDER}]")
        self.drain()
        reask = gw._reask_id(ask)
        gw._save_inflight(set())                     # the re-ask left the ledger
        self.answer(reask)
        inflight: "set[str]" = set()
        self.assertTrue(gw._keep_relay_results_in_flight(inflight))
        self.assertEqual((inflight, gw._load_inflight()), ({reask}, {reask}))
        gw._post_ready_results(inflight)
        self.assertEqual(self.broker.results(), [ANSWER])
        self.assertFalse(gw._keep_relay_results_in_flight(set()), "a delivered result was put back")

    def test_a_wait_inside_the_backoff_is_logged_on_a_backoff(self):
        ask = self.serve(voice_ask())
        typed = self.serve(typed_task())
        self.answer(ask)
        self.answer(typed, "A collaborative workspace.")
        self.broker.result = "down"
        self.drain()                                 # one failed send each, then their backoff
        now = time.time()
        gw._delivery_core("relay").backend.clock = lambda: now
        gw._delivery_core().backend.clock = lambda: now
        for _pass in range(8):
            self.drain()
        waits = [line for line in self.lines if line.startswith(f"result {ask}: pending retry")]
        self.assertEqual(len(waits), 4, "not the 1st, 2nd, 4th and 8th wait")
        typed_waits = [line for line in self.lines if line.startswith(f"result {typed}: pending retry")]
        self.assertEqual(len(typed_waits), 8, "a typed result's wait is no longer logged as before")

    def test_a_refused_payload_with_no_failed_to_swap_in_ends_at_once(self):
        for stored in ("not json", json.dumps({"body": "no id"}), json.dumps({"id": ""}), json.dumps(FAILED)):
            self.assertIsNone(gw._failed_in_place_of(stored))
        self.assertEqual(json.loads(gw._failed_in_place_of(json.dumps(ANSWER))), FAILED)
        # Through the outbox: a malformed envelope is refused for good, and nothing names a task.
        core = gw._delivery_core("relay")
        core.backend.publish(ASK_A, b"not json")
        core.deliver_one(ASK_A, b"not json")
        record = item(self.outbox_root, ASK_A)
        self.assertEqual((record["status"], record["reason"], record["payload"]),
                         ("PARKED", FOR_GOOD, "not json"))
        self.assertNotIn("refused_payload", record)

    def test_only_a_parked_result_is_reopened(self):
        ask = self.serve(voice_ask())
        self.answer(ask)
        self.broker.result = "down"
        self.drain()
        core = gw._delivery_core("relay")
        before = item(self.outbox_root, ASK_A)
        gw._reopen_relay_result(core, ASK_A, ask, {"status": "QUEUED"})
        self.assertEqual(item(self.outbox_root, ASK_A), before, "a live item was requeued")
        self.assertFalse(any("was parked" in line for line in self.lines))

    def transient_failures_keep_the_same_result(self, answer: str) -> None:
        ask = self.serve(voice_ask())
        self.answer(ask)
        self.broker.result = answer
        self.drain()
        age_retry(self.outbox_root, ASK_A, 700)              # failing past the 10-minute window
        self.drain()
        record = item(self.outbox_root, ASK_A)
        self.assertEqual((record["status"], json.loads(record["payload"])), ("READY", ANSWER))
        self.assertNotIn("requeue_reason", record, "it was parked on the way")
        self.broker.result = "ok"
        gw._delivery_core("relay").backend.clock = lambda: time.time() + 60.0
        self.drain()
        self.assertEqual(self.broker.results(), [ANSWER] * 3)

    def test_the_brokers_accept_is_final_its_room_send_is_the_brokers(self):
        # The broker posts a relay ask's outcome itself, dead letter included; read back
        # here, a send it dead-letters would be a refusal retried with no ceiling.
        self.broker.delivery = DEAD
        ask = self.serve(voice_ask())
        self.answer(ask)
        self.assertNotIn(ask, self.drain())
        self.assertEqual(item(self.outbox_root, ASK_A)["status"], "DELIVERED")
        self.assertEqual(self.broker.delivery_reads, 0, "the relay lane read the delivery")
        self.assertEqual(gw._load_ack_first(), {})
        self.assertIs(gw._delivery_core("relay").provider.read_back, False)
        self.assertIs(gw._delivery_core().provider.read_back, True, "today's lane lost its read")
        # The same answer under today's policy is the refusal it is meant to be.
        typed = self.serve(typed_task())
        self.answer(typed, TEXT)
        self.assertIn(typed, self.drain())
        self.assertEqual(self.broker.delivery_reads, 1)
        self.assertNotEqual(item(self.outbox_root, TYPED_A)["status"], "DELIVERED")


for _answer in ("408", "425", "429", "502", "down"):
    setattr(TheRule, f"test_a_{_answer}_keeps_the_same_result_past_the_window",
            lambda self, answer=_answer: self.transient_failures_keep_the_same_result(answer))


class TheRuleOnANamedInstance(_Bridge):
    INSTANCE = "dev"

    def test_a_delivered_relay_results_backoff_counts_go_by_both_ids(self):
        ask = self.delivered_after_waiting()
        self.assertNotEqual(ask, ASK_A)
        self.assertEqual(self.backoff_counts(ASK_A, ask), [], "counts outlived the delivery")

    def test_a_reported_folds_backoff_counts_go_by_both_ids(self):
        holder = self.serve(voice_ask(ASK_A))
        asker = self.serve(voice_ask(ASK_B))
        self.answer(holder, "Your calendar is clear.")   # answered, not yet accepted: a report
        self.answer(asker, f"[deduped: {holder}]")
        self.broker.result = "down"
        gw._post_ready_results({asker})                  # the report missed: by broker id
        self.drain_while_the_marks_cannot_be_read(asker)  # the report waited: by local id
        self.assertEqual({k[0] for k in self.backoff_counts(ASK_B, asker)}, {ASK_B, asker})
        self.broker.result = "ok"
        gw._delivery_core("relay").backend.clock = lambda: time.time() + 60.0
        gw._post_ready_results({asker})
        self.assertEqual(self.broker.results()[-1]["body"], UNVERIFIED)
        self.assertEqual(gw._load_ack_first(), {ASK_A: holder}, "the report kept its mark")
        self.assertEqual(self.backoff_counts(ASK_B, asker), [], "counts outlived the report")

    def test_an_ended_relay_results_backoff_counts_go_by_both_ids(self):
        ask = self.serve(voice_ask())
        self.answer(ask)
        self.broker.result = "400"
        clock = [time.time()]
        gw._delivery_core("relay").backend.clock = lambda: clock[0]
        self.drain()                                     # the answer refused: a miss, by broker id
        clock[0] += 60.0
        self.drain_while_the_marks_cannot_be_read()      # a wait, by local id
        self.assertEqual({k[0] for k in self.backoff_counts(ASK_A, ask)}, {ASK_A, ask})
        for _pass in range(2):
            gw._save_inflight(self.drain())
            clock[0] += 60.0
        self.assertEqual(item(self.outbox_root, ASK_A)["reason"], FOR_GOOD)
        self.assertEqual(gw._load_ack_first(), {})
        self.assertEqual(self.backoff_counts(ASK_A, ask), [], "counts outlived the end")

    def outside_the_in_flight_set(self, age: float) -> "tuple[str, str]":
        ask = self.serve(voice_ask())
        typed = self.serve(typed_task())
        gw._save_inflight(set())                     # the ledger lost both; only the sweep looks
        for tid in (ask, typed):
            self.answer(tid)
            landed = time.time() - age
            os.utime(gw.RESULTS_DIR / f"{tid}.txt", (landed, landed))
        inflight: "set[str]" = set()
        with patch.object(gw, "_last_orphan_sweep", 0.0):
            gw._reconcile_orphan_results(inflight)
        self.assertEqual(inflight, {ask})
        self.assertEqual(gw._load_inflight(), {ask})
        self.assertTrue((gw.RESULTS_DIR / f"{ask}.txt").exists(), "the sweep moved the relay result")
        self.assertNotIn(ASK_A, [p["id"] for p in self.broker.results()], "the sweep posted it")
        gw._post_ready_results(inflight)
        self.assertEqual([p for p in self.broker.results() if p["id"] == ASK_A], [ANSWER],
                         "not posted once by the drain, unlabelled")
        self.assertEqual(item(self.outbox_root, ASK_A)["status"], "DELIVERED")
        return ask, typed

    def test_the_orphan_sweep_never_relabels_a_relay_result(self):
        _ask, typed = self.outside_the_in_flight_set(age=3600.0)
        # A typed result in the same state keeps today's labelled recovery.
        self.assertEqual([p["body"] for p in self.broker.results() if p["id"] == TYPED_A],
                         [gw._RECOVERED_LABEL + TEXT])

    def test_the_orphan_sweep_never_quarantines_a_day_old_relay_result(self):
        _ask, typed = self.outside_the_in_flight_set(age=1.5 * DAY)
        self.assertEqual([n.split(".")[1] for n in self.quarantined()], ["too-old"])
        self.assertTrue(self.quarantined()[0].startswith(typed))

    def unreadable_then_swept(self, ledger) -> None:
        ask = self.serve(voice_ask())
        gw._save_inflight(set())
        self.answer(ask)
        landed = time.time() - 1.5 * DAY
        os.utime(gw.RESULTS_DIR / f"{ask}.txt", (landed, landed))
        ledger.write_text("{not json")
        with patch.object(gw, "_last_orphan_sweep", 0.0):
            gw._reconcile_orphan_results(set())
        self.assertTrue((gw.RESULTS_DIR / f"{ask}.txt").exists())
        self.assertEqual((self.quarantined(), self.broker.results()), ([], []))

    def test_while_the_marks_cannot_be_read_the_sweep_leaves_a_voice_asks_result(self):
        self.unreadable_then_swept(gw.ACK_FIRST_FILE)

    def test_while_the_alias_ledger_cannot_be_read_the_sweep_leaves_a_voice_asks_result(self):
        self.unreadable_then_swept(gw.DEDUP_ALIAS_FILE)


class ReportedWhileRetrying(_Bridge):
    """A relay result the broker has not accepted ten minutes into its retry run is reported in
    the gateway status, as a count and the oldest one's age, and is still retried."""

    def setUp(self):
        super().setUp()
        for name, value in (("GATEWAY_STATUS_FILE", self.root / "state" / "gateway-status.json"),
                            ("_HELD_UNRESOLVED", set())):
            patcher = patch.object(gw, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def status(self) -> dict:
        gw._emit_gateway_status(True)
        return json.loads(gw.GATEWAY_STATUS_FILE.read_text())

    def retrying(self, *asks: str) -> "list[str]":
        tids = [self.serve(voice_ask(ask)) for ask in asks]
        for tid in tids:
            self.answer(tid)
        self.broker.result = "down"
        self.drain()
        return tids

    def test_only_a_relay_result_retrying_past_the_threshold_is_reported(self):
        self.retrying(ASK_A, ASK_B)
        typed = self.serve(typed_task())
        self.answer(typed, "A collaborative workspace.")
        self.drain()
        self.assertNotIn("relay_results_retrying", self.status(), "reported under the threshold")
        age_retry(self.outbox_root, ASK_A, 900)
        age_retry(self.outbox_root, ASK_B, gw.RELAY_RESULTS_RETRYING_AFTER_S - 60)
        age_retry(self.outbox_root, TYPED_A, 900)        # not a relay result: it parks instead
        reported = self.status()["relay_results_retrying"]
        self.assertEqual(reported["count"], 1)
        self.assertTrue(900 <= reported["oldest_age_s"] < 960, reported)
        age_retry(self.outbox_root, ASK_B, 120)
        reported = self.status()["relay_results_retrying"]
        self.assertEqual(reported["count"], 2)
        self.assertTrue(900 <= reported["oldest_age_s"] < 960, "not the oldest one's age")
        self.assertNotEqual(item(self.outbox_root, ASK_A)["status"], "PARKED", "reporting gave up on it")
        self.broker.result = "ok"
        self.drain()
        self.assertEqual(gw._load_ack_first(), {})
        self.assertNotIn("relay_results_retrying", self.status(), "the report outlived the delivery")

    def test_a_delivered_result_whose_mark_a_crash_kept_is_not_reported(self):
        ask, = self.retrying(ASK_A)
        age_retry(self.outbox_root, ASK_A, 900)
        self.broker.result = "ok"
        with patch.object(gw, "_forget_ack_first"):    # died before the mark went
            self.drain()
        self.assertEqual((item(self.outbox_root, ASK_A)["status"], gw._load_ack_first()),
                         ("DELIVERED", {ASK_A: ask}))
        self.assertNotIn("relay_results_retrying", self.status())

    def test_a_record_that_cannot_be_read_is_skipped_and_the_status_still_written(self):
        self.retrying(ASK_A, ASK_B)
        for ask in (ASK_A, ASK_B):
            age_retry(self.outbox_root, ask, 900)
        real = gw.read_item

        def read_item(root, item_id):
            if item_id == ASK_A:
                raise PermissionError(13, "Permission denied")
            return real(root, item_id)
        with patch.object(gw, "read_item", read_item):
            status = self.status()
        self.assertIs(status["connected"], True)
        self.assertEqual(status["relay_results_retrying"]["count"], 1)

    def test_a_field_that_cannot_be_worked_out_is_left_out_and_the_status_still_written(self):
        self.retrying(ASK_A)
        path = outbox._item_path(self.outbox_root, ASK_A)
        record = json.loads(path.read_text())
        record["retry"]["started_at"] = float("-inf")    # an infinite age has no int
        path.write_text(json.dumps(record))
        for _write in range(2):
            status = self.status()
            self.assertIs(status["connected"], True)
            self.assertNotIn("relay_results_retrying", status)
        self.assertEqual(sum("relay_results_retrying not reported (OverflowError" in line
                             for line in self.lines), 2, "not logged on the backoff")


class TheReclaim(_Bridge):
    """The reclaim at start, in process, over what an older bridge left."""

    def parked_by_an_older_bridge(self) -> str:
        ask = self.serve(voice_ask())
        self.answer(ask)
        older_bridge.park(gw, ask)
        self.assertEqual(item(self.outbox_root, ASK_A)["status"], "PARKED")
        return ask

    def test_a_copy_set_aside_on_purpose_stays_for_the_operator(self):
        ask = self.serve(voice_ask())
        self.answer(ask)
        # Withheld before any publish (an unattributed worker's reply): no parked item.
        gw._quarantine_undelivered(gw.RESULTS_DIR / f"{ask}.txt", ask, "attribution refused")
        kept = self.quarantined()
        inflight: "set[str]" = set()
        gw._reclaim_parked_relay_results(inflight)
        self.assertEqual((self.quarantined(), inflight), (kept, set()))
        self.assertFalse((gw.RESULTS_DIR / f"{ask}.txt").exists())

    def test_a_live_result_at_its_name_is_never_replaced(self):
        ask = self.parked_by_an_older_bridge()
        kept = self.quarantined()
        self.answer(ask, "A newer reply.")
        gw._reclaim_parked_relay_results(set())
        self.assertEqual((gw.RESULTS_DIR / f"{ask}.txt").read_text(), "A newer reply.")
        self.assertEqual(self.quarantined(), kept)
        self.assertEqual(item(self.outbox_root, ASK_A)["status"], "PARKED")

    def test_the_newest_copy_is_restored_under_the_asks_id(self):
        ask = self.parked_by_an_older_bridge()
        [older] = self.quarantined()
        reask = gw._reask_id(ask)
        newer = gw.UNDELIVERABLE_RESULTS_DIR / f"{reask}-{time.time_ns() + 10**9}.txt"
        newer.write_text("[dm-only]\nThe re-ask's answer.")
        gw._reclaim_parked_relay_results(set())
        self.assertEqual((gw.RESULTS_DIR / f"{ask}.txt").read_text(), "[dm-only]\nThe re-ask's answer.")
        self.assertFalse((gw.RESULTS_DIR / f"{reask}.txt").exists())
        self.assertEqual(self.quarantined(), [older])

    def test_a_reclaim_cut_short_is_finished_by_the_next_start(self):
        ask = self.parked_by_an_older_bridge()
        gw._save_inflight(set())                     # it left the in-flight set
        inflight: "set[str]" = set()
        with patch.object(gw.undelivered_quarantine, "rename_noreplace",
                          side_effect=OSError(5, "I/O error")):
            gw._reclaim_parked_relay_results(inflight)
        record = item(self.outbox_root, ASK_A)
        self.assertEqual((record["status"], record["requeue_reason"]), ("QUEUED", gw._RELAY_RECLAIMED))
        self.assertEqual(gw._load_inflight(), {ask})
        self.assertFalse((gw.RESULTS_DIR / f"{ask}.txt").exists())
        gw._reclaim_parked_relay_results(set(gw._load_inflight()))
        self.assertEqual((gw.RESULTS_DIR / f"{ask}.txt").read_text(), PRIVATE)
        self.assertEqual(self.quarantined(), [])
        self.drain()
        self.assertEqual(self.broker.results(), [{"id": ASK_A, "body": TEXT}])

    def assert_left_parked_or_resumable(self, ask: str) -> None:
        """Nothing moved: the copy waits in quarantine, nothing is live at the ask's name."""
        self.assertEqual(len(self.quarantined()), 1)
        self.assertFalse((gw.RESULTS_DIR / f"{ask}.txt").exists())

    def test_a_quarantine_that_cannot_be_listed_leaves_the_parked_item_for_the_operator(self):
        ask = self.parked_by_an_older_bridge()

        class _Unlistable(type(gw.UNDELIVERABLE_RESULTS_DIR)):
            def glob(self, pattern):
                raise OSError(errno.EACCES, "Permission denied")
        with patch.object(gw, "UNDELIVERABLE_RESULTS_DIR", _Unlistable(gw.UNDELIVERABLE_RESULTS_DIR)):
            self.assertIsNone(gw._parked_copy(ask))
            gw._reclaim_parked_relay_results(set())
        self.assert_left_parked_or_resumable(ask)
        self.assertEqual(item(self.outbox_root, ASK_A)["status"], "PARKED", "requeued with no copy to send")
        self.assertIn(f"result {ask}: its relay result is parked and no quarantined copy of it was found; "
                      "left for the operator", self.lines)

    def test_a_mark_that_names_no_valid_task_is_passed_over(self):
        ask = self.parked_by_an_older_bridge()
        gw._save_inflight(set())
        # Malformed entries beside the real one: an id the broker never issues, a local id not a string.
        gw.ACK_FIRST_FILE.write_text(json.dumps({ASK_A: ask, "../escape": "task-x", "ask-7d4e2f1a9b8c": 7}))
        (gw.RESULTS_DIR / "task-x.txt").write_text("not a relay answer")
        inflight: "set[str]" = set()
        gw._reclaim_parked_relay_results(inflight)
        self.assertEqual(inflight, {ask}, "only the valid mark is reclaimed")
        self.assertFalse(gw._keep_relay_results_in_flight(inflight), "a malformed mark was followed")
        self.assertEqual(inflight, {ask})

    def test_a_parked_item_someone_else_requeued_first_is_left_to_them(self):
        ask = self.parked_by_an_older_bridge()
        gw._save_inflight(set())
        inflight: "set[str]" = set()
        with patch.object(gw, "requeue_item_with_epoch",
                          return_value=(outbox.RequeueOutcome.NOT_PARKED, None)) as requeue:
            gw._reclaim_parked_relay_results(inflight)
        requeue.assert_called_once()
        self.assertEqual(inflight, set(), "put in flight without its requeue")
        self.assert_left_parked_or_resumable(ask)

    def test_an_id_that_cannot_be_put_back_in_flight_moves_nothing_until_the_next_start(self):
        ask = self.parked_by_an_older_bridge()
        gw._save_inflight(set())
        with patch.object(gw, "_save_inflight", return_value=False):
            gw._reclaim_parked_relay_results(set())
        self.assert_left_parked_or_resumable(ask)
        self.assertIn(f"result {ask}: its parked relay result could not be put back in flight; "
                      "the next start reclaims it", self.lines)
        record = item(self.outbox_root, ASK_A)
        self.assertEqual((record["status"], record["requeue_reason"]), ("QUEUED", gw._RELAY_RECLAIMED))
        gw._reclaim_parked_relay_results(set())          # the next start
        self.assertEqual((gw.RESULTS_DIR / f"{ask}.txt").read_text(), PRIVATE)
        self.assertEqual(gw._load_inflight(), {ask})

    def test_a_name_taken_in_the_move_or_no_no_replace_rename_keeps_the_copy(self):
        ask = self.parked_by_an_older_bridge()
        for code, why in ((errno.EEXIST, f"a live result already holds {ask}.txt"),
                          (errno.ENOTSUP, "this platform has no no-replace rename")):
            with patch.object(gw.undelivered_quarantine, "rename_noreplace",
                              side_effect=FileExistsError(code, os.strerror(code))):
                gw._reclaim_parked_relay_results(set())
            self.assert_left_parked_or_resumable(ask)
            [kept] = self.quarantined()
            self.assertIn(f"result {ask}: its parked relay result stays as {kept}: {why}", self.lines)


class _Crash(Exception):
    """A process that died at this step."""


class EndCutShort(_Bridge):
    """The end of a relay result the broker refused for good, cut short at each of its steps:
    a start-up finishes it once, and nothing is posted again."""

    def refused_for_good_until(self, step: str, before: bool) -> str:
        ask = self.serve(voice_ask())
        self.answer(ask)
        self.broker.result = "400"
        clock = [time.time()]
        gw._delivery_core("relay").backend.clock = lambda: clock[0]
        real = getattr(gw, step)
        self.drain()                                     # the answer refused: a miss, by broker id
        clock[0] += 60.0
        self.drain_while_the_marks_cannot_be_read()      # a wait, by local id

        def dies(*a, **k):
            if not before:
                real(*a, **k)
            raise _Crash(step)
        with patch.object(gw, step, new=dies), self.assertRaises(_Crash):
            for _pass in range(4):
                self.drain()
                clock[0] += 60.0
        self.assertEqual(item(self.outbox_root, ASK_A)["reason"], FOR_GOOD)
        self.assertEqual(gw._load_ack_first(), {ASK_A: ask}, "the mark went before the rest")
        return ask

    def assert_finished_by_a_start(self, ask: str) -> None:
        self.assertEqual({k[0] for k in self.backoff_counts(ASK_A, ask)}, {ASK_A, ask},
                         "no count under each id to check the finish against")
        with patch.multiple(gw, _SETTLE_PENDING=set(), _SETTLE_SCANNED=set(), _SETTLE_LOADED=False):
            gw._reclaim_parked_relay_results(set(gw._load_inflight()))
            self.assertEqual(gw._load_ack_first(), {})
            self.assertEqual(self.backoff_counts(ASK_A, ask), [], "counts outlived the finished end")
            self.assertNotIn(ask, gw._load_inflight())
            self.assertIsNone(gw.find_task_file(gw.TASKS_DIR, ask), "the task was left in the queue")
            self.assertEqual(len(self.quarantined()), 1)
            self.assertFalse((gw.RESULTS_DIR / f"{ask}.txt").exists())
            for _pass in range(3):
                self.drain()
        self.assertEqual(self.broker.results(), [ANSWER, FAILED], "posted again")
        self.assertEqual(item(self.outbox_root, ASK_A)["reason"], FOR_GOOD)

    def test_cut_short_before_the_quarantine(self):
        self.assert_finished_by_a_start(self.refused_for_good_until("_dispose_terminal", before=True))

    def test_cut_short_before_the_task_is_settled(self):
        self.assert_finished_by_a_start(self.refused_for_good_until("_settle_task", before=True))

    def test_cut_short_after_the_task_is_settled_before_the_ledger_is_saved(self):
        ask = self.refused_for_good_until("_settle_task", before=False)
        self.assertIn(ask, gw._load_inflight(), "the ledger was saved before the cut")
        self.assert_finished_by_a_start(ask)

    def test_cut_short_before_the_mark_is_retired(self):
        ask = self.refused_for_good_until("_forget_ack_first", before=True)
        self.assertNotIn(ask, gw._load_inflight())
        self.assert_finished_by_a_start(ask)

    def test_an_unreadable_alias_ledger_leaves_the_end_for_the_next_start(self):
        ask = self.refused_for_good_until("_dispose_terminal", before=True)
        gw.DEDUP_ALIAS_FILE.write_text("{not json")
        gw._reclaim_parked_relay_results(set(gw._load_inflight()))
        self.assertEqual(gw._load_ack_first(), {ASK_A: ask})
        gw.DEDUP_ALIAS_FILE.unlink()
        self.assert_finished_by_a_start(ask)


    def test_a_finish_that_cannot_complete_a_step_leaves_the_end_for_the_next_start(self):
        ask = self.refused_for_good_until("_dispose_terminal", before=True)
        for step, stuck in (("_dispose_terminal", None), ("_settle_task", False), ("_save_inflight", False),
                            ("_forget_ack_first", False)):
            with patch.object(gw, step, return_value=stuck):
                gw._reclaim_parked_relay_results(set(gw._load_inflight()))
            self.assertEqual(gw._load_ack_first(), {ASK_A: ask}, f"the mark went with {step} undone")
        complete = f"result {ask}: its relay result's end is complete"
        self.assertFalse(any(line.startswith(complete) for line in self.lines), "success logged before it was")
        self.assertIn(f"result {ask}: its task is settled, but its relay mark could not be retired; "
                      "the next start finishes the end", self.lines)
        self.assert_finished_by_a_start(ask)
        self.assertEqual(sum(line.startswith(complete) for line in self.lines), 1)


class EndCutShortOnANamedInstance(EndCutShort):
    INSTANCE = "dev"


class WithdrawnAccess(_Bridge):
    """A held relay ask whose local access is withdrawn after it was staged is withheld
    (#5617). It is already acknowledged, so withheld for good it would wait forever: past
    the threshold it is ended with a `failed` result instead, and never published."""

    def withheld_on_a_drop(self, age: float = 0.0) -> Path:
        """Staged under the owner cap, then acked under a guest cap: kept staged, unpublished."""
        with patch.object(gw, "_post_ack_first", return_value="retry"):
            self.assertFalse(gw._take_ack_first(voice_ask(), set(gw._load_inflight())))
        [staged] = sorted(gw._held_dir().glob(f".{ASK_A}.txt.*.tmp"))
        if age:
            landed = time.time() - age
            os.utime(staged, (landed, landed))
        self.advance(cap="guest")
        self.assertFalse((gw.TASKS_DIR / f"{ASK_A}.txt").exists(), "published under the lowered cap")
        if age < gw.ACK_FIRST_UNRESOLVED_AFTER_S:
            self.assertIs(gw._load_held()[ASK_A]["accepted"], True)
        return staged

    def advance(self, cap: "str | None" = "guest") -> bool:
        with patch.object(gw, "LOCAL_TIER", cap or gw.LOCAL_TIER):
            return gw._advance_held_tasks(set(gw._load_inflight()))

    def assert_ended(self) -> None:
        self.assertEqual([p for p in self.broker.results() if p["id"] == ASK_A][-1], FAILED)
        self.assertEqual(item(self.outbox_root, ASK_A)["status"], "DELIVERED")
        self.assertFalse((gw.TASKS_DIR / f"{ASK_A}.txt").exists(), "an ended ask was published")
        self.assertEqual((sorted(gw._held_dir().glob("*.tmp")), gw._load_held()), ([], {}))
        self.assertEqual(gw._load_ack_first(), {})
        self.assertNotIn(ASK_A, gw._load_inflight())

    def test_still_withdrawn_past_the_threshold_it_ends_with_one_failed_result(self):
        self.withheld_on_a_drop(age=700.0)
        self.assert_ended()
        self.assertEqual(self.broker.results(), [FAILED], "not exactly one `failed`")
        warnings = [line for line in self.lines if line.startswith(f"WARNING ack_first task {ASK_A}: local access withdrawn")]
        self.assertEqual(len(warnings), 1)
        self.assertEqual(self.backoff_counts(ASK_A), [])
        # A restart revives nothing: the held tasks, the reclaim, the keep pass, the drain.
        self.advance(cap="guest")
        self.advance(cap=None)
        gw._reclaim_parked_relay_results(set(gw._load_inflight()))
        self.assertFalse(gw._keep_relay_results_in_flight(set(gw._load_inflight())))
        self.drain()
        self.assertEqual(self.broker.results(), [FAILED])
        self.assertFalse((gw.TASKS_DIR / f"{ASK_A}.txt").exists())

    def test_a_cap_restored_before_the_threshold_publishes_it_once(self):
        staged = self.withheld_on_a_drop()
        landed = time.time() - 300.0
        os.utime(staged, (landed, landed))
        self.advance(cap="guest")                        # still withdrawn, not yet overdue
        self.assertIn(ASK_A, gw._load_held())
        self.assertTrue(self.advance(cap="owner"))       # restored: published
        self.assertTrue((gw.TASKS_DIR / f"{ASK_A}.txt").exists())
        self.assertEqual((gw._load_held(), self.broker.results()), ({}, []), "a `failed` was sent")
        self.assertEqual(gw._load_ack_first(), {ASK_A: ASK_A}, "the published ask lost its mark")

    def test_an_unprovable_envelope_past_the_threshold_stays_withheld(self):
        staged = self.withheld_on_a_drop()
        cases = {"no access_tier line": lambda: staged.write_text(
                     staged.read_text().replace("\naccess_tier: owner\n", "\n"))}
        if os.geteuid() != 0:                            # root reads a mode-000 file
            cases["unreadable"] = lambda: staged.chmod(0)
        original = staged.read_text()
        for name, damage in cases.items():
            with self.subTest(name):
                staged.write_text(original)
                damage()
                landed = time.time() - 700.0
                os.utime(staged, (landed, landed))
                self.advance(cap="guest")
                staged.chmod(0o600)
                self.assertIn(ASK_A, gw._load_held(), "an unprovable envelope was ended")
                self.assertIn(ASK_A, gw._HELD_UNRESOLVED, "not reported")
                self.assertEqual(self.broker.results(), [], "a `failed` was sent")
                self.assertFalse((gw.TASKS_DIR / f"{ASK_A}.txt").exists())

    def test_a_failed_the_broker_cannot_take_yet_is_retried_then_the_end_completes(self):
        self.broker.result = "down"
        self.withheld_on_a_drop(age=700.0)
        held = gw._load_held()[ASK_A]
        self.assertEqual((held["ending"], held["accepted"]), (True, True), "the decision was not recorded first")
        self.assertEqual(gw._load_ack_first(), {ASK_A: ASK_A})
        self.broker.result = "ok"
        gw._delivery_core("relay").backend.clock = lambda: time.time() + 60.0
        self.advance(cap="owner")                        # the decision stands though the cap came back
        self.assert_ended()
        self.assertFalse((gw.TASKS_DIR / f"{ASK_A}.txt").exists())

    def test_a_failed_refused_for_good_still_ends_it(self):
        self.broker.result = "400"
        self.withheld_on_a_drop(age=700.0)
        self.assertEqual(item(self.outbox_root, ASK_A)["reason"], FOR_GOOD)
        self.assertEqual(self.broker.results(), [FAILED])
        self.assertEqual((gw._load_held(), gw._load_ack_first()), ({}, {}))

    def test_a_failed_the_broker_dead_letters_still_ends_it(self):
        self.broker.delivery = DEAD
        self.withheld_on_a_drop(age=700.0)
        self.assert_ended()
        self.assertEqual((self.broker.results(), self.broker.delivery_reads), ([FAILED], 0))

    def test_an_end_whose_steps_cannot_complete_waits_for_the_next_pass(self):
        staged = self.withheld_on_a_drop()
        landed = time.time() - 700.0
        os.utime(staged, (landed, landed))
        real_put = gw._put_held
        with patch.object(gw, "_put_held", new=lambda tid, entry: False if entry and entry.get("ending")
                          else real_put(tid, entry)):
            self.advance(cap="guest")                    # the decision cannot be recorded: nothing sent
        self.assertEqual((self.broker.results(), gw._load_held()[ASK_A].get("ending")), ([], None))
        self.broker.result = "down"
        self.advance(cap="guest")                        # decided; the `failed` cannot be sent yet
        root = self.outbox_root
        outbox.park_item(root, ASK_A, "invalid-retry-state")
        self.advance(cap="guest")                        # a park the relay rule could not prevent: reopened
        self.assertEqual((item(root, ASK_A)["status"], item(root, ASK_A)["requeue_reason"]),
                         ("QUEUED", gw._RELAY_REOPENED))
        self.broker.result = "ok"
        with patch.object(gw, "_save_inflight", return_value=False):
            self.advance(cap="guest")                    # sent, but the ledger cannot be saved
        self.assertIs(gw._load_held()[ASK_A]["ending"], True, "the entry went before the ledger")
        with patch.object(gw, "_forget_ack_first", return_value=False):
            self.advance(cap="guest")                    # done, but the mark cannot be retired
        self.assertIn(f"ack_first task {ASK_A}: ended unpublished, but its relay mark could not be "
                      "retired; the next start retires it", self.lines)
        self.assertEqual(gw._load_held(), {})
        gw._retire_delivered_marks()                     # the next start
        self.assert_ended()
        self.assertEqual([p["id"] for p in self.broker.results()].count(ASK_A), 2, "sent more than retried")

    def test_an_end_cut_short_is_finished_by_the_next_pass_or_start(self):
        # Cut after the `failed` is accepted, before anything is retired: the start's mark
        # sweep keeps the mark the entry still names, and the end retires it last.
        with patch.object(gw, "_drop_staged", side_effect=_Crash("after the accepted `failed`")), \
                self.assertRaises(_Crash):
            self.withheld_on_a_drop(age=700.0)
        self.assertEqual(item(self.outbox_root, ASK_A)["status"], "DELIVERED")
        gw._retire_delivered_marks()
        self.assertEqual(gw._load_ack_first(), {ASK_A: ASK_A}, "the start retired the mark first")
        with patch.object(gw, "_load_held", return_value=None):
            gw._retire_delivered_marks()                 # nor while the manifest cannot be read
        self.assertEqual(gw._load_ack_first(), {ASK_A: ASK_A})
        # Cut before the entry goes: the next pass finishes, the staged file already gone.
        real_put = gw._put_held

        def dies_removing(tid, entry):
            if entry is None:
                raise _Crash("before the entry goes")
            return real_put(tid, entry)
        with patch.object(gw, "_put_held", new=dies_removing), self.assertRaises(_Crash):
            self.advance(cap="guest")
        self.assertEqual((sorted(gw._held_dir().glob("*.tmp")), gw._load_ack_first()), ([], {ASK_A: ASK_A}))
        gw._retire_delivered_marks()
        self.assertEqual(gw._load_ack_first(), {ASK_A: ASK_A})
        self.advance(cap="guest")
        self.assert_ended()
        self.assertEqual(self.broker.results(), [FAILED])
        # Cut before the mark, the entry gone: now the start's sweep retires it.
        gw._record_ack_first(ASK_A)
        gw._retire_delivered_marks()
        self.assertEqual(gw._load_ack_first(), {})


# ── through the real poll loop ───────────────────────────────────────────────

class _Loop(unittest.TestCase):
    INSTANCE = ""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="relay-no-ceiling-loop-")
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.relay = Relay()
        self.addCleanup(self.relay.close)
        self.bridges = Bridges(self, self.root, self.relay)
        if self.INSTANCE:
            self.bridges.env["GATEWAY_INSTANCE"] = self.INSTANCE
        self.ws = self.bridges.ws
        self.results = self.ws / "results"
        self.outbox_root = gateway_instance.outbox_root(self.results, self.INSTANCE)
        self.local = f"task-{self.INSTANCE}~{ASK_A}" if self.INSTANCE else ASK_A

    def state(self, stem: str):
        suffix = f".{self.INSTANCE}" if self.INSTANCE else ""
        path = self.ws / "state" / f"{stem}{suffix}.json"
        return json.loads(path.read_text()) if path.exists() else {}

    def logged(self, text: str) -> int:
        return (self.root / f"bridge-{self.bridges.runs}.log").read_text(errors="replace").count(text)

    def quarantined(self) -> "list[str]":
        d = self.results / "undelivered"
        return sorted(p.name for p in d.iterdir() if not p.name.startswith(".")) if d.is_dir() else []

    def archived(self, tid: str) -> "list[str]":
        return [p.read_text() for p in (self.results / "archive").glob(f"{glob.escape(tid)}-*.txt")]

    def posted(self, broker_id: str = ASK_A) -> "list[dict]":
        return [p for p, _bearer in self.relay.posted if p.get("id") == broker_id]

    def serve(self, *tasks: dict) -> "subprocess.Popen":
        """A running bridge that has acknowledged and published every task."""
        self.relay.queue.extend(tasks)
        bridge = self.bridges.start()
        for task in tasks:
            tid = f"task-{self.INSTANCE}~{task['id']}" if self.INSTANCE else task["id"]
            self.bridges.wait_for((self.ws / "tasks" / f"{tid}.txt").exists, bridge, f"{tid} published")
        return bridge


class NoCeiling(_Loop):
    """The relay rule against a real loop: outages, refusals and credentials."""

    def test_a_relay_result_outlives_a_broker_outage_past_10_minutes_and_a_restart(self):
        self.relay.refusing = True
        first = self.serve(voice_ask(), typed_task())
        (self.results / f"{ASK_A}.txt").write_text(PRIVATE)
        (self.results / f"{TYPED_A}.txt").write_text("A collaborative workspace.")
        self.bridges.refused_result_recorded(first, ASK_A)
        self.bridges.refused_result_recorded(first, TYPED_A)
        self.bridges.stop(first)
        for item_id in (ASK_A, TYPED_A):
            age_retry(self.outbox_root, item_id, 700)
        second = self.bridges.start()
        self.bridges.wait_for(lambda: self.quarantined(), second, "the typed result parked")
        self.bridges.wait_logged(second, f"result POST not confirmed for {ASK_A}")
        self.assertEqual(item(self.outbox_root, TYPED_A)["reason"], "retry-window-exhausted")
        self.assertNotEqual(item(self.outbox_root, ASK_A).get("status"), "PARKED")
        self.bridges.wait_for(lambda: self.state("gateway-status").get("relay_results_retrying"),
                              second, "the relay result reported as retrying")
        self.assertEqual(self.state("gateway-status")["relay_results_retrying"]["count"], 1)
        self.relay.refusing = False
        self.bridges.wait_for(lambda: self.relay.results, second, "the relay result accepted")
        self.bridges.wait_for(lambda: not self.state("remote-task-ack-first"), second, "its mark retired")
        self.bridges.wait_for(lambda: "relay_results_retrying" not in self.state("gateway-status"),
                              second, "the report gone once accepted")
        self.bridges.polls_after(second)
        self.bridges.stop(second)
        self.assertEqual(self.relay.results, [ANSWER], "not posted exactly once")
        self.assertNotIn("requeue_reason", item(self.outbox_root, ASK_A), "it was parked on the way")
        self.assertTrue(all(p == ANSWER for p in self.posted()), "the payload changed")
        self.assertEqual(self.archived(ASK_A), [PRIVATE])
        self.assertEqual([n.split("-")[0] for n in self.quarantined()], ["task"])

    def test_a_definite_refusal_swaps_in_a_failed_result_that_the_broker_accepts(self):
        self.relay.on_result = lambda p: None if p.get("status") == "failed" else (400, {"error": "bad"})
        bridge = self.serve(voice_ask(), typed_task())
        (self.results / f"{ASK_A}.txt").write_text(PRIVATE)
        (self.results / f"{TYPED_A}.txt").write_text("A collaborative workspace.")
        self.bridges.wait_for(lambda: self.relay.results, bridge, "the failed result accepted")
        self.bridges.wait_for(lambda: not self.state("remote-task-ack-first"), bridge, "its mark retired")
        self.bridges.wait_for(lambda: self.quarantined(), bridge, "the typed result parked")
        self.bridges.polls_after(bridge)
        self.bridges.stop(bridge)
        self.assertEqual(self.relay.results, [FAILED])
        self.assertEqual(self.posted(), [ANSWER, FAILED])
        self.assertNotIn("requeue_reason", item(self.outbox_root, ASK_A), "it was parked on the way")
        self.assertEqual(self.archived(ASK_A), [PRIVATE], "the answer was not kept in the archive")
        self.assertEqual(item(self.outbox_root, TYPED_A)["reason"], "permanent-refusal")
        self.assertEqual([n.split("-")[0] for n in self.quarantined()], ["task"])

    def test_a_failed_refused_too_ends_and_a_restart_does_not_revive_it(self):
        self.relay.on_result = lambda p: (400, {"error": "unknown task"})
        first = self.serve(voice_ask())
        (self.results / f"{ASK_A}.txt").write_text(PRIVATE)
        self.bridges.wait_for(lambda: self.quarantined() and not self.state("remote-task-ack-first")
                              and ASK_A not in self.state("remote-task-inflight"), first, "the result ended")
        self.bridges.stop(first)
        self.assertEqual(self.posted(), [ANSWER, FAILED])
        second = self.bridges.start()
        self.bridges.polls_after(second, 5)
        self.bridges.stop(second)
        self.assertEqual(self.posted(), [ANSWER, FAILED], "the restart revived it")
        record = item(self.outbox_root, ASK_A)
        self.assertEqual((record["status"], record["reason"]), ("PARKED", FOR_GOOD))
        self.assertEqual(len(self.quarantined()), 1)
        self.assertEqual(self.relay.results, [])

    def test_a_restart_between_the_refusal_and_the_acceptance_changes_nothing(self):
        def refuse_then_go_down(payload):
            if payload.get("status") != "failed":
                self.relay.refusing = True       # the broker goes down right after refusing
                return 400, {"error": "bad"}
            return None
        self.relay.on_result = refuse_then_go_down
        first = self.serve(voice_ask())
        (self.results / f"{ASK_A}.txt").write_text(PRIVATE)
        self.bridges.wait_for(lambda: self.posted()[-1:] == [FAILED]
                              and self.logged(f"result POST not confirmed for {ASK_A}") >= 2,
                              first, "the failed result refused while the broker is down")
        self.bridges.stop(first)
        self.assertEqual(json.loads(item(self.outbox_root, ASK_A)["payload"]), FAILED)
        self.relay.on_result, self.relay.refusing = None, False
        second = self.bridges.start()
        self.bridges.wait_for(lambda: self.relay.results, second, "the failed result accepted")
        # The mark goes after the archive, so a stop cannot cut the archive short.
        self.bridges.wait_for(lambda: not self.state("remote-task-ack-first"), second, "its mark retired")
        self.bridges.polls_after(second)
        self.bridges.stop(second)
        self.assertEqual(self.relay.results, [FAILED])
        self.assertEqual(self.posted().count(ANSWER), 1, "the refused answer was sent again")
        self.assertEqual(self.archived(ASK_A), [PRIVATE])

    def credential_refused_past_the_window(self, code: int) -> None:
        token_file = self.root / "remote-task.env"
        self.bridges.env.update({"REMOTE_TASK_TOKEN_FILE": str(token_file), "REMOTE_REENROLL": "0",
                                 "REMOTE_AUTH_RECHECK_INTERVAL": "1"})
        self.relay.token = "hermetic-test-token"
        first = self.serve(voice_ask())
        self.relay.token, self.relay.auth_code = "rotated-secret", code   # revoked by the broker
        (self.results / f"{ASK_A}.txt").write_text(PRIVATE)
        self.bridges.refused_result_recorded(first, ASK_A)
        self.bridges.stop(first)
        age_retry(self.outbox_root, ASK_A, 700)
        token_file.write_text("REMOTE_TASK_TOKEN=rotated-secret\n")       # the re-onboard lands
        token_file.chmod(0o600)
        second = self.bridges.start()
        self.bridges.wait_for(lambda: self.relay.results, second, "the result sent again")
        self.bridges.wait_for(lambda: not self.state("remote-task-ack-first"), second, "its mark retired")
        self.bridges.polls_after(second)
        self.bridges.stop(second)
        # A bridge started after the re-onboard takes the rotated token from the file at startup.
        self.assertGreaterEqual(self.logged("waiting for token rotation") + self.logged("token file already rotated")
                                + self.logged("token loaded from REMOTE_TASK_TOKEN_FILE"), 1,
                                "the rotated credential was never picked up from the token file")
        self.assertEqual(self.relay.results, [ANSWER])
        accepted = [bearer for p, bearer in self.relay.posted if p == ANSWER]
        self.assertEqual(accepted[-1], "Bearer rotated-secret")
        self.assertIn("Bearer hermetic-test-token", accepted[:-1], "no send was ever refused")

    def test_a_401_recovers_its_credential_and_sends_again(self):
        self.credential_refused_past_the_window(401)

    def test_a_403_recovers_its_credential_and_sends_again(self):
        self.credential_refused_past_the_window(403)


# The older bridge in a process of its own over the shared workspace, as `older_bridge` replays it.
_OLDER = r"""
import json, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from _helpers.hermetic_gateway import isolate_then_import
gw, _reads = isolate_then_import(root=Path(sys.argv[2]), env=json.loads(sys.argv[5]))
from _helpers import older_bridge
out = older_bridge.park(gw, sys.argv[4]) if sys.argv[3] == "park" else older_bridge.restart(gw)
Path(sys.argv[6]).write_text(json.dumps(out))
"""


class AfterADowngrade(_Loop):
    """An ask acknowledged by an updated bridge, a downgrade before its result, the broker
    unavailable past 10 minutes so the older bridge parks it, then the updated bridge back."""

    def older(self, step: str, answered: str = ""):
        out = self.root / f"older-{step}.json"
        env = hermetic_env(self.root / "home")
        done = subprocess.run(
            [sys.executable, "-c", _OLDER, str(_TESTS), str(self.ws), step, answered,
             json.dumps({"GATEWAY_INSTANCE": self.INSTANCE} if self.INSTANCE else {}), str(out)],
            env=env, capture_output=True, text=True, timeout=120)
        self.assertEqual(done.returncode, 0, done.stdout[-2000:] + done.stderr[-2000:])
        return json.loads(out.read_text())

    def acknowledged_then_downgraded(self, reask: bool) -> str:
        """The updated bridge takes the ask (and, with `reask`, publishes a re-ask of it), and
        stops. Returns the local id whose answer the core then writes."""
        if reask:
            # A holder with no answer: the planner asks again rather than fold.
            (self.results / "archive").mkdir(parents=True, exist_ok=True)
            (self.results / "archive" / f"{EMPTY_HOLDER}-1785976425.txt").write_text("")
        bridge = self.serve(voice_ask())
        answered = self.local
        if reask:
            answered = gw._reask_id(self.local)
            (self.results / f"{self.local}.txt").write_text(f"[deduped: {EMPTY_HOLDER}]")
            self.bridges.wait_for(lambda: self.state("remote-task-inflight") == [answered], bridge,
                                  "the re-ask in flight in place of the ask")
        self.bridges.stop(bridge)
        self.assertEqual(self.state("remote-task-ack-first"), {ASK_A: self.local})
        return answered

    def run_case(self, *, reask: bool = False, restart: bool = False, days_late: float = 0.0):
        answered = self.acknowledged_then_downgraded(reask)
        rfile = self.results / f"{answered}.txt"
        rfile.write_text(PRIVATE)
        if days_late:
            landed = time.time() - days_late * DAY
            os.utime(rfile, (landed, landed))
        parked = self.older("park", answered)
        self.assertEqual((parked["broker"], parked["reason"]), (ASK_A, "retry-window-exhausted"))
        self.assertEqual(json.loads(parked["payload"]), {"id": ASK_A, "body": TEXT})
        self.assertEqual(len(self.quarantined()), 1)
        settled = self.older("restart") if restart else []
        # The older bridge never reads or writes the mark, so it survives the settle.
        self.assertEqual(self.state("remote-task-ack-first"), {ASK_A: self.local})
        updated = self.bridges.start()
        self.bridges.wait_for(lambda: self.relay.results, updated, "the reclaimed result accepted")
        self.bridges.wait_for(lambda: not self.state("remote-task-ack-first"), updated, "its mark retired")
        self.bridges.wait_for(lambda: self.local not in self.state("remote-task-inflight"), updated,
                              "out of flight")
        self.bridges.polls_after(updated, 3)
        self.bridges.stop(updated)
        # Posted once, under the relay ask's id, with the older bridge's stored payload: no
        # contract, so the broker treats it as private. Never relabelled, never quarantined.
        self.assertEqual(self.relay.results, [{"id": ASK_A, "body": TEXT}])
        self.assertEqual([p for p, _b in self.relay.posted], [{"id": ASK_A, "body": TEXT}])
        self.assertEqual(self.quarantined(), [], "a copy was left, or quarantined as too old")
        self.assertIn(PRIVATE, self.archived(self.local), "not restored under the relay ask's local id")
        self.assertEqual(self.archived(answered) if reask else [], [], "restored under the re-ask's id")
        record = item(self.outbox_root, ASK_A)
        self.assertEqual((record["status"], record["requeue_reason"]), ("DELIVERED", gw._RELAY_RECLAIMED))
        self.assertNotIn(self.local, self.state("remote-task-inflight"))
        self.assertEqual(self.logged("reclaimed the relay result an older bridge parked"), 1)
        return settled

    def test_parked_by_the_older_bridge_then_restored(self):
        self.run_case()

    def test_the_older_bridge_restarted_and_the_mac_updated_a_day_later(self):
        # On the primary lane the older bridge never settles an `ask-…` id: its lookup adds `task-`.
        self.assertEqual(self.run_case(restart=True, days_late=1.5), [])

    def test_a_re_ask_with_that_restart_a_day_later(self):
        self.assertEqual(self.run_case(reask=True, restart=True, days_late=1.5),
                         [gw._reask_id(self.local)])


class AfterADowngradeOnANamedInstance(AfterADowngrade):
    INSTANCE = "dev"

    def test_the_older_bridge_restarted_and_the_mac_updated_a_day_later(self):
        # A named instance's local id starts with `task-`, so the older bridge settles it.
        self.assertEqual(self.run_case(restart=True, days_late=1.5), [self.local])


class EndCutShortThroughTheLoop(_Loop):
    """The same, with the real loop dying at each step and a fresh process starting."""

    def cut_short_then_restarted(self, crash: str) -> None:
        self.relay.on_result = lambda p: (400, {"error": "unknown task"})
        self.relay.queue.append(voice_ask())
        first = self.bridges.start(crash=crash)
        self.bridges.wait_for((self.ws / "tasks" / f"{self.local}.txt").exists, first, "the relay ask published")
        (self.results / f"{self.local}.txt").write_text(PRIVATE)
        self.bridges.crashed(first, crash)
        self.assertEqual(self.posted(), [ANSWER, FAILED])
        self.assertEqual(self.state("remote-task-ack-first"), {ASK_A: self.local})
        second = self.bridges.start()
        self.bridges.wait_for(lambda: not self.state("remote-task-ack-first")
                              and self.local not in self.state("remote-task-inflight"), second, "the end finished")
        self.bridges.polls_after(second, 3)
        self.bridges.stop(second)
        self.assertEqual(self.posted(), [ANSWER, FAILED], "posted again after the restart")
        record = item(self.outbox_root, ASK_A)
        self.assertEqual((record["status"], record["reason"]), ("PARKED", FOR_GOOD))
        self.assertEqual(len(self.quarantined()), 1)
        self.assertFalse((self.ws / "tasks" / f"{self.local}.txt").exists(), "the task was left in the queue")

    def test_cut_short_before_the_quarantine(self):
        self.cut_short_then_restarted("end:before-quarantine")

    def test_cut_short_before_the_task_is_settled(self):
        self.cut_short_then_restarted("end:before-settle")

    def test_cut_short_after_the_task_is_settled(self):
        self.cut_short_then_restarted("end:after-settle")

    def test_cut_short_before_the_mark_is_retired(self):
        self.cut_short_then_restarted("end:before-mark")


class EndCutShortThroughTheLoopOnANamedInstance(EndCutShortThroughTheLoop):
    INSTANCE = "dev"


class WithdrawnAccessThroughTheLoop(_Loop):
    """The same end through the real loop, across restarts."""

    def test_withdrawn_past_the_threshold_after_a_restart_ends_once_and_stays_ended(self):
        self.relay.queue.append(voice_ask())
        self.bridges.crashed(self.bridges.start(crash="before-publish"), "the publish")
        [staged] = sorted((self.ws / "tasks" / ".ack-first").glob(f".{ASK_A}.txt.*.tmp"))
        landed = time.time() - 700.0                     # staged ten minutes and more ago
        os.utime(staged, (landed, landed))
        self.bridges.env["REMOTE_TASK_TIER"] = "guest"   # the operator withdrew local access
        second = self.bridges.start()
        self.bridges.wait_for(lambda: self.relay.results and not self.state("remote-task-ack-first")
                              and ASK_A not in self.state("remote-task-inflight")
                              and not self.state("remote-task-ack-first-manifest"), second, "the end")
        self.bridges.polls_after(second)
        self.bridges.stop(second)
        third = self.bridges.start()
        self.bridges.polls_after(third, 3)
        self.bridges.stop(third)
        self.assertEqual(self.posted(), [FAILED], "not exactly one `failed`")
        self.assertEqual(self.relay.results, [FAILED])
        self.assertEqual(self.bridges.fired(), [], "the ended ask reached the core")
        self.assertFalse((self.ws / "tasks" / f"{ASK_A}.txt").exists())
        self.assertEqual(sorted((self.ws / "tasks" / ".ack-first").glob("*.tmp")), [])

    def test_a_crash_after_the_failed_is_accepted_leaves_the_mark_for_the_end_to_retire_last(self):
        self.relay.queue.append(voice_ask())
        self.bridges.crashed(self.bridges.start(crash="before-publish"), "the publish")
        [staged] = sorted((self.ws / "tasks" / ".ack-first").glob(f".{ASK_A}.txt.*.tmp"))
        landed = time.time() - 700.0
        os.utime(staged, (landed, landed))
        self.bridges.env["REMOTE_TASK_TIER"] = "guest"
        self.bridges.crashed(self.bridges.start(crash="withdrawn:after-failed"), "the accepted `failed`")
        self.assertEqual(self.relay.results, [FAILED])
        held = self.state("remote-task-ack-first-manifest")
        self.assertIs(held[ASK_A]["ending"], True)
        self.assertEqual((self.state("remote-task-ack-first"), staged.exists()), ({ASK_A: ASK_A}, True))
        self.assertIn(ASK_A, self.state("remote-task-inflight"))
        # The next start's mark sweep sees a delivered result and no live file: it must keep the mark.
        self.bridges.crashed(self.bridges.start(crash="start:after-mark-sweep"), "the start's mark sweep")
        self.assertEqual(self.state("remote-task-ack-first"), {ASK_A: ASK_A},
                         "the start retired the mark before the end retired the rest")
        self.assertIs(self.state("remote-task-ack-first-manifest")[ASK_A]["ending"], True)
        last = self.bridges.start()
        self.bridges.wait_for(lambda: not self.state("remote-task-ack-first")
                              and not self.state("remote-task-ack-first-manifest"), last, "the end finished")
        self.bridges.polls_after(last)
        self.bridges.stop(last)
        self.assertEqual(self.posted(), [FAILED], "the `failed` was sent again")
        self.assertEqual(self.bridges.fired(), [], "the ended ask reached the core")
        self.assertFalse((self.ws / "tasks" / f"{ASK_A}.txt").exists())
        self.assertFalse(staged.exists())
        self.assertNotIn(ASK_A, self.state("remote-task-inflight"))
        self.assertEqual(self.logged(f"ack_first task {ASK_A}: ended unpublished (local access withdrawn); "
                                     "its relay mark is retired"), 1)


class OutsideTheInFlightSet(_Loop):
    """A relay ask's result whose id left the in-flight set is posted by the drain, once and
    unlabelled, never handled by the orphan sweep (which reads only `task-*` names, so on
    the primary lane nothing else would ever post it)."""

    def assert_posted_once_by_the_drain(self, bridge) -> None:
        self.bridges.wait_for(lambda: self.relay.results, bridge, "the relay result accepted")
        self.bridges.wait_for(lambda: not self.state("remote-task-ack-first"), bridge, "its mark retired")
        self.bridges.polls_after(bridge, 3)
        self.bridges.stop(bridge)
        self.assertEqual(self.relay.results, [ANSWER], "relabelled, or not posted exactly once")
        self.assertEqual([p for p, _b in self.relay.posted], [ANSWER])
        self.assertEqual(self.quarantined(), [], "quarantined as an orphan")
        self.assertEqual(self.archived(self.local), [PRIVATE])

    def lands(self, days_late: float) -> None:
        rfile = self.results / f"{self.local}.txt"
        rfile.write_text(PRIVATE)
        if days_late:
            landed = time.time() - days_late * DAY
            os.utime(rfile, (landed, landed))

    def lost_from_the_ledger_then_restarted(self, days_late: float) -> None:
        self.bridges.stop(self.serve(voice_ask()))
        suffix = f".{self.INSTANCE}" if self.INSTANCE else ""
        (self.ws / "state" / f"remote-task-inflight{suffix}.json").write_text("[]")
        self.lands(days_late)
        self.assert_posted_once_by_the_drain(self.bridges.start())
        self.assertEqual(self.logged("(restored 1 in-flight)"), 1, "not back in flight at start")

    def test_a_fresh_result_outside_the_ledger_after_a_restart(self):
        self.lost_from_the_ledger_then_restarted(days_late=0.0)

    def test_a_day_old_result_outside_the_ledger_after_a_restart(self):
        self.lost_from_the_ledger_then_restarted(days_late=1.5)

    def test_a_result_that_lands_after_its_id_was_dropped_as_abandoned(self):
        bridge = self.serve(voice_ask())
        # The task left the queue long ago with no result: two passes later the id is dropped.
        archive = self.ws / "tasks" / "archive"
        archive.mkdir(parents=True, exist_ok=True)
        moved = archive / f"{self.local}.txt"
        (self.ws / "tasks" / f"{self.local}.txt").rename(moved)
        long_ago = time.time() - 7200
        os.utime(moved, (long_ago, long_ago))
        self.bridges.wait_logged(bridge, f"dropped abandoned in-flight id {self.local}")
        self.lands(days_late=1.5)
        self.assert_posted_once_by_the_drain(bridge)


class OutsideTheInFlightSetOnANamedInstance(OutsideTheInFlightSet):
    INSTANCE = "dev"


class BridgeImportIsHermetic(unittest.TestCase):
    def test_the_bridge_import_reads_no_host_config_token_or_vault(self):
        assert_hermetic(self, _IMPORT_READS)


if __name__ == "__main__":
    unittest.main(verbosity=2)
