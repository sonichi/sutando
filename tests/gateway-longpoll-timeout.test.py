#!/usr/bin/env python3
"""Long-poll timeout grace preserves retry timing without inventing poll evidence."""
from __future__ import annotations

import re
import sys
import json
import tempfile
import threading
import unittest
from contextlib import ExitStack
from unittest.mock import Mock, patch
from pathlib import Path

from _helpers.hermetic_gateway import assert_hermetic, isolate_then_import  # noqa: E402
_GW, _IMPORT_READS = isolate_then_import()   # before anything else imports the bridge

_REPO = Path(__file__).resolve().parent.parent
_PKG = _REPO / "packages" / "ag2-sparrow"
if str(_PKG) not in sys.path:
    sys.path.insert(0, str(_PKG))

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(_REPO / "src"))
from gateway_serving import read_verdict  # noqa: E402


class _Clock:
    """Stands in for the module's `time`, so the grace can expire without waiting."""

    def __init__(self):
        self.now = 1_000_000.0
        self.slept: list[float] = []

    def time(self) -> float:
        return self.now

    def monotonic(self) -> float:
        return self.now

    def sleep(self, s: float) -> None:
        self.slept.append(s)
        self.now += s


class _Loop:
    """One iteration of the real `main()`: the second singleton check ends it."""

    _PATCH = ("TOKEN", "URL", "time", "_acquire_singleton", "_load_inflight",
              "_recover_orphan_proactive", "_maybe_start_event_channel",
              "_heartbeat_singleton", "_post_heartbeat", "_req", "_write_task",
              "_post_task_ack", "_post_ready_results", "_post_proactive",
              "_reconcile_abandoned", "_reconcile_orphan_results", "_emit_gateway_status",
              "_save_inflight", "_log", "RESULTS_DIR")

    def __init__(self, gw, poll_raises=None, advance=0.0):
        self.gw, self.poll_raises, self.advance = gw, poll_raises, advance
        self.clock = _Clock()
        self.status: list[tuple] = []
        self.logs: list[str] = []
        self.polls: list[str] = []
        self.served: "list[list[dict]]" = []      # the tasks each poll is served, in order
        self._saved: dict = {}
        self._alive = [True, False]

    def _poll(self, method, path, payload=None, **kw):
        if path.startswith("/v1/tasks?wait="):
            self.polls.append(path)
            self.clock.now += self.advance
            # A list is one outcome per poll (None = a healthy empty poll).
            if isinstance(self.poll_raises, list):
                exc = self.poll_raises.pop(0) if self.poll_raises else None
            else:
                exc = self.poll_raises
            if exc is not None:
                raise exc
            return {"tasks": self.served.pop(0) if self.served else []}
        return {}

    def __enter__(self):
        gw = self.gw
        for n in self._PATCH:
            self._saved[n] = getattr(gw, n)
        gw.TOKEN, gw.URL = "secret", "http://relay.invalid"
        gw.time = self.clock
        gw._acquire_singleton = lambda *a, **k: True
        gw._load_inflight = lambda *a, **k: set()
        gw._heartbeat_singleton = lambda *a, **k: self._alive.pop(0) if self._alive else False
        for noop in ("_recover_orphan_proactive", "_maybe_start_event_channel",
                     "_post_heartbeat", "_post_task_ack", "_post_ready_results",
                     "_post_proactive", "_reconcile_orphan_results"):
            setattr(gw, noop, lambda *a, **k: None)
        # The loop under test is the poll; no results dir on the host may be read or written.
        self._tmp = tempfile.TemporaryDirectory()
        gw.RESULTS_DIR = Path(self._tmp.name) / "results-never-created"
        gw._save_inflight = lambda *a, **k: True
        gw._write_task = lambda *a, **k: None
        gw._reconcile_abandoned = lambda inflight, s, *a, **k: s
        gw._req = self._poll
        gw._emit_gateway_status = lambda connected, **k: self.status.append((connected, k))
        gw._log = lambda m: self.logs.append(str(m))
        return self

    def __exit__(self, *exc):
        for n, v in self._saved.items():
            setattr(self.gw, n, v)
        self._tmp.cleanup()
        return False

    def run(self):
        self.gw.main()
        return self


class _StatusLoop(_Loop):
    """Controlled poll responses, with production status, task and ack persistence."""

    def __enter__(self):
        super().__enter__()
        self.stack = ExitStack()
        gw = self.gw
        root = Path(self._tmp.name)
        for name in ("_emit_gateway_status", "_write_task", "_save_inflight",
                     "_load_inflight", "_post_task_ack"):
            setattr(gw, name, self._saved[name])
        for name in ("GATEWAY_STATUS_FILE", "INFLIGHT_FILE", "PENDING_ACK_FILE",
                     "TASK_MEDIA_FILE", "TASK_ROOMS_FILE", "OWNER_ACTIVITY_FILE",
                     "ACK_FIRST_FILE", "ACK_FIRST_MANIFEST_FILE", "DEDUP_ALIAS_FILE"):
            self.stack.enter_context(patch.object(gw, name, root / "state" / getattr(gw, name).name))
        self.stack.enter_context(patch.multiple(
            gw, _STATE=root / "state", TASKS_DIR=root / "tasks",
            ARCHIVE_RESULTS_DIR=root / "archive", UNDELIVERABLE_RESULTS_DIR=root / "undelivered",
            GATEWAY_INSTANCE="", ON_TASK_CMD="", _ack_disabled_until=0.0,
            _LOCK_LOST=threading.Event(), _OUTBOUND_STOP=threading.Event(),
            _OUTBOUND_WAKE=threading.Event(), _INFLIGHT_DEGRADED=False))
        for name in ("_resolve_host_label", "_start_lock_heartbeat", "_stamp_loop_progress",
                     "refresh_routing", "_retire_delivered_marks", "_sweep_held_dir",
                     "_reclaim_parked_relay_results", "_keep_relay_results_in_flight",
                     "_retry_withheld_reviews", "_push_pool_advertisement", "_advance_held_tasks"):
            self.stack.enter_context(patch.object(gw, name, return_value=None))
        self.stack.enter_context(patch.object(gw, "_start_results_watcher", return_value=None))
        # A stopped worker: the loop exits only once the worker reports it is gone.
        idle_worker = Mock(**{"is_alive.return_value": False})
        self.stack.enter_context(patch.object(gw, "_start_outbound_worker", return_value=idle_worker))
        gw._heartbeat_singleton = self._next_iteration
        self.acks = []
        return self

    def __exit__(self, *exc):
        self.stack.close()
        return super().__exit__(*exc)

    def _next_iteration(self):
        self.records.append(json.loads(self.gw.GATEWAY_STATUS_FILE.read_text()))
        verdict = read_verdict(self.gw.GATEWAY_STATUS_FILE, now=self.clock.time(), max_age=90)
        self.serving.append(verdict.serving if verdict is not None else None)
        return bool(self.outcomes)

    def _poll(self, method, path, payload=None, **kw):
        if method == "GET" and path.startswith("/v1/tasks?wait="):
            self.polls.append(path)
            advance, outcome = self.outcomes.pop(0)
            self.clock.now += advance
            if isinstance(outcome, Exception):
                raise outcome
            return outcome
        if method == "POST" and path.endswith("/ack"):
            gw = self.gw
            tid = payload["id"]
            self.acks.append({
                "payload": dict(payload),
                "task": (gw.TASKS_DIR / f"{tid}.txt").read_text(),
                "inflight": json.loads(gw.INFLIGHT_FILE.read_text()),
                "pending": json.loads(gw.PENDING_ACK_FILE.read_text()),
                "leases": json.loads(gw._ack_lease_file().read_text()),
                "status": json.loads(gw.GATEWAY_STATUS_FILE.read_text()),
            })
            return {}
        raise AssertionError(f"unexpected request: {method} {path}")

    def run(self, outcomes):
        self.outcomes = list(outcomes)
        self.records, self.serving = [], []
        self.gw.main()
        return self


