"""Route classification has ONE owner; the union must not decide alone.

The union and notify_reviewers both read a roster row and answer "what route is
this?". While the union recognised only Matrix, a local row naming a complete
Discord route read as a PLACEHOLDER to it and as a USABLE ROUTE to the notifier,
so a synced peer's Matrix row promoted over it and changed the destination.

Three arms FAIL at the parent commit; the rest pass there and must keep passing.
The fix widens what counts as a route, it does not reorder precedence, and it
leaves a lone Discord id promotable — that asymmetry is pinned here, not assumed.

The composed case is the one a single-side unit test cannot show. It is asserted
on the union's own output first, then on `declared_routes` — what the notifier
selects its transport from once it delegates here.
"""
import importlib.util
import json
import pathlib
import tempfile
import unittest

SRC = (pathlib.Path(__file__).resolve().parents[1] /
       "skills" / "collaboration-intelligence" / "scripts" / "roster_union.py")


def _load(p):
    s = importlib.util.spec_from_file_location("ru", p)
    m = importlib.util.module_from_spec(s)
    s.loader.exec_module(m)
    return m


# Measured, not invented: the shapes the composed notifier routed on. `donly` is
# the control row — same local fields, no peer — and must resolve identically.
LOCAL_DISCORD = {"discord_id": "D2", "home_channel": "C2", "human": "@shadow"}
PEER_MATRIX = {"stand": "@peer:ag2.space", "room": "!peer:ag2.space"}
LOCAL_NULL = {"stand": None, "room": None, "discord": None}


