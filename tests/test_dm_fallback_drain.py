"""Regression spec for PR #394 (fix(discord-bridge): drain poll_dm_fallback on empty + stale files).

The bug: `poll_dm_fallback` in `src/discord-bridge.py` only `unlink`ed on a
successful Discord send, so any file that Discord permanently rejected
(empty body → HTTP 400, stale channel → 403) stayed in `results/` and got
retried every 30s forever. On Mac Mini this produced ~45k `HTTP 400` log
lines across 28 zero-byte task files from Apr 11–12, and the retry storm
eventually starved the gateway event loop for ~2 days.

The fix (2026-04-16) adds two drops past the 90s grace window:
    - `st.st_size == 0` → drop (Discord never accepts empty bodies)
    - `age > 86400` → drop (bound the retry window to 24h)

This file locks the decision table for that drain logic.

Why the replica pattern
-----------------------
`src/discord-bridge.py` is not cleanly importable from a test context:
    - Module top-level calls `exit(1)` if `DISCORD_BOT_TOKEN` is missing.
    - Module top-level instantiates a `discord.Client` and creates dirs.
    - The drain decision is inlined in an `async for` loop, not a helper.

Rather than wedge test setup around those side-effects, we replicate the
drain-decision truth table here and assert the invariants. If the source
drain logic diverges from this table, the real bridge behavior has changed
and this file should be updated in the same PR (or the source refactored
to expose a `_should_drop` helper the test can import directly).

Run:  python3 -m unittest tests.test_dm_fallback_drain
"""
import unittest


GRACE_SECONDS = 90
MAX_RETRY_AGE_SECONDS = 86400  # 24h


def drain_decision(st_size: int, age: float) -> str:
    """Replica of the control flow inside poll_dm_fallback's per-file loop.

    Returns one of: 'skip-grace', 'drop-empty', 'drop-stale', 'send'.
    """
    if age < GRACE_SECONDS:
        return "skip-grace"
    if st_size == 0:
        return "drop-empty"
    if age > MAX_RETRY_AGE_SECONDS:
        return "drop-stale"
    return "send"


class DrainDecisionTest(unittest.TestCase):
    def test_grace_skips_even_when_empty(self):
        self.assertEqual(drain_decision(0, age=30), "skip-grace")

    def test_grace_skips_even_when_would_be_stale(self):
        self.assertEqual(drain_decision(100, age=30), "skip-grace")

    def test_past_grace_empty_is_dropped(self):
        self.assertEqual(drain_decision(0, age=GRACE_SECONDS + 1), "drop-empty")

    def test_empty_dropped_well_before_24h_cap(self):
        self.assertEqual(drain_decision(0, age=3600), "drop-empty")

    def test_stale_nonempty_is_dropped(self):
        self.assertEqual(drain_decision(500, age=MAX_RETRY_AGE_SECONDS + 1), "drop-stale")

    def test_valid_is_sent(self):
        self.assertEqual(drain_decision(500, age=3600), "send")

    def test_valid_at_24h_boundary_is_sent(self):
        self.assertEqual(drain_decision(500, age=MAX_RETRY_AGE_SECONDS), "send")

    def test_empty_and_stale_both_match_empty_wins(self):
        self.assertEqual(
            drain_decision(0, age=MAX_RETRY_AGE_SECONDS + 10),
            "drop-empty",
        )


if __name__ == "__main__":
    unittest.main()
