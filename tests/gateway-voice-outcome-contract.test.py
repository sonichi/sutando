#!/usr/bin/env python3
"""The Mac bridge's side of the voice-outcome contract (AG2 Space voice relay).

Every result carries `capabilities: ["voice-outcome-v1"]` with an explicit
`dm_only`: an updated broker treats a relay ask's result without them as private,
because an older bridge strips the core's `[dm-only]` line and posts the rest. An
honoured fold of a task served with `ack_first` (a voice relay ask), whose mark is
kept on disk, names the task it was folded into, by the BROKER id the lane knows, in
`consolidated_into`, also after a restart. Typed and Signal tasks keep today's bare
close: the broker already acts on `consolidated_into` for any task. A re-ask and a
report are unchanged, also when the holder itself closed by an honoured fold.

Drives the real `_write_task` (a relay ask through `_take_ack_first`, acknowledged
before it is published), `_post_ready_results`, the orphan sweep and the HITL
control-result retry. A restart runs the real poll loop (`main()`) in fresh
processes over one workspace, against a relay on a real socket.

Run: python3 tests/gateway-voice-outcome-contract.test.py
"""
from __future__ import annotations

import contextlib
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

_REPO = Path(__file__).resolve().parent.parent
_PKG = _REPO / "packages" / "ag2-sparrow"
_TESTS = Path(__file__).resolve().parent
if str(_PKG) not in sys.path:
    sys.path.insert(0, str(_PKG))

sys.path.insert(0, str(_TESTS))
from _helpers.hermetic_gateway import assert_hermetic, isolate_then_import  # noqa: E402
_GW, _IMPORT_READS = isolate_then_import()   # before anything else imports the bridge

from _helpers.gateway_relay import Bridges, Relay  # noqa: E402
from ag2_sparrow import outbox, remote_gateway_bridge as gw  # noqa: E402

CONTRACT = ["voice-outcome-v1"]
OWNER = "@owner:ag2.space"
ROOM = "!room:ag2.space"
# The lane's correlation ids, which the broker serves a relay ask under.
ASK_A, ASK_B, ASK_C = "ask-5f3c9e0a1b2c", "ask-7d4e2f1a9b8c", "ask-9e8d7c6b5a4f"
TYPED_A, TYPED_B = "task-22d83e59601f3a1fef", "task-633325612fbde6e777"
SKIP = "task-skip0000000000000"
EMPTY_HOLDER = "task-0e5d1c2b3a4f5e6d7c"
SIGNAL = {"v": 1, "cid": "cid-1", "route_attempt": 1,
          "source_root": "$root:ag2.space", "source_room": ROOM}
UNVERIFIED = ("⚠️ The holder's delivery destination or sender could not be verified. "
              "This question needs a direct answer.")

# Every per-instance ledger the drain reads, named as the bridge names it.
_LEDGERS = {"INFLIGHT_FILE": "remote-task-inflight", "TASK_ROOMS_FILE": "remote-task-rooms",
            "TASK_MEDIA_FILE": "remote-task-media", "PENDING_ACK_FILE": "remote-task-acks",
            "DEDUP_ALIAS_FILE": "remote-dedup-alias", "ACK_FIRST_FILE": "remote-task-ack-first",
            "ACK_FIRST_MANIFEST_FILE": "remote-task-ack-first-manifest"}


def voice_ask(broker_id: str, ack_first=True,
              text: str = "What's on my calendar today?") -> dict:
    """A task as the broker serves one for `ask_sutando`: no room, the owner as `user_id`."""
    task = {"id": broker_id, "task": text, "source": "matrixrtc-voice", "user_id": OWNER,
            "asked_by": OWNER, "access_tier": "owner", "lease_id": f"gen-{broker_id}"}
    if ack_first is not None:
        task["ack_first"] = ack_first
    return task


def typed_task(broker_id: str, **extra) -> dict:
    """A message typed in a room."""
    return {"id": broker_id, "task": "What is AG2Space?", "source": "ag2space",
            "channel_id": ROOM, "user_id": OWNER, "access_tier": "owner", **extra}


class _Broker:
    """The broker's side of the wire: every result is accepted."""

    def __init__(self):
        self.calls: "list[tuple]" = []

    def request(self, method, path, payload=None, **_kw):
        self.calls.append((method, path, None if payload is None else json.loads(json.dumps(payload))))
        if path == "/v1/agents":
            return {"agents": []}
        return {"ok": True, "delivery_readable": True}

    def results(self) -> "list[dict]":
        return [p for _m, path, p in self.calls if path == "/v1/results"]


