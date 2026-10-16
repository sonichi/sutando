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
import os
import sys
import tempfile
import time
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
from ag2_sparrow import delivery_core, outbox, remote_gateway_bridge as gw  # noqa: E402

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
    "ok", "gone" (404 not leased), "stale" (404 stale lease generation), "down" (no
    connection), "busy" (503) or "auth" (401); an ack with nothing listed is accepted.
    `states[broker id]` is what `GET /v1/tasks/<id>/status` answers (404 when absent)."""

    def __init__(self):
        self.calls: "list[tuple]" = []
        self.answers: "dict[str, list[str]]" = {}
        self.states: "dict[str, str]" = {}

    def request(self, method, path, payload=None, **_kw):
        self.calls.append((method, path, None if payload is None else json.loads(json.dumps(payload))))
        wire = urllib.parse.unquote(path.split("/")[-2]) if path.startswith("/v1/tasks/") else ""
        if path.endswith("/status"):
            if wire not in self.states:
                raise urllib.error.HTTPError(f"http://relay{path}", 404, "Not Found", {},
                                             io.BytesIO(b'{"error": "unknown task"}'))
            return {"id": wire, "state": self.states[wire]}
        if path.endswith("/ack"):
            answer = (self.answers.get(wire) or ["ok"]).pop(0)
            if answer == "down":
                raise urllib.error.URLError("connection refused")
            if answer in ("busy", "auth"):
                code = 503 if answer == "busy" else 401
                raise urllib.error.HTTPError(f"http://relay{path}", code, answer, {}, io.BytesIO(b"{}"))
            if answer in ("gone", "stale"):
                raise _not_leased(path, stale=answer == "stale")
            self.states[wire] = "acknowledged"
            return {"ok": True, "durable": True}
        return {"agents": []} if path == "/v1/agents" else {"ok": True, "delivery_readable": True}

    def acks(self) -> "list[dict]":
        return [p for _m, path, p in self.calls if path.endswith("/ack")]

    def status_reads(self) -> int:
        return sum(path.endswith("/status") for _m, path, _p in self.calls)


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
            _HELD_UNRESOLVED=set(), _LOG_COUNTS={}, GATEWAY_STATUS_FILE=state / "gateway-status.json",
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
        return sorted(gw._held_dir().glob(f"{tid}.txt.*.tmp"))

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
        self.assertEqual((self.inflight, self.broker.status_reads()), ({ASK_A}, 0))

    def assert_discarded(self, ask: str) -> None:
        self.assert_never_reached_the_core(ask)
        self.assertNotIn(ask, self.marks(), "a task that never ran kept its mark")
        self.assertNotIn(ask, self.inflight)
        self.assertNotIn(ask, self.on_disk_inflight())
        acks = len(self.broker.acks())
        self.assertFalse(gw._advance_held_tasks(self.inflight))
        self.assertEqual(len(self.broker.acks()), acks, "a discarded task was acked again")

    def test_a_stale_refusal_discards_the_staged_task_without_asking_the_broker(self):
        # A newer serve holds the lease, and an acknowledged ask is never served again.
        self.broker.answers[ASK_A] = ["stale"]
        self.broker.states[ASK_A] = "acknowledged"          # that other serve's
        self.assertFalse(self.take(voice_ask(ASK_A)))
        self.assert_discarded(ASK_A)
        self.assertEqual(self.broker.status_reads(), 0)

    def test_a_refused_acknowledgement_discards_the_staged_task(self):
        for n, state in enumerate(("withdrawn", "dead", "result_ready")):
            with self.subTest(state=state):
                ask = f"{ASK_B}{n}"
                self.broker.answers[ask] = ["gone"]
                self.broker.states[ask] = state
                self.assertFalse(self.take(voice_ask(ask)))
                self.assert_discarded(ask)
        self.assertEqual(self.broker.status_reads(), 3)

    def test_an_acknowledged_ask_is_never_discarded_when_its_acceptance_was_not_kept(self):
        real = gw._put_held
        for n, reack in enumerate(("gone", "ok")):
            with self.subTest(reack=reack):
                ask = f"{ASK_C}{n}"
                failing = {"left": 1}

                def put(tid, entry):
                    if isinstance(entry, dict) and entry.get("accepted") and failing["left"]:
                        failing["left"] -= 1
                        return False                   # the acceptance does not reach the disk
                    return real(tid, entry)

                with patch.object(gw, "_put_held", side_effect=put):
                    self.assertFalse(self.take(voice_ask(ask)))
                self.assertEqual(self.broker.states[ask], "acknowledged")
                self.assertFalse((gw.TASKS_DIR / f"{ask}.txt").exists())
                self.assertEqual(len(self.staged(ask)), 1, "an acknowledged task was discarded")
                self.assertIs(self.manifest()[ask]["accepted"], False)
                # The next pass acks again: the broker either answers `duplicate`, or has
                # ended the lease and says, when asked, that the ask is acknowledged.
                self.broker.answers[ask] = [reack]
                self.assertTrue(gw._advance_held_tasks(self.inflight))
                self.assertTrue((gw.TASKS_DIR / f"{ask}.txt").exists())
                self.assertEqual((self.fired.count(ask), self.staged(ask)), (1, []))
                self.assertNotIn(ask, self.manifest())
        self.assertEqual(self.broker.status_reads(), 1)

    def test_an_ack_the_broker_cannot_settle_keeps_the_task_staged(self):
        cases = (("gone", "queued"), ("gone", "served"), ("gone", None),
                 ("busy", "acknowledged"), ("busy", "queued"))
        for n, (answer, state) in enumerate(cases):
            with self.subTest(answer=answer, state=state):
                ask = f"{ASK_A}{n}"
                self.broker.answers[ask] = [answer]
                if state is not None:
                    self.broker.states[ask] = state
                self.assertFalse(self.take(voice_ask(ask)))
                self.assertFalse((gw.TASKS_DIR / f"{ask}.txt").exists())
                self.assertEqual(len(self.staged(ask)), 1)
                self.assertIs(self.manifest()[ask]["accepted"], False)
                self.assertIn(ask, self.marks())
        # Only the ack says whose acknowledgement it is, so a 503 never publishes on the state.
        self.assertEqual(self.fired, [])

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

    def test_a_mark_that_cannot_commit_acks_nothing_until_it_can(self):
        for n, content in enumerate(("{not json", "[]")):
            with self.subTest(content=content):
                ask = f"{ASK_A}{n}"
                gw.ACK_FIRST_FILE.write_text(content)
                self.assertFalse(self.take(voice_ask(ask)))
                self.assertEqual(self.broker.acks(), [])
                self.assertFalse((gw.TASKS_DIR / f"{ask}.txt").exists())
                self.assertEqual(gw.ACK_FIRST_FILE.read_text(), content, "unreadable marks were overwritten")
                self.assertNotIn(ask, self.on_disk_inflight())
                self.assertIn(ask, self.manifest(), "the staged task lost its recovery record")
                gw.ACK_FIRST_FILE.unlink()                 # readable again: marked, acked, published
                self.assertTrue(gw._advance_held_tasks(self.inflight))
                self.assertEqual((self.marks(), self.fired[-1]), ({ask: ask}, ask))
                self.broker.calls.clear()

    def test_a_manifest_that_cannot_commit_stages_nothing_and_acks_nothing(self):
        gw.ACK_FIRST_MANIFEST_FILE.write_text("{not json")
        self.assertFalse(self.take(voice_ask(ASK_A)))
        self.assertEqual(self.broker.acks(), [])
        self.assertFalse((gw.TASKS_DIR / f"{ASK_A}.txt").exists())
        self.assertEqual((self.staged(ASK_A), self.marks()), ([], {}))
        self.assertEqual(gw.ACK_FIRST_MANIFEST_FILE.read_text(), "{not json")

    def test_the_staged_file_is_on_disk_in_its_directory_before_its_entry(self):
        synced: "list[tuple]" = []
        real = gw._fsync_in_place

        def fsync(tid, *targets):
            synced.append((targets, ASK_A in self.manifest()))
            return real(tid, *targets)

        self.broker.answers[ASK_A] = ["down"]
        with patch.object(gw, "_fsync_in_place", side_effect=fsync):
            self.take(voice_ask(ASK_A))
        self.assertEqual(synced, [((gw._held_dir(), gw.TASKS_DIR), False)])
        with patch.object(gw, "_fsync_in_place", return_value=False):
            self.assertFalse(self.take(voice_ask(ASK_B)))
        self.assertEqual((self.staged(ASK_B), list(self.manifest())), ([], [ASK_A]))

    def test_the_rename_is_on_disk_in_both_directories_before_the_entry_goes(self):
        events: "list" = []
        fsync, publish, put = gw._fsync_in_place, gw._publish_staged, gw._put_held
        fail_after_publish = {"left": 1}

        def fsync_at(tid, *targets):
            published = "publish" in events
            events.append(("fsync", targets))
            if published and fail_after_publish["left"]:
                fail_after_publish["left"] -= 1
                return False                         # the source directory did not commit
            return fsync(tid, *targets)

        def publish_at(tmp, path):
            if Path(path).parent == gw.TASKS_DIR:        # not a state file's durable write
                events.append("publish")
            return publish(tmp, path)

        def put_at(tid, entry):
            events.append(("put", None if entry is None else "entry"))
            return put(tid, entry)

        with patch.multiple(gw, _fsync_in_place=fsync_at, _publish_staged=publish_at, _put_held=put_at):
            self.assertTrue(self.take(voice_ask(ASK_A)))
            after = events[events.index("publish") + 1:]
            self.assertEqual(after, [("fsync", (gw._held_dir(), gw.TASKS_DIR))],
                             "the entry went before both directories held the rename")
            self.assertIs(self.manifest()[ASK_A]["accepted"], True)
            self.assertFalse(gw._advance_held_tasks(self.inflight))   # the next pass: fsync, then clear
        after = events[events.index("publish") + 2:]
        self.assertEqual(after[-2:], [("fsync", (gw._held_dir(), gw.TASKS_DIR)), ("put", None)])
        self.assertEqual((self.manifest(), self.fired, len(self.broker.acks())), ({}, [ASK_A], 1))

    def test_a_staged_copy_beside_its_published_copy_is_dropped_not_published_again(self):
        # A power loss can keep the publish's new name and bring back the staged one.
        (gw.TASKS_DIR / "archive").mkdir()
        where = {"queued": lambda t: t,
                 "claimed": lambda t: t.rename(t.with_name(f"{t.stem}.claimed-core-1")),
                 "archived": lambda t: t.rename(gw.TASKS_DIR / "archive" / t.name)}
        for n, (name, move) in enumerate(where.items()):
            with self.subTest(name):
                ask = f"{ASK_C}{n}"
                self.broker.answers[ask] = ["down"]
                self.take(voice_ask(ask))
                [staged] = self.staged(ask)
                task = gw.TASKS_DIR / f"{ask}.txt"
                task.write_text(staged.read_text())
                gw.ACK_FIRST_MANIFEST_FILE.write_text(json.dumps(
                    {ask: {**self.manifest()[ask], "accepted": True}}))
                copy = move(task)
                before = copy.stat()
                self.assertFalse(gw._advance_held_tasks(self.inflight))
                self.assertEqual((self.staged(ask), self.manifest(), self.fired), ([], {}, []))
                self.assertEqual(copy.stat().st_ino, before.st_ino, "the task was published again")
                self.assertEqual(task.exists(), name == "queued")
                self.assertEqual(len(self.broker.acks()), n + 1, "an accepted task was acked again")

    def test_a_staged_task_is_invisible_to_the_queue_and_to_its_lookups(self):
        self.broker.answers[ASK_A] = ["down"]
        self.take(voice_ask(ASK_A))
        self.assertEqual(len(self.staged(ASK_A)), 1)
        self.assertIsNone(gw.find_task_file(gw.TASKS_DIR, ASK_A))
        self.assertEqual(sorted(p.name for p in gw.TASKS_DIR.iterdir() if p.is_file()), [])
        self.assertFalse(gw._task_pending(ASK_A))

    def test_at_start_staged_files_no_entry_names_are_removed(self):
        self.broker.answers[ASK_A] = ["down"]
        self.take(voice_ask(ASK_A))
        [named] = self.staged(ASK_A)
        orphans = [gw._held_dir() / f"{ASK_B}.txt.1.{'0' * 32}.tmp",
                   gw._held_dir() / f"{ASK_A}.txt.2.{'1' * 32}.tmp"]
        for orphan in orphans:
            orphan.write_text("id: orphan\n")
        kept = gw.ACK_FIRST_MANIFEST_FILE.read_text()
        gw.ACK_FIRST_MANIFEST_FILE.write_text("{not json")
        gw._sweep_held_dir()                             # the manifest cannot be read: nothing goes
        self.assertTrue(all(f.exists() for f in orphans + [named]))
        gw.ACK_FIRST_MANIFEST_FILE.write_text(kept)
        gw._sweep_held_dir()
        self.assertEqual((named.exists(), [o.exists() for o in orphans]), (True, [False, False]))

    def test_a_discard_cut_short_is_finished_by_the_next_pass(self):
        self.broker.answers[ASK_A] = ["stale"]
        with patch.object(gw, "_forget_ack_first", return_value=False):
            self.assertFalse(self.take(voice_ask(ASK_A)))
        self.assertIs(self.manifest()[ASK_A]["refused"], True)
        self.assertEqual((self.staged(ASK_A), self.marks()), ([], {ASK_A: ASK_A}))
        self.assertFalse(gw._advance_held_tasks(self.inflight))
        self.assertEqual(len(self.broker.acks()), 1, "a refused task was acked again")
        self.assert_never_reached_the_core(ASK_A)
        self.assertEqual((self.marks(), self.on_disk_inflight()), ({}, set()))

    def test_a_vanished_staged_file_without_a_published_copy_is_kept_and_reported(self):
        for name, other in (("no copy", None), ("a task file that does not match", b"id: other\n"),
                            ("a task file that cannot be read", b"\xff\xfe not utf-8")):
            with self.subTest(name):
                self.lines.clear()
                gw._HELD_REPORTED.clear()
                self.broker.answers[ASK_A] = ["down"]
                self.take(voice_ask(ASK_A))
                entry = self.manifest()[ASK_A]
                self.staged(ASK_A)[0].unlink()
                if other is not None:
                    (gw.TASKS_DIR / f"{ASK_A}.txt").write_bytes(other)
                for _ in range(2):
                    self.assertFalse(gw._advance_held_tasks(self.inflight))
                    gw._emit_gateway_status(True)
                    self.assertEqual(self.reported(), [ASK_A])
                self.assertEqual(self.manifest(), {ASK_A: entry}, "the entry was guessed away")
                self.assertEqual(len(self.broker.acks()), 1, "a task that cannot be found was acked")
                self.assertEqual(sum("staged file gone" in line for line in self.lines), 1)
                self.assertEqual(self.fired, [])
                gw.ACK_FIRST_MANIFEST_FILE.unlink()          # resolved by a person
                (gw.TASKS_DIR / f"{ASK_A}.txt").unlink(missing_ok=True)
                gw._advance_held_tasks(self.inflight)
                gw._emit_gateway_status(True)
                self.assertEqual(self.reported(), None)
                self.broker.calls.clear()

    def reported(self):
        return json.loads(gw.GATEWAY_STATUS_FILE.read_text()).get("ack_first_unresolved")

    def test_a_malformed_manifest_entry_is_left_alone_and_reported(self):
        victim = gw.INFLIGHT_FILE
        victim.write_text("[]")
        entry = {"broker_id": ASK_A, "lease_id": None, "staged": "../state/remote-task-inflight.json",
                 "dest": f"{ASK_A}.txt", "sha256": "", "accepted": True}
        gw.ACK_FIRST_MANIFEST_FILE.write_text(json.dumps({ASK_A: entry}))
        self.assertFalse(gw._advance_held_tasks(self.inflight))
        self.assertEqual((victim.read_text(), self.fired, self.broker.acks()), ("[]", [], []))
        self.assertFalse((gw.TASKS_DIR / f"{ASK_A}.txt").exists())
        self.assertTrue(any("malformed" in line for line in self.lines))
        gw._emit_gateway_status(True)
        self.assertEqual(self.reported(), [ASK_A])

    def test_an_archived_task_or_its_result_proves_the_publish_after_a_crash(self):
        # The publish happened and the entry's removal did not: what the core did since proves it.
        moves = {"archived task": lambda t: t.rename(gw.TASKS_DIR / "archive" / t.name),
                 "live result": lambda t: (t.unlink(), (gw.RESULTS_DIR / t.name).write_text("Clear.")),
                 "archived result": lambda t: (t.unlink(), (gw.ARCHIVE_RESULTS_DIR
                                                           / f"{t.stem}-1785976425.txt").write_text("Clear."))}
        (gw.TASKS_DIR / "archive").mkdir()
        gw.ARCHIVE_RESULTS_DIR.mkdir(parents=True)
        for n, (name, move) in enumerate(moves.items()):
            with self.subTest(name):
                ask = f"{ASK_B}{n}"
                self.assertTrue(self.take(voice_ask(ask)))
                task = gw.TASKS_DIR / f"{ask}.txt"
                entry = {"broker_id": ask, "lease_id": f"gen-{ask}", "staged": f"{ask}.txt.1.{'0' * 32}.tmp",
                         "dest": task.name, "sha256": gw._held_digest(task.read_text()), "accepted": True}
                gw.ACK_FIRST_MANIFEST_FILE.write_text(json.dumps({ask: entry}))
                move(task)
                acks, fired = len(self.broker.acks()), list(self.fired)
                self.assertFalse(gw._advance_held_tasks(self.inflight))
                self.assertEqual(self.manifest(), {})
                self.assertFalse(task.exists(), "the task was published again")
                self.assertEqual((len(self.broker.acks()), self.fired), (acks, fired))

    def test_an_accepted_task_is_back_in_flight_before_its_publish(self):
        # An older bridge, run in between, drops an id with no task file from its in-flight set.
        self.broker.answers[ASK_A] = ["down"]
        self.take(voice_ask(ASK_A))
        gw.ACK_FIRST_MANIFEST_FILE.write_text(json.dumps(
            {ASK_A: {**self.manifest()[ASK_A], "accepted": True}}))
        gw._save_inflight(set())
        with patch.object(gw, "_save_inflight", return_value=False):
            self.assertFalse(gw._advance_held_tasks(set()))    # not in flight: not published
        self.assertFalse((gw.TASKS_DIR / f"{ASK_A}.txt").exists())
        self.assertTrue(gw._advance_held_tasks(set()))
        self.assertEqual((self.on_disk_inflight(), len(self.broker.acks())), ({ASK_A}, 1))

    def test_an_acceptance_the_unreadable_manifest_cannot_hold_keeps_the_task_staged(self):
        self.broker.answers[ASK_A] = ["down"]
        self.take(voice_ask(ASK_A))
        entry, kept = self.manifest()[ASK_A], gw.ACK_FIRST_MANIFEST_FILE.read_text()
        gw.ACK_FIRST_MANIFEST_FILE.write_text("{not json")
        self.assertFalse(gw._advance_held(ASK_A, entry, self.inflight))   # acked, not recorded
        self.assertEqual(self.broker.states[ASK_A], "acknowledged")
        self.assertEqual((len(self.staged(ASK_A)), self.fired), (1, []))
        self.assertEqual(gw.ACK_FIRST_MANIFEST_FILE.read_text(), "{not json")
        gw.ACK_FIRST_MANIFEST_FILE.write_text(kept)              # readable again: acked, published
        self.assertTrue(gw._advance_held_tasks(self.inflight))
        self.assertEqual((self.fired, len(self.broker.acks())), ([ASK_A], 3))

    def test_removing_an_entry_that_is_already_gone_rewrites_nothing(self):
        # A pass after a partial failure removes the same entry again.
        self.broker.answers[ASK_B] = ["down"]
        self.take(voice_ask(ASK_B))
        before = (gw.ACK_FIRST_MANIFEST_FILE.read_text(), gw.ACK_FIRST_MANIFEST_FILE.stat().st_mtime_ns)
        self.assertTrue(gw._put_held(ASK_A, None))
        self.assertEqual((gw.ACK_FIRST_MANIFEST_FILE.read_text(),
                          gw.ACK_FIRST_MANIFEST_FILE.stat().st_mtime_ns), before)

    def test_a_refusal_that_cannot_be_recorded_discards_nothing_yet(self):
        self.broker.answers[ASK_A] = ["stale", "stale"]
        real = gw._put_held
        with patch.object(gw, "_put_held", side_effect=lambda tid, entry: False
                          if isinstance(entry, dict) and entry.get("refused") else real(tid, entry)):
            self.assertFalse(self.take(voice_ask(ASK_A)))
        self.assertEqual((len(self.staged(ASK_A)), self.marks()), (1, {ASK_A: ASK_A}))
        self.assertNotIn("refused", self.manifest()[ASK_A])
        self.assertIn(ASK_A, self.on_disk_inflight())
        self.assertFalse(gw._advance_held_tasks(self.inflight))      # acked again, refused again
        self.assert_never_reached_the_core(ASK_A)
        self.assertEqual((self.marks(), len(self.broker.acks())), ({}, 2))

    def test_a_discard_whose_in_flight_save_fails_is_finished_by_the_next_pass(self):
        self.broker.answers[ASK_A] = ["stale"]
        real, failing = gw._save_inflight, {"left": 1}

        def save(inflight):
            if ASK_A not in inflight and failing["left"]:         # the discard's save
                failing["left"] -= 1
                return False
            return real(inflight)

        with patch.object(gw, "_save_inflight", side_effect=save):
            self.assertFalse(self.take(voice_ask(ASK_A)))
        self.assertIs(self.manifest()[ASK_A]["refused"], True)
        self.assertEqual((self.staged(ASK_A), self.marks()), ([], {}))
        self.assertIn(ASK_A, self.on_disk_inflight())
        self.assertFalse(gw._advance_held_tasks(self.inflight))
        self.assertEqual((self.manifest(), self.on_disk_inflight(), len(self.broker.acks())),
                         ({}, set(), 1))

    def test_an_in_flight_set_that_cannot_be_saved_holds_the_ack(self):
        with patch.object(gw, "_save_inflight", return_value=False):
            self.assertFalse(self.take(voice_ask(ASK_A)))
        self.assertEqual((self.broker.acks(), len(self.staged(ASK_A))), ([], 1))
        self.assertNotIn(ASK_A, self.inflight)
        self.assertTrue(gw._advance_held_tasks(self.inflight))
        self.assertEqual((self.fired, self.on_disk_inflight()), ([ASK_A], {ASK_A}))

    def test_a_publish_that_fails_is_retried_without_a_new_ack(self):
        real, failing = gw._publish_staged, {"left": 1}

        def publish(tmp, path):
            if Path(path).parent == gw.TASKS_DIR and failing["left"]:
                failing["left"] -= 1
                return False
            return real(tmp, path)

        with patch.object(gw, "_publish_staged", side_effect=publish):
            self.assertFalse(self.take(voice_ask(ASK_A)))
        self.assertIs(self.manifest()[ASK_A]["accepted"], True)
        self.assertEqual((len(self.staged(ASK_A)), self.fired), (1, []))
        self.assertTrue(gw._advance_held_tasks(self.inflight))
        self.assertEqual((self.fired, len(self.broker.acks()), self.manifest()), ([ASK_A], 1, {}))

    def test_a_task_staged_before_a_restart_gets_only_its_wake_on_the_publish(self):
        self.broker.answers[ASK_A] = ["down"]
        self.take(voice_ask(ASK_A))
        gw._HELD_TASKS.clear()                          # the served task lived only in memory
        self.assertTrue(gw._advance_held_tasks(self.inflight))
        self.assertEqual(self.fired, [ASK_A])
        self.assertIn(f"queued {ASK_A} (staged and acknowledged before a restart)", self.lines)
        self.assertFalse(gw.OWNER_ACTIVITY_FILE.exists(), "a presence hint was replayed")

    def test_a_relay_ask_served_again_once_queued_is_acked_not_staged_again(self):
        self.assertTrue(self.take(voice_ask(ASK_A)))
        self.assertFalse(self.take(voice_ask(ASK_A, lease="gen-2")))
        self.assertEqual(self.broker.acks()[-1], {"id": ASK_A, "durable": True, "lease_id": "gen-2"})
        self.assertEqual((self.fired, self.staged(ASK_A), self.manifest()), ([ASK_A], [], {}))
        self.assertEqual(gw._load_pending_acks(), {}, "the confirmed ack was not retired")
        # While the manifest cannot be read, not even that ack is sent.
        gw.ACK_FIRST_MANIFEST_FILE.write_text("{not json")
        self.assertFalse(self.take(voice_ask(ASK_A, lease="gen-3")))
        self.assertEqual(len(self.broker.acks()), 2)

    def age(self, tid: str, seconds: float) -> None:
        """Make the staged file look `seconds` old: its mtime is when it was staged."""
        [staged] = self.staged(tid)
        then = time.time() - seconds
        os.utime(staged, (then, then))

    def test_a_task_still_staged_past_the_threshold_is_reported_and_still_retried(self):
        limit = gw.ACK_FIRST_UNRESOLVED_AFTER_S
        self.broker.answers[ASK_A] = ["busy", "busy", "busy", "ok"]
        self.take(voice_ask(ASK_A))
        self.age(ASK_A, limit - 30)
        gw._advance_held_tasks(self.inflight)
        gw._emit_gateway_status(True)
        self.assertIsNone(self.reported(), "reported before the threshold")
        self.age(ASK_A, limit + 1)
        gw._advance_held_tasks(self.inflight)
        gw._emit_gateway_status(True)
        self.assertEqual(self.reported(), [ASK_A])
        self.assertEqual(len(self.broker.acks()), 3, "a reported task stopped being retried")
        self.assertEqual(sum("still staged" in line for line in self.lines), 1)
        self.assertTrue(gw._advance_held_tasks(self.inflight))      # accepted at last: published
        gw._emit_gateway_status(True)
        self.assertIsNone(self.reported(), "a resolved task stayed reported")
        self.assertEqual(self.fired, [ASK_A])

    def test_a_task_whose_mark_cannot_be_written_is_reported_past_the_threshold_too(self):
        # Not only an ack that keeps failing: any step that keeps it staged.
        self.broker.answers[ASK_A] = ["down"]
        self.take(voice_ask(ASK_A))
        gw.ACK_FIRST_FILE.write_text("{not json")
        self.age(ASK_A, gw.ACK_FIRST_UNRESOLVED_AFTER_S + 1)
        self.assertFalse(gw._advance_held_tasks(self.inflight))
        gw._emit_gateway_status(True)
        self.assertEqual((self.reported(), len(self.broker.acks())), ([ASK_A], 1))
        self.assertFalse(gw._held_overdue(gw._held_dir() / "gone.txt.1.tmp"), "a missing file is not overdue")

    def test_a_retrying_ack_logs_on_a_backoff(self):
        self.broker.answers[ASK_A] = ["busy"] * 9
        self.take(voice_ask(ASK_A))
        for _ in range(8):
            gw._advance_held_tasks(self.inflight)
        retries = [line for line in self.lines if "kept staged, retrying" in line]
        self.assertEqual(len(self.broker.acks()), 9)
        self.assertEqual([line.rsplit("retrying", 1)[1] for line in retries],
                         ["", " (seen 2 times)", " (seen 4 times)", " (seen 8 times)"])

    def counted(self, tid: str) -> "set[str]":
        return {k[1] for k in gw._LOG_COUNTS if k[0] == tid}

    def test_a_retired_task_leaves_no_backoff_counts_and_its_next_serve_starts_over(self):
        gw._LOG_COUNTS[("item-of-another-task", "pending")] = 3
        # Published after failing acks and an overdue report: every kind of its count goes.
        self.broker.answers[ASK_A] = ["busy", "busy", "ok"]
        self.take(voice_ask(ASK_A))
        self.age(ASK_A, gw.ACK_FIRST_UNRESOLVED_AFTER_S + 1)
        gw._advance_held_tasks(self.inflight)
        self.assertEqual(self.counted(ASK_A), {"ack-retry", "ack-status", "overdue"})
        self.assertTrue(gw._advance_held_tasks(self.inflight))
        self.assertEqual(self.counted(ASK_A), set())
        # Discarded after a failing ack: its count goes too.
        self.broker.answers[ASK_B] = ["busy", "stale"]
        self.take(voice_ask(ASK_B))
        self.assertEqual(self.counted(ASK_B), {"ack-retry", "ack-status"})
        gw._advance_held_tasks(self.inflight)
        self.assertNotIn(ASK_B, self.manifest())
        self.assertEqual(self.counted(ASK_B), set())
        self.assertEqual(gw._LOG_COUNTS, {("item-of-another-task", "pending"): 3})
        # Served again, the same task's first failing ack logs as a first one.
        self.lines.clear()
        self.broker.answers[ASK_B] = ["busy"]
        self.take(voice_ask(ASK_B, lease="gen-2"))
        [retry] = [line for line in self.lines if "kept staged, retrying" in line]
        self.assertNotIn("(seen", retry)

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
        self.assertFalse(gw._held_dir().exists())
        self.assertEqual(self.broker.acks(), [], "written tasks are acked after the batch, as before")


class StagedPolicyGate(_Bridge):
    """Before a held task is published, the tier its staged file froze must be proven and still
    granted; anything else withholds it, reports it and checks it again on the next pass."""

    def staged_once(self) -> Path:
        self.broker.answers[ASK_A] = ["down"]
        self.take(voice_ask(ASK_A))
        [staged] = self.staged(ASK_A)
        self.assertEqual(staged.read_text().count("\naccess_tier: owner\n"), 1)
        return staged

    def unreadable(self, staged: Path) -> None:
        if os.geteuid() == 0:
            self.skipTest("root reads a mode-000 file")
        staged.chmod(0)
        self.addCleanup(lambda: staged.exists() and staged.chmod(0o600))

    def withheld(self) -> None:
        """Nothing published; the staged file and the accepted entry kept; the ask reported."""
        self.assertFalse((gw.TASKS_DIR / f"{ASK_A}.txt").exists(), "an unproven envelope was published")
        self.assertEqual((len(self.staged(ASK_A)), self.fired), (1, []))
        self.assertIs(self.manifest()[ASK_A]["accepted"], True)
        gw._emit_gateway_status(True)
        self.assertEqual(json.loads(gw.GATEWAY_STATUS_FILE.read_text()).get("ack_first_unresolved"), [ASK_A])

    def test_a_staged_file_that_cannot_be_read_is_withheld_until_it_can(self):
        staged = self.staged_once()
        self.unreadable(staged)
        for _ in range(2):                              # checked again on every pass
            self.assertFalse(gw._advance_held_tasks(self.inflight))
            self.withheld()
        self.assertEqual(sum("cannot be read" in line for line in self.lines), 2)
        staged.chmod(0o600)                             # readable again, the cap unchanged
        self.assertTrue(gw._advance_held_tasks(self.inflight))
        self.assertEqual((self.fired, self.manifest(), len(self.broker.acks())), ([ASK_A], {}, 2))
        gw._emit_gateway_status(True)
        self.assertIsNone(json.loads(gw.GATEWAY_STATUS_FILE.read_text()).get("ack_first_unresolved"))

    def test_a_staged_file_read_again_under_a_lowered_cap_stays_withheld(self):
        staged = self.staged_once()
        self.unreadable(staged)
        self.assertFalse(gw._advance_held_tasks(self.inflight))
        self.withheld()
        staged.chmod(0o600)
        with patch.object(gw, "LOCAL_TIER", "guest"):
            self.assertFalse(gw._advance_held_tasks(self.inflight))
        self.withheld()
        self.assertTrue(any("staged as owner but local access is now guest" in line for line in self.lines))

    def test_a_staged_file_naming_no_known_tier_is_withheld(self):
        staged = self.staged_once()
        original = staged.read_text()
        cases = {"no access_tier line": original.replace("\naccess_tier: owner\n", "\n"),
                 "an unknown tier": original.replace("access_tier: owner", "access_tier: superuser")}
        for name, text in cases.items():
            with self.subTest(name):
                staged.write_text(text)
                self.assertFalse(gw._advance_held_tasks(self.inflight))
                self.withheld()
        staged.write_text(original)
        with self.subTest("a parse that raises"), patch.object(
                gw.local_task_protocol, "parse_task_headers_trusted", side_effect=ValueError("bad")):
            self.assertFalse(gw._advance_held_tasks(self.inflight))
            self.withheld()
        self.assertTrue(gw._advance_held_tasks(self.inflight))         # proven again: published once
        self.assertEqual((self.fired, self.manifest()), ([ASK_A], {}))


class TheMark(_Bridge):
    """The `ack_first` mark: broker id -> the local id, in a file of its own."""

    def deliver(self, broker_id: str) -> None:
        """The outbox shows this result delivered, as a pass that crashed before its archive left it."""
        backend = gw._delivery_core().backend
        backend.publish(broker_id, json.dumps({"id": broker_id, "body": "Clear."}).encode())
        backend.complete(backend.claim(broker_id, "a-crashed-process"), delivery_core.DeliveryOutcome.CONFIRMED)

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

    def test_at_start_a_malformed_mark_is_left_as_it_is(self):
        self.assertTrue(self.take(voice_ask(ASK_A)))
        self.deliver(ASK_A)
        gw.ACK_FIRST_FILE.write_text(json.dumps({ASK_A: ASK_A, "../escape": "x", ASK_B: 7}))
        gw._retire_delivered_marks()
        self.assertEqual(self.marks(), {"../escape": "x", ASK_B: 7})

    def test_unreadable_marks_close_a_fold_as_before_when_its_task_file_cannot_be_read(self):
        # Only a task file that reads `source: matrixrtc-voice` defers its fold.
        gw.ACK_FIRST_FILE.write_text("{not json")
        self.assertEqual(gw._fold_target(TYPED_A, TYPED_A, ASK_B), "")       # no task file at all
        (gw.TASKS_DIR / f"{TYPED_A}.txt").mkdir()                           # one that cannot be read
        self.assertEqual(gw._fold_target(TYPED_A, TYPED_A, ASK_B), "")
        self.assertFalse(any("deferred" in line for line in self.lines))

    def test_at_start_an_unreadable_alias_ledger_keeps_every_mark(self):
        # A re-ask's live result cannot be ruled out, so nothing is retired early.
        self.assertTrue(self.take(voice_ask(ASK_A)))
        self.deliver(ASK_A)
        gw.DEDUP_ALIAS_FILE.write_text("{not json")
        gw._retire_delivered_marks()
        self.assertEqual(self.marks(), {ASK_A: ASK_A})
        gw.DEDUP_ALIAS_FILE.unlink()
        gw._retire_delivered_marks()
        self.assertEqual(self.marks(), {})


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
        return sorted((self.ws / "tasks" / ".ack-first").glob(f"{ASK_A}.txt.*.tmp"))

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

    def test_a_crash_between_the_brokers_acceptance_and_its_record_runs_once_after_the_lease_ended(self):
        self.relay.queue.append(voice_ask(ASK_A))
        self.bridges.crashed(self.bridges.start(crash="after-ack"), "after the accepted ack")
        self.assertIs(self.entry()["accepted"], False)
        self.relay.lease_ended.add(ASK_A)                  # its re-ack now answers 404 not leased
        bridge = self.bridges.start()
        self.bridges.wait_for(lambda: self.bridges.fired(), bridge, "the publish after the restart")
        self.bridges.polls_after(bridge)
        self.bridges.stop(bridge)
        self.assertEqual(self.bridges.fired(), [f"{ASK_A}.txt"], "the task did not run exactly once")
        self.assertEqual((self.relay.acks, self.relay.status_reads), ([ASK_A, ASK_A], [ASK_A]))
        self.assertEqual((self.staged(), self.entry()), ([], {}))

    def test_a_cap_lowered_while_an_ask_was_staged_keeps_it_held_instead_of_replaying_it(self):
        """A staged ask freezes its whole policy envelope — the `access_tier` line, that
        tier's in-band guardrail, the owner-only prelude — and a resume publishes that
        text verbatim. So an operator who lowers the gateway's cap between the stage and
        the publish must not be overridden by replay: the ask had never been acked or
        exposed to the core, and republishing it would grant exactly the capability that
        was just withdrawn."""
        self.relay.queue.append(voice_ask(ASK_A))              # owner-attested
        self.bridges.crashed(self.bridges.start(crash="before-publish"), "the publish")
        self.assertEqual(len(self.staged()), 1, "nothing was staged to replay")
        self.bridges.env["REMOTE_TASK_TIER"] = "guest"         # the operator restricts it
        bridge = self.bridges.start()
        self.bridges.wait_logged(bridge, "kept staged, not published")
        self.bridges.polls_after(bridge)
        self.bridges.stop(bridge)
        self.assertEqual(self.bridges.fired(), [],
                         "the owner-tier envelope was published under a guest cap")
        self.assertFalse(self.task.exists(), "the frozen owner envelope reached the core")
        self.assertEqual(len(self.staged()), 1, "the ask was dropped instead of kept held")
        self.assertIn(ASK_A, self.state("gateway-status").get("ack_first_unresolved", [None])[0]
                      or "", "a withheld ask was not reported in the gateway status")

    def test_an_unchanged_cap_still_publishes_the_staged_ask(self):
        """The control for the test above: the withholding must key on the cap actually
        dropping, not merely on a restart having happened — otherwise every resume would
        stall and the stall would look like the fix working."""
        self.relay.queue.append(voice_ask(ASK_A))
        self.bridges.crashed(self.bridges.start(crash="before-publish"), "the publish")
        bridge = self.bridges.start()                          # same cap, no env change
        self.bridges.wait_for(lambda: self.bridges.fired(), bridge, "the publish after restart")
        self.bridges.polls_after(bridge)
        self.bridges.stop(bridge)
        self.assertEqual(self.bridges.fired(), [f"{ASK_A}.txt"], "the ask did not publish once")
        self.assertEqual((self.staged(), self.entry()), ([], {}))

    def unreadable_after_a_crash(self) -> Path:
        if os.geteuid() == 0:
            self.skipTest("root reads a mode-000 file")
        self.relay.queue.append(voice_ask(ASK_A))              # owner-attested
        self.bridges.crashed(self.bridges.start(crash="before-publish"), "the publish")
        [staged] = self.staged()
        staged.chmod(0)
        self.addCleanup(lambda: staged.exists() and staged.chmod(0o600))
        return staged

    def test_an_unreadable_staged_file_restarted_under_a_lower_cap_is_never_published(self):
        staged = self.unreadable_after_a_crash()
        self.bridges.env["REMOTE_TASK_TIER"] = "guest"
        bridge = self.bridges.start()
        self.bridges.wait_logged(bridge, "cannot be read")
        self.bridges.polls_after(bridge)
        self.assertFalse(self.task.exists(), "an envelope nobody could check reached the core")
        self.assertEqual((self.staged(), self.bridges.fired()), ([staged], []))
        self.assertEqual(self.state("gateway-status").get("ack_first_unresolved"), [ASK_A])
        staged.chmod(0o600)                                    # readable again: the drop is seen
        self.bridges.wait_logged(bridge, "staged as owner but local access is now guest")
        self.bridges.polls_after(bridge)
        self.bridges.stop(bridge)
        self.assertFalse(self.task.exists(), "the frozen owner envelope reached the core")
        self.assertEqual((self.staged(), self.bridges.fired()), ([staged], []))

    def test_an_unreadable_staged_file_under_the_same_cap_publishes_once_it_reads(self):
        staged = self.unreadable_after_a_crash()
        bridge = self.bridges.start()
        self.bridges.wait_logged(bridge, "cannot be read")
        self.bridges.polls_after(bridge)
        self.assertFalse(self.task.exists())
        staged.chmod(0o600)
        self.bridges.wait_for(lambda: self.bridges.fired(), bridge, "the publish once it reads")
        self.bridges.polls_after(bridge)
        self.bridges.stop(bridge)
        self.assertEqual(self.bridges.fired(), [f"{ASK_A}.txt"], "the ask did not publish exactly once")
        self.assertEqual((self.staged(), self.entry(), self.relay.acks), ([], {}, [ASK_A]))

    def test_a_crash_after_the_staged_file_and_before_its_entry_leaves_nothing_behind(self):
        self.relay.queue.append(voice_ask(ASK_A))
        self.bridges.crashed(self.bridges.start(crash="before-entry"), "the entry")
        self.assertEqual((len(self.staged()), self.entry(), self.relay.acks), (1, {}, []))
        self.relay.queue.append(voice_ask(ASK_A, lease="gen-2"))     # unacked, so served again
        bridge = self.bridges.start()
        self.bridges.wait_for(lambda: self.bridges.fired(), bridge, "the new serve published")
        self.bridges.polls_after(bridge)
        self.bridges.stop(bridge)
        self.assertIn("no ack_first manifest entry names it", self.bridges.log())
        self.assertEqual((self.staged(), self.bridges.fired()), ([], [f"{ASK_A}.txt"]))
        self.assertEqual([b.get("lease_id") for b in self.relay.ack_bodies], ["gen-2"])

    def test_a_crash_after_the_entry_and_before_the_mark_runs_the_task_once(self):
        self.relay.queue.append(voice_ask(ASK_A))
        self.bridges.crashed(self.bridges.start(crash="before-mark"), "the mark")
        self.assertEqual((len(self.staged()), self.state("remote-task-ack-first"), self.relay.acks),
                         (1, {}, []))
        bridge = self.bridges.start()
        self.bridges.wait_for(lambda: self.bridges.fired(), bridge, "the publish after the restart")
        self.bridges.polls_after(bridge)
        self.bridges.stop(bridge)
        self.assertEqual(self.bridges.fired(), [f"{ASK_A}.txt"])
        self.assertEqual((self.state("remote-task-ack-first"), self.relay.acks), ({ASK_A: ASK_A}, [ASK_A]))

    def test_a_crash_inside_a_discard_is_finished_after_the_restart(self):
        self.relay.withdrawn.add(ASK_A)
        self.relay.queue.append(voice_ask(ASK_A))
        self.bridges.crashed(self.bridges.start(crash="after-refusal-recorded"), "the recorded refusal")
        self.assertIs(self.entry().get("refused"), True)
        self.assertEqual((len(self.staged()), self.state("remote-task-ack-first")), (1, {ASK_A: ASK_A}))
        bridge = self.bridges.start()
        self.bridges.wait_for(lambda: not self.entry(), bridge, "the discard finished")
        self.bridges.polls_after(bridge)
        self.bridges.stop(bridge)
        self.assertEqual(self.relay.acks, [ASK_A], "a refused task was acked again")
        self.assertFalse(self.task.exists())
        self.assertEqual((self.staged(), self.bridges.fired()), ([], []))
        self.assertNotIn(ASK_A, self.state("remote-task-ack-first"))
        self.assertNotIn(ASK_A, self.state("remote-task-inflight") or [])

    def crash_between_the_publish_and_the_entrys_removal(self, what_the_core_did,
                                                         resurrect: bool = False,
                                                         settled=lambda: True) -> None:
        """`what_the_core_did` moves the published task as the core would, and returns where
        it is now (None once only its result is left). `resurrect` also brings the staged
        name back, as a power loss before the source directory's fsync can."""
        self.relay.queue.append(voice_ask(ASK_A))
        self.bridges.crashed(self.bridges.start(crash="after-publish"), "after the publish")
        self.assertTrue(self.task.exists())
        self.assertEqual((self.staged(), self.entry().get("accepted")), ([], True))
        if resurrect:
            (self.ws / "tasks" / ".ack-first" / self.entry()["staged"]).write_text(self.task.read_text())
        copy = what_the_core_did(self.task)
        before = copy.stat() if copy else None
        bridge = self.bridges.start()
        self.bridges.wait_for(lambda: not self.entry() and settled(), bridge, "the entry cleared")
        self.bridges.polls_after(bridge)
        self.bridges.stop(bridge)
        if copy is not None:
            after = copy.stat()
            self.assertEqual((after.st_ino, after.st_mtime_ns), (before.st_ino, before.st_mtime_ns),
                             "the task was published again")
        self.assertEqual(self.task.exists(), copy == self.task, "a task the core took was queued again")
        self.assertEqual(self.relay.acks, [ASK_A])
        # The first process died before its wake, and a restart never re-fires one.
        self.assertEqual((self.staged(), self.bridges.fired()), ([], []))

    def test_a_crash_after_the_publish_and_before_the_entry_is_removed_runs_the_task_once(self):
        self.crash_between_the_publish_and_the_entrys_removal(lambda task: task)

    def test_the_same_crash_with_the_task_claimed_by_the_core_runs_it_once(self):
        self.crash_between_the_publish_and_the_entrys_removal(
            lambda task: task.rename(task.with_name(f"{ASK_A}.claimed-core-1")))

    def test_the_same_crash_with_the_task_archived_by_the_core_runs_it_once(self):
        def archive(task):
            (task.parent / "archive").mkdir()
            return task.rename(task.parent / "archive" / task.name)
        self.crash_between_the_publish_and_the_entrys_removal(archive)

    def test_a_staged_copy_back_beside_the_published_one_after_a_power_loss_runs_once(self):
        self.crash_between_the_publish_and_the_entrys_removal(lambda task: task, resurrect=True)

    def test_the_same_with_the_task_archived_by_the_core_runs_once(self):
        def archive(task):
            (task.parent / "archive").mkdir()
            return task.rename(task.parent / "archive" / task.name)
        self.crash_between_the_publish_and_the_entrys_removal(archive, resurrect=True)

    def test_the_same_crash_with_the_task_answered_by_the_core_runs_it_once(self):
        def answer(task):
            task.unlink()
            (self.ws / "results" / task.name).write_text("Your calendar is clear.")
        # The outbound worker posts it on its own cadence, not the poll loop's.
        self.crash_between_the_publish_and_the_entrys_removal(answer, settled=lambda: self.relay.results)
        self.assertEqual([r["id"] for r in self.relay.results], [ASK_A])

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