def _round_error_once():
    """`_receive_round` raising for the first round that serves a task, then the real one."""
    real, raised = _GW._receive_round, []

    def receive(tasks):
        if tasks and not raised:
            raised.append(True)
            raise FileExistsError(17, "File exists")
        return real(tasks)
    return patch.object(_GW, "_receive_round", side_effect=receive)


class GatewayStatusEvidenceTest(unittest.TestCase):
    EMPTY = {"tasks": []}
    TASK = {"id": "task-poll-evidence", "task": "synthetic task", "lease_id": "test-lease"}

    def timeout(self, advance=1):
        return advance, TimeoutError("The read operation timed out")

    def test_startup_timeouts_without_a_success_never_serve(self):
        with _StatusLoop(_GW) as loop:
            loop.run([self.timeout(), self.timeout(), self.timeout(_GW.POLL_TIMEOUT_GRACE_S)])
            self.assertEqual([r["last_ok_ts"] for r in loop.records], [None] * 4)
            self.assertEqual([r["connected"] for r in loop.records], [False] * 4)
            self.assertEqual(loop.serving, [False] * 4)
            self.assertEqual(loop.clock.slept, [1])

    def test_startup_timeouts_preserve_a_thirteen_hour_old_success(self):
        with _StatusLoop(_GW) as loop:
            loop.run([(1, self.EMPTY)])
            old_ok = int(loop.clock.time())
            self.assertEqual(loop.records[-1]["last_ok_ts"], old_ok)
            loop.clock.now += 13 * 3600
            loop.run([self.timeout(), self.timeout(), self.timeout(_GW.POLL_TIMEOUT_GRACE_S)])
            self.assertEqual([r["last_ok_ts"] for r in loop.records], [old_ok] * 4)
            self.assertEqual([r["connected"] for r in loop.records], [False] * 4)
            self.assertEqual(loop.serving, [False] * 4)
            self.assertEqual(loop.clock.slept, [1])

    def test_confirmed_poll_retains_evidence_through_the_inclusive_grace_boundary(self):
        with _StatusLoop(_GW) as loop:
            ok = int(loop.clock.time()) + 1
            loop.run([(1, self.EMPTY), self.timeout(), self.timeout(_GW.POLL_TIMEOUT_GRACE_S - 1)])
            self.assertEqual([r["last_ok_ts"] for r in loop.records], [None, ok, ok, ok])
            self.assertEqual(loop.serving, [False, True, True, True])
            self.assertEqual(loop.clock.slept, [])
            self.assertFalse(any("poll network error" in m for m in loop.logs))

    def test_expired_grace_retains_success_and_bounded_backoff(self):
        with _StatusLoop(_GW) as loop:
            ok = int(loop.clock.time()) + 1
            loop.run([(1, self.EMPTY), self.timeout(_GW.POLL_TIMEOUT_GRACE_S + 1)]
                     + [self.timeout()] * 7)
            self.assertEqual([r["last_ok_ts"] for r in loop.records[1:]], [ok] * 9)
            self.assertEqual(loop.serving, [False, True] + [False] * 8)
            self.assertEqual(loop.clock.slept, [1, 2, 4, 8, 16, 32, 60, 60])
            self.assertEqual([r["backoff_s"] for r in loop.records[2:]], loop.clock.slept)

    def test_empty_and_task_responses_restore_serving_and_ack_after_timeout(self):
        with _StatusLoop(_GW) as loop:
            loop.run([self.timeout(), (1, self.EMPTY), self.timeout(),
                      self.timeout(_GW.POLL_TIMEOUT_GRACE_S + 1),
                      (1, {"tasks": [self.TASK]}), self.timeout()])
            empty_ok = 1_000_002
            task_ok = empty_ok + _GW.POLL_TIMEOUT_GRACE_S + 4
            self.assertEqual([r["last_ok_ts"] for r in loop.records],
                             [None, None, empty_ok, empty_ok, empty_ok, task_ok, task_ok])
            self.assertEqual(loop.serving, [False, False, True, True, False, True, True])
            self.assertEqual(loop.clock.slept, [1])
            self.assertEqual(len(loop.acks), 1)
            ack = loop.acks[0]
            tid = self.TASK["id"]
            self.assertEqual(ack["payload"], {"id": tid, "durable": True, "lease_id": "test-lease"})
            self.assertIn("task: synthetic task", ack["task"])
            self.assertEqual(ack["inflight"], [tid])
            self.assertEqual(ack["pending"], {tid: True})
            self.assertEqual(ack["leases"], {tid: "test-lease"})
            self.assertEqual(ack["status"]["last_ok_ts"], task_ok)
            self.assertEqual(_GW._load_pending_acks(), {})

    def test_round_error_cannot_discard_the_completed_poll(self):
        # A task's own write error is isolated per task; this raise escapes the round.
        for prior_success in (False, True):
            with self.subTest(prior_success=prior_success), _StatusLoop(_GW) as loop, \
                    _round_error_once():
                prefix = [(1, self.EMPTY)] if prior_success else []
                ok = 1_000_001 + int(prior_success)
                loop.run(prefix + [(1, {"tasks": [self.TASK]}), self.timeout(),
                                   self.timeout(_GW.POLL_TIMEOUT_GRACE_S + 1)]
                         + [self.timeout()] * 7)
                records = loop.records[1 + len(prefix):]
                self.assertEqual([r["last_ok_ts"] for r in records], [ok] * 10)
                self.assertEqual(loop.serving[1 + len(prefix):], [False, True] + [False] * 8)
                self.assertIn("unexpected:", records[0]["error"])
                self.assertTrue(any("FileExistsError" in m for m in loop.logs))
                self.assertEqual(loop.clock.slept, [1, 1, 2, 4, 8, 16, 32, 60, 60])
                self.assertEqual(loop.acks, [])
                self.assertFalse(_GW.INFLIGHT_FILE.exists())
                self.assertFalse(_GW.PENDING_ACK_FILE.exists())

    def test_failed_status_write_is_retried_with_the_completed_poll_timestamp(self):
        with _StatusLoop(_GW) as loop, _round_error_once():
            replace = _GW.os.replace
            failed = []

            def fail_once(src, dst, *a, **k):
                if dst == _GW.GATEWAY_STATUS_FILE and not failed:
                    if json.loads(src.read_text())["last_ok_ts"] is not None:
                        failed.append(True)
                        raise OSError("status unavailable")
                replace(src, dst, *a, **k)

            with patch.object(_GW.os, "replace", side_effect=fail_once):
                loop.run([(1, {"tasks": [self.TASK]}), self.timeout(),
                          self.timeout(_GW.POLL_TIMEOUT_GRACE_S + 1)])
            self.assertEqual(failed, [True])
            self.assertEqual([r["last_ok_ts"] for r in loop.records],
                             [None, 1_000_001, 1_000_001, 1_000_001])
            self.assertEqual(loop.serving, [False, False, True, False])
            self.assertEqual(loop.acks, [])

    def test_http_and_auth_failures_preserve_evidence_and_existing_handling(self):
        import urllib.error

        for code in (500, 401, 403):
            with self.subTest(code=code), _StatusLoop(_GW) as loop, \
                    patch.object(_GW, "_recover_auth", return_value=False) as recover:
                outcomes = [(1, self.EMPTY),
                            (1, urllib.error.HTTPError(_GW.URL, code, "test rejection", {}, None))]
                if code in (401, 403):
                    with self.assertRaisesRegex(SystemExit, f"FATAL: gateway auth rejected \\(HTTP {code}\\)"):
                        loop.run(outcomes)
                    recover.assert_called_once_with(code)
                    self.assertEqual(loop.clock.slept, [])
                else:
                    loop.run(outcomes)
                    recover.assert_not_called()
                    self.assertEqual(loop.clock.slept, [1])
                rec = json.loads(_GW.GATEWAY_STATUS_FILE.read_text())
                self.assertEqual(rec["last_ok_ts"], 1_000_001)
                self.assertFalse(rec["connected"])
                self.assertIn(f"HTTP {code}", rec["error"])


