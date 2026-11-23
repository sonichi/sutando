#!/usr/bin/env python3
"""Contract of `proactive_routing.proactive_route`: the routed channel and the
reason for it, and its agreement with `should_claim_proactive` on every shape
of `state/last-owner-activity.json` (#5076: nothing said where proactive
messages were going, so a quiet bridge read as a stalled one)."""

import json
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

from proactive_routing import (  # noqa: E402
    BRIDGE_CHANNELS, DEFAULT_PROACTIVE_CHANNEL, proactive_route, should_claim_proactive)

NOW = 1_790_000_000.0

# (content, expected channel, expected reason); None = file absent, str = verbatim.
CASES = [
    (None, "discord", "missing"),
    ("{not json", "discord", "unreadable"),
    ("[1, 2]", "discord", "malformed"),
    ({"ts": NOW - 60}, "discord", "no-channel"),
    ({"channel": "", "ts": NOW - 60}, "discord", "no-channel"),
    ({"channel": 7, "ts": NOW - 60}, "discord", "no-channel"),
    ({"channel": "voice", "ts": NOW - 60}, "discord", "non-bridge"),
    ({"channel": "discord", "ts": NOW - 60}, "discord", "last-activity"),
    ({"channel": "telegram", "ts": NOW - 60}, "telegram", "last-activity"),
    ({"channel": "slack", "ts": NOW - 60}, "slack", "last-activity"),
    ({"channel": "ag2space", "ts": NOW - 60}, "ag2space", "last-activity"),
]


class ProactiveRouteTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.state = Path(self._tmp.name) / "last-owner-activity.json"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _write(self, content) -> None:
        if self.state.exists():
            self.state.unlink()
        if content is None:
            return
        self.state.write_text(content if isinstance(content, str) else json.dumps(content))

    def test_every_shape_names_its_channel_and_reason(self) -> None:
        for content, channel, reason in CASES:
            with self.subTest(content=content):
                self._write(content)
                route = proactive_route(self.state, now=NOW)
                self.assertEqual((route["channel"], route["reason"]), (channel, reason))
                self.assertEqual(route["field"], "channel")

    def test_the_claim_decision_is_the_route(self) -> None:
        # One policy: exactly the routed bridge claims, for every shape.
        for content, _channel, _reason in CASES:
            self._write(content)
            routed = proactive_route(self.state)["channel"]
            for bridge in sorted(BRIDGE_CHANNELS):
                with self.subTest(content=content, bridge=bridge):
                    self.assertEqual(should_claim_proactive(self.state, bridge), bridge == routed)

    def test_age_comes_from_the_ts_field(self) -> None:
        self._write({"channel": "discord", "ts": NOW - 125})
        self.assertAlmostEqual(proactive_route(self.state, now=NOW)["age_s"], 125.0)

    def test_value_is_the_raw_field_for_a_non_bridge_surface(self) -> None:
        self._write({"channel": "github-commits", "ts": NOW})
        route = proactive_route(self.state, now=NOW)
        self.assertEqual(route["value"], "github-commits")
        self.assertEqual(route["channel"], DEFAULT_PROACTIVE_CHANNEL)

    def test_missing_or_unusable_ts_gives_no_age(self) -> None:
        for ts in (None, "yesterday", True):
            with self.subTest(ts=ts):
                body = {"channel": "slack"}
                if ts is not None:
                    body["ts"] = ts
                self._write(body)
                self.assertIsNone(proactive_route(self.state, now=NOW)["age_s"])
        self._write(None)
        self.assertIsNone(proactive_route(self.state, now=NOW)["age_s"])


if __name__ == "__main__":
    unittest.main()
