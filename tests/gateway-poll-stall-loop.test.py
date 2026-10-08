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
    """Stands in for the module's `time`: the limit can expire without waiting."""

    def __init__(self):
        self.now = 1_000_000.0
        self.slept: list[float] = []

    def time(self) -> float:
        return self.now

    def sleep(self, s: float) -> None:
        self.slept.append(s)
        self.now += s

    # `_log` formats a stamp through these; the fake must still answer.
    def strftime(self, *a, **k):
        return "stamp"

    def gmtime(self, *a, **k):
        return None


class _Loop:
    """`main()` for as many iterations as `alive` has True entries; the first
    False singleton check ends it."""

    _PATCH = ("TOKEN", "URL", "time", "_acquire_singleton", "_load_inflight",
              "_recover_orphan_proactive", "_maybe_start_event_channel",
              "_heartbeat_singleton", "_post_heartbeat", "_req", "_write_task",
              "_post_task_ack", "_post_ready_results", "_post_proactive",
              "_reconcile_abandoned", "_emit_gateway_status", "_save_inflight", "_log",
              "_recover_auth", "POLL_STALL_EXIT_S", "POLL_STALL_RESTART_OWNER",
              "_STALL_REPORTED_FOR")

    def __init__(self, gw, *, polls, alive, limit=100.0, owner="launchd",
                 recover_auth=None):
        self.gw, self.limit, self.owner = gw, limit, owner
        self.polls = list(polls)          # per poll: a callable(clock) -> resp, or an exception
        self._alive = list(alive)
        self.recover_auth = recover_auth
        self.clock = _Clock()
        self.status: list[tuple] = []
        self.logs: list[str] = []
        self._saved: dict = {}

    def _poll(self, method, path, payload=None, **kw):
        if not path.startswith("/v1/tasks?wait="):
            return {}
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
        gw._acquire_singleton = lambda *a, **k: True
        gw._load_inflight = lambda *a, **k: set()
        gw._heartbeat_singleton = lambda *a, **k: self._alive.pop(0) if self._alive else False
        for noop in ("_recover_orphan_proactive", "_maybe_start_event_channel",
                     "_post_heartbeat", "_post_task_ack", "_post_ready_results",
                     "_post_proactive"):
            setattr(gw, noop, lambda *a, **k: None)
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
            self.gw._abort_if_poll_stalled = lambda last_ok: calls.append(last_ok)
            try:
                self.assertIsNone(loop.run())
            finally:
                self.gw._abort_if_poll_stalled = orig
        # Three singleton checks happened, so three beats, each guarded first.
        self.assertEqual(len(calls), 3, calls)

    # ── fresh ────────────────────────────────────────────────────────────
    def test_polls_that_keep_succeeding_never_trip_it(self):
        def ok(clock):
            clock.now += 30.0
            return {"tasks": []}
        with _Loop(self.gw, polls=[ok, ok, ok, ok], alive=[True] * 4 + [False],
                   limit=100.0) as loop:
            self.assertIsNone(loop.run(), "a serving bridge must keep running")
        self.assertEqual(self._stall_logs(loop), [])
        self.assertIn((True, {}), loop.status)

    # ── stale ────────────────────────────────────────────────────────────
    def test_polls_stale_past_the_limit_exit_nonzero_with_one_log_line(self):
        def dead(clock):
            # Each failed poll costs most of a beat; backoff sleeps add the rest.
            clock.now += 40.0
            return urllib.error.URLError("no route")
        with _Loop(self.gw, polls=[dead] * 10, alive=[True] * 10, limit=100.0) as loop:
            exc = loop.run()
        self.assertIsNotNone(exc, "a bridge whose polls stay stale must exit")
        self.assertNotEqual(exc.code, 0, "non-zero, or launchd's SuccessfulExit=false never fires")
        self.assertIn("no successful poll", str(exc.code))
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
            clock.now += 5 * 3600  # the owner took five hours to re-link
            return True
        with _Loop(self.gw, polls=[_http_401()], alive=[True, True, False], limit=100.0,
                   recover_auth=resumed) as loop:
            self.assertIsNone(loop.run(),
                              "the wait was on a human; resuming must not exit")
        self.assertEqual(self._stall_logs(loop), [])
        # The resumed iteration polled and reported connected.
        self.assertIn((True, {}), loop.status)

    # ── clock skew backwards ─────────────────────────────────────────────
    def test_a_clock_that_jumps_backwards_reads_as_fresh(self):
        def skewed(clock):
            clock.now -= 7 * 24 * 3600  # NTP yanked the clock a week back
            return {"tasks": []}
        with _Loop(self.gw, polls=[skewed, skewed], alive=[True, True, False],
                   limit=100.0) as loop:
            self.assertIsNone(loop.run())
        self.assertEqual(self._stall_logs(loop), [])
        self.assertFalse(self.gw._poll_stalled(1000.0, 1000.0 - 1e6, 100.0))

    # ── no restart owner ─────────────────────────────────────────────────
    def test_an_unsupervised_lane_logs_the_stall_but_keeps_retrying(self):
        def dead(clock):
            clock.now += 40.0
            return urllib.error.URLError("no route")
        with _Loop(self.gw, polls=[dead] * 6, alive=[True] * 6 + [False], limit=100.0,
                   owner="") as loop:
            self.assertIsNone(loop.run(), "nothing would restart it; it must stay")
        self.assertEqual(self._stall_logs(loop), [])
        self.assertTrue(any("no restart owner" in m for m in loop.logs), loop.logs)


if __name__ == "__main__":
    unittest.main(verbosity=2)