class LongPollTimeoutTest(unittest.TestCase):
    def setUp(self):
        try:
            from ag2_sparrow import remote_gateway_bridge as gw
        except (Exception, SystemExit) as e:  # noqa: BLE001
            self.skipTest(f"gateway not importable: {str(e)[:60]}")
        self.gw = gw

    # ── the policy on its own ────────────────────────────────────────────
    def test_the_grace_covers_at_least_one_whole_poll_window(self):
        self.assertGreaterEqual(self.gw.POLL_TIMEOUT_GRACE_S, self.gw.POLL_WAIT + 10)

    def test_benign_from_zero_up_to_and_including_the_boundary(self):
        g = self.gw.POLL_TIMEOUT_GRACE_S
        for dt in (0.0, g / 2, g):
            self.assertTrue(self.gw._poll_timeout_is_empty(1000.0, 1000.0 + dt), dt)

    def test_past_the_boundary_it_is_an_outage_again(self):
        g = self.gw.POLL_TIMEOUT_GRACE_S
        for dt in (g + 1, g * 10, 86400):
            self.assertFalse(self.gw._poll_timeout_is_empty(1000.0, 1000.0 + dt), dt)

    # ── the loop ─────────────────────────────────────────────────────────
    def test_a_timeout_inside_the_grace_neither_logs_nor_backs_off(self):
        with _Loop(self.gw, poll_raises=TimeoutError("The read operation timed out"),
                   advance=1.0) as loop:
            loop.run()
        self.assertEqual(loop.clock.slept, [], "a benign empty poll must not back off")
        self.assertNotIn("poll network error",
                         " ".join(loop.logs), "and must not log an outage")
        self.assertIn((False, {"poll_ok_ts": None}), loop.status,
                      "startup grace alone does not establish connectivity")

    def test_a_timeout_past_the_grace_takes_the_outage_path(self):
        grace = self.gw.POLL_TIMEOUT_GRACE_S
        with _Loop(self.gw, poll_raises=TimeoutError("The read operation timed out"),
                   advance=grace + 1) as loop:
            loop.run()
        self.assertEqual(loop.clock.slept, [1], "a real outage still backs off")
        self.assertIn("poll network error", " ".join(loop.logs))
        self.assertTrue(any(c is False and "network" in str(k.get("error", ""))
                            for c, k in loop.status),
                        "and still reports the gateway as not serving")

    def test_a_connect_failure_is_untouched_by_the_timeout_branch(self):
        # URLError is not a TimeoutError, so an unreachable relay keeps taking
        # the outage path however recently a poll succeeded.
        import urllib.error
        with _Loop(self.gw, poll_raises=urllib.error.URLError("no route"),
                   advance=1.0) as loop:
            loop.run()
        self.assertEqual(loop.clock.slept, [1])
        self.assertIn("poll network error", " ".join(loop.logs))

    def test_a_successful_poll_still_reports_connected(self):
        with _Loop(self.gw, poll_raises=None, advance=1.0) as loop:
            loop.run()
        self.assertEqual(loop.clock.slept, [])
        self.assertIn((True, {"poll_ok_ts": loop.clock.time()}), loop.status)

    def test_every_poll_carries_the_voice_outcome_contract(self):
        # One agent's credential may serve more than one bridge, so the broker serves
        # a voice relay ask only to a poll that names the contract itself.
        with _Loop(self.gw, poll_raises=None, advance=1.0) as loop:
            loop._alive = [True, True, False]
            loop.run()
        self.assertEqual(loop.polls, [f"/v1/tasks?wait={self.gw.POLL_WAIT}"
                                      "&capabilities=voice-outcome-v1"] * 2)