class RouteClassificationHasOneOwner(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.m = _load(SRC)

    def _union(self, local, peer):
        lp = pathlib.Path(self.d, "local.json")
        pp = pathlib.Path(self.d, "peer.json")
        lp.write_text(json.dumps(local))
        pp.write_text(json.dumps(peer))
        return self.m.roster_union([("local", lp), ("zpeer", pp)])

    def test_a_local_discord_route_is_not_outranked_by_a_synced_matrix_peer(self):
        u = self._union({"shadow": LOCAL_DISCORD}, {"shadow": PEER_MATRIX})
        row = u["shadow"]
        self.assertEqual(
            row.get("discord_id"), "D2",
            "the local Discord route lost the bare key to a synced peer; it is "
            f"only reachable as 'shadow@local'. union keys: {sorted(u)}")
        self.assertIsNone(
            row.get("stand"),
            "the peer's Stand was promoted over a local row that already named "
            f"a complete route. row={row}")

    def test_a_synced_peer_does_not_change_the_transport_the_row_implies(self):
        """The composed case: the same local row, with and without a peer sync.

        Asserted on the union's own output first, so this arm FAILS at the
        parent rather than erroring on an attribute that is not there yet.
        """
        alone = self._union({"donly": LOCAL_DISCORD}, {})["donly"]
        synced = self._union({"donly": LOCAL_DISCORD},
                             {"donly": PEER_MATRIX})["donly"]
        self.assertEqual(
            synced, alone,
            "a peer that synced a Matrix row changed the route fields this row "
            f"carries: alone={alone} synced={synced}")
        self.assertEqual(self.m.declared_routes(synced)[:1], ("discord",))

    def test_a_genuine_matrix_only_row_still_routes_to_matrix(self):
        """The other direction, asserted so it PASSES at the parent too: making
        Discord a route must not let a Discord peer displace a Matrix row."""
        mx = {"stand": "@sutando-mx:ag2.space", "room": "!mx:ag2.space"}
        u = self._union({"mxonly": mx}, {"mxonly": LOCAL_DISCORD})
        self.assertEqual(u["mxonly"], mx,
                         f"a Discord peer displaced a Matrix-only row: {u}")
        self.assertTrue(self.m._usable(mx))

    def test_the_owner_classifies_a_matrix_only_row_as_matrix(self):
        mx = {"stand": "@sutando-mx:ag2.space", "room": "!mx:ag2.space"}
        self.assertEqual(self.m.declared_routes(mx), ("matrix",))

    def test_matrix_is_preferred_when_a_row_names_both_routes(self):
        """Preference order is the owner's, and it is the order it always was."""
        both = dict(PEER_MATRIX, **LOCAL_DISCORD)
        self.assertEqual(self.m.declared_routes(both), ("matrix", "discord"))

    def test_a_null_filled_local_row_still_loses_to_a_usable_peer(self):
        u = self._union({"q": LOCAL_NULL}, {"q": PEER_MATRIX})
        self.assertEqual(u["q"].get("stand"), PEER_MATRIX["stand"])
        self.assertEqual(u["q@local"], LOCAL_NULL)

    def test_promotion_keeps_peer_routing_when_a_local_field_spells_it_false(self):
        """`false` names no route, so the row is a placeholder — but it IS a
        declared value, and overlaying it would blank the peer's route."""
        u = self._union({"q": {"stand": False, "discord_id": False}},
                        {"q": PEER_MATRIX})
        self.assertEqual(u["q"].get("stand"), PEER_MATRIX["stand"],
                         f"a `false` local field overwrote peer routing: {u['q']}")
        self.assertEqual(self.m.declared_routes(u["q"]), ("matrix",))

    def test_promotion_does_not_let_a_false_local_field_blank_a_peer_route(self):
        """`_promote` must exclude EVERY routing field, not just the Matrix pair:
        a declared-but-empty local `discord_id` would erase the peer's."""
        peer = dict(PEER_MATRIX, discord_id="D", home_channel="C")
        u = self._union({"q": {"stand": None, "discord_id": False,
                               "home_channel": False}}, {"q": peer})
        self.assertEqual(self.m.declared_routes(u["q"]), ("matrix", "discord"),
                         f"a `false` local route field blanked the peer's: {u['q']}")

    def test_origin_still_wins_when_both_rows_are_usable(self):
        u = self._union({"q": LOCAL_DISCORD}, {"q": PEER_MATRIX})
        self.assertEqual(u["q"], LOCAL_DISCORD)
        self.assertEqual(u["q@zpeer"], PEER_MATRIX)

    def test_a_refusal_row_is_still_never_overwritten(self):
        refusal = {"stand": "", "refusal_basis": "asked not to be pinged"}
        u = self._union({"q": refusal}, {"q": PEER_MATRIX})
        self.assertEqual(u["q"], refusal)

    def test_the_route_contract_covers_every_spelling_the_notifier_passes(self):
        """`stand_discord_id` and an integer id are both live roster spellings."""
        d = self.m.declared_routes
        self.assertEqual(d({"stand_discord_id": 42, "home_channel": "C"}),
                         ("discord",))
        self.assertEqual(d({"discord_id": 42, "home_channel": "C"}), ("discord",))
        self.assertEqual(d({"discord_id": "D"}), ())
        self.assertEqual(d({"home_channel": "C"}), ())
        self.assertEqual(d({"stand": "@s:x"}), ())
        self.assertEqual(d({"stand": "@s:x", "room": "   "}), ())
        self.assertEqual(d(None), ())
        self.assertIn("home_channel", self.m.routing_fields())
        self.assertIn("stand", self.m.routing_fields())

    def test_a_partial_stand_still_blocks_promotion(self):
        """Unchanged: a row naming a Stand but no room states a Matrix identity,
        and promoting over it would address the person as a different Stand."""
        self.assertTrue(self.m.states_routing({"stand": "@s:x"}, ("matrix",)))
        self.assertFalse(self.m.states_routing({"stand": "  ", "room": None}))
        u = self._union({"q": {"stand": "@s:x"}}, {"q": PEER_MATRIX})
        self.assertEqual(u["q"], {"stand": "@s:x"})

    def test_a_lone_discord_id_is_still_promotable(self):
        """Deliberately NOT symmetric, and the asymmetry is now written down: a
        lone id names no route, so a peer's Stand is the only way to reach them."""
        self.assertTrue(self.m.states_routing({"discord_id": "D"}))
        self.assertFalse(self.m.states_routing({"discord_id": "D"}, ("matrix",)))
        u = self._union({"q": {"discord_id": "D"}}, {"q": PEER_MATRIX})
        self.assertEqual(u["q"].get("room"), PEER_MATRIX["room"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
