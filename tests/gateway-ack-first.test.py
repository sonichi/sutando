#!/usr/bin/env python3
"""The Mac bridge acknowledges a voice relay ask before the core can see it.

An AG2 Space broker serves a voice relay ask with `"ack_first": true`, and only to a poll
that carries the voice-outcome contract. The bridge stages such a task, commits its mark
and a manifest entry, acks it, and publishes it only once the ack is accepted. A refused
ack (a withdrawal, an expiry, a stale lease) discards it, so a withdrawn ask never reaches
the core. After a crash the manifest publishes an accepted task once and discards a refused
one. The mark is kept in a file of its own until the broker accepts the task's result.
Typed and Signal tasks keep today's order: written, then acked after the batch.

Drives the real `_take_ack_first`, `_advance_held_tasks`, `_reconcile_abandoned`,
`_retire_delivered_marks` and `_post_heartbeat` in process. Crashes, restarts and the
poll's proof run the real poll loop (`main()`) in fresh processes over one workspace,
against a relay on a real socket.

Run: python3 tests/gateway-ack-first.test.py
"""
from __future__ import annotations

import contextlib
import io
import json
import sys
import tempfile
import unittest
import urllib.error
import urllib.parse
import urllib.request
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
from ag2_sparrow.delivery_core import DeliveryOutcome  # noqa: E402

CONTRACT = ["voice-outcome-v1"]
OWNER = "@owner:ag2.space"
ROOM = "!room:ag2.space"
ASK_A, ASK_B, ASK_C = "ask-5f3c9e0a1b2c", "ask-7d4e2f1a9b8c", "ask-9e8d7c6b5a4f"
TYPED_A = "task-22d83e59601f3a1fef"
SIGNAL = {"v": 1, "cid": "cid-1", "route_attempt": 1,
          "source_root": "$root:ag2.space", "source_room": ROOM}

_LEDGERS = {"INFLIGHT_FILE": "remote-task-inflight", "TASK_ROOMS_FILE": "remote-task-rooms",
            "TASK_MEDIA_FILE": "remote-task-media", "PENDING_ACK_FILE": "remote-task-acks",
            "DEDUP_ALIAS_FILE": "remote-dedup-alias", "ACK_FIRST_FILE": "remote-task-ack-first",
            "ACK_FIRST_MANIFEST_FILE": "remote-task-ack-first-manifest"}


def voice_ask(broker_id: str, lease: str = "", **extra) -> dict:
    """A relay ask as the broker serves one: no room, the owner as `user_id`."""
    return {"id": broker_id, "task": "What's on my calendar today?", "source": "matrixrtc-voice",
            "user_id": OWNER, "asked_by": OWNER, "access_tier": "owner",
            "lease_id": lease or f"gen-{broker_id}", "ack_first": True, **extra}


def typed_task(broker_id: str, **extra) -> dict:
    return {"id": broker_id, "task": "What is AG2Space?", "source": "ag2space",
            "channel_id": ROOM, "user_id": OWNER, "access_tier": "owner", **extra}


def _not_leased(path: str, stale: bool) -> urllib.error.HTTPError:
    body = {"error": "not leased to you", **({"reason": "stale lease generation"} if stale else {})}
    return urllib.error.HTTPError(f"http://relay{path}", 404, "Not Found", {},
                                  io.BytesIO(json.dumps(body).encode()))


class _Broker:
    """The broker's side of the wire. `answers[broker id]` lists what its next acks get:
    "ok", "withdrawn" or "stale" (404 not leased), "down" (no connection), "busy" (503)
    or "auth" (401); an ack with nothing listed is accepted."""

    def __init__(self):
        self.calls: "list[tuple]" = []
        self.answers: "dict[str, list[str]]" = {}

    def request(self, method, path, payload=None, **_kw):
        self.calls.append((method, path, None if payload is None else json.loads(json.dumps(payload))))
        if path.endswith("/ack"):
            pending = self.answers.get(urllib.parse.unquote(path.split("/")[-2])) or ["ok"]
            answer = pending.pop(0)
            if answer == "down":
                raise urllib.error.URLError("connection refused")
            if answer in ("busy", "auth"):
                code = 503 if answer == "busy" else 401
                raise urllib.error.HTTPError(f"http://relay{path}", code, answer, {}, io.BytesIO(b"{}"))
            if answer in ("withdrawn", "stale"):
                raise _not_leased(path, stale=answer == "stale")
            return {"ok": True, "durable": True}
        return {"agents": []} if path == "/v1/agents" else {"ok": True, "delivery_readable": True}

    def acks(self) -> "list[dict]":
        return [p for _m, path, p in self.calls if path.endswith("/ack")]