class _Bridge(unittest.TestCase):
    """The real bridge over a scratch workspace; `INSTANCE` names its gateway."""

    INSTANCE = ""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="voice-outcome-")
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
            _DELIVERY_CORE=None, _req=self.broker.request, _log=self.lines.append,
            _WITHHELD_TASK_OUTPUT={}, _SETTLE_PENDING=set(), _SETTLE_SCANNED=set(),
            _SETTLE_LOADED=False, _REDELIVERED=set(), _HELD_TASKS={}, _HELD_REPORTED=set(),
            **{name: state / f"{stem}{suffix}.json" for name, stem in _LEDGERS.items()})
        stack = contextlib.ExitStack()
        for name, value in values.items():
            stack.enter_context(patch.object(gw, name, value))
        stack.enter_context(patch.object(outbox, "_activity_completed", lambda *a, **k: None))
        self.addCleanup(stack.close)

    # ── the poll loop's half, the core's half, the drain ─────────────────────

    def serve(self, task: dict) -> str:
        """Queue a served task as the poll loop does: a relay ask staged, acknowledged and
        published; any other task written, then durably in flight."""
        if task.get("ack_first") is True:
            published = gw._take_ack_first(dict(task), set(gw._load_inflight()))
            self.assertTrue(published, "the relay ask was not published")
            return gw._local_tid(task["id"])
        written = gw._write_task(dict(task))
        self.assertIsNotNone(written, "the served task was not queued")
        tid, durable = written
        self.assertTrue(durable, "the served task was not committed durably")
        gw._save_inflight(set(gw._load_inflight()) | {tid})
        return tid

    def answer(self, tid: str, body: str) -> None:
        (gw.RESULTS_DIR / f"{tid}.txt").write_text(body)

    def drain(self, *tids: str) -> "set[str]":
        inflight = set(tids)
        gw._post_ready_results(inflight)
        return inflight

    def accepted(self, broker_id: str) -> bool:
        record = outbox.read_item(gw._delivery_core().backend.root, broker_id) or {}
        return record.get("status") == "DELIVERED"

    def reask_of(self, tid: str) -> str:
        """The one re-ask the planner published for `tid`."""
        reask = gw._reask_id(tid)
        self.assertTrue((gw.TASKS_DIR / f"{reask}.txt").exists(), "no re-ask was published")
        return reask


class EveryResultCarriesTheContract(_Bridge):
    def test_a_private_answer_carries_dm_only_and_only_its_text(self):
        ask = self.serve(voice_ask(ASK_A))
        self.answer(ask, "[dm-only]\nYour 3pm is with Dana.")
        inflight = self.drain(ask)
        self.assertEqual(self.broker.results(), [
            {"id": ASK_A, "body": "Your 3pm is with Dana.", "capabilities": CONTRACT,
             "dm_only": True}])
        self.assertNotIn(ask, inflight)

    def test_a_public_answer_carries_the_contract_and_dm_only_false(self):
        ask = self.serve(voice_ask(ASK_A))
        self.answer(ask, "Your calendar is clear.")
        self.drain(ask)
        self.assertEqual(self.broker.results(), [
            {"id": ASK_A, "body": "Your calendar is clear.", "capabilities": CONTRACT,
             "dm_only": False}])

    def test_a_skip_carries_the_contract_and_dm_only_false(self):
        ask = self.serve(voice_ask(ASK_A))
        self.answer(ask, "[dm-only]\n[no-send]")     # a skip wins: it is delivered nowhere
        self.drain(ask)
        self.assertEqual(self.broker.results(), [
            {"id": ASK_A, "body": "[no-send]", "no_send": True, "capabilities": CONTRACT,
             "dm_only": False}])

    def test_typed_and_signal_results_carry_it_too(self):
        # The broker reads the contract only for a relay ask; every result carries it.
        plain = self.serve(typed_task(TYPED_A))
        signal = self.serve(typed_task(TYPED_B, signal=dict(SIGNAL), thread_root="$root:ag2.space"))
        self.answer(plain, "[dm-only]\nprivate")
        self.answer(signal, "public")
        self.drain(plain, signal)
        posts = {p["id"]: p for p in self.broker.results()}
        self.assertEqual(posts[TYPED_A], {"id": TYPED_A, "body": "private",
                                          "capabilities": CONTRACT, "dm_only": True})
        self.assertEqual(posts[TYPED_B], {"id": TYPED_B, "body": "public",
                                          "capabilities": CONTRACT, "dm_only": False})

    def test_a_hitl_control_result_carries_it(self):
        control = {"id": "task-hitl-click-1"}
        with patch.object(gw, "_WITHHELD_CONTROL_DIR", self.root / "state" / "control"):
            gw._queue_review_control_result(control)
            gw._retry_review_control_results()
        self.assertEqual(self.broker.results(), [
            {"id": "task-hitl-click-1", "body": "[no-send]", "capabilities": CONTRACT,
             "dm_only": False}])


