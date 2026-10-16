#!/usr/bin/env python3
"""The Mac bridge's side of the voice-outcome contract (AG2 Space voice relay).

The heartbeat declares `voice-outcome-v1`, and every result carries it with an
explicit `dm_only`: an updated broker treats a relay ask's result without them as
private, because an older bridge strips the core's `[dm-only]` line and posts
the rest. An honoured fold of a task the broker served with `ack_first` (a voice
relay ask) names the task it was folded into, by the BROKER id the lane knows,
in `consolidated_into`. Typed and Signal tasks keep today's bare close: the broker
already acts on `consolidated_into` for any task. A re-ask and a report are
unchanged. The `ack_first` mark lives in a file of its own, committed before the
task is published and retired once its result is accepted. The poll's half is in
tests/gateway-longpoll-timeout.test.py.

Drives the real `_write_task`, `_post_ready_results`, the orphan sweep, the HITL
control-result retry and `_post_heartbeat`. A restart is a fresh `python3`
process that reads only what the first one left on disk.

Run: python3 tests/gateway-voice-outcome-contract.test.py
"""
from __future__ import annotations

import contextlib
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

_REPO = Path(__file__).resolve().parent.parent
_PKG = _REPO / "packages" / "ag2-sparrow"
if str(_PKG) not in sys.path:
    sys.path.insert(0, str(_PKG))

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _helpers.hermetic_gateway import assert_hermetic, isolate_then_import  # noqa: E402
_GW, _IMPORT_READS = isolate_then_import()   # before anything else imports the bridge

from ag2_sparrow import outbox, remote_gateway_bridge as gw  # noqa: E402
from ag2_sparrow.delivery_core import DeliveryOutcome  # noqa: E402

CONTRACT = ["voice-outcome-v1"]
OWNER = "@owner:ag2.space"
ROOM = "!room:ag2.space"
# The lane's correlation ids, which the broker serves a relay ask under.
ASK_A, ASK_B = "ask-5f3c9e0a1b2c", "ask-7d4e2f1a9b8c"
TYPED_A, TYPED_B = "task-22d83e59601f3a1fef", "task-633325612fbde6e777"
EMPTY_HOLDER = "task-0e5d1c2b3a4f5e6d7c"
SIGNAL = {"v": 1, "cid": "cid-1", "route_attempt": 1,
          "source_root": "$root:ag2.space", "source_room": ROOM}
UNVERIFIED = ("⚠️ The holder's delivery destination or sender could not be verified. "
              "This question needs a direct answer.")

# Every per-instance ledger the drain reads, named as the bridge names it.
_LEDGERS = {"INFLIGHT_FILE": "remote-task-inflight", "TASK_ROOMS_FILE": "remote-task-rooms",
            "TASK_MEDIA_FILE": "remote-task-media", "PENDING_ACK_FILE": "remote-task-acks",
            "DEDUP_ALIAS_FILE": "remote-dedup-alias", "ACK_FIRST_FILE": "remote-task-ack-first"}


def voice_ask(broker_id: str, ack_first: bool = True,
              text: str = "What's on my calendar today?") -> dict:
    """A task as the broker serves one for `ask_sutando`: no room, the owner as `user_id`."""
    task = {"id": broker_id, "task": text, "source": "matrixrtc-voice", "user_id": OWNER,
            "asked_by": OWNER, "access_tier": "owner", "lease_id": f"gen-{broker_id}"}
    if ack_first:
        task["ack_first"] = True
    return task


def typed_task(broker_id: str, **extra) -> dict:
    """A message typed in a room."""
    return {"id": broker_id, "task": "What is AG2Space?", "source": "ag2space",
            "channel_id": ROOM, "user_id": OWNER, "access_tier": "owner", **extra}