class AckFirstInTheLoopTest(unittest.TestCase):
    """What `main()` itself does for a relay ask served with `ack_first`."""

    def setUp(self):
        try:
            from ag2_sparrow import remote_gateway_bridge as gw
        except (Exception, SystemExit) as e:  # noqa: BLE001
            self.skipTest(f"gateway not importable: {str(e)[:60]}")
        self.gw = gw
        self.calls: list = []

    def woken(self, mark):
        """A wake recorder for the loop's own thread (the results watcher wakes from another)."""
        def wake():
            if threading.current_thread() is threading.main_thread():
                self.calls.append(mark() if callable(mark) else mark)
        return wake

    def test_a_relay_ask_is_taken_on_its_own_before_the_rest_of_the_batch(self):
        relay = {"id": "ask-5f3c9e0a1b2c", "task": "calendar?", "ack_first": True}
        typed = {"id": "task-22d83e59601f3a1fef", "task": "What is AG2Space?", "ack_first": "true"}
        with _Loop(self.gw) as loop:
            loop.served = [[typed, relay]]
            self.gw._write_task = lambda task, *a, **k: self.calls.append(("write", task["id"]))
            with patch.object(self.gw, "_take_ack_first",
                              side_effect=lambda task, inflight, *a: self.calls.append(("take", task["id"])) or True), \
                    patch.object(self.gw, "wake_outbound", new=self.woken("wake")):
                loop.run()
        # Taken first even though served second; only the exact boolean is a relay ask.
        self.assertEqual(self.calls, [("take", relay["id"]), ("write", typed["id"]), "wake"])

    def test_a_publish_from_a_retried_ack_wakes_the_outbound_worker_before_the_poll(self):
        with _Loop(self.gw) as loop, \
                patch.object(self.gw, "_advance_held_tasks", return_value=True), \
                patch.object(self.gw, "wake_outbound", new=self.woken(lambda: len(loop.polls))):
            loop.run()
        self.assertEqual(self.calls, [0])
        with _Loop(self.gw) as loop, \
                patch.object(self.gw, "_advance_held_tasks", return_value=False), \
                patch.object(self.gw, "wake_outbound", new=self.woken("woken")):
            loop.run()
        self.assertEqual(self.calls, [0], "nothing published, nothing to wake")

    def test_a_start_sweep_that_fails_is_logged_and_the_loop_still_starts(self):
        def _retire_delivered_marks():
            raise RuntimeError("marks")

        with _Loop(self.gw) as loop, \
                patch.object(self.gw, "_retire_delivered_marks", new=_retire_delivered_marks), \
                patch.object(self.gw, "_sweep_held_dir", new=lambda: self.calls.append("swept")):
            loop.run()
        self.assertTrue(any(re.fullmatch(r"_retire_delivered_marks failed \(RuntimeError at [\w.-]+:\d+\) — continuing", ln)
                            for ln in loop.logs), loop.logs)
        self.assertEqual(self.calls, ["swept"], "one sweep's failure skipped the next")
        self.assertEqual(len(loop.polls), 1)


class OneTaskFailsInABatchTest(unittest.TestCase):
    """A task that raises while it is taken costs only itself: its batch-mates are still
    queued and acked, so the broker does not re-serve the whole batch until each parks."""

    def setUp(self):
        try:
            from ag2_sparrow import remote_gateway_bridge as gw
        except (Exception, SystemExit) as e:  # noqa: BLE001
            self.skipTest(f"gateway not importable: {str(e)[:60]}")
        self.gw = gw
        self.acked: list = []

    def _run(self, served, write):
        with _Loop(self.gw) as loop, \
                patch.object(self.gw, "_record_pending_acks", return_value=True), \
                patch.object(self.gw, "wake_outbound", new=lambda: None):
            loop.served = [served]
            self.gw._write_task = write
            self.gw._post_task_ack = lambda tid, durable=False, lease=None, **k: \
                self.acked.append((tid, lease)) or True
            loop.run()
        return loop

    def test_a_task_that_raises_does_not_withhold_the_other_acks(self):
        tasks = [{"id": f"task-{n}", "task": "hi", "lease_id": f"lease-{n}"}
                 for n in ("a1", "b2", "c3")]

        def write(task, *a, **k):
            if task["id"] == "task-b2":
                raise ValueError("bad field")
            return task["id"], True

        loop = self._run(tasks, write)
        self.assertEqual(self.acked, [("task-a1", "lease-a1"), ("task-c3", "lease-c3")])
        self.assertEqual(loop.clock.slept, [], "one bad task is not an outage: no back-off")
        self.assertTrue(any("task-b2" in m and "ValueError" in m for m in loop.logs),
                        "the failing task and its error are named")

    def test_every_task_of_a_batch_is_acked_in_the_round_it_is_served(self):
        # The ack is on receipt, before any worker runs, so serial answering never outlasts a lease.
        tasks = [{"id": f"task-e{n}", "task": "hi", "lease_id": f"lease-e{n}"} for n in range(8)]
        loop = self._run(tasks, lambda task, *a, **k: (task["id"], True))
        self.assertEqual(self.acked, [(t["id"], t["lease_id"]) for t in tasks])
        self.assertEqual(len(loop.polls), 1, "all eight acked before the next poll")

    def _consumed(self, hitl, review=False):
        queued: list = []
        typed = {"id": "task-f6", "task": "hi", "lease_id": "lease-f6"}
        click = {"id": "task-k5", "task": "Approve", "lease_id": "lease-k5"}
        with patch.object(self.gw, "_handle_hitl_action",
                          side_effect=lambda t: hitl if t is click else False), \
                patch.object(self.gw, "_handle_review_decision",
                             side_effect=lambda t: review and t is click), \
                patch.object(self.gw, "_queue_review_control_result",
                             side_effect=lambda t, body=None: queued.append((t["id"], body))), \
                patch.object(self.gw, "_retry_review_control_results", new=lambda: None):
            loop = self._run([click, typed], lambda task, *a, **k: (task["id"], True))
        return loop, queued

    def test_a_card_click_is_consumed_with_a_no_send_result_and_its_batch_mate_acked(self):
        loop, queued = self._consumed("applied")
        self.assertEqual(queued, [("task-k5", "[no-send]")])
        self.assertEqual(self.acked, [("task-f6", "lease-f6")], "a consumed click is not queued")
        self.assertIn("hitl: consumed card click task-k5 from the task relay (applied)", loop.logs)

    def test_a_rejected_click_is_answered_with_the_reason_and_its_batch_mate_acked(self):
        loop, queued = self._consumed("rejected:card moved")
        self.assertEqual(len(queued), 1)
        self.assertEqual(queued[0][0], "task-k5")
        self.assertIn("did not apply: the card had already moved (card moved)", queued[0][1])
        self.assertEqual(self.acked, [("task-f6", "lease-f6")])

    def test_a_review_decision_is_consumed_and_its_batch_mate_acked(self):
        loop, queued = self._consumed(False, review=True)
        self.assertEqual(queued, [("task-k5", None)])
        self.assertEqual(self.acked, [("task-f6", "lease-f6")])
        self.assertIn("consumed private review decision task-k5", loop.logs)

    def test_only_a_task_new_to_the_in_flight_set_wakes_the_outbound_worker(self):
        woken: list = []
        for preloaded, want in ((set(), ["woken"]), ({"task-g7"}, [])):
            woken.clear()
            with _Loop(self.gw) as loop, \
                    patch.object(self.gw, "_record_pending_acks", return_value=True), \
                    patch.object(self.gw, "wake_outbound", new=lambda: woken.append("woken")):
                self.gw._load_inflight = lambda *a, **k: set(preloaded)
                loop.served = [[{"id": "task-g7", "task": "hi", "lease_id": "lease-g7"}]]
                self.gw._write_task = lambda task, *a, **k: (task["id"], True)
                loop.run()
            self.assertEqual(woken, want, preloaded)

    def test_an_auth_rejection_while_taking_a_task_still_reaches_auth_recovery(self):
        import urllib.error
        recovered: list = []
        relay = {"id": "ask-5f3c9e0a1b2c", "task": "calendar?", "ack_first": True}
        err = urllib.error.HTTPError("http://relay.invalid", 401, "Unauthorized", {}, None)
        with patch.object(self.gw, "_take_ack_first", side_effect=err), \
                patch.object(self.gw, "_recover_auth",
                             side_effect=lambda code: recovered.append(code) or True):
            loop = self._run([relay], lambda task, *a, **k: (task["id"], True))
        self.assertEqual(recovered, [401])
        self.assertFalse(any("not taken" in m for m in loop.logs))

    def test_a_non_auth_http_error_costs_only_its_task(self):
        import urllib.error
        relay = {"id": "ask-5f3c9e0a1b2c", "task": "calendar?", "ack_first": True}
        typed = {"id": "task-h8", "task": "hi", "lease_id": "lease-h8"}
        err = urllib.error.HTTPError("http://relay.invalid", 500, "boom", {}, None)
        with patch.object(self.gw, "_take_ack_first", side_effect=err):
            loop = self._run([relay, typed], lambda task, *a, **k: (task["id"], True))
        self.assertEqual(self.acked, [("task-h8", "lease-h8")])
        self.assertTrue(any(relay["id"] in m and "HTTPError" in m for m in loop.logs))

    def test_an_ack_first_task_that_raises_does_not_stop_the_batch(self):
        relay = {"id": "ask-5f3c9e0a1b2c", "task": "calendar?", "ack_first": True}
        typed = {"id": "task-d4", "task": "hi", "lease_id": "lease-d4"}
        with patch.object(self.gw, "_take_ack_first", side_effect=RuntimeError("held")):
            loop = self._run([relay, typed], lambda task, *a, **k: (task["id"], True))
        self.assertEqual(self.acked, [("task-d4", "lease-d4")])
        self.assertTrue(any(relay["id"] in m and "RuntimeError" in m for m in loop.logs))