class TodaysKeysAreUntouched(_Bridge):
    """An older broker accepts these payloads. Its `POST /v1/results` handler (backend
    services/agent-message-broker/api/v1.py, post_results) reads only the keys it names
    (`id`, `thread`, `status`, `error_code`, `metadata`, `body`, `no_send`,
    `consolidated_into`) and refuses only on those, so the new keys are inert to it.
    What it reads must therefore be byte-identical to today's, with the two contract
    keys purely added."""

    TODAY = {TYPED_A: {"id": TYPED_A, "body": "Here is the answer."},
             TYPED_B: {"id": TYPED_B, "body": "private"},
             SKIP: {"id": SKIP, "body": "[no-send]", "no_send": True},
             ASK_A: {"id": ASK_A, "body": "Your calendar is clear."}}

    def test_existing_keys_are_todays_bytes_and_the_contract_is_purely_added(self):
        answers = {TYPED_A: "Here is the answer.", TYPED_B: "[dm-only]\nprivate",
                   SKIP: "[no-send]", ASK_A: "Your calendar is clear."}
        tids = []
        for broker_id, body in answers.items():
            task = voice_ask(broker_id) if broker_id.startswith("ask-") else typed_task(broker_id)
            tids.append(self.serve(task))
            self.answer(tids[-1], body)
        self.assertEqual(self.drain(*tids), set())
        posts = self.broker.results()
        self.assertEqual(sorted(p["id"] for p in posts), sorted(self.TODAY))
        for post in posts:
            today = self.TODAY[post["id"]]
            with self.subTest(post["id"]):
                self.assertEqual(set(post) - set(today), {"capabilities", "dm_only"})
                existing = {k: v for k, v in post.items() if k not in ("capabilities", "dm_only")}
                self.assertEqual(json.dumps(existing), json.dumps(today))


