#!/usr/bin/env python3
"""A held long poll is the documented empty poll — until the grace expires.

Drives the real poll loop for one iteration. The relay's contract is
`200 {"tasks": []}` when the hold window expires; when it instead lets the
client's read timeout fire, the two are indistinguishable, so classifying the
timeout as a network error backed a healthy bridge off and wrote
`connected: false` while tasks were still arriving.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
_PKG = _REPO / "packages" / "ag2-sparrow"
if str(_PKG) not in sys.path:
    sys.path.insert(0, str(_PKG))


class _Clock:
    """Stands in for the module's `time`, so the grace can expire without waiting."""

    def __init__(self):
        self.now = 1_000_000.0
        self.slept: list[float] = []

    def time(self) -> float:
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
              "_reconcile_abandoned", "_emit_gateway_status", "_save_inflight", "_log")

    def __init__(self, gw, poll_raises=None, advance=0.0):
        self.gw, self.poll_raises, self.advance = gw, poll_raises, advance
        self.clock = _Clock()
        self.status: list[tuple] = []
        self.logs: list[str] = []
        self._saved: dict = {}
        self._alive = [True, False]

    def _poll(self, method, path, payload=None, **kw):
        if path.startswith("/v1/tasks?wait="):
            self.clock.now += self.advance
            if self.poll_raises is not None:
                raise self.poll_raises
            return {"tasks": []}
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
                     "_post_proactive"):
            setattr(gw, noop, lambda *a, **k: None)
        gw._save_inflight = lambda *a, **k: None
        gw._write_task = lambda *a, **k: None
        gw._reconcile_abandoned = lambda inflight, s, *a, **k: s
        gw._req = self._poll
        gw._emit_gateway_status = lambda connected, **k: self.status.append((connected, k))
        gw._log = lambda m: self.logs.append(str(m))
        return self

    def __exit__(self, *exc):
        for n, v in self._saved.items():
            setattr(self.gw, n, v)
        return False

    def run(self):
        self.gw.main()
        return self


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
        self.assertIn((True, {}), loop.status,
                      "the iteration completes, so the bridge reports connected")

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
        self.assertIn((True, {}), loop.status)


if __name__ == "__main__":
    unittest.main(verbosity=2)