class RoundAccountingTest(unittest.TestCase):
    """Every served task is named at receipt and settled by the end of its round, and a poll
    answer with no task list is a logged anomaly, never a silent empty poll."""

    ROOM = "!secretroom:ag2.space"

    def setUp(self):
        try:
            from ag2_sparrow import remote_gateway_bridge as gw
        except (Exception, SystemExit) as e:  # noqa: BLE001
            self.skipTest(f"gateway not importable: {str(e)[:60]}")
        self.gw = gw

    def _task(self, n):
        return {"id": f"task-r{n}", "task": "hi", "lease_id": f"lease-r{n}", "channel_id": self.ROOM}

    def _run(self, served, ack=lambda tid: True, commit=True, poll=None):
        with _Loop(self.gw) as loop, \
                patch.object(self.gw, "_record_pending_acks", return_value=commit), \
                patch.object(self.gw, "wake_outbound", new=lambda: None), \
                patch.dict(self.gw._LOG_COUNTS, clear=True):
            loop.served = [served]
            if poll is not None:
                self.gw._req = lambda method, path, *a, **k: (
                    loop.polls.append(path) or poll() if path.startswith("/v1/tasks?") else {})
            self.gw._write_task = lambda task, *a, **k: (task["id"], True)
            self.gw._post_task_ack = lambda tid, durable=False, lease=None, **k: ack(tid)
            loop.run()
        return loop

    def test_each_served_task_is_logged_at_receipt_with_its_lease_and_no_room(self):
        loop = self._run([self._task(1), self._task(2)])
        self.assertIn("served task-r1 lease=lease-r1", loop.logs)
        self.assertIn("served task-r2 lease=lease-r2", loop.logs)
        self.assertFalse(any(self.ROOM in m for m in loop.logs), "no room id in any line")

    def test_a_fully_acked_round_flags_nothing(self):
        loop = self._run([self._task(1), self._task(2)])
        self.assertFalse(any("was not acked" in m for m in loop.logs))

    def test_a_task_whose_ack_did_not_land_is_named_at_the_end_of_the_round(self):
        loop = self._run([self._task(1), self._task(2)], ack=lambda tid: tid != "task-r2")
        flagged = [m for m in loop.logs if "was not acked, staged, consumed or dropped" in m]
        self.assertEqual(len(flagged), 1)
        self.assertTrue(flagged[0].startswith("served task-r2 "))

    def test_a_round_whose_ledger_did_not_commit_names_every_task(self):
        loop = self._run([self._task(1), self._task(2)], commit=False)
        self.assertIn("acks withheld for 2 task(s): the in-flight set or ack ledger did not commit",
                      loop.logs)
        flagged = [m.split()[1] for m in loop.logs if "was not acked" in m]
        self.assertEqual(flagged, ["task-r1", "task-r2"])

    def test_dropped_tasks_are_settled_and_an_ack_first_task_without_a_fate_is_named(self):
        relay = {"id": "ask-5f3c9e0a1b2c", "task": "calendar?", "ack_first": True}
        with patch.object(self.gw, "_take_ack_first", return_value=False), \
                patch.object(self.gw, "_handle_hitl_action", return_value=False), \
                patch.object(self.gw, "_handle_review_decision", return_value=False):
            with _Loop(self.gw) as loop, \
                    patch.object(self.gw, "_record_pending_acks", return_value=True):
                loop.served = [[relay, self._task(3)]]
                self.gw._write_task = lambda task, *a, **k: None      # logged its own reason
                loop.run()
        flagged = [m.split()[1] for m in loop.logs if "was not acked" in m]
        self.assertEqual(flagged, ["ask-5f3c9e0a1b2c"],
                         "a False from _take_ack_first is not a fate; the dropped write is")
        self.assertIn("served ask-5f3c9e0a1b2c lease=none ack_first", loop.logs)

    def test_take_ack_first_records_the_fate_it_reached(self):
        gw = self.gw
        task = {"id": "ask-5f3c9e0a1b2c", "task": "calendar?", "ack_first": True,
                "lease_id": "lease-q"}
        key = "ask-5f3c9e0a1b2c"

        def fate(**patches):
            fates = {key: None}
            with patch.multiple(gw, **patches):
                gw._take_ack_first(task, set(), fates)
            return fates[key]

        written = lambda *a, **k: (key, True)  # noqa: E731
        self.assertEqual(fate(_write_task=lambda *a, **k: None), "dropped")
        self.assertEqual(fate(_write_task=written, _load_held=lambda: None), "dropped")
        redelivered = dict(_write_task=written, _load_held=lambda: {},
                           _held_entry_ok=lambda tid, e: False,
                           _track_durably=lambda tid, inflight: True,
                           _record_pending_acks=lambda p: True)
        self.assertEqual(fate(**redelivered, _post_task_ack=lambda *a, **k: True), "acked")
        self.assertIsNone(fate(**redelivered, _post_task_ack=lambda *a, **k: False),
                          "an ack that did not land is left for the round to name")
        entry = {"staged": "x"}
        advancing = dict(_write_task=written, _held_entry_ok=lambda tid, e: True)
        self.assertEqual(fate(**advancing, _load_held=lambda: {key: entry},
                              _advance_held=lambda tid, e, i: True), "acked")
        self.assertEqual(fate(**advancing, _load_held=lambda: {key: entry},
                              _advance_held=lambda tid, e, i: False), "staged")
        held = [{key: entry}, {}]
        self.assertEqual(fate(**advancing, _load_held=lambda: held.pop(0),
                              _advance_held=lambda tid, e, i: False), "dropped")

    def test_an_empty_poll_body_is_a_logged_counted_anomaly(self):
        before = self.gw._POLL_ANOMALIES
        loop = self._run([], poll=lambda: {})
        lines = [m for m in loop.logs if m.startswith("poll anomaly #")]
        self.assertEqual(len(lines), 1)
        self.assertIn("answered an empty body", lines[0])
        self.assertEqual(self.gw._POLL_ANOMALIES, before + 1)
        self.assertEqual(loop.clock.slept, [1], "an answer that settles nothing is not re-polled at once")

    def test_json_without_a_task_list_is_a_logged_anomaly(self):
        for answer, shape in (({"error": "busy"}, "dict without a tasks list"),
                              ({"tasks": None}, "dict without a tasks list"),
                              ([], "list without a tasks list")):
            loop = self._run([], poll=lambda a=answer: a)
            self.assertTrue(any(shape in m for m in loop.logs if m.startswith("poll anomaly")),
                            answer)
            self.assertEqual(loop.clock.slept, [1], answer)

    def test_a_run_of_malformed_answers_backs_off_and_thins_its_log(self):
        with _Loop(self.gw) as loop, patch.dict(self.gw._LOG_COUNTS, clear=True):
            loop._alive = [True] * 4 + [False]
            self.gw._req = lambda method, path, *a, **k: (
                loop.polls.append(path) or {"tasks": None} if path.startswith("/v1/tasks?") else {})
            loop.run()
        self.assertEqual(loop.clock.slept, [1, 2, 4, 8], "doubling, as before the anomaly line")
        lines = [m for m in loop.logs if m.startswith("poll anomaly #")]
        self.assertEqual(len(lines), 3, "logged the 1st, 2nd and 4th time")
        self.assertIn("(seen 4 times)", lines[-1])

    def test_the_anomaly_names_status_length_and_edge_ray_from_the_real_request(self):
        import urllib.request

        class _Resp:
            status = 200
            headers = {"Content-Length": "0", "CF-Ray": "8f00aa11bb22cc33-SJC"}

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return b""

        with _Loop(self.gw) as loop, patch.dict(self.gw._LOG_COUNTS, clear=True):
            real_req = loop._saved["_req"]
            self.gw._req = lambda method, path, *a, **k: (
                real_req(method, path, *a, **k) if path.startswith("/v1/tasks?") else {})
            saved_open = urllib.request.urlopen
            urllib.request.urlopen = lambda *a, **k: _Resp()
            try:
                loop.run()
            finally:
                urllib.request.urlopen = saved_open
        lines = [m for m in loop.logs if m.startswith("poll anomaly #")]
        self.assertEqual(len(lines), 1)
        self.assertIn("status=200, content-length=0, cf-ray=8f00aa11bb22cc33-SJC", lines[0])
        self.assertFalse(any("secret" in m for m in loop.logs), "the bearer never reaches a line")

    def test_the_read_timeout_grace_path_is_logged(self):
        with _Loop(self.gw, poll_raises=TimeoutError("The read operation timed out"),
                   advance=1.0) as loop:
            loop.run()
        self.assertTrue(any(m.startswith("poll read timed out after 35s inside the")
                            for m in loop.logs))