class HonouredFold(_Bridge):
    def fold(self, asker_task: dict, holder_task: dict) -> "tuple[str, str]":
        """`asker` folded into `holder`, which answered elsewhere: the planner honours it."""
        holder = self.serve(holder_task)
        asker = self.serve(asker_task)
        self.answer(holder, "[REPLIED]")
        self.answer(asker, f"[deduped: {holder}]")
        return asker, holder

    def test_a_relay_ask_names_its_holder_by_broker_id(self):
        asker, _holder = self.fold(voice_ask(ASK_B), voice_ask(ASK_A))
        inflight = self.drain(asker)
        # No marker and no `no_send`: an updated broker reads either as `suppressed`.
        self.assertEqual(self.broker.results(), [
            {"id": ASK_B, "body": "", "consolidated_into": ASK_A, "capabilities": CONTRACT,
             "dm_only": False}])
        self.assertNotIn(asker, inflight)
        self.assertTrue(self.accepted(ASK_B))

    def test_an_ask_not_served_with_ack_first_closes_as_before(self):
        # Only the broker's exact boolean counts.
        for n, value in enumerate((None, False, "true", 1)):
            with self.subTest(ack_first=value):
                self.broker.calls.clear()
                holder_id, asker_id = f"ask-holder-{n}", f"ask-asker-{n}"
                asker, holder = self.fold(voice_ask(asker_id, ack_first=value),
                                          voice_ask(holder_id))
                self.drain(asker)
                self.assertEqual(self.broker.results(), [
                    {"id": asker_id, "body": f"[deduped: {holder}]", "no_send": True,
                     "capabilities": CONTRACT, "dm_only": False}])

    def test_typed_and_signal_folds_never_get_consolidated_into(self):
        # The broker already acts on the field for any task, so it is never sent for one.
        for name, extra in (("typed", {}),
                            ("signal", {"signal": dict(SIGNAL), "thread_root": "$root:ag2.space"})):
            with self.subTest(name):
                self.broker.calls.clear()
                holder_id, asker_id = (f"{TYPED_A}{name}", f"{TYPED_B}{name}")
                holder = self.serve(typed_task(holder_id, **extra))
                self.answer(holder, "the full answer")
                self.drain(holder)
                self.assertTrue(self.accepted(holder_id))
                asker = self.serve(typed_task(asker_id, **extra))
                self.answer(asker, f"[deduped: {holder}]")
                self.drain(asker)
                self.assertEqual(self.broker.results()[-1], {
                    "id": asker_id, "body": f"[deduped: {holder}]", "no_send": True,
                    "capabilities": CONTRACT, "dm_only": False})

    def test_an_unreadable_alias_ledger_sends_nothing_until_it_reads_again(self):
        asker, _holder = self.fold(voice_ask(ASK_B), voice_ask(ASK_A))
        # The fold's own check fires only if the ledger fails between two reads in one pass.
        with patch.object(gw, "_delivery_tid", return_value=None):
            self.assertIsNone(gw._fold_target(asker, ASK_B, ASK_A))
        gw.DEDUP_ALIAS_FILE.write_text("{not json")
        inflight = self.drain(asker)
        self.assertEqual(self.broker.results(), [], "a result was sent past an unreadable ledger")
        self.assertIn(asker, inflight)
        self.assertTrue((gw.RESULTS_DIR / f"{asker}.txt").exists())
        gw.DEDUP_ALIAS_FILE.unlink()                 # readable again: sent as the fold
        self.drain(asker)
        self.assertEqual(self.broker.results()[0]["consolidated_into"], ASK_A)

    def test_a_holder_from_another_gateway_is_never_named(self):
        # A primary-lane id seen by a named instance: its broker never issued it.
        asker = self.serve(voice_ask(ASK_B))
        with patch.object(gw, "GATEWAY_INSTANCE", "dev"):
            self.assertEqual(gw._fold_target(asker, ASK_B, "task-dev~ask-other"), "ask-other")
            self.assertEqual(gw._fold_target(asker, ASK_B, ASK_A), "")

    def accepted_then_folded(self, keep_mark: bool) -> "tuple[str, set]":
        """The asker's answer is accepted, then the core folds it as a second reply."""
        holder = self.serve(voice_ask(ASK_A))
        asker = self.serve(voice_ask(ASK_B))
        self.answer(asker, "Your calendar is clear.")
        self.drain(asker)
        self.assertNotIn(ASK_B, gw._load_ack_first(), "the accepted result kept its mark")
        if keep_mark:
            gw._record_ack_first(asker)              # a retirement that did not commit
        self.answer(holder, "[REPLIED]")
        self.answer(asker, f"[deduped: {holder}]")
        return asker, self.drain(asker)

    def test_a_fold_for_an_already_accepted_ask_closes_like_a_suppression(self):
        # Its mark went with the accepted answer; the close rides its own record.
        asker, inflight = self.accepted_then_folded(keep_mark=False)
        self.assertEqual(self.broker.results()[-1], {
            "id": ASK_B, "body": f"[deduped: {ASK_A}]", "no_send": True,
            "capabilities": CONTRACT, "dm_only": False})
        self.assertTrue(self.accepted(f"{ASK_B}.lease-close"))
        self.assertNotIn(asker, inflight)
        self.assertEqual(list(gw.UNDELIVERABLE_RESULTS_DIR.glob("*")), [],
                         "the close was quarantined as an unsent reply")

    def test_a_fold_with_a_mark_left_after_acceptance_rides_its_own_close_record(self):
        # The fold posts nothing either, so it closes an accepted id as no_send does.
        asker, inflight = self.accepted_then_folded(keep_mark=True)
        self.assertEqual(self.broker.results()[-1]["consolidated_into"], ASK_A)
        self.assertEqual(len(self.broker.results()), 2)
        self.assertTrue(self.accepted(f"{ASK_B}.lease-close"))
        self.assertNotIn(asker, inflight)
        self.assertEqual(list(gw.UNDELIVERABLE_RESULTS_DIR.glob("*")), [],
                         "the close was quarantined as an unsent reply")

    def test_a_fold_whose_archive_failed_is_sent_again_with_consolidated_into(self):
        # The mark is retired after the archive, so the re-run keeps the fold's shape.
        asker, _holder = self.fold(voice_ask(ASK_B), voice_ask(ASK_A))
        real = gw._archive_result
        with patch.object(gw, "_archive_result", side_effect=[False]):
            inflight = self.drain(asker)
        self.assertIn(asker, inflight)
        self.assertIn(ASK_B, gw._load_ack_first())
        with patch.object(gw, "_archive_result", side_effect=real):
            inflight = self.drain(asker)
        self.assertNotIn(asker, inflight)
        self.assertEqual([p.get("consolidated_into") for p in self.broker.results()], [ASK_A, ASK_A])
        self.assertNotIn(ASK_B, gw._load_ack_first(), "the archived fold kept its mark")

    def test_unreadable_marks_defer_a_voice_asks_fold(self):
        for content in ("{not json", "[]"):
            with self.subTest(content=content):
                self.broker.calls.clear()
                asker, _holder = self.fold(voice_ask(f"{ASK_B}{len(content)}"),
                                           voice_ask(f"{ASK_A}{len(content)}"))
                gw.ACK_FIRST_FILE.write_text(content)
                inflight = self.drain(asker)
                self.assertEqual(self.broker.results(), [])
                self.assertIn(asker, inflight)
                gw.ACK_FIRST_FILE.unlink()

    def test_unreadable_marks_never_stall_a_typed_fold(self):
        # Only a voice ask can carry a mark, so a typed fold closes as before.
        holder = self.serve(typed_task(TYPED_A))
        self.answer(holder, "the full answer")
        self.drain(holder)
        asker = self.serve(typed_task(TYPED_B))
        self.answer(asker, f"[deduped: {holder}]")
        gw.ACK_FIRST_FILE.write_text("{not json")
        inflight = self.drain(asker)
        self.assertEqual(self.broker.results()[-1], {
            "id": TYPED_B, "body": f"[deduped: {holder}]", "no_send": True,
            "capabilities": CONTRACT, "dm_only": False})
        self.assertNotIn(asker, inflight)


