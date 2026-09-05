#!/usr/bin/env python3
"""ONE Matrix target grammar: vendored sink == shared classifier, room IDs only.

Room-ID-only contract: the backend sends `room_id` verbatim into Matrix's
/rooms/{roomId}/send with no alias resolution, so `#alias:server` is NOT an
executable target — the sink must route it foreign (never claim what it cannot
deliver) instead of claiming, retrying and parking an undeliverable nudge.
Valid room ids may carry a server port or a bracketed IPv6 host (kewei P1):
the merge-base provider rule accepted both, so the shared rule must too, and
the two shipped entry points must agree on every family.

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


class VendoredMatrixTargetRule(unittest.TestCase):
    def test_room_id_redirect_routes_send(self):
        route, room, body = m._proactive_route(
            "[channel: !room:ag2.space]\nroom-directed nudge")
        self.assertEqual((route, room), ("send", "!room:ag2.space"))
        self.assertIn("room-directed nudge", body)

    def test_ported_and_ipv6_room_ids_route_send(self):
        # Matrix server names may carry an explicit port or a bracketed IPv6
        # host; the merge-base rule accepted these and they must not regress.
        for dest in ("!r:example.org:8448",
                     "!r:[1234:5678::abcd]",
                     "!r:[1234:5678::abcd]:5678"):
            route, room, _ = m._proactive_route(f"[channel: {dest}]\nhi")
            self.assertEqual((route, room), ("send", dest), dest)

    def test_alias_routes_foreign_not_send(self):
        # room-ID-only: the backend cannot resolve an alias, so claiming it
        # would retry-and-park a nudge that can never land
        route, room, _ = m._proactive_route(
            "[channel: #general:ag2.space]\nalias-directed nudge")
        self.assertEqual((route, room), ("foreign", None))

    def test_discord_target_still_foreign(self):
        route, room, _ = m._proactive_route(
            "[channel: 123456789012345678]\nhi")
        self.assertEqual((route, room), ("foreign", None))

    def test_foreign_tuple_carries_stripped_body(self):
        # a host whose FILENAME rule overrides the foreign call delivers this
        # body to the default room — the marker must already be stripped
        route, _, body = m._proactive_route(
            "[channel: 123456789012345678]\nconflict body")
        self.assertEqual(route, "foreign")
        self.assertIn("conflict body", body)
        self.assertNotIn("[channel:", body)

    def test_vendored_rule_matches_shared_classifier(self):
        # the src classifier is policy owner; the vendored default must not
        # drift from it on ANY family — ported and IPv6 forms included
        sys.path.insert(0, str(REPO / "src"))
        pr = importlib.import_module("proactive_routing")
        for dest in ("!r:s.org", "#a:s.org",
                     "!r:example.org:8448", "#a:example.org:8448",
                     "!r:[1234:5678::abcd]", "!r:[1234:5678::abcd]:5678",
                     "#a:[1234:5678::abcd]:5678",
                     "!r:s.org:notaport", "!r:s:8448:9"):
            self.assertEqual(
                bool(m._MATRIX_ROOM_RE.match(dest)),
                bool(pr.MATRIX_TARGET_RE.match(dest)), dest)

    def test_destined_here_default_is_no_override(self):
        # standalone default: no host filename rule, no foreign override
        self.assertIsNone(m.PROACTIVE_DESTINED_HERE)
        self.assertFalse(m._destined_here("proactive-1.to-ag2space.txt"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