class SlowAckTest(unittest.TestCase):
    """One ack that drops, then hangs on its retry, must not push its healthy siblings' first
    acks past the broker's 15 s delivery lease; the retry spends only a budget under it."""

    LEASE_S = 15.0

    def setUp(self):
        try:
            from ag2_sparrow import remote_gateway_bridge as gw
        except (Exception, SystemExit) as e:  # noqa: BLE001
            self.skipTest(f"gateway not importable: {str(e)[:60]}")
        self.gw = gw

    def _run(self, first_drop_after, hang=10.0, n_healthy=3, ack_first=False):
        import contextlib
        import http.client
        bad_task = {"id": "task-bad", "task": "hi", "lease_id": "lease-bad"}
        redelivery = contextlib.ExitStack()
        if ack_first:
            # A redelivered ack_first task: already queued here, so it is acked as any redelivery.
            bad_task["ack_first"] = True
            for name, value in (("_load_held", lambda: {}), ("_held_entry_ok", lambda tid, e: False),
                                ("_track_durably", lambda tid, inflight: True)):
                redelivery.enter_context(patch.object(self.gw, name, new=value))
        tasks = [bad_task] + [
            {"id": f"task-ok{n}", "task": "hi", "lease_id": f"lease-ok{n}"} for n in range(n_healthy)]
        acks: list = []      # (task id, seconds since the poll answered, outcome, timeout)
        with _Loop(self.gw) as loop, \
                patch.object(self.gw, "_record_pending_acks", return_value=True), \
                patch.object(self.gw, "_forget_pending_ack", new=lambda *a, **k: None), \
                patch.object(self.gw, "wake_outbound", new=lambda: None), \
                patch.dict(self.gw._LOG_COUNTS, clear=True), redelivery:
            self.gw._post_task_ack = loop._saved["_post_task_ack"]
            self.gw._write_task = lambda task, *a, **k: (task["id"], True)
            loop.served = [tasks]
            served_at: list = []
            bad_calls = [0]

            def req(method, path, payload=None, timeout=None, deadline=None, **k):
                if path.startswith("/v1/tasks?"):
                    out = loop._poll(method, path)
                    served_at.append(loop.clock.now)
                    return out
                if not path.endswith("/ack"):
                    return {}
                tid = payload["id"]
                if tid == "task-bad":
                    bad_calls[0] += 1
                    if bad_calls[0] == 1:
                        loop.clock.now += first_drop_after
                        acks.append((tid, loop.clock.now - served_at[0], "dropped", deadline))
                        raise http.client.RemoteDisconnected("Remote end closed connection without response")
                    # The retry hangs; the real _req gives up at its deadline (see SlowDripTest).
                    until = loop.clock.now + hang
                    loop.clock.now = min(until, deadline) if deadline is not None else until
                    acks.append((tid, loop.clock.now - served_at[0], "hung", deadline))
                    raise TimeoutError("timed out")
                loop.clock.now += 0.1
                acks.append((tid, loop.clock.now - served_at[0], "ok", timeout))
                return {"ok": True}
            self.gw._req = req
            loop.run()
        return loop, acks

    def test_a_dropped_then_hanging_ack_does_not_delay_its_siblings_past_the_lease(self):
        loop, acks = self._run(first_drop_after=5.0)
        healthy = [a for a in acks if a[0] != "task-bad"]
        self.assertEqual([a[0] for a in healthy], ["task-ok0", "task-ok1", "task-ok2"])
        self.assertTrue(all(a[1] < 6.0 for a in healthy), healthy)
        self.assertTrue(all(a[1] < self.LEASE_S for a in acks), acks)
        bad = [a for a in acks if a[0] == "task-bad"]
        self.assertEqual(len(bad), 2, "the dropped ack gets exactly one retry")
        self.assertLessEqual(bad[1][1], self.gw.ACK_RETRY_BUDGET_S + 1e-6,
                             "the retry ends inside the budget")
        self.assertTrue(any(m.startswith("served task-bad was not acked") for m in loop.logs))
        self.assertTrue(any(m.startswith("task ack for task-bad lost to a dropped connection (RemoteDisconnected ")
                            and m.endswith(") — set aside for retry") for m in loop.logs), loop.logs)
        self.assertFalse(any(m.startswith("task ack network error for task-bad: RemoteDisconnected")
                             for m in loop.logs), "pass 1 does not call a set-aside ack failed")

    def test_a_redelivered_ack_first_ack_that_drops_does_not_delay_its_siblings(self):
        loop, acks = self._run(first_drop_after=5.0, ack_first=True)
        healthy = [a for a in acks if a[0] != "task-bad"]
        self.assertEqual([a[0] for a in healthy], ["task-ok0", "task-ok1", "task-ok2"])
        self.assertTrue(all(a[1] < 6.0 for a in healthy), healthy)
        bad = [a for a in acks if a[0] == "task-bad"]
        self.assertEqual(len(bad), 2, "the redelivery's dropped ack gets one budgeted retry")
        self.assertLessEqual(bad[1][1], self.gw.ACK_RETRY_BUDGET_S + 1e-6, bad)

    def test_a_retry_the_budget_cannot_cover_is_left_to_the_pending_ack_retry(self):
        loop, acks = self._run(first_drop_after=9.0)
        self.assertEqual(len([a for a in acks if a[0] == "task-bad"]), 1, "no retry past the budget")
        self.assertTrue(all(a[1] < 10.0 for a in acks if a[0] != "task-bad"), acks)
        self.assertIn("1 dropped ack(s) left to the pending-ack retry: the 10s retry budget is spent",
                      loop.logs)