class ReAskAndReport(_Bridge):
    """A relay ask's re-ask and report are today's, plus the contract."""

    def reask(self, broker_id: str = ASK_A) -> "tuple[str, str]":
        ask = self.serve(voice_ask(broker_id))
        gw.ARCHIVE_RESULTS_DIR.mkdir(parents=True, exist_ok=True)
        (gw.ARCHIVE_RESULTS_DIR / f"{EMPTY_HOLDER}-1785976425.txt").write_text("")
        self.answer(ask, f"[deduped: {EMPTY_HOLDER}]")
        inflight = self.drain(ask)
        reask = self.reask_of(ask)
        self.assertEqual(inflight, {reask})
        self.assertEqual(self.broker.results(), [], "a re-ask posts nothing")
        return ask, reask

    def test_a_re_asks_answer_goes_under_the_relay_asks_id(self):
        _ask, reask = self.reask()
        self.answer(reask, "Your calendar is clear.")
        self.drain(reask)
        self.assertEqual(self.broker.results(), [
            {"id": ASK_A, "body": "Your calendar is clear.", "capabilities": CONTRACT,
             "dm_only": False}])

    def test_a_re_asks_private_answer_carries_dm_only(self):
        _ask, reask = self.reask()
        self.answer(reask, "[dm-only]\nYour 3pm is with Dana.")
        self.drain(reask)
        self.assertEqual(self.broker.results(), [
            {"id": ASK_A, "body": "Your 3pm is with Dana.", "capabilities": CONTRACT,
             "dm_only": True}])

    def test_a_re_asks_honoured_fold_carries_consolidated_into(self):
        # Posted under the relay ask's id, so the relay ask's flag applies to it.
        _ask, reask = self.reask()
        holder = self.serve(voice_ask(ASK_B))
        self.answer(holder, "[REPLIED]")
        self.answer(reask, f"[deduped: {holder}]")
        self.drain(reask)
        self.assertEqual(self.broker.results(), [
            {"id": ASK_A, "body": "", "consolidated_into": ASK_B, "capabilities": CONTRACT,
             "dm_only": False}])

    def test_a_fold_into_a_re_ask_names_the_ask_it_answers_not_its_local_id(self):
        _ask, reask = self.reask()
        self.answer(reask, "[REPLIED]")
        asker = self.serve(voice_ask(ASK_B))
        self.answer(asker, f"[deduped: {reask}]")
        self.drain(asker)
        self.assertEqual(self.broker.results()[0]["consolidated_into"], ASK_A,
                         f"named the re-ask's local id {reask}, which the broker never issued")

    def test_a_fold_into_an_honoured_fold_is_re_asked_as_before(self):
        # The holder's stored fold has an empty body; it reads as the marker it replaced.
        first, second, third = (self.serve(voice_ask(i)) for i in (ASK_A, ASK_B, ASK_C))
        self.answer(first, "[REPLIED]")
        self.answer(second, f"[deduped: {first}]")
        self.drain(second)
        self.assertEqual(self.broker.results()[-1]["consolidated_into"], ASK_A)
        self.broker.calls.clear()
        self.answer(third, f"[deduped: {second}]")
        inflight = self.drain(third)
        reask = self.reask_of(third)
        self.assertEqual(inflight, {reask})
        self.assertEqual(self.broker.results(), [], "a fold into a fold was reported, not re-asked")
        self.answer(reask, "Your calendar is clear.")
        self.drain(reask)
        self.assertEqual(self.broker.results(), [
            {"id": ASK_C, "body": "Your calendar is clear.", "capabilities": CONTRACT,
             "dm_only": False}])

    def test_a_report_is_a_completed_warning_without_consolidated_into(self):
        holder = self.serve(voice_ask(ASK_A))
        asker = self.serve(voice_ask(ASK_B))
        self.answer(holder, "Your calendar is clear.")    # answered, not yet accepted
        self.answer(asker, f"[deduped: {holder}]")
        inflight = self.drain(asker)
        self.assertEqual(self.broker.results(), [
            {"id": ASK_B, "body": UNVERIFIED, "capabilities": CONTRACT, "dm_only": False}])
        self.assertNotIn(asker, inflight)
        self.assertEqual(gw._load_ack_first(), {ASK_A: holder}, "the accepted report kept its mark")


