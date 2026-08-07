#!/usr/bin/env python3
"""resolve(endpoint, mode) contract: same address, different lanes, different
transports — and the known remote-realtime gap stays loud, never silent."""
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

from agent_endpoint import (  # noqa: E402
    MODES, Route, UnsupportedLane, parse_endpoint, resolve,
)

DESCRIPTOR = {
    "workspace": "/ws",
    "runtimeSocket": "/run/sutando-runtime.sock",
    "call_tiers": [
        {"tier": "direct-tailnet", "url": "https://mac.tail.ts.net", "reachable": False},
        {"tier": "direct-lan", "url": "http://10.0.0.2:8080", "reachable": True},
    ],
}


class TestParse(unittest.TestCase):
    def test_scheme_and_bare_forms_normalize_identically(self):
        self.assertEqual(parse_endpoint("sutando://qingyun-001"), "qingyun-001")
        self.assertEqual(parse_endpoint("qingyun-001"), "qingyun-001")

    def test_junk_is_rejected(self):
        for bad in ("sutando://", "sutando://../x", "a b", "", "sutando://UPPER"):
            with self.assertRaises(ValueError):
                parse_endpoint(bad)


class TestSelfRoutes(unittest.TestCase):
    def test_durable_routes_to_the_task_filesystem(self):
        r = resolve("sutando://qingyun-001", "durable", DESCRIPTOR, self_id="qingyun-001")
        self.assertEqual(r, Route("filesystem", "/ws/tasks", "qingyun-001", "durable"))

    def test_local_control_and_realtime_route_to_the_uds(self):
        for mode in ("local-control", "realtime"):
            r = resolve("self", mode, DESCRIPTOR)
            self.assertEqual((r.transport, r.address), ("uds", "/run/sutando-runtime.sock"))

    def test_missing_descriptor_fields_fail_loud(self):
        with self.assertRaises(ValueError):
            resolve("self", "durable", {"runtimeSocket": "/s"})
        with self.assertRaises(ValueError):
            resolve("self", "local-control", {"workspace": "/ws"})


class TestRemoteRoutes(unittest.TestCase):
    def test_remote_durable_prefers_the_first_reachable_tier(self):
        r = resolve("sutando://wu-air", "durable", DESCRIPTOR, self_id="qingyun-001")
        self.assertEqual((r.transport, r.address), ("gateway", "http://10.0.0.2:8080"))

    def test_no_reachable_tier_fails_loud(self):
        dead = dict(DESCRIPTOR, call_tiers=[{"tier": "t", "url": "u", "reachable": False}])
        with self.assertRaises(ValueError):
            resolve("sutando://wu-air", "durable", dead, self_id="qingyun-001")

    def test_remote_realtime_is_a_loud_gap_not_a_silent_fallback(self):
        # The named gap from the model: no session gateway exists. This must
        # raise UnsupportedLane — if it ever returns a Route, the gateway got
        # built and this pin should be updated alongside it.
        for mode in ("realtime", "local-control"):
            with self.assertRaises(UnsupportedLane):
                resolve("sutando://wu-air", mode, DESCRIPTOR, self_id="qingyun-001")


class TestModeVocabulary(unittest.TestCase):
    def test_unknown_mode_is_rejected(self):
        with self.assertRaises(ValueError):
            resolve("self", "batch", DESCRIPTOR)

    def test_modes_are_the_models_lanes(self):
        self.assertEqual(MODES, ("durable", "realtime", "local-control"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