class SlowDripTest(unittest.TestCase):
    """urllib's timeout bounds each socket operation; a body that drips in a byte at a time
    must still end the request at its wall-clock deadline."""

    def setUp(self):
        try:
            from ag2_sparrow import remote_gateway_bridge as gw
        except (Exception, SystemExit) as e:  # noqa: BLE001
            self.skipTest(f"gateway not importable: {str(e)[:60]}")
        self.gw = gw

    def _drip(self, clock, body=b'{"ok": true}' + b" " * 60, per_byte_s=1.0):
        class _Resp:
            status = 200
            headers = {"Content-Length": str(len(body))}

            def __init__(self):
                self.left = body

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self, amt=None):
                if not self.left:
                    return b""
                n = len(self.left) if amt is None else 1      # drips one byte per socket read
                clock.now += per_byte_s * n
                out, self.left = self.left[:n], self.left[n:]
                return out
        return _Resp

    def test_a_dripping_ack_ends_at_its_deadline(self):
        import urllib.request
        clock = _Clock()
        with patch.object(self.gw, "time", clock), \
                patch.object(self.gw, "TOKEN", "secret"), patch.object(self.gw, "URL", "http://relay.invalid"), \
                patch.object(urllib.request.OpenerDirector, "open", new=lambda *a, **k: self._drip(clock)()):
            start = clock.now
            with self.assertRaises(TimeoutError):
                self.gw._req("POST", "/v1/tasks/task-1/ack", {"id": "task-1"}, timeout=10,
                             deadline=start + 5)
            self.assertLessEqual(clock.now - start, 5 + 1, "one drip past the deadline at most")

    def test_the_budgeted_retry_gives_up_at_the_budget(self):
        import urllib.request
        clock = _Clock()
        logs: list = []
        received = clock.now
        fates = {"task-1": None}
        with patch.object(self.gw, "time", clock), patch.object(self.gw, "_log", new=logs.append), \
                patch.object(self.gw, "TOKEN", "secret"), patch.object(self.gw, "URL", "http://relay.invalid"), \
                patch.object(self.gw, "_forget_pending_ack", new=lambda *a, **k: None), \
                patch.object(urllib.request.OpenerDirector, "open", new=lambda *a, **k: self._drip(clock)()):
            self.gw._retry_dropped_acks([("task-1", True, "lease-1")], received, fates)
        self.assertLessEqual(clock.now - received, self.gw.ACK_RETRY_BUDGET_S + 1)
        self.assertIsNone(fates["task-1"], "a retry that ran out of budget is not acked")
        self.assertTrue(any(m.startswith("task ack network error for task-1:") for m in logs), logs)


class RelayResultsInTheLoopTest(unittest.TestCase):
    """What `main()` does for a voice relay ask's result: reclaimed at start, and put back in
    flight on every pass when it lands outside the in-flight set."""

    ASK = "ask-5f3c9e0a1b2c"

    def setUp(self):
        try:
            from ag2_sparrow import remote_gateway_bridge as gw
        except (Exception, SystemExit) as e:  # noqa: BLE001
            self.skipTest(f"gateway not importable: {str(e)[:60]}")
        self.gw = gw
        self.calls: list = []

    def test_a_reclaim_that_fails_is_logged_and_the_loop_still_starts(self):
        def _reclaim_parked_relay_results(inflight):
            raise RuntimeError("outbox")

        with _Loop(self.gw) as loop, \
                patch.object(self.gw, "_reclaim_parked_relay_results", new=_reclaim_parked_relay_results):
            loop.run()
        self.assertTrue(any(re.fullmatch(r"relay result reclaim failed \(RuntimeError at [\w.-]+:\d+\) — continuing", ln)
                            for ln in loop.logs), loop.logs)
        self.assertEqual(len(loop.polls), 1)

    def test_a_relay_result_landing_outside_the_in_flight_set_wakes_the_outbound_worker(self):
        with tempfile.TemporaryDirectory() as d, _Loop(self.gw) as loop, \
                patch.multiple(self.gw, ACK_FIRST_FILE=Path(d) / "marks.json",
                               DEDUP_ALIAS_FILE=Path(d) / "aliases.json"):
            self.gw.ACK_FIRST_FILE.write_text(f'{{"{self.ASK}": "{self.ASK}"}}')
            self.gw.RESULTS_DIR.mkdir()
            saved: list = []
            self.gw._save_inflight = lambda ids: saved.append(set(ids)) or True
            poll = self.gw._req

            def lands_during_the_poll(method, path, *a, **k):
                if path.startswith("/v1/tasks?"):
                    (self.gw.RESULTS_DIR / f"{self.ASK}.txt").write_text("Your calendar is clear.")
                return poll(method, path, *a, **k)
            self.gw._req = lands_during_the_poll
            with patch.object(self.gw, "wake_outbound", new=lambda: self.calls.append(
                    ("woken", threading.current_thread() is threading.main_thread(), len(loop.polls),
                     any(self.ASK in ids for ids in saved)))):
                loop.run()
        # At start nothing was live; after the poll the result is back in flight, then the worker woken.
        self.assertIn(("woken", True, 1, True), self.calls)
        self.assertEqual([ids for ids in saved if self.ASK in ids][:1], [{self.ASK}])


