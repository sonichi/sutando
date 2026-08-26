#!/usr/bin/env python3
"""Instanced gateway lanes must never claim unaddressed proactive files —
those default to the owner's primary surface, served by the default lane.
Owner report 2026-08-26: the intermittent dev lane stole owner nudges."""
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO / "packages" / "ag2-sparrow"))

from ag2_sparrow import remote_gateway_bridge as rgb  # noqa: E402


class InstanceScope(unittest.TestCase):
    def tearDown(self):
        rgb.GATEWAY_INSTANCE = ""
        rgb.GATEWAY_ROOM_SUFFIX = ""

    def test_default_lane_claims_everything(self):
        rgb.GATEWAY_INSTANCE = ""
        self.assertTrue(rgb._instance_may_claim(None))
        self.assertTrue(rgb._instance_may_claim("!r:ag2.space"))

    def test_instanced_lane_never_claims_unaddressed(self):
        rgb.GATEWAY_INSTANCE = "dev"
        self.assertFalse(rgb._instance_may_claim(None))

    def test_instanced_lane_claims_addressed(self):
        rgb.GATEWAY_INSTANCE = "dev"
        self.assertTrue(rgb._instance_may_claim("!r:dev.ag2.space"))

    def test_suffix_fence_excludes_other_servers(self):
        rgb.GATEWAY_INSTANCE = "dev"
        rgb.GATEWAY_ROOM_SUFFIX = ":dev.ag2.space"
        self.assertTrue(rgb._instance_may_claim("!r:dev.ag2.space"))
        self.assertFalse(rgb._instance_may_claim("!r:ag2.space"))
        self.assertFalse(rgb._instance_may_claim(None))

    def test_claim_loop_calls_the_predicate(self):
        # wiring pin: the pre-claim gate must consult the predicate
        import inspect
        src = inspect.getsource(rgb)
        self.assertIn("if not _instance_may_claim(peek_room):", src)
        self.assertIn("not _instance_may_claim(room_override)", src)


if __name__ == "__main__":
    unittest.main(verbosity=1)