class NamedInstance(_Bridge):
    INSTANCE = "dev"

    def test_a_fold_of_one_relay_ask_into_another_sends_the_earlier_ask_id(self):
        holder = self.serve(voice_ask(ASK_A))
        asker = self.serve(voice_ask(ASK_B))
        self.assertEqual((holder, asker), (f"task-dev~{ASK_A}", f"task-dev~{ASK_B}"))
        self.answer(holder, "[REPLIED]")
        self.answer(asker, f"[deduped: {holder}]")
        self.drain(asker)
        self.assertEqual(self.broker.results(), [
            {"id": ASK_B, "body": "", "consolidated_into": ASK_A, "capabilities": CONTRACT,
             "dm_only": False}])
        self.assertTrue(self.accepted(ASK_B))
        self.assertEqual(gw._delivery_core().backend.root.name, ".outbox.dev")

    def test_a_fold_into_a_re_ask_strips_the_instance_and_the_hash(self):
        ask = self.serve(voice_ask(ASK_A))
        gw.ARCHIVE_RESULTS_DIR.mkdir(parents=True, exist_ok=True)
        (gw.ARCHIVE_RESULTS_DIR / f"{EMPTY_HOLDER}-1785976425.txt").write_text("")
        self.answer(ask, f"[deduped: {EMPTY_HOLDER}]")
        self.drain(ask)
        reask = self.reask_of(ask)
        self.answer(reask, "[REPLIED]")
        asker = self.serve(voice_ask(ASK_B))
        self.answer(asker, f"[deduped: {reask}]")
        self.drain(asker)
        self.assertEqual(self.broker.results(), [
            {"id": ASK_B, "body": "", "consolidated_into": ASK_A, "capabilities": CONTRACT,
             "dm_only": False}])

    def test_a_private_answer_the_orphan_sweep_recovers_carries_dm_only(self):
        # The sweep reads only `task-*` names, so a relay ask reaches it on a named instance.
        ask = self.serve(voice_ask(ASK_A))
        gw._save_inflight(set())                     # the ledger lost it; only the sweep looks
        self.answer(ask, "[dm-only]\nYour 3pm is with Dana.")
        landed = time.time() - 3600                  # past the sweep's grace, under a day
        os.utime(gw.RESULTS_DIR / f"{ask}.txt", (landed, landed))
        with patch.object(gw, "_last_orphan_sweep", 0.0):
            gw._reconcile_orphan_results(set())
        self.assertEqual(self.broker.results(), [
            {"id": ASK_A, "body": gw._RECOVERED_LABEL + "Your 3pm is with Dana.",
             "capabilities": CONTRACT, "dm_only": True}])
        self.assertEqual(gw._load_ack_first(), {}, "the recovered result kept its mark")


