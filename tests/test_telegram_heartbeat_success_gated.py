"""Regression spec for PR #395 (telegram-bridge: success-gated heartbeat).

The bug: the heartbeat was written at the top of the poll loop, before the
`api("getUpdates")` call. A loop tick that failed the API call still bumped
the heartbeat and then `continue`d — so a sustained outage looked healthy
to `health-check.py`'s 120s staleness threshold forever.

Real incident (2026-04-16, Mac Mini): PID 17372 etime=4d20h, log mtime 32h
old, 48 consecutive DNS-resolution errors followed by silence. Heartbeat
file was fresh the whole time. DNS had recovered but the bridge was stuck.

The fix: write the heartbeat only *after* `api(...)` returned a response
Telegram accepted (`result.get("ok") == True`). A bumped heartbeat now
means "the Telegram round-trip is working."

Why the replica pattern
-----------------------
`src/telegram-bridge.py`:
    - Reads `TELEGRAM_BOT_TOKEN` at module load and exits if missing.
    - The heartbeat decision is inlined in `main()`'s `while True` loop.
Importing it for a unit test would require mocking the env var and
refactoring the loop into a helper. Replica-style keeps the test tiny
and the invariants readable.

Run:  python3 -m unittest tests.test_telegram_heartbeat_success_gated
"""
import unittest


HEARTBEAT_INTERVAL_SECONDS = 60


def should_advance_heartbeat(api_result, now: float, last_heartbeat: float) -> bool:
    """Replica of the gate inside telegram-bridge.main()'s poll loop.

    `api_result` is the dict returned by `api()` (or None if `api()` raised —
    the source handles that via try/except + `continue` before the gate).
    """
    if api_result is None:
        return False
    if not api_result.get("ok"):
        return False
    if now - last_heartbeat < HEARTBEAT_INTERVAL_SECONDS:
        return False
    return True


class HeartbeatGateTest(unittest.TestCase):
    # --- #395 fix: failed API calls must NOT bump the heartbeat ------------
    # This is the core regression — the 32h zombie happened because
    # failures still bumped.
    def test_api_exception_does_not_advance(self):
        self.assertFalse(should_advance_heartbeat(None, now=1000.0, last_heartbeat=0.0))

    def test_api_not_ok_does_not_advance(self):
        # Telegram returned but refused (e.g. auth, rate limit, 5xx wrapped
        # by api() into {"ok": False}).
        self.assertFalse(
            should_advance_heartbeat({"ok": False, "description": "Unauthorized"},
                                     now=1000.0, last_heartbeat=0.0)
        )

    # --- Rate limit: don't bump more than once per HEARTBEAT_INTERVAL ------
    # Preserves the original throttle. Prevents a healthy bridge from
    # writing the heartbeat file every tick (~10s) and burning fsync.
    def test_success_within_interval_does_not_advance(self):
        self.assertFalse(
            should_advance_heartbeat({"ok": True, "result": []},
                                     now=100.0, last_heartbeat=50.0)
        )

    def test_success_at_interval_boundary_advances(self):
        # Source uses `>= 60`, so exactly 60s since last IS a bump.
        # Replica mirrors this: `< INTERVAL` skips, equal falls through to True.
        self.assertTrue(
            should_advance_heartbeat({"ok": True, "result": []},
                                     now=60.0, last_heartbeat=0.0)
        )

    # --- Happy path: successful API response past interval advances --------
    def test_success_past_interval_advances(self):
        self.assertTrue(
            should_advance_heartbeat({"ok": True, "result": [{"update_id": 1}]},
                                     now=200.0, last_heartbeat=100.0)
        )

    def test_success_with_empty_result_still_advances(self):
        # Long-poll timeout is the common case: ok=True but result=[].
        # Must still advance — otherwise a quiet channel looks zombie.
        self.assertTrue(
            should_advance_heartbeat({"ok": True, "result": []},
                                     now=1000.0, last_heartbeat=900.0)
        )

    # --- Failure-then-recovery: heartbeat resumes on first post-recovery ok
    # Ensures a transient outage doesn't permanently stop the heartbeat.
    def test_recovery_advances_on_first_ok_past_interval(self):
        # Simulate: last successful bump at t=0, then 100s of failures,
        # then API recovers at t=100. First successful call should bump.
        self.assertTrue(
            should_advance_heartbeat({"ok": True, "result": []},
                                     now=100.0, last_heartbeat=0.0)
        )


if __name__ == "__main__":
    unittest.main()
