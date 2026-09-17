"""_rows_equal() must not treat a differently-typed numeric route value as
identical to a valid one, or the collision branch that would otherwise
retain the losing row under a suffix never runs (keweichen, #4047 review).

Measured against production roster_union(): a local `discord_id: 42.0`
(float, invalid per _names_route's int-not-bool rule) compared equal to a
peer's valid `discord_id: 42` (int), so the peer's real route was discarded
outright -- not even kept under `<key>@<host>`.
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


class RowsEqualIsTypeSensitiveForNumericRouteFields(unittest.TestCase):
    def setUp(self):
        self.m = _load(SRC)

    def test_float_vs_int_discord_id_are_not_equal_rows(self):
        self.assertFalse(self.m._rows_equal(
            {"discord_id": 42.0}, {"discord_id": 42}))

    def test_bool_vs_int_stand_discord_id_are_not_equal_rows(self):
        self.assertFalse(self.m._rows_equal(
            {"stand_discord_id": True}, {"stand_discord_id": 1}))

    def test_same_type_equal_numeric_route_value_is_still_equal(self):
        """Positive control: the fix must not make every numeric field diverge."""
        self.assertTrue(self.m._rows_equal(
            {"discord_id": 42, "home_channel": "!c:ag2.space"},
            {"discord_id": 42, "home_channel": "!c:ag2.space"}))

    def test_non_route_numeric_field_is_unaffected(self):
        """Only NUMERIC_ROUTE_FIELDS get the stricter check -- an ordinary
        int/float field elsewhere in a row keeps Python's normal equality."""
        self.assertTrue(self.m._rows_equal(
            {"priority": 1.0}, {"priority": 1}))


class TheLosingRowIsRetainedNotDropped(unittest.TestCase):
    """The point of fixing equality: roster_union()'s collision branch must
    now actually run, so the peer's valid route survives under a suffix
    instead of being discarded as "identical"."""

    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.local = pathlib.Path(self.d, "local.json")
        self.peer = pathlib.Path(self.d, "peer.json")
        self.m = _load(SRC)

    def _union(self, local_row, peer_row):
        self.local.write_text(json.dumps({"reviewer": local_row}))
        self.peer.write_text(json.dumps({"reviewer": peer_row}))
        return self.m.roster_union([("local", self.local), ("peer", self.peer)])

    def test_float_local_vs_int_peer_discord_id_keeps_the_valid_peer_route(self):
        # Same keys on both rows -- differing only in discord_id's type.
        home = "!c:ag2.space"
        local_row = {"discord_id": 42.0, "home_channel": home}
        u = self._union(local_row, {"discord_id": 42, "home_channel": home})
        self.assertEqual(u["reviewer"].get("discord_id"), 42)
        self.assertEqual(self.m.declared_routes(u["reviewer"]), ("discord",))
        # THE POINT: the losing (local) row is retained under a suffix, not dropped.
        self.assertIn("reviewer@local", u)
        self.assertEqual(u["reviewer@local"], local_row)

    def test_bool_local_vs_int_peer_stand_discord_id_keeps_the_valid_peer_route(self):
        # 1, not another int: True == 1 is the exact collision naive `==` misses.
        home = "!c:ag2.space"
        local_row = {"stand_discord_id": True, "home_channel": home}
        u = self._union(local_row, {"stand_discord_id": 1, "home_channel": home})
        self.assertEqual(u["reviewer"].get("stand_discord_id"), 1)
        self.assertEqual(self.m.declared_routes(u["reviewer"]), ("discord",))
        self.assertIn("reviewer@local", u)
        self.assertEqual(u["reviewer@local"], local_row)


if __name__ == "__main__":
    unittest.main()
