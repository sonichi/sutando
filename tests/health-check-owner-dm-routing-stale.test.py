#!/usr/bin/env python3
"""Tests for health-check.py's check_owner_dm_routing_stale (#5072).

Proactive messages route through the gateway's reported `owner_dm_room`
(remote_gateway_bridge.refresh_routing), which is treated as authoritative by
design (#3995) and never overridden locally. When that gateway field itself
goes stale — the owner moved to a new DM room and the gateway record never
catches up — nothing today notices. This probe does not fix the routing (that
needs a backend Action; see docs/architecture-boundaries.md "Shared adapter
policy" and the PR this ships with); it only surfaces the divergence between
`state/owner-routing.json` (what proactive sends target) and
`state/last-owner-activity.json` (where the owner was last seen on ag2.space),
once that divergence has stood for `min_age_sec`.

Run: python3 tests/health-check-owner-dm-routing-stale.test.py
"""
from __future__ import annotations

import importlib.util
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from _helpers.hermetic_env import isolate_os_environ  # noqa: E402

isolate_os_environ()

REPO = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("health_check", REPO / "src" / "health-check.py")
hc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(hc)


class TestOwnerDmRoutingStale(unittest.TestCase):
    def _run(self, td, **kw):
        with mock.patch.object(hc, "WORKSPACE_DIR", Path(td)), \
             mock.patch.object(hc, "_gateway_configured", return_value=True):
            return hc.check_owner_dm_routing_stale(**kw)

    def _routing(self, td, owner_dm):
        state = Path(td) / "state"
        state.mkdir(parents=True, exist_ok=True)
        (state / "owner-routing.json").write_text(json.dumps({
            "identity": "@agent:x", "gateway": "https://x", "binding": "b" * 64,
            "owner_dm": owner_dm, "private": True,
        }))

    def _activity(self, td, channel, channel_id, age_s, ts=None):
        state = Path(td) / "state"
        state.mkdir(parents=True, exist_ok=True)
        payload = {"ts": int(ts if ts is not None else time.time() - age_s),
                   "channel": channel, "channel_id": channel_id, "summary": "hi"}
        (state / "last-owner-activity.json").write_text(json.dumps(payload))

    def test_not_configured_is_none(self):
        with tempfile.TemporaryDirectory() as td:
            with mock.patch.object(hc, "WORKSPACE_DIR", Path(td)), \
                 mock.patch.object(hc, "_gateway_configured", return_value=False):
                self.assertIsNone(hc.check_owner_dm_routing_stale())

    def test_no_routing_reading_is_ok(self):
        with tempfile.TemporaryDirectory() as td:
            r = self._run(td)
            self.assertEqual(r["status"], "ok", r)
            self.assertIn("no owner DM routing reading yet", r["detail"])

    def test_no_activity_recorded_is_ok(self):
        with tempfile.TemporaryDirectory() as td:
            self._routing(td, "!dm:x")
            r = self._run(td)
            self.assertEqual(r["status"], "ok", r)
            self.assertIn("no owner activity recorded", r["detail"])

    def test_non_ag2space_activity_is_ignored(self):
        with tempfile.TemporaryDirectory() as td:
            self._routing(td, "!dm:x")
            self._activity(td, "discord", "12345", age_s=3 * 3600)
            r = self._run(td)
            self.assertEqual(r["status"], "ok", r)
            self.assertIn("not on ag2space", r["detail"])

    def test_matching_room_is_ok(self):
        with tempfile.TemporaryDirectory() as td:
            self._routing(td, "!dm:x")
            self._activity(td, "ag2space", "!dm:x", age_s=3 * 3600)
            r = self._run(td)
            self.assertEqual(r["status"], "ok", r)
            self.assertIn("matches proactive routing", r["detail"])

    def test_fresh_divergence_is_within_the_settle_window(self):
        """A single stray message seconds ago must not page — it may be a
        shared-room ping that the next owner message in the real DM clears."""
        with tempfile.TemporaryDirectory() as td:
            self._routing(td, "!old:x")
            self._activity(td, "ag2space", "!new:x", age_s=30)
            r = self._run(td, min_age_sec=1800)
            self.assertEqual(r["status"], "ok", r)
            self.assertIn("settle window", r["detail"])

    def test_sustained_divergence_warns_and_names_both_rooms(self):
        """The divergence itself, not the activity file's single ts, drives the
        clock: two passes straddling min_age_sec with the mismatch held constant."""
        with tempfile.TemporaryDirectory() as td:
            self._routing(td, "!old:x")
            self._activity(td, "ag2space", "!new:x", age_s=0)
            first = self._run(td, min_age_sec=1800, now=1_000_000.0)
            self.assertEqual(first["status"], "ok", first)
            later = self._run(td, min_age_sec=1800, now=1_000_000.0 + 1800)
            self.assertEqual(later["status"], "warn", later)
            # Assert roles, not mere presence: swapping the two room labels must fail this.
            self.assertIn("from !new:x", later["detail"])
            self.assertIn("routing to !old:x", later["detail"])
            self.assertNotIn("from !old:x", later["detail"])
            self.assertNotIn("routing to !new:x", later["detail"])

    def test_repeated_new_room_messages_eventually_warn(self):
        """Reviewer repro: the owner keeps actively messaging in the moved-to room,
        refreshing last-owner-activity.json's ts on every message, while the gateway's
        cached DM stays on the old room throughout. Age-of-last-message alone would
        stay under the settle window forever; age-of-divergence must not."""
        with tempfile.TemporaryDirectory() as td:
            self._routing(td, "!old:x")
            start = 2_000_000.0
            last = None
            # A message every 5 minutes for 2 hours — always "fresh" by ts, never
            # by divergence age once the settle window (30 min) is crossed.
            for minute in range(0, 121, 5):
                t = start + minute * 60
                self._activity(td, "ag2space", "!new:x", age_s=0, ts=t)
                last = self._run(td, min_age_sec=1800, now=t)
            self.assertEqual(last["status"], "warn", last)
            self.assertIn("from !new:x", last["detail"])
            self.assertIn("routing to !old:x", last["detail"])

    def test_cleared_mismatch_resets_the_clock(self):
        """Once the rooms match again, a later divergence (even to the same pair)
        starts a fresh clock instead of inheriting the old first_seen."""
        with tempfile.TemporaryDirectory() as td:
            self._routing(td, "!old:x")
            self._activity(td, "ag2space", "!new:x", age_s=0)
            self._run(td, min_age_sec=1800, now=5_000_000.0)
            self._activity(td, "ag2space", "!old:x", age_s=0)
            cleared = self._run(td, min_age_sec=1800, now=5_000_000.0 + 10)
            self.assertEqual(cleared["status"], "ok", cleared)
            self._activity(td, "ag2space", "!new:x", age_s=0)
            reobserved = self._run(td, min_age_sec=1800, now=5_000_000.0 + 20)
            self.assertEqual(reobserved["status"], "ok", reobserved)
            self.assertIn("settle window", reobserved["detail"])

    def test_activity_without_ts_field_does_not_crash(self):
        """The divergence clock lives in owner-dm-routing-stale.json's own
        first_seen, not in last-owner-activity.json's ts (that field is never
        read here); an activity record missing ts entirely must not crash."""
        with tempfile.TemporaryDirectory() as td:
            self._routing(td, "!old:x")
            state = Path(td) / "state"
            (state / "last-owner-activity.json").write_text(json.dumps(
                {"channel": "ag2space", "channel_id": "!new:x"}))
            r = self._run(td, min_age_sec=1800)
            self.assertEqual(r["status"], "ok", r)

    def test_malformed_state_file_is_treated_as_empty(self):
        """A state file holding valid JSON that isn't an object (e.g. a bare
        list) must not crash the probe; it is treated the same as no state,
        starting a fresh first_seen clock rather than reusing or crashing on
        the non-dict value."""
        with tempfile.TemporaryDirectory() as td:
            self._routing(td, "!old:x")
            self._activity(td, "ag2space", "!new:x", age_s=0)
            state = Path(td) / "state"
            state.mkdir(parents=True, exist_ok=True)
            (state / "owner-dm-routing-stale.json").write_text(json.dumps([1]))
            first = self._run(td, min_age_sec=1800, now=3_000_000.0)
            self.assertEqual(first["status"], "ok", first)
            self.assertIn("settle window", first["detail"])
            later = self._run(td, min_age_sec=1800, now=3_000_000.0 + 1800)
            self.assertEqual(later["status"], "warn", later)

    def test_state_write_failure_is_tolerated(self):
        """If persisting the divergence clock fails (e.g. disk full, a
        read-only state dir), the probe must not crash — it silently
        continues without the persisted clock, the same error-tolerance
        pattern as other optional-state writes in this file."""
        with tempfile.TemporaryDirectory() as td:
            self._routing(td, "!old:x")
            self._activity(td, "ag2space", "!new:x", age_s=0)
            with mock.patch("os.replace", side_effect=OSError("disk full")):
                r = self._run(td, min_age_sec=1800, now=4_000_000.0)
            self.assertEqual(r["status"], "ok", r)
            self.assertIn("settle window", r["detail"])

    def test_unreadable_activity_file_is_ok_not_a_crash(self):
        with tempfile.TemporaryDirectory() as td:
            self._routing(td, "!dm:x")
            state = Path(td) / "state"
            (state / "last-owner-activity.json").write_text("{not json")
            r = self._run(td)
            self.assertEqual(r["status"], "ok", r)

    def test_the_operators_real_workspace_is_never_touched(self):
        real = hc.WORKSPACE_DIR / "state" / "last-owner-activity.json"
        before = real.read_bytes() if real.is_file() else None
        with tempfile.TemporaryDirectory() as td:
            self._routing(td, "!dm:x")
            self._activity(td, "ag2space", "!other:x", age_s=7200)
            self._run(td)
        after = real.read_bytes() if real.is_file() else None
        self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main(verbosity=2)