class DroppedConnectionTest(unittest.TestCase):
    """urllib wraps only the send in URLError. A peer or proxy that closes the
    socket after it raises http.client's own errors bare from getresponse(), and
    the loop filed those as "unexpected" — a crash-looking line for a dropped link."""

    def setUp(self):
        try:
            from ag2_sparrow import remote_gateway_bridge as gw
        except (Exception, SystemExit) as e:  # noqa: BLE001
            self.skipTest(f"gateway not importable: {str(e)[:60]}")
        self.gw = gw

    def _assert_network_path(self, loop, cls):
        logs = " ".join(loop.logs)
        self.assertEqual(loop.clock.slept, [1], "backs off like any outage")
        self.assertIn(f"poll network error: {cls}", logs, "names the exception class, never its text")
        self.assertIn("1 consecutive, 1s since last successful poll", logs)
        self.assertNotIn("unexpected", logs, "a dropped connection is not a crash")
        self.assertTrue(any(c is False and "network" in str(k.get("error", ""))
                            for c, k in loop.status))

    def test_a_connection_closed_before_the_response_is_a_network_error(self):
        import http.client
        exc = http.client.RemoteDisconnected("Remote end closed connection without response")
        with _Loop(self.gw, poll_raises=exc, advance=1.0) as loop:
            loop.run()                    # returns: the loop went on to its next pass
        self._assert_network_path(loop, "RemoteDisconnected")

    def test_a_truncated_response_body_is_a_network_error(self):
        import http.client
        with _Loop(self.gw, poll_raises=http.client.IncompleteRead(b""), advance=1.0) as loop:
            loop.run()
        self._assert_network_path(loop, "IncompleteRead")

    def test_the_counter_climbs_and_a_good_poll_logs_the_recovery(self):
        import http.client
        import re
        outcomes = [http.client.RemoteDisconnected("gone"),
                    http.client.RemoteDisconnected("gone"), None]
        with _Loop(self.gw, poll_raises=outcomes, advance=1.0) as loop:
            loop._alive = [True, True, True, False]
            loop.run()
        logs = loop.logs
        errors = [m for m in logs if m.startswith("poll network error")]
        self.assertEqual(len(errors), 2)
        self.assertIn("1 consecutive, 1s since", errors[0])
        self.assertIn("2 consecutive, 3s since", errors[1], "counts up; clock = 1s + 1s back-off + 1s")
        self.assertEqual(loop.clock.slept, [1, 2], "back-off doubles across the outage")
        recovered = [m for m in logs if m.startswith("poll recovered")]
        self.assertEqual(len(recovered), 1, "one recovery line, after the good poll")
        self.assertRegex(recovered[0], r"^poll recovered after 2 failures, 6s$")
        self.assertLess(logs.index(errors[1]), logs.index(recovered[0]))
        self.assertIn((True, {"poll_ok_ts": loop.clock.time()}), loop.status)

    def test_a_second_good_poll_logs_no_recovery_and_the_next_outage_counts_from_one(self):
        import urllib.error
        fail = urllib.error.URLError("no route")
        with _Loop(self.gw, poll_raises=[fail, None, None, fail, None], advance=1.0) as loop:
            loop._alive = [True] * 5 + [False]
            loop.run()
        recovered = [m for m in loop.logs if m.startswith("poll recovered")]
        self.assertEqual(len(recovered), 2, "one line per outage, none for a later good poll")
        for line in recovered:
            self.assertRegex(line, r"^poll recovered after 1 failures, ")
        errors = [m for m in loop.logs if m.startswith("poll network error")]
        self.assertIn("1 consecutive", errors[1], "the good poll reset the counter")

    def test_a_grace_timeout_after_an_outage_is_not_the_recovery(self):
        import urllib.error
        outcomes = [urllib.error.URLError("no route"),
                    TimeoutError("The read operation timed out"), None]
        with _Loop(self.gw, poll_raises=outcomes, advance=1.0) as loop:
            loop._alive = [True, True, True, False]
            loop.run()
        recovered = [m for m in loop.logs if m.startswith("poll recovered")]
        # clock: 1s poll + 1s back-off + 1s grace poll + 1s good poll
        self.assertEqual(recovered, ["poll recovered after 1 failures, 4s"],
                         "only the poll that answered ends the outage")

    def test_a_reset_while_reading_the_body_is_a_network_error(self):
        import urllib.request

        class _Resp:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                raise ConnectionResetError(54, "Connection reset by peer")

        with _Loop(self.gw) as loop:
            real_req = loop._saved["_req"]

            def _req(method, path, *a, **k):
                if not path.startswith("/v1/tasks?wait="):
                    return {}
                loop.clock.now += 1.0
                return real_req(method, path, *a, **k)

            self.gw._req = _req
            saved_open = urllib.request.urlopen
            urllib.request.urlopen = lambda *a, **k: _Resp()
            try:
                loop.run()
            finally:
                urllib.request.urlopen = saved_open
        self._assert_network_path(loop, "ConnectionResetError")

    def test_an_ack_cut_off_mid_flight_reports_a_network_error_not_a_crash(self):
        import http.client
        gw = self.gw
        logs: list[str] = []
        saved = (gw._req, gw._log, gw._ack_disabled_until)
        gw._req = lambda *a, **k: (_ for _ in ()).throw(
            http.client.RemoteDisconnected("Remote end closed connection without response"))
        gw._log = lambda m: logs.append(str(m))
        gw._ack_disabled_until = 0.0
        try:
            self.assertFalse(gw._post_task_ack("task-1700000000", False))
        finally:
            gw._req, gw._log, gw._ack_disabled_until = saved
        self.assertIn("task ack network error", " ".join(logs))

    def test_a_heartbeat_cut_off_mid_flight_reports_a_network_error_not_a_crash(self):
        import http.client
        gw = self.gw
        logs: list[str] = []
        saved = (gw._req, gw._log, gw._heartbeat_retry_at)
        gw._req = lambda *a, **k: (_ for _ in ()).throw(
            http.client.RemoteDisconnected("Remote end closed connection without response"))
        gw._log = lambda m: logs.append(str(m))
        gw._heartbeat_retry_at = 0.0
        try:
            self.assertFalse(gw._post_heartbeat(set(), force=True))
        finally:
            gw._req, gw._log, gw._heartbeat_retry_at = saved
        self.assertIn("heartbeat network error", " ".join(logs))


class BridgeImportIsHermetic(unittest.TestCase):
    def test_the_bridge_import_reads_no_host_config_token_or_vault(self):
        assert_hermetic(self, _IMPORT_READS)


if __name__ == "__main__":
    unittest.main(verbosity=2)