class _Bridge(unittest.TestCase):
    """The real bridge over a scratch workspace; `fired` records each wake of the core."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="ack-first-")
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.broker = _Broker()
        self.lines: "list[str]" = []
        self.fired: "list[str]" = []
        for d in ("results", "tasks", "state"):
            (self.root / d).mkdir()
        state = self.root / "state"
        values = dict(
            RESULTS_DIR=self.root / "results", ARCHIVE_RESULTS_DIR=self.root / "results" / "archive",
            UNDELIVERABLE_RESULTS_DIR=self.root / "results" / "undelivered",
            TASKS_DIR=self.root / "tasks", _STATE=state, GATEWAY_INSTANCE="", _INST_SUFFIX="",
            OWNER_ACTIVITY_FILE=state / "last-owner-activity.json",
            _DELIVERY_CORE=None, _req=self.broker.request, _log=self.lines.append,
            _fire_on_task=lambda tid, path, text=None: self.fired.append(tid),
            _WITHHELD_TASK_OUTPUT={}, _SETTLE_PENDING=set(), _SETTLE_SCANNED=set(),
            _SETTLE_LOADED=False, _REDELIVERED=set(), _HELD_TASKS={}, _HELD_REPORTED=set(),
            **{name: state / f"{stem}.json" for name, stem in _LEDGERS.items()})
        stack = contextlib.ExitStack()
        for name, value in values.items():
            stack.enter_context(patch.object(gw, name, value))
        stack.enter_context(patch.object(outbox, "_activity_completed", lambda *a, **k: None))
        self.addCleanup(stack.close)
        self.inflight: "set[str]" = set()

    def take(self, task: dict) -> bool:
        return gw._take_ack_first(dict(task), self.inflight)

    def staged(self, tid: str) -> "list[Path]":
        return sorted(gw.TASKS_DIR.glob(f"{tid}.txt.*.tmp"))

    def manifest(self) -> dict:
        return json.loads(gw.ACK_FIRST_MANIFEST_FILE.read_text()) if gw.ACK_FIRST_MANIFEST_FILE.exists() else {}

    def marks(self) -> dict:
        return json.loads(gw.ACK_FIRST_FILE.read_text()) if gw.ACK_FIRST_FILE.exists() else {}

    def on_disk_inflight(self) -> "set[str]":
        return set(json.loads(gw.INFLIGHT_FILE.read_text())) if gw.INFLIGHT_FILE.exists() else set()

    def assert_never_reached_the_core(self, tid: str) -> None:
        self.assertFalse((gw.TASKS_DIR / f"{tid}.txt").exists(), "the task was published")
        self.assertEqual(self.staged(tid), [], "the staged task was left behind")
        self.assertEqual(self.fired, [], "the core was woken for it")
        self.assertNotIn(tid, self.manifest())


class AcknowledgingFirst(_Bridge):
    def test_nothing_reaches_the_core_before_the_ack_is_accepted(self):
        seen: "list[dict]" = []

        def at_the_ack(method, path, payload=None, **kw):
            if path.endswith("/ack"):
                seen.append({"published": (gw.TASKS_DIR / f"{ASK_A}.txt").exists(),
                             "staged": len(self.staged(ASK_A)), "mark": self.marks(),
                             "entry": dict(self.manifest().get(ASK_A, {})),
                             "inflight": self.on_disk_inflight(), "woken": list(self.fired),
                             "owner_active": gw.OWNER_ACTIVITY_FILE.exists()})
            return self.broker.request(method, path, payload, **kw)

        with patch.object(gw, "_req", at_the_ack):
            self.assertTrue(self.take(voice_ask(ASK_A)))
        [at_ack] = seen
        self.assertEqual((at_ack["published"], at_ack["staged"], at_ack["woken"],
                          at_ack["owner_active"]), (False, 1, [], False))
        # What follows a publish follows this one: the owner's activity, then the wake.
        self.assertIn("What's on my calendar", gw.OWNER_ACTIVITY_FILE.read_text())
        self.assertIn(f"queued {ASK_A}", self.lines)
        # Everything the `durable` ack claims is on disk before it is sent.
        self.assertEqual(at_ack["mark"], {ASK_A: ASK_A})
        self.assertEqual(at_ack["inflight"], {ASK_A})
        entry = at_ack["entry"]
        self.assertEqual({k: entry[k] for k in ("broker_id", "lease_id", "dest", "accepted")},
                         {"broker_id": ASK_A, "lease_id": f"gen-{ASK_A}", "dest": f"{ASK_A}.txt",
                          "accepted": False})
        self.assertEqual(self.broker.acks(), [{"id": ASK_A, "durable": True, "lease_id": f"gen-{ASK_A}"}])
        published = gw.TASKS_DIR / f"{ASK_A}.txt"
        self.assertEqual(gw._held_digest(published.read_text()), entry["sha256"])
        self.assertIn("source: matrixrtc-voice", published.read_text())
        self.assertEqual((self.staged(ASK_A), self.manifest(), self.fired), ([], {}, [ASK_A]))
        self.assertEqual(self.marks(), {ASK_A: ASK_A}, "the mark went before any result")
        self.assertEqual(self.inflight, {ASK_A})

    def test_a_refused_acknowledgement_discards_the_staged_task(self):
        for answer in ("stale", "withdrawn"):
            with self.subTest(answer=answer):
                self.broker.answers[ASK_A] = [answer]
                self.assertFalse(self.take(voice_ask(ASK_A)))
                self.assert_never_reached_the_core(ASK_A)
                self.assertEqual(self.marks(), {}, "a task that never ran kept its mark")
                self.assertNotIn(ASK_A, self.inflight)
                self.assertNotIn(ASK_A, self.on_disk_inflight())
                acks = len(self.broker.acks())
                self.assertFalse(gw._advance_held_tasks(self.inflight))
                self.assertEqual(len(self.broker.acks()), acks, "a discarded task was acked again")

    def test_an_unanswered_ack_keeps_the_task_staged_until_it_is_accepted(self):
        for n, answer in enumerate(("down", "busy", "auth")):
            with self.subTest(answer=answer):
                ask = f"{ASK_B}{n}"
                self.broker.answers[ask] = [answer]
                self.assertFalse(self.take(voice_ask(ask)))      # never raises, even on 401
                self.assertFalse((gw.TASKS_DIR / f"{ask}.txt").exists())
                self.assertEqual(len(self.staged(ask)), 1)
                self.assertIn(ask, self.on_disk_inflight())
                self.assertTrue(gw._advance_held_tasks(self.inflight))   # the next pass, accepted
                self.assertTrue((gw.TASKS_DIR / f"{ask}.txt").exists())
                self.assertEqual((self.staged(ask), self.fired[-1]), ([], ask))
                self.assertNotIn(ask, self.manifest())
        self.assertEqual(len(self.fired), 3, "a task was published more than once")

    def test_a_task_served_again_before_its_ack_is_acked_under_its_newest_lease(self):
        self.broker.answers[ASK_A] = ["down"]
        self.assertFalse(self.take(voice_ask(ASK_A, lease="gen-1")))
        [first] = self.staged(ASK_A)
        self.assertTrue(self.take(voice_ask(ASK_A, lease="gen-2")))
        self.assertFalse(first.exists(), "the earlier serve's staged file was left behind")
        self.assertEqual([a["lease_id"] for a in self.broker.acks()], ["gen-1", "gen-2"])
        self.assertEqual((self.fired, self.manifest()), ([ASK_A], {}))

    def test_a_mark_that_cannot_commit_stages_nothing_and_acks_nothing(self):
        for content in ("{not json", "[]"):
            with self.subTest(content=content):
                gw.ACK_FIRST_FILE.write_text(content)
                self.assertFalse(self.take(voice_ask(ASK_A)))
                self.assertEqual(self.broker.acks(), [])
                self.assert_never_reached_the_core(ASK_A)
                self.assertEqual(gw.ACK_FIRST_FILE.read_text(), content, "unreadable marks were overwritten")

    def test_a_manifest_that_cannot_commit_stages_nothing_and_acks_nothing(self):
        gw.ACK_FIRST_MANIFEST_FILE.write_text("{not json")
        self.assertFalse(self.take(voice_ask(ASK_A)))
        self.assertEqual(self.broker.acks(), [])
        self.assertFalse((gw.TASKS_DIR / f"{ASK_A}.txt").exists())
        self.assertEqual((self.staged(ASK_A), self.marks()), ([], {}))
        self.assertEqual(gw.ACK_FIRST_MANIFEST_FILE.read_text(), "{not json")

    def test_a_vanished_staged_file_without_a_published_copy_is_kept_and_reported(self):
        for name, other in (("no copy", None), ("a task file that does not match", "id: other\n")):
            with self.subTest(name):
                self.lines.clear()
                gw._HELD_REPORTED.clear()
                self.broker.answers[ASK_A] = ["down"]
                self.take(voice_ask(ASK_A))
                entry = self.manifest()[ASK_A]
                self.staged(ASK_A)[0].unlink()
                if other is not None:
                    (gw.TASKS_DIR / f"{ASK_A}.txt").write_text(other)
                for _ in range(2):
                    self.assertFalse(gw._advance_held_tasks(self.inflight))
                self.assertEqual(self.manifest(), {ASK_A: entry}, "the entry was guessed away")
                self.assertEqual(len(self.broker.acks()), 1, "a task that cannot be found was acked")
                self.assertEqual(sum("staged file gone" in line for line in self.lines), 1)
                self.assertEqual(self.fired, [])
                gw.ACK_FIRST_MANIFEST_FILE.unlink()
                (gw.TASKS_DIR / f"{ASK_A}.txt").unlink(missing_ok=True)
                self.broker.calls.clear()

    def test_a_malformed_manifest_entry_is_left_alone(self):
        victim = gw.INFLIGHT_FILE
        victim.write_text("[]")
        entry = {"broker_id": ASK_A, "lease_id": None, "staged": "../state/remote-task-inflight.json",
                 "dest": f"{ASK_A}.txt", "sha256": "", "accepted": True}
        gw.ACK_FIRST_MANIFEST_FILE.write_text(json.dumps({ASK_A: entry}))
        self.assertFalse(gw._advance_held_tasks(self.inflight))
        self.assertEqual((victim.read_text(), self.fired, self.broker.acks()), ("[]", [], []))
        self.assertFalse((gw.TASKS_DIR / f"{ASK_A}.txt").exists())
        self.assertTrue(any("malformed" in line for line in self.lines))

    def test_a_staged_task_is_never_dropped_as_abandoned(self):
        self.broker.answers[ASK_A] = ["down"]
        self.take(voice_ask(ASK_A))
        suspects = gw._reconcile_abandoned(self.inflight, set())
        gw._reconcile_abandoned(self.inflight, suspects)
        self.assertIn(ASK_A, self.inflight)
        self.assertTrue(gw._advance_held_tasks(self.inflight))
        self.assertEqual(self.fired, [ASK_A])

    def test_typed_and_signal_tasks_never_touch_the_manifest_or_the_marks(self):
        for task in (typed_task(TYPED_A), typed_task(f"{TYPED_A}s", signal=dict(SIGNAL),
                                                     thread_root="$root:ag2.space")):
            with self.subTest(task["id"]):
                self.assertEqual(gw._write_task(dict(task)), (task["id"], True))
                self.assertTrue((gw.TASKS_DIR / f"{task['id']}.txt").exists())
        self.assertFalse(gw.ACK_FIRST_MANIFEST_FILE.exists())
        self.assertFalse(gw.ACK_FIRST_FILE.exists())
        self.assertEqual(self.broker.acks(), [], "written tasks are acked after the batch, as before")


class TheMark(_Bridge):
    """The `ack_first` mark: broker id -> the local id, in a file of its own."""

    def deliver(self, broker_id: str) -> None:
        """The outbox shows this result delivered, as a pass that crashed before its archive left it."""
        backend = gw._delivery_core().backend
        backend.publish(broker_id, json.dumps({"id": broker_id, "body": "Clear."}).encode())
        backend.complete(backend.claim(broker_id, "a-crashed-process"), DeliveryOutcome.CONFIRMED)

    def test_the_mark_names_the_local_id_on_a_named_instance(self):
        with patch.multiple(gw, GATEWAY_INSTANCE="dev", _INST_SUFFIX=".dev"):
            self.assertTrue(self.take(voice_ask(ASK_A)))
        self.assertEqual(self.marks(), {ASK_A: f"task-dev~{ASK_A}"})

    def test_at_start_a_mark_goes_only_when_the_outbox_shows_its_result_delivered(self):
        for ask in (ASK_A, ASK_B, ASK_C):
            self.assertTrue(self.take(voice_ask(ask)))
        self.deliver(ASK_A)                                # archived: nothing of it is live
        self.deliver(ASK_B)
        (gw.RESULTS_DIR / f"{ASK_B}.txt").write_text("Clear.")    # accepted, not yet archived
        gw._retire_delivered_marks()
        self.assertEqual(self.marks(), {ASK_B: ASK_B, ASK_C: ASK_C})

    def test_at_start_a_re_asks_live_answer_keeps_its_originals_mark(self):
        self.assertTrue(self.take(voice_ask(ASK_A)))
        reask = gw._reask_id(ASK_A)
        gw.DEDUP_ALIAS_FILE.write_text(json.dumps({reask: ASK_A}))
        self.deliver(ASK_A)
        (gw.RESULTS_DIR / f"{reask}.txt").write_text("Clear.")
        gw._retire_delivered_marks()
        self.assertEqual(self.marks(), {ASK_A: ASK_A})


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


class ThroughTheLoop(unittest.TestCase):
    """The real poll loop in fresh processes; a crash is an exit at a named point."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="ack-first-loop-")
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.relay = Relay()
        self.addCleanup(self.relay.close)
        self.bridges = Bridges(self, self.root, self.relay)
        self.ws = self.bridges.ws
        self.task = self.ws / "tasks" / f"{ASK_A}.txt"

    def state(self, stem: str):
        path = self.ws / "state" / f"{stem}.json"
        return json.loads(path.read_text()) if path.exists() else {}

    def staged(self) -> "list[Path]":
        return sorted((self.ws / "tasks").glob(f"{ASK_A}.txt.*.tmp"))

    def entry(self) -> dict:
        return self.state("remote-task-ack-first-manifest").get(ASK_A) or {}

    def test_a_withdrawal_between_serve_and_acknowledgement_never_reaches_the_core(self):
        self.relay.withdrawn.add(ASK_A)
        self.relay.queue.append(voice_ask(ASK_A))
        bridge = self.bridges.start()
        self.bridges.wait_for(lambda: self.relay.acks and not self.entry(), bridge, "the discard")
        self.bridges.polls_after(bridge)
        self.bridges.stop(bridge)
        self.assertEqual(self.relay.acks, [ASK_A], "a refused ack was retried")
        self.assertFalse(self.task.exists())
        self.assertEqual((self.staged(), self.bridges.fired()), ([], []))
        self.assertNotIn(ASK_A, self.state("remote-task-ack-first"))
        self.assertNotIn(ASK_A, self.state("remote-task-inflight") or [])

    def test_after_a_crash_before_the_ack_a_refused_ack_discards_the_task(self):
        self.relay.queue.append(voice_ask(ASK_A))
        self.bridges.crashed(self.bridges.start(crash="before-ack"), "the ack")
        self.assertEqual((len(self.staged()), self.entry().get("accepted")), (1, False))
        self.relay.withdrawn.add(ASK_A)                    # withdrawn while the Mac was down
        bridge = self.bridges.start()
        self.bridges.wait_for(lambda: not self.entry(), bridge, "the discard after the restart")
        self.bridges.polls_after(bridge)
        self.bridges.stop(bridge)
        self.assertEqual(self.relay.acks, [ASK_A])
        self.assertFalse(self.task.exists())
        self.assertEqual((self.staged(), self.bridges.fired()), ([], []))

    def crash_between_the_ack_and_the_publish(self, crash: str, accepted: bool) -> None:
        self.relay.queue.append(voice_ask(ASK_A))
        self.bridges.crashed(self.bridges.start(crash=crash), crash)
        self.assertFalse(self.task.exists(), "published before the crash point")
        [staged] = self.staged()
        digest = self.entry()["sha256"]
        self.assertEqual(self.entry()["accepted"], accepted)
        self.assertEqual(self.relay.acks, [ASK_A])
        bridge = self.bridges.start()
        self.bridges.wait_for(lambda: self.bridges.fired(), bridge, "the publish after the restart")
        self.bridges.polls_after(bridge)
        self.bridges.stop(bridge)
        self.assertEqual(self.bridges.fired(), [f"{ASK_A}.txt"], "the task did not run exactly once")
        self.assertEqual(gw._held_digest(self.task.read_text()), digest)
        self.assertEqual((self.staged(), self.entry()), ([], {}))
        self.assertFalse(staged.exists())

    def test_a_crash_after_an_accepted_ack_and_before_the_publish_runs_the_task_once(self):
        self.crash_between_the_ack_and_the_publish("after-ack", accepted=False)
        # Not yet recorded as accepted: acked again, and a repeat answers `duplicate`.
        self.assertEqual(self.relay.acks, [ASK_A, ASK_A])

    def test_a_crash_after_the_accepted_ack_is_recorded_publishes_without_a_new_ack(self):
        self.crash_between_the_ack_and_the_publish("before-publish", accepted=True)
        self.assertEqual(self.relay.acks, [ASK_A])

    def crash_between_the_publish_and_the_entrys_removal(self, claimed: bool) -> None:
        self.relay.queue.append(voice_ask(ASK_A))
        self.bridges.crashed(self.bridges.start(crash="after-publish"), "after the publish")
        self.assertTrue(self.task.exists())
        self.assertEqual((self.staged(), self.entry().get("accepted")), ([], True))
        copy = self.task.with_name(f"{ASK_A}.claimed-core-1") if claimed else self.task
        if claimed:
            self.task.rename(copy)
        before = copy.stat()
        bridge = self.bridges.start()
        self.bridges.wait_for(lambda: not self.entry(), bridge, "the entry cleared")
        self.bridges.polls_after(bridge)
        self.bridges.stop(bridge)
        after = copy.stat()
        self.assertEqual((after.st_ino, after.st_mtime_ns), (before.st_ino, before.st_mtime_ns),
                         "the task was published again")
        self.assertEqual(self.task.exists(), not claimed, "a claimed task was queued again")
        self.assertEqual(self.relay.acks, [ASK_A])
        # The first process died before its wake, and a restart never re-fires one.
        self.assertEqual((self.staged(), self.bridges.fired()), ([], []))

    def test_a_crash_after_the_publish_and_before_the_entry_is_removed_runs_the_task_once(self):
        self.crash_between_the_publish_and_the_entrys_removal(claimed=False)

    def test_the_same_crash_with_the_task_claimed_by_the_core_runs_it_once(self):
        self.crash_between_the_publish_and_the_entrys_removal(claimed=True)

    def test_an_updated_bridges_poll_is_served_a_relay_ask_and_an_old_ones_is_not(self):
        self.relay.queue.extend([voice_ask(ASK_A), typed_task(TYPED_A)])
        # The poll an older bridge sends: no contract, so the relay ask keeps its place.
        with urllib.request.urlopen(f"{self.relay.url}/v1/tasks?wait=1", timeout=10) as resp:
            old = json.loads(resp.read())
        self.assertEqual([t["id"] for t in old["tasks"]], [TYPED_A])
        self.assertEqual([t["id"] for t in self.relay.queue], [ASK_A])
        polled = len(self.relay.polls)
        bridge = self.bridges.start()
        self.bridges.wait_for(self.task.exists, bridge, "the relay ask published")
        self.bridges.polls_after(bridge)
        self.bridges.stop(bridge)
        self.assertIn((ASK_A, "wait=1&capabilities=voice-outcome-v1"), self.relay.served)
        self.assertTrue(self.relay.polls[polled:])
        self.assertTrue(all(q.endswith("&capabilities=voice-outcome-v1") for q in self.relay.polls[polled:]),
                        "a poll of the updated bridge did not carry the contract")

    def test_a_relay_ask_is_acknowledged_before_the_rest_of_its_batch(self):
        not_relay = voice_ask(ASK_B, ack_first="true")      # only the exact boolean counts
        self.relay.queue.extend([typed_task(TYPED_A), not_relay, voice_ask(ASK_A)])
        bridge = self.bridges.start()
        self.bridges.wait_for(lambda: len(self.relay.acks) == 3, bridge, "three acks")
        self.bridges.stop(bridge)
        self.assertEqual(self.relay.acks[0], ASK_A)
        self.assertEqual(sorted(self.relay.acks[1:]), sorted([TYPED_A, ASK_B]))
        for tid in (ASK_A, ASK_B, TYPED_A):
            self.assertTrue((self.ws / "tasks" / f"{tid}.txt").exists(), f"{tid} not queued")
        self.assertEqual(self.state("remote-task-ack-first"), {ASK_A: ASK_A})
        self.assertEqual(self.relay.served[0][0], TYPED_A, "the relay served the batch out of order")

    def test_the_mark_survives_a_restart_and_is_removed_only_on_acceptance(self):
        self.relay.queue.append(voice_ask(ASK_A))
        first = self.bridges.start()
        self.bridges.wait_for(self.task.exists, first, "the relay ask published")
        self.bridges.stop(first)
        self.assertEqual(self.state("remote-task-ack-first"), {ASK_A: ASK_A})
        self.relay.refusing = True
        (self.ws / "results" / f"{ASK_A}.txt").write_text("Your calendar is clear.")
        second = self.bridges.start()
        self.bridges.refused_result_recorded(second, ASK_A)
        self.assertEqual(self.state("remote-task-ack-first"), {ASK_A: ASK_A},
                         "the mark went with a refused result")
        self.bridges.stop(second)
        self.relay.refusing = False
        third = self.bridges.start()
        self.bridges.wait_for(lambda: not self.state("remote-task-ack-first"), third,
                              "the mark retired once the result was accepted")
        self.bridges.stop(third)
        self.assertEqual([r["id"] for r in self.relay.results], [ASK_A])
        # A crash between the archive and the retirement leaves the mark: the next start
        # finds the result delivered and nothing of it live, and retires it.
        (self.ws / "state" / "remote-task-ack-first.json").write_text(json.dumps({ASK_A: ASK_A}))
        fourth = self.bridges.start()
        self.bridges.wait_for(lambda: self.relay.polls and not self.state("remote-task-ack-first"),
                              fourth, "the mark retired at start")
        self.bridges.stop(fourth)
        self.assertEqual([r["id"] for r in self.relay.results], [ASK_A], "the result was sent again")


class BridgeImportIsHermetic(unittest.TestCase):
    def test_the_bridge_import_reads_no_host_config_token_or_vault(self):
        assert_hermetic(self, _IMPORT_READS)


if __name__ == "__main__":
    unittest.main(verbosity=2)
