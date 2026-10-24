#!/usr/bin/env python3
"""The poll loop consults the stall watchdog on every beat, and only a real
stall trips it.

Drives the production `main()` for a few iterations with a fake clock. A bridge
whose polls stop succeeding stays alive on its own, and the launchd job restarts
it only on exit — so the loop itself has to notice and leave. What must NOT
trip it: a wait on a human (the auth-wait loop), a clock that jumps backwards,
and a lane nothing would restart.
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parent))
from _helpers.hermetic_env import isolate_os_environ  # noqa: E402

isolate_os_environ()

_REPO = Path(__file__).resolve().parent.parent
_PKG = _REPO / "packages" / "ag2-sparrow"
if str(_PKG) not in sys.path:
    sys.path.insert(0, str(_PKG))
# Hermetic: the module resolves its channel .env and sidecar dir at import, so
# point both at scratch rather than the developer's real config and workspace.
os.environ["CLAUDE_CONFIG_DIR"] = tempfile.mkdtemp(prefix="stall-loop-cfg-")
os.environ["AGENT_CONNECT_STATE_DIR"] = tempfile.mkdtemp(prefix="stall-loop-state-")
os.environ["REMOTE_TASK_URL"] = "http://relay.invalid"
os.environ["REMOTE_TASK_TOKEN"] = "dummy-secret"


class _Clock:
    """Stands in for the module's `time`: the limit can expire without waiting.
    `now` is wall time; `mono` is the monotonic clock, which a sleeping Mac stops."""

    def __init__(self):
        self.now = 1_000_000.0
        self.mono = 5_000.0
        self.slept: list[float] = []

    def advance(self, s: float) -> None:
        self.now += s
        self.mono += s

    def time(self) -> float:
        return self.now

    def monotonic(self) -> float:
        return self.mono

    def sleep(self, s: float) -> None:
        self.slept.append(s)
        self.advance(s)

    # `_log` formats a stamp through these; the fake must still answer.
    def strftime(self, *a, **k):
        return "stamp"

    def gmtime(self, *a, **k):
        return None


class _Worker:
    """Stays alive for its first `busy_joins` joins, like a worker mid-send."""

    def __init__(self, busy_joins=0):
        self.joined = False
        self.joins = 0
        self.busy_joins = busy_joins

    def join(self, timeout=None):
        self.joined = True
        self.joins += 1

    def is_alive(self):
        return self.joins <= self.busy_joins


class _Loop:
    """`main()` for as many iterations as `alive` has True entries; the first
    False singleton check ends it."""

    _PATCH = ("TOKEN", "URL", "time", "_acquire_singleton", "_load_inflight",
              "_recover_orphan_proactive", "_maybe_start_event_channel",
              "_heartbeat_singleton", "_post_heartbeat", "_req", "_write_task",
              "_post_task_ack", "_post_ready_results", "_post_proactive",
              "_reconcile_abandoned", "_emit_gateway_status", "_save_inflight", "_log",
              "_recover_auth", "POLL_STALL_EXIT_S", "POLL_STALL_RESTART_OWNER",
              "_STALL_REPORTED_FOR", "_STALL_EXIT_PENDING", "_resolve_host_label", "_start_lock_heartbeat",
              "_retire_delivered_marks", "_sweep_held_dir", "_reclaim_parked_relay_results",
              "_keep_relay_results_in_flight", "refresh_routing", "_start_results_watcher",
              "_start_outbound_worker", "_retry_withheld_reviews", "_push_pool_advertisement",
              "_advance_held_tasks", "_reconcile_orphan_results", "_receive_round")

    def __init__(self, gw, *, polls, alive, limit=100.0, owner="launchd",
                 recover_auth=None, busy_joins=0):
        self.gw, self.limit, self.owner = gw, limit, owner
        self.polls = list(polls)          # per poll: a callable(clock) -> resp, or an exception
        self._alive = list(alive)
        self.recover_auth = recover_auth
        self.clock = _Clock()
        self.status: list[tuple] = []
        self.logs: list[str] = []
        self.outbound = _Worker(busy_joins)
        self.poll_count = 0
        self._saved: dict = {}

    def _poll(self, method, path, payload=None, **kw):
        if not path.startswith("/v1/tasks?wait="):
            return {}
        self.poll_count += 1
        step = self.polls.pop(0) if self.polls else {"tasks": []}
        if callable(step):
            step = step(self.clock)
        if isinstance(step, BaseException):
            raise step
        return step

    def __enter__(self):
        gw = self.gw
        for n in self._PATCH:
            self._saved[n] = getattr(gw, n)
        gw.TOKEN, gw.URL = "secret", "http://relay.invalid"
        gw.time = self.clock
        gw.POLL_STALL_EXIT_S = self.limit
        gw.POLL_STALL_RESTART_OWNER = self.owner
        gw._STALL_REPORTED_FOR = None
        gw._STALL_EXIT_PENDING = False
        gw._acquire_singleton = lambda *a, **k: True
        gw._load_inflight = lambda *a, **k: set()
        gw._heartbeat_singleton = lambda *a, **k: self._alive.pop(0) if self._alive else False
        for noop in ("_recover_orphan_proactive", "_maybe_start_event_channel",
                     "_post_heartbeat", "_post_task_ack", "_post_ready_results",
                     "_post_proactive", "_resolve_host_label", "_start_lock_heartbeat",
                     "_retire_delivered_marks", "_sweep_held_dir",
                     "_reclaim_parked_relay_results", "_keep_relay_results_in_flight",
                     "refresh_routing", "_start_results_watcher", "_retry_withheld_reviews",
                     "_push_pool_advertisement", "_advance_held_tasks",
                     "_reconcile_orphan_results"):
            setattr(gw, noop, lambda *a, **k: None)
        gw._start_outbound_worker = lambda *a, **k: self.outbound
        gw._receive_round = lambda tasks: (list(tasks), {})
        gw._save_inflight = lambda *a, **k: None
        gw._write_task = lambda *a, **k: None
        gw._reconcile_abandoned = lambda inflight, s, *a, **k: s
        gw._req = self._poll
        gw._emit_gateway_status = lambda connected, **k: self.status.append((connected, k))
        gw._log = lambda m: self.logs.append(str(m))
        if self.recover_auth is not None:
            gw._recover_auth = lambda code: self.recover_auth(self.clock)
        return self

    def __exit__(self, *exc):
        for n, v in self._saved.items():
            setattr(self.gw, n, v)
        self.gw._OUTBOUND_STOP.clear()
        self.gw._OUTBOUND_WAKE.clear()
        self.gw._LOCK_LOST.clear()
        return False

    def run(self):
        """Returns the SystemExit the loop raised, or None when it ended normally."""
        try:
            self.gw.main()
        except SystemExit as e:
            return e
        return None


def _http_401():
    return urllib.error.HTTPError("http://relay.invalid/v1/tasks", 401, "nope", {}, None)


class PollStallLoopTest(unittest.TestCase):
    def setUp(self):
        try:
            from ag2_sparrow import remote_gateway_bridge as gw
        except (Exception, SystemExit) as e:  # noqa: BLE001
            self.skipTest(f"gateway not importable: {str(e)[:60]}")
        self.gw = gw

    def _stall_logs(self, loop):
        return [m for m in loop.logs if "stalled: no successful poll" in m]

    # ── the loop calls the watchdog every beat ───────────────────────────
    def test_every_iteration_consults_the_watchdog(self):
        calls: list[float] = []
        with _Loop(self.gw, polls=[], alive=[True, True, False]) as loop:
            orig = self.gw._abort_if_poll_stalled
            self.gw._abort_if_poll_stalled = lambda anchor, *a: calls.append(anchor)
            try:
                self.assertIsNone(loop.run())
            finally:
                self.gw._abort_if_poll_stalled = orig
        # Three singleton checks happened, so three beats, each guarded first.
        self.assertEqual(len(calls), 3, calls)

    # ── fresh ────────────────────────────────────────────────────────────
    def test_polls_that_keep_succeeding_never_trip_it(self):
        def ok(clock):
            clock.advance(30.0)
            return {"tasks": []}
        with _Loop(self.gw, polls=[ok, ok, ok, ok], alive=[True] * 4 + [False],
                   limit=100.0) as loop:
            self.assertIsNone(loop.run(), "a serving bridge must keep running")
        self.assertEqual(self._stall_logs(loop), [])
        self.assertTrue(any(c is True for c, _ in loop.status), loop.status)

    # ── stale ────────────────────────────────────────────────────────────
    def test_polls_stale_past_the_limit_exit_nonzero_with_one_log_line(self):
        def dead(clock):
            # Each failed poll costs most of a beat; backoff sleeps add the rest.
            clock.advance(40.0)
            return urllib.error.URLError("no route")
        with _Loop(self.gw, polls=[dead] * 10, alive=[True] * 10, limit=100.0) as loop:
            exc = loop.run()
        self.assertIsNotNone(exc, "a bridge whose polls stay stale must exit")
        self.assertEqual(exc.code, self.gw.POLL_STALL_EXIT_CODE,
                         "the wrapper tells a stall from a crash by this code")
        self.assertTrue(loop.outbound.joined, "exited without joining the outbound worker")
        lines = self._stall_logs(loop)
        self.assertEqual(len(lines), 1, lines)
        self.assertRegex(lines[0], r"no successful poll in \d+s \(limit 100s\)")
        self.assertIn("exiting so launchd restarts it", lines[0])
        self.assertTrue(any(c is False and "stalled" in str(k.get("error", ""))
                            for c, k in loop.status), loop.status)
        # And it was the loop's own outage path that preceded it, not a crash.
        self.assertIn("poll network error", " ".join(loop.logs))

    # ── paused on a human ────────────────────────────────────────────────
    def test_a_resumed_auth_wait_is_not_a_stall(self):
        def resumed(clock):
            clock.advance(5 * 3600)  # the owner took five hours to re-link
            return True
        with _Loop(self.gw, polls=[_http_401()], alive=[True, True, False], limit=100.0,
                   recover_auth=resumed) as loop:
            self.assertIsNone(loop.run(),
                              "the wait was on a human; resuming must not exit")
        self.assertEqual(self._stall_logs(loop), [])
        # The resumed iteration polled and reported connected.
        self.assertTrue(any(c is True for c, _ in loop.status), loop.status)

    # ── clock skew backwards ─────────────────────────────────────────────
    def test_a_clock_that_jumps_backwards_reads_as_fresh(self):
        def skewed(clock):
            clock.now -= 7 * 24 * 3600  # NTP yanked the wall clock a week back
            return {"tasks": []}
        with _Loop(self.gw, polls=[skewed, skewed], alive=[True, True, False],
                   limit=100.0) as loop:
            self.assertIsNone(loop.run())
        self.assertEqual(self._stall_logs(loop), [])
        self.assertFalse(self.gw._poll_stalled(1000.0, 1000.0 - 1e6, 100.0))

    # ── system sleep ─────────────────────────────────────────────────────
    def test_a_sleep_longer_than_the_limit_does_not_exit_on_the_first_failure(self):
        def slept_then_dead(clock):
            clock.now += 3 * 3600  # lid closed: wall time moves, the monotonic clock does not
            return urllib.error.URLError("network not up yet")
        def ok(clock):
            clock.advance(5.0)
            return {"tasks": []}
        with _Loop(self.gw, polls=[slept_then_dead, ok], alive=[True, True, False],
                   limit=100.0) as loop:
            self.assertIsNone(loop.run(), "waking from sleep is not a stall")
        self.assertEqual(self._stall_logs(loop), [])

    # ── no restart owner ─────────────────────────────────────────────────
    def test_an_unsupervised_lane_logs_the_stall_but_keeps_retrying(self):
        def dead(clock):
            clock.advance(40.0)
            return urllib.error.URLError("no route")
        with _Loop(self.gw, polls=[dead] * 6, alive=[True] * 6 + [False], limit=100.0,
                   owner="") as loop:
            self.assertIsNone(loop.run(), "nothing would restart it; it must stay")
        self.assertEqual(self._stall_logs(loop), [])
        self.assertTrue(any("no restart owner" in m for m in loop.logs), loop.logs)


    # ── a send still in flight when the stall trips ──────────────────────
    def test_a_busy_worker_holds_the_exit_and_polling_never_resumes(self):
        polled_at_stop = []

        def dead(clock):
            clock.advance(40.0)
            return urllib.error.URLError("no route")

        def ok(clock):
            clock.advance(1.0)
            return {"tasks": []}
        # The relay is back by the time the stop is requested; it must not matter.
        with _Loop(self.gw, polls=[dead] * 3 + [ok] * 10, alive=[True] * 20, limit=100.0,
                   busy_joins=2) as loop:
            orig = self.gw._stop_background_workers

            def stop(*a):
                polled_at_stop.append(loop.poll_count)
                return orig(*a)
            self.gw._stop_background_workers = stop
            try:
                exc = loop.run()
            finally:
                self.gw._stop_background_workers = orig
        self.assertIsNotNone(exc, "the held exit must still happen once the worker stops")
        self.assertEqual(exc.code, self.gw.POLL_STALL_EXIT_CODE)
        self.assertEqual(loop.outbound.joins, 3, "one join per held pass, then the exit")
        self.assertEqual(len(set(polled_at_stop)), 1,
                         f"polled between stop attempts: {polled_at_stop}")
        self.assertEqual(sum("stall exit held" in m for m in loop.logs), 2, loop.logs)
        self.assertEqual(len(self._stall_logs(loop)), 1)

    def test_a_lost_lock_waits_for_a_busy_worker_before_returning(self):
        with _Loop(self.gw, polls=[], alive=[True, False, True, True, True],
                   busy_joins=1) as loop:
            self.assertIsNone(loop.run())
            polls = loop.poll_count
        self.assertEqual(loop.outbound.joins, 2, "returned with the worker still sending")
        self.assertEqual(polls, 1, "polled again after losing the lock")
        self.assertTrue(any("still finishing a send" in m for m in loop.logs), loop.logs)

if __name__ == "__main__":
    unittest.main(verbosity=2)
