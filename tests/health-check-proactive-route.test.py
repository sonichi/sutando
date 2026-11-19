#!/usr/bin/env python3
"""`check_proactive_route`: health-check names the bridge activity-routing
DEFAULTS proactive messages to, so a quiet outbox on another bridge usually
reads as routing, not a stall, and warns only when the routed bridge
itself is unhealthy.

The row reports the `proactive_routing.proactive_route` default-policy
LAYER only — it must not read as the final claim decision. The real
per-file claim (`claims_unless_routed_elsewhere`) keys on a recorded
bridge-channel activity first, which claims exclusively; only with no such
record does a lone configured bridge claim its own file regardless of the
default, and, where one is configured, an eligible gateway bridge
separately claims a stopped bridge's file after its own grace window."""

from __future__ import annotations

from _helpers.hermetic_env import isolate_os_environ

# Before anything reads the environment: no outer credential selector survives.
isolate_os_environ()

import importlib.util
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

from proactive_routing import claims_unless_routed_elsewhere  # noqa: E402

spec = importlib.util.spec_from_file_location(
    "health_check", REPO / "src" / "health-check.py"
)
hc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(hc)


class ProactiveRouteCheckTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self._tmp.name)
        (self.ws / "state").mkdir()
        self._saved = hc.WORKSPACE_DIR
        hc.WORKSPACE_DIR = self.ws

    def tearDown(self) -> None:
        hc.WORKSPACE_DIR = self._saved
        self._tmp.cleanup()

    def _activity(self, channel: str, age_s: int = 600) -> None:
        (self.ws / "state" / "last-owner-activity.json").write_text(
            json.dumps({"ts": int(time.time()) - age_s, "channel": channel, "summary": "hi"}))

    def test_routed_to_discord_while_ag2space_is_quiet_is_not_a_stall(self) -> None:
        self._activity("discord", age_s=7200)
        # The gateway is up but has claimed nothing in hours: that is routing.
        rows = [{"name": "discord-bridge", "status": "ok", "detail": "running"},
                {"name": "gateway-bridge", "status": "ok", "detail": "running + connected"}]
        verdict = hc.check_proactive_route(rows)
        self.assertEqual(verdict["status"], "ok")
        self.assertIn("routed to discord", verdict["detail"])
        self.assertIn("last owner activity was on discord, 2.0h ago", verdict["detail"])
        self.assertNotIn("stall", verdict["detail"])
        self.assertNotIn("ag2space", verdict["detail"])

    def test_routed_bridge_unhealthy_warns(self) -> None:
        self._activity("ag2space")
        rows = [{"name": "discord-bridge", "status": "ok", "detail": "running"},
                {"name": "gateway-bridge", "status": "warn", "detail": "NOT serving"}]
        verdict = hc.check_proactive_route(rows)
        self.assertEqual(verdict["status"], "warn")
        self.assertIn("routed to ag2space", verdict["detail"])
        self.assertIn("gateway-bridge is warn", verdict["detail"])

    def test_another_bridge_unhealthy_does_not_warn(self) -> None:
        self._activity("discord")
        rows = [{"name": "discord-bridge", "status": "ok", "detail": "running"},
                {"name": "telegram-bridge", "status": "fail", "detail": "held"}]
        self.assertEqual(hc.check_proactive_route(rows)["status"], "ok")

    def test_no_record_names_the_default_and_why(self) -> None:
        verdict = hc.check_proactive_route([])
        self.assertEqual(verdict["status"], "ok")
        self.assertIn("routed to discord (default: owner activity record missing", verdict["detail"])
        self.assertIn("discord-bridge is not checked on this host", verdict["detail"])

    def test_non_bridge_activity_names_the_surface(self) -> None:
        self._activity("voice")
        verdict = hc.check_proactive_route(
            [{"name": "discord-bridge", "status": "ok", "detail": "running"}])
        self.assertEqual(verdict["status"], "ok")
        self.assertIn("last owner activity was on voice", verdict["detail"])
        self.assertIn("routed to discord", verdict["detail"])

    def test_single_bridge_no_record_does_not_overclaim_exclusivity(self) -> None:
        """A fresh Slack-only install with no activity record.

        `proactive_route` names discord as the DEFAULT, but the real
        per-file decision (`claims_unless_routed_elsewhere`, what
        slack-bridge.py actually calls) lets the one configured bridge
        claim it anyway — the row must not say otherwise.
        """
        state = self.ws / "state" / "last-owner-activity.json"
        self.assertFalse(state.exists())
        self.assertTrue(claims_unless_routed_elsewhere(
            "proactive-1.txt", state, "slack", other_bridges_configured=False))
        verdict = hc.check_proactive_route(
            [{"name": "slack-bridge", "status": "ok", "detail": "running"}])
        self.assertEqual(verdict["status"], "ok")
        self.assertIn("routed to discord", verdict["detail"])
        self.assertIn("default-routing policy, not a final claim", verdict["detail"])
        self.assertIn("lone configured bridge", verdict["detail"])
        self.assertNotIn("other bridges do not claim", verdict["detail"])

    def test_recorded_other_bridge_does_not_extend_the_lone_bridge_exception(self) -> None:
        """A single-bridge (Slack) install whose activity record names a
        DIFFERENT bridge channel (Discord): the lone-bridge exception does
        not apply once a bridge channel is recorded, so the real claim
        decision stays False for Slack and the row's wording must not
        suggest Slack claims the file regardless.
        """
        self._activity("discord")
        state = self.ws / "state" / "last-owner-activity.json"
        self.assertFalse(claims_unless_routed_elsewhere(
            "proactive-1.txt", state, "slack", other_bridges_configured=False))
        verdict = hc.check_proactive_route(
            [{"name": "slack-bridge", "status": "ok", "detail": "running"}])
        self.assertEqual(verdict["status"], "ok")
        self.assertIn("routed to discord", verdict["detail"])
        self.assertNotIn("lone configured bridge", verdict["detail"])

    def test_stopped_bridge_wording_names_the_gateway_fallback(self) -> None:
        # A stopped routed bridge may still be delivered via the gateway's
        # grace-window fallback — exclusivity wording must not say otherwise.
        self._activity("telegram")
        verdict = hc.check_proactive_route(
            [{"name": "telegram-bridge", "status": "down", "detail": "not running"}])
        self.assertEqual(verdict["status"], "warn")
        self.assertIn("fallback grace window", verdict["detail"])

    def test_age_unknown_when_ts_is_missing(self) -> None:
        (self.ws / "state" / "last-owner-activity.json").write_text(
            json.dumps({"channel": "discord"}))  # no `ts` field at all
        verdict = hc.check_proactive_route([])
        self.assertIn("last owner activity was on discord, age unknown", verdict["detail"])

    def test_day_old_activity_is_formatted_in_days(self) -> None:
        self._activity("telegram", age_s=2 * 86400)
        verdict = hc.check_proactive_route(
            [{"name": "telegram-bridge", "status": "ok", "detail": "running"}])
        self.assertIn("last owner activity was on telegram, 2.0d ago", verdict["detail"])

    def test_import_error_warns_without_crashing(self) -> None:
        with mock.patch.dict(sys.modules, {"proactive_routing": None}):
            verdict = hc.check_proactive_route([])
        self.assertEqual(verdict["status"], "warn")
        self.assertIn("could not load proactive_routing", verdict["detail"])

    def test_probe_runs_after_the_bridge_rows_in_the_full_pipeline(self) -> None:
        """Behavioral: drive the real `run_all_checks()` end to end and
        assert on the actual returned row order, not source text. No bridge
        is configured in the hermetic test environment, so anchor on the
        other proactive rows that `check_proactive_route` is deliberately
        appended after, per the real call order in `run_all_checks`."""
        saved_repo = hc.REPO_DIR
        repo_dir = self.ws / "repo"
        (repo_dir / "src").mkdir(parents=True)
        (self.ws / "logs").mkdir(exist_ok=True)
        hc.REPO_DIR = repo_dir
        try:
            checks = hc.run_all_checks()
        finally:
            hc.REPO_DIR = saved_repo
        names = [c.get("name") for c in checks]
        self.assertIn("proactive-route", names)
        self.assertIn("stranded-destined-proactive", names)
        self.assertIn("stale-proactive-backlog", names)
        self.assertIn("task-watcher", names)
        self.assertLess(names.index("stranded-destined-proactive"), names.index("proactive-route"))
        self.assertLess(names.index("stale-proactive-backlog"), names.index("proactive-route"))
        self.assertLess(names.index("proactive-route"), names.index("task-watcher"))


class RouteAgeTextTest(unittest.TestCase):
    """Direct branch coverage for `_route_age_text`'s unknown-age and
    day-age formatting branches."""

    def test_none_and_negative_are_age_unknown(self) -> None:
        self.assertEqual(hc._route_age_text(None), "age unknown")
        self.assertEqual(hc._route_age_text(-5), "age unknown")

    def test_minutes_hours_and_days_format(self) -> None:
        self.assertEqual(hc._route_age_text(120), "2m ago")
        self.assertEqual(hc._route_age_text(7200), "2.0h ago")
        self.assertEqual(hc._route_age_text(2 * 86400), "2.0d ago")


if __name__ == "__main__":
    unittest.main()