class AfterARestart(unittest.TestCase):
    """Results posted by a restarted bridge, through the real poll loop."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="voice-outcome-loop-")
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.relay = Relay()
        self.addCleanup(self.relay.close)
        self.bridges = Bridges(self, self.root, self.relay)
        self.ws = self.bridges.ws

    def serve_once(self, *broker_ids: str) -> None:
        """A first bridge takes each relay ask, acknowledges and publishes it, and stops."""
        self.relay.queue.extend(voice_ask(b) for b in broker_ids or (ASK_A,))
        first = self.bridges.start()
        for broker_id in broker_ids or (ASK_A,):
            self.bridges.wait_for((self.ws / "tasks" / f"{broker_id}.txt").exists, first,
                                  f"{broker_id} published")
        self.bridges.stop(first)

    def write_private_answer(self) -> None:
        (self.ws / "results" / f"{ASK_A}.txt").write_text("[dm-only]\nYour 3pm is with Dana.")

    def test_a_private_answer_that_landed_while_the_bridge_was_down(self):
        self.serve_once()
        self.write_private_answer()
        second = self.bridges.start()
        self.bridges.wait_for(lambda: self.relay.results, second, "the restarted bridge's result")
        self.bridges.stop(second)
        self.assertEqual(self.relay.results, [
            {"id": ASK_A, "body": "Your 3pm is with Dana.", "capabilities": CONTRACT,
             "dm_only": True}])

    def test_a_private_answer_refused_before_the_restart_is_resent_with_dm_only(self):
        # A retry resends the payload stored at the first attempt, so it must hold dm_only.
        self.relay.refusing = True
        self.relay.queue.append(voice_ask(ASK_A))
        first = self.bridges.start()
        self.bridges.wait_for((self.ws / "tasks" / f"{ASK_A}.txt").exists, first, "the publish")
        self.write_private_answer()
        self.bridges.refused_result_recorded(first, ASK_A)
        self.bridges.stop(first)
        stored = json.loads(outbox.read_item(self.ws / "results" / ".outbox", ASK_A)["payload"])
        self.relay.refusing = False
        second = self.bridges.start()
        self.bridges.wait_for(lambda: self.relay.results, second, "the restarted bridge's resend")
        self.bridges.stop(second)
        self.assertEqual(self.relay.results, [
            {"id": ASK_A, "body": "Your 3pm is with Dana.", "capabilities": CONTRACT,
             "dm_only": True}])
        self.assertEqual(self.relay.results[0], stored, "the restart did not resend the stored payload")

    def test_a_fold_after_a_restart_still_sends_consolidated_into(self):
        self.serve_once(ASK_A, ASK_B)
        (self.ws / "results" / f"{ASK_A}.txt").write_text("[REPLIED]")
        (self.ws / "results" / f"{ASK_B}.txt").write_text(f"[deduped: {ASK_A}]")
        # Only the asker in flight: a room-less holder whose own close is accepted first
        # is no longer verifiable, and the planner reports instead.
        (self.ws / "state" / "remote-task-inflight.json").write_text(json.dumps([ASK_B]))
        second = self.bridges.start()
        self.bridges.wait_for(lambda: self.relay.results, second, "the restarted bridge's fold")
        marks = self.ws / "state" / "remote-task-ack-first.json"
        self.bridges.wait_for(lambda: ASK_B not in json.loads(marks.read_text()), second,
                              "the fold's mark retired after its archive")
        self.bridges.stop(second)
        self.assertEqual(self.relay.results, [
            {"id": ASK_B, "body": "", "consolidated_into": ASK_A, "capabilities": CONTRACT,
             "dm_only": False}])
        self.assertEqual(json.loads(marks.read_text()), {ASK_A: ASK_A},
                         "the holder's mark went although its result was never accepted")


class BridgeImportIsHermetic(unittest.TestCase):
    def test_the_bridge_import_reads_no_host_config_token_or_vault(self):
        assert_hermetic(self, _IMPORT_READS)


if __name__ == "__main__":
    unittest.main(verbosity=2)
