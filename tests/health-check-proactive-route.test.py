#!/usr/bin/env python3
"""`check_proactive_route`: health-check names the bridge proactive messages
are routed to, so a quiet outbox on another bridge reads as routing, not a
stall (#5076), and warns only when the routed bridge itself is unhealthy."""

from __future__ import annotations

from _helpers.hermetic_env import isolate_os_environ

# Before anything reads the environment: no outer credential selector survives.
isolate_os_environ()

import importlib.util
import inspect
import json
import tempfile
import time
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

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

    def test_probe_is_registered_after_the_bridge_rows(self) -> None:
        src = inspect.getsource(hc.run_all_checks)
        self.assertTrue("checks.append(check_proactive_route(checks))" in src,
                        "check_proactive_route() is never appended to `checks`")
        self.assertLess(src.index("check_gateway_bridge()"),
                        src.index("check_proactive_route(checks)"))


if __name__ == "__main__":
    unittest.main()