def _http(code: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError("http://relay.invalid/v1/results", code, "refused", {},
                                  io.BytesIO(b"{}"))


class _Broker:
    """The broker's side of the wire: every result is accepted unless `offline`."""

    def __init__(self):
        self.calls: "list[tuple]" = []
        self.offline = False

    def request(self, method, path, payload=None, **_kw):
        self.calls.append((method, path, None if payload is None else json.loads(json.dumps(payload))))
        if path == "/v1/agents":
            return {"agents": []}
        if path == "/v1/results" and self.offline:
            raise urllib.error.URLError("broker unreachable")
        return {"ok": True, "delivery_readable": True}

    def results(self) -> "list[dict]":
        return [p for _m, path, p in self.calls if path == "/v1/results"]


class _PreContractBroker(_Broker):
    """A broker that predates the contract, as backend `v1.py` post_results and
    `taskqueue.add_result` are today: it refuses only a missing `id` or a bad
    `thread`, reads `body`, `no_send` and `consolidated_into`, and ignores every
    other key. It records the outcome it would give each result."""

    def __init__(self):
        super().__init__()
        self.outcomes: "dict[str, str]" = {}

    def request(self, method, path, payload=None, **kw):
        if path != "/v1/results":
            return super().request(method, path, payload, **kw)
        self.calls.append((method, path, json.loads(json.dumps(payload))))
        rid = str(payload.get("id") or "").strip()
        thread = payload.get("thread")
        if not rid or not (thread is None or thread is False or thread == "ask"):
            raise _http(400)
        body = str(payload.get("body") or "").strip()
        if str(payload.get("consolidated_into") or "").strip():
            outcome = "consolidated"
        elif (payload.get("no_send") or not body
              or body.startswith(("[no-send]", "[REPLIED]")) or body.lower().startswith("[deduped:")):
            outcome = "suppressed"
        else:
            outcome = "responded"
        self.outcomes[rid] = outcome
        return {"ok": True, "delivery_readable": True}


class _Bridge(unittest.TestCase):
    """The real bridge over a scratch workspace; `INSTANCE` names its gateway."""

    INSTANCE = ""
    BROKER = _Broker

    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="voice-outcome-")
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.broker = self.BROKER()
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
            _SETTLE_LOADED=False, _REDELIVERED=set(),
            **{name: state / f"{stem}{suffix}.json" for name, stem in _LEDGERS.items()})
        stack = contextlib.ExitStack()
        for name, value in values.items():
            stack.enter_context(patch.object(gw, name, value))
        stack.enter_context(patch.object(outbox, "_activity_completed", lambda *a, **k: None))
        self.addCleanup(stack.close)

    # ── the poll loop's half, the core's half, the drain ─────────────────────

    def serve(self, task: dict) -> str:
        """Queue a served task as the poll loop does: written, then durably in flight."""
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

    # ── a restart: a fresh process that reads only the disk ──────────────────

    _RESTARTED = r"""
import json, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from ag2_sparrow import outbox, remote_gateway_bridge as gw
root, instance, now, ledgers = Path(sys.argv[2]), sys.argv[3], float(sys.argv[4]), json.loads(sys.argv[5])
suffix = "." + instance if instance else ""
state = root / "state"
gw.RESULTS_DIR = root / "results"
gw.ARCHIVE_RESULTS_DIR = gw.RESULTS_DIR / "archive"
gw.UNDELIVERABLE_RESULTS_DIR = gw.RESULTS_DIR / "undelivered"
gw.TASKS_DIR, gw._STATE = root / "tasks", state
gw.GATEWAY_INSTANCE, gw._INST_SUFFIX = instance, suffix
gw.OWNER_ACTIVITY_FILE = state / "last-owner-activity.json"
for name, stem in ledgers.items():
    setattr(gw, name, state / (stem + suffix + ".json"))
outbox._activity_completed = lambda *a, **k: None
gw._log = lambda m: print(m, file=sys.stderr)
posts = []
def request(method, path, payload=None, **kw):
    if path == "/v1/results":
        posts.append(payload)
        return {"ok": True, "delivery_readable": True}
    return {"agents": []} if path == "/v1/agents" else {}
gw._req = request
gw._delivery_core().backend.clock = lambda: now
inflight = gw._load_inflight()
gw._post_ready_results(inflight)
print(json.dumps({"posts": posts, "inflight": sorted(inflight)}))
"""

    def restart_and_drain(self, now: "float | None" = None) -> dict:
        """A new bridge process drains what this one left in flight, at `now`."""
        run = subprocess.run(
            [sys.executable, "-c", self._RESTARTED, str(_PKG), str(self.root), self.INSTANCE,
             str(time.time() if now is None else now), json.dumps(_LEDGERS)],
            env={k: v for k, v in os.environ.items() if k != "GATEWAY_INSTANCE"},
            capture_output=True, text=True, timeout=60)
        self.assertEqual(run.returncode, 0, run.stderr)
        return json.loads(run.stdout.strip().splitlines()[-1])


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
        self.assertEqual(gw._load_ack_first(), {ASK_A: ASK_A, ASK_B: ASK_B},
                         "both asks were served with ack_first, so both are marked")
        inflight = self.drain(asker)
        # No marker and no `no_send`: an updated broker reads either as `suppressed`.
        self.assertEqual(self.broker.results(), [
            {"id": ASK_B, "body": "", "consolidated_into": ASK_A, "capabilities": CONTRACT,
             "dm_only": False}])
        self.assertNotIn(asker, inflight)
        self.assertTrue(self.accepted(ASK_B))
        self.assertEqual(gw._load_ack_first(), {ASK_A: ASK_A},
                         "the mark goes with the accepted result, and only that one")

    def test_an_ask_not_served_with_ack_first_closes_as_before(self):
        asker, holder = self.fold(voice_ask(ASK_B, ack_first=False), voice_ask(ASK_A))
        self.drain(asker)
        self.assertEqual(self.broker.results(), [
            {"id": ASK_B, "body": f"[deduped: {holder}]", "no_send": True,
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

    def test_an_unreadable_alias_ledger_defers_the_fold(self):
        asker, _holder = self.fold(voice_ask(ASK_B), voice_ask(ASK_A))
        # Unreadable when the holder is resolved: nothing is guessed, nothing is sent.
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

    def test_unreadable_marks_defer_rather_than_close_as_a_suppression(self):
        asker, _holder = self.fold(voice_ask(ASK_B), voice_ask(ASK_A))
        gw.ACK_FIRST_FILE.write_text("{not json")
        inflight = self.drain(asker)
        self.assertEqual(self.broker.results(), [])
        self.assertIn(asker, inflight)

    def test_a_holder_from_another_gateway_is_never_named(self):
        # A primary-lane id seen by a named instance: its broker never issued it.
        asker = self.serve(voice_ask(ASK_B))
        with patch.object(gw, "GATEWAY_INSTANCE", "dev"):
            self.assertEqual(gw._fold_target(asker, ASK_B, "task-dev~ask-other"), "ask-other")
            self.assertEqual(gw._fold_target(asker, ASK_B, ASK_A), "")

    def test_a_fold_after_a_restart_still_sends_consolidated_into(self):
        asker, _holder = self.fold(voice_ask(ASK_B), voice_ask(ASK_A))
        self.assertEqual(self.broker.results(), [], "nothing was decided before the restart")
        # Only the asker in flight: a holder whose own close is accepted first is no
        # longer verifiable for a room-less ask, and the planner reports instead.
        gw._save_inflight({asker})
        after = self.restart_and_drain()
        self.assertEqual(after["posts"], [
            {"id": ASK_B, "body": "", "consolidated_into": ASK_A, "capabilities": CONTRACT,
             "dm_only": False}])
        self.assertNotIn(asker, after["inflight"])


class TheMark(_Bridge):
    """The `ack_first` mark: in a file of its own, committed before the task is."""

    def test_only_the_exact_broker_boolean_marks_a_task(self):
        for value in ("true", 1, None):
            with self.subTest(value=value):
                task = voice_ask(f"ask-not-{value}", ack_first=False)
                task["ack_first"] = value
                self.serve(task)
        self.serve(typed_task(TYPED_A))
        self.assertFalse(gw.ACK_FIRST_FILE.exists(), "no task here was served with ack_first")

    def test_the_mark_is_on_disk_before_the_task_is_published(self):
        real = gw._publish_staged
        seen: "list[str]" = []

        def publish(tmp, path):
            if path.parent == gw.TASKS_DIR:
                seen.append(path.name)
                self.assertIn(ASK_A, json.loads(gw.ACK_FIRST_FILE.read_text()),
                              "the core could answer, and fold, a task with no mark yet")
            return real(tmp, path)

        with patch.object(gw, "_publish_staged", side_effect=publish):
            self.serve(voice_ask(ASK_A))
        self.assertEqual(seen, [f"{ASK_A}.txt"])

    def test_a_mark_that_cannot_commit_publishes_neither_task_nor_ack(self):
        with patch.object(gw, "_durable_write", return_value=False):
            self.assertIsNone(gw._write_task(voice_ask(ASK_A)))
        self.assertFalse((gw.TASKS_DIR / f"{ASK_A}.txt").exists())
        self.assertEqual(list(gw.TASKS_DIR.glob("*.tmp")), [], "the staged task was left behind")

    def test_a_redelivered_pending_ask_recommits_its_mark(self):
        ask = self.serve(voice_ask(ASK_A))
        gw.ACK_FIRST_FILE.unlink()
        self.assertEqual(gw._write_task(voice_ask(ASK_A)), (ask, True))
        self.assertEqual(gw._load_ack_first(), {ASK_A: ask})

    def test_a_mark_that_outlived_its_accepted_result_is_retired_by_the_next_pass(self):
        # A process that crashed after the broker accepted the result, before the
        # mark was retired: the next pass finds the acceptance and retires it.
        ask = self.serve(voice_ask(ASK_A))
        self.answer(ask, "Your calendar is clear.")
        backend = gw._delivery_core().backend
        backend.publish(ASK_A, json.dumps({"id": ASK_A, "body": "Your calendar is clear."}).encode())
        backend.complete(backend.claim(ASK_A, "a-crashed-process"), DeliveryOutcome.CONFIRMED)
        self.assertIn(ASK_A, gw._load_ack_first())
        inflight = self.drain(ask)
        self.assertEqual(self.broker.results(), [], "an accepted result is not sent again")
        self.assertEqual(gw._load_ack_first(), {})
        self.assertNotIn(ask, inflight)

    def test_a_fold_for_an_already_accepted_ask_closes_like_a_suppression(self):
        # A mark that outlived its acceptance (a crash before it was retired): the
        # fold rides its own close record, as the bare marker's no_send does.
        holder = self.serve(voice_ask(ASK_A))
        asker = self.serve(voice_ask(ASK_B))
        self.answer(asker, "Your calendar is clear.")
        self.drain(asker)
        gw._record_ack_first(asker, voice_ask(ASK_B))
        self.answer(holder, "[REPLIED]")
        self.answer(asker, f"[deduped: {holder}]")
        inflight = self.drain(asker)
        self.assertEqual(self.broker.results()[-1]["consolidated_into"], ASK_A)
        self.assertEqual(len(self.broker.results()), 2)
        self.assertTrue(self.accepted(f"{ASK_B}.lease-close"))
        self.assertNotIn(asker, inflight)
        self.assertEqual(list(gw.UNDELIVERABLE_RESULTS_DIR.glob("*")), [],
                         "the close was quarantined as an unsent reply")


class ReAskAndReport(_Bridge):
    """A relay ask's re-ask and report are today's, plus the contract."""

    def reask(self) -> "tuple[str, str]":
        ask = self.serve(voice_ask(ASK_A))
        gw.ARCHIVE_RESULTS_DIR.mkdir(parents=True, exist_ok=True)
        (gw.ARCHIVE_RESULTS_DIR / f"{EMPTY_HOLDER}-1785976425.txt").write_text("")
        self.answer(ask, f"[deduped: {EMPTY_HOLDER}]")
        inflight = self.drain(ask)
        reask = self.reask_of(ask)
        self.assertEqual(inflight, {reask})
        self.assertEqual(self.broker.results(), [], "a re-ask posts nothing")
        self.assertIn(ASK_A, gw._load_ack_first(), "the delivery it answers is still the relay ask")
        return ask, reask

    def test_a_re_asks_answer_goes_under_the_relay_asks_id(self):
        _ask, reask = self.reask()
        self.answer(reask, "Your calendar is clear.")
        self.drain(reask)
        self.assertEqual(self.broker.results(), [
            {"id": ASK_A, "body": "Your calendar is clear.", "capabilities": CONTRACT,
             "dm_only": False}])
        self.assertNotIn(ASK_A, gw._load_ack_first())

    def test_a_re_asks_private_answer_carries_dm_only(self):
        _ask, reask = self.reask()
        self.answer(reask, "[dm-only]\nYour 3pm is with Dana.")
        self.drain(reask)
        self.assertEqual(self.broker.results(), [
            {"id": ASK_A, "body": "Your 3pm is with Dana.", "capabilities": CONTRACT,
             "dm_only": True}])

    def test_a_fold_into_a_re_ask_names_the_ask_it_answers_not_its_local_id(self):
        _ask, reask = self.reask()
        self.answer(reask, "[REPLIED]")
        asker = self.serve(voice_ask(ASK_B))
        self.answer(asker, f"[deduped: {reask}]")
        self.drain(asker)
        self.assertEqual(self.broker.results()[0]["consolidated_into"], ASK_A,
                         f"named the re-ask's local id {reask}, which the broker never issued")

    def test_a_report_is_a_completed_warning_without_consolidated_into(self):
        holder = self.serve(voice_ask(ASK_A))
        asker = self.serve(voice_ask(ASK_B))
        self.answer(holder, "Your calendar is clear.")    # answered, not yet accepted
        self.answer(asker, f"[deduped: {holder}]")
        inflight = self.drain(asker)
        self.assertEqual(self.broker.results(), [
            {"id": ASK_B, "body": UNVERIFIED, "capabilities": CONTRACT, "dm_only": False}])
        self.assertNotIn(asker, inflight)


class NamedInstance(_Bridge):
    INSTANCE = "dev"

    def test_a_fold_of_one_relay_ask_into_another_sends_the_earlier_ask_id(self):
        holder = self.serve(voice_ask(ASK_A))
        asker = self.serve(voice_ask(ASK_B))
        self.assertEqual((holder, asker), (f"task-dev~{ASK_A}", f"task-dev~{ASK_B}"))
        self.assertEqual(gw._load_ack_first(), {ASK_A: holder, ASK_B: asker},
                         "keyed by broker id, naming the local id each was published under")
        self.assertTrue(gw.ACK_FIRST_FILE.name.endswith(".dev.json"))
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


class AfterARestart(_Bridge):
    def test_a_private_answer_that_landed_while_the_bridge_was_down(self):
        ask = self.serve(voice_ask(ASK_A))
        self.answer(ask, "[dm-only]\nYour 3pm is with Dana.")
        after = self.restart_and_drain()
        self.assertEqual(after["posts"], [
            {"id": ASK_A, "body": "Your 3pm is with Dana.", "capabilities": CONTRACT,
             "dm_only": True}])

    def test_a_private_answer_whose_first_send_failed_before_the_restart(self):
        # A retry resends the payload stored at the first attempt, so it must hold dm_only.
        ask = self.serve(voice_ask(ASK_A))
        self.answer(ask, "[dm-only]\nYour 3pm is with Dana.")
        self.broker.offline = True
        self.assertIn(ask, self.drain(ask))
        record = outbox.read_item(gw._delivery_core().backend.root, ASK_A)
        self.assertNotEqual(record["status"], "DELIVERED")
        after = self.restart_and_drain(now=record["retry"]["next_attempt_at"])
        self.assertEqual(after["posts"], [
            {"id": ASK_A, "body": "Your 3pm is with Dana.", "capabilities": CONTRACT,
             "dm_only": True}])
        self.assertEqual(after["inflight"], [])


class APreContractBroker(_Bridge):
    """An older broker accepts every payload, and typed results keep today's outcome."""

    BROKER = _PreContractBroker

    def test_it_accepts_each_shape_and_typed_tasks_keep_their_outcome(self):
        answers = {TYPED_A: ("Here is the answer.", "responded"),
                   TYPED_B: ("[dm-only]\nprivate", "responded"),
                   "task-skip0000000000000": ("[no-send]", "suppressed"),
                   ASK_A: ("Your calendar is clear.", "responded")}
        tids = {}
        for broker_id, (body, _outcome) in answers.items():
            task = voice_ask(broker_id) if broker_id.startswith("ask-") else typed_task(broker_id)
            tids[broker_id] = self.serve(task)
            self.answer(tids[broker_id], body)
        inflight = self.drain(*tids.values())
        self.assertEqual(inflight, set(), "a result the broker accepted is retired")
        self.assertEqual(self.broker.outcomes, {k: v[1] for k, v in answers.items()})
        legacy = {p["id"]: {k: v for k, v in p.items() if k not in ("capabilities", "dm_only")}
                  for p in self.broker.results()}
        self.assertEqual(legacy[TYPED_A], {"id": TYPED_A, "body": "Here is the answer."},
                         "beyond the two contract keys, a typed result is today's payload")
        self.assertEqual(legacy["task-skip0000000000000"],
                         {"id": "task-skip0000000000000", "body": "[no-send]", "no_send": True})

    def test_it_records_a_relay_asks_fold_as_consolidated(self):
        holder = self.serve(voice_ask(ASK_A))
        asker = self.serve(voice_ask(ASK_B))
        self.answer(holder, "[REPLIED]")
        self.answer(asker, f"[deduped: {holder}]")
        self.assertEqual(self.drain(asker), set())
        self.assertEqual(self.broker.outcomes, {ASK_B: "consolidated"})


class Heartbeat(unittest.TestCase):
    def test_the_heartbeat_declares_the_contract_beside_task_ack(self):
        with tempfile.TemporaryDirectory() as d, \
                patch.multiple(gw, _STATE=Path(d), _heartbeat_retry_at=0.0,
                               _last_heartbeat_at=0.0, _last_core_health=None), \
                patch.object(gw, "_health_snapshot", return_value=None), \
                patch.object(gw, "_req") as req:
            self.assertTrue(gw._post_heartbeat(set(), force=True))
        capabilities = req.call_args.args[2]["capabilities"]
        for entry in ("task-ack", "result-skip-markers", "voice-outcome-v1"):
            self.assertIn(entry, capabilities)


class BridgeImportIsHermetic(unittest.TestCase):
    def test_the_bridge_import_reads_no_host_config_token_or_vault(self):
        assert_hermetic(self, _IMPORT_READS)


if __name__ == "__main__":
    unittest.main(verbosity=2)
