#!/usr/bin/env python3
"""The VENDORED gateway sink must route Matrix aliases itself (no loader).

Package consumers import ag2_sparrow directly — the src loader's injected
classifier never runs there, so a '!'-only vendored rule strands every
alias-directed body: all bridges decline '#alias:server' as Matrix-owned
while the only Matrix claimant calls it foreign (kewei's #3305 blocker).

Run: python3 tests/sparrow-proactive-alias-route.test.py   (stdlib only)
"""
from __future__ import annotations

import importlib
import pathlib
import sys
import unittest

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "packages" / "ag2-sparrow"))
m = importlib.import_module("ag2_sparrow.remote_gateway_bridge")


class VendoredAliasRoute(unittest.TestCase):
    def test_alias_redirect_routes_send_not_foreign(self):
        route, room, body = m._proactive_route(
            "[channel: #general:ag2.space]\nalias-directed nudge")
        self.assertEqual((route, room), ("send", "#general:ag2.space"))
        self.assertIn("alias-directed nudge", body)

    def test_room_id_redirect_still_send(self):
        route, room, _ = m._proactive_route(
            "[channel: !room:ag2.space]\nhi")
        self.assertEqual((route, room), ("send", "!room:ag2.space"))

    def test_discord_target_still_foreign(self):
        route, room, _ = m._proactive_route(
            "[channel: 123456789012345678]\nhi")
        self.assertEqual((route, room), ("foreign", None))

    def test_vendored_rule_matches_shared_classifier(self):
        # the src classifier is policy owner; the vendored default must not
        # drift narrower than it on the Matrix families
        sys.path.insert(0, str(REPO / "src"))
        pr = importlib.import_module("proactive_routing")
        for dest in ("!r:s.org", "#a:s.org"):
            self.assertEqual(
                bool(m._MATRIX_ROOM_RE.match(dest)),
                bool(pr.MATRIX_TARGET_RE.match(dest)), dest)


if __name__ == "__main__":
    unittest.main(verbosity=2)
