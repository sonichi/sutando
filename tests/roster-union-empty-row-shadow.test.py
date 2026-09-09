"""An all-null local row must not shadow a usable peer row in roster_union.

Built on the host where this reproduces. Arm 1 FAILS at the parent commit; arms 2-4 pass there and must keep passing,
because the fix is a CONDITIONAL tie-break, not a reordering.

The fixture is measured, not invented: the two rows below are the shapes actually
on disk here for `qingyun-wu` (local placeholder, peer complete).
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

LOCAL_PLACEHOLDER = {"stand": None, "room": None, "discord": None}
PEER_COMPLETE     = {"stand": "@sutando-qingyun-001:ag2.space", "room": "!r:ag2.space"}


class ShadowedByAnEmptyLocalRow(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.local = pathlib.Path(self.d, "local.json")
        self.peer  = pathlib.Path(self.d, "peer.json")
        self.local.write_text(json.dumps({"qingyun-wu": LOCAL_PLACEHOLDER}))
        self.peer.write_text(json.dumps({"qingyun-wu": PEER_COMPLETE}))
        self.m = _load(SRC)

    def test_a_usable_peer_row_is_not_shadowed_by_an_empty_local_one(self):
        u = self.m.roster_union([("local", self.local), ("peer", self.peer)])
        row = u["qingyun-wu"]
        self.assertTrue(row.get("stand") and row.get("room"),
            "the bare key resolves to the placeholder; the usable row is only "
            f"reachable as 'qingyun-wu@peer'. union keys: {sorted(u)}")

    def test_the_peer_row_is_still_retained_under_its_suffix(self):
        """The fix must not drop the losing row — that property is deliberate."""
        u = self.m.roster_union([("local", self.local), ("peer", self.peer)])
        self.assertTrue(any(k.startswith("qingyun-wu@") for k in u), sorted(u))

    def test_origin_order_still_decides_when_BOTH_rows_are_usable(self):
        """Completeness breaks the tie; it does not replace local-wins."""
        self.local.write_text(json.dumps({"k": {"stand": "@a:x", "room": "!a:x"}}))
        self.peer.write_text(json.dumps({"k": {"stand": "@b:x", "room": "!b:x"}}))
        u = self.m.roster_union([("local", self.local), ("peer", self.peer)])
        self.assertEqual(u["k"]["stand"], "@a:x")

    def test_origin_order_still_decides_when_BOTH_rows_are_empty(self):
        self.local.write_text(json.dumps({"k": {"stand": None, "note": "local"}}))
        self.peer.write_text(json.dumps({"k": {"stand": None, "note": "peer"}}))
        u = self.m.roster_union([("local", self.local), ("peer", self.peer)])
        self.assertEqual(u["k"]["note"], "local")

    def test_a_deliberate_local_REFUSAL_is_not_treated_as_a_placeholder(self):
        """Blank stand/room PLUS refusal_basis is DO-NOT-ROUTE per schema.md, not
        missing data. keweichen, reviewing this PR: the tie-break must not route
        around it, or a synced peer row silently overrides an explicit refusal."""
        self.local.write_text(json.dumps({"qingyun-wu": {
            "stand": "", "room": "", "refusal_basis": "DO NOT ROUTE — asked off-channel"}}))
        self.peer.write_text(json.dumps({"qingyun-wu": PEER_COMPLETE}))
        u = self.m.roster_union([("local", self.local), ("peer", self.peer)])
        self.assertEqual(u["qingyun-wu"].get("refusal_basis"),
                         "DO NOT ROUTE — asked off-channel",
                         f"the refusal lost the collision; union: {sorted(u)}")

    def test_a_note_alone_also_protects_the_row(self):
        self.local.write_text(json.dumps({"k": {"stand": None, "note": "human-only by request"}}))
        self.peer.write_text(json.dumps({"k": {"stand": "@b:x", "room": "!b:x"}}))
        u = self.m.roster_union([("local", self.local), ("peer", self.peer)])
        self.assertEqual(u["k"].get("note"), "human-only by request")

    def test_a_NON_DICT_row_is_never_usable(self):
        """A roster value that is not an object (a bare string, a null) must not
        win a collision as if it were addressable — and must not crash the merge."""
        self.local.write_text(json.dumps({"k": "just-a-string"}))
        self.peer.write_text(json.dumps({"k": PEER_COMPLETE}))
        u = self.m.roster_union([("local", self.local), ("peer", self.peer)])
        self.assertEqual(u["k"], PEER_COMPLETE,
                         f"a non-dict local row shadowed a usable peer row; union: {sorted(u)}")


class DiscordOnlyRowIsNotARoute(unittest.TestCase):
    """keweichen on #4047: _usable counted discord_id, but resolve() builds
    Matrix targets from stand+room alone — so a discord-only local row was
    RETAINED as usable and then could not be addressed."""

    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.m = _load(SRC)

    def _roster(self, name, data):
        p = pathlib.Path(self.d, name)
        p.write_text(json.dumps(data))
        return p

    def test_a_discord_only_row_is_not_usable(self):
        self.assertFalse(self.m._usable({"discord_id": "123456789"}))
        self.assertFalse(self.m._usable({"discord": "someone#1"}))

    def test_a_discord_only_local_row_loses_to_a_routable_peer_row(self):
        u = self.m.roster_union([
            ("local", self._roster("l.json", {"r": {"discord_id": "123"}})),
            ("peer",  self._roster("p.json", {"r": PEER_COMPLETE})),
        ])
        self.assertEqual(u["r"].get("room"), PEER_COMPLETE["room"],
            "a discord id is not a delivery route for either consumer")

    def test_a_refusal_row_still_wins_over_a_routable_peer_row(self):
        u = self.m.roster_union([
            ("local", self._roster("l2.json", {"r": {"stand": "", "room": "",
                                    "refusal_basis": "owner disabled"}})),
            ("peer",  self._roster("p2.json", {"r": PEER_COMPLETE})),
        ])
        self.assertEqual(u["r"].get("stand"), "")
        self.assertEqual(u["r"].get("refusal_basis"), "owner disabled")


class UnionToResolveRegression(unittest.TestCase):
    """keweichen's named unblock condition on #4047: drive the PRODUCTION
    roster_union() -> notify_reviewers.resolve() path, not just the union."""

    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.m = _load(SRC)
        nr = (pathlib.Path(__file__).resolve().parents[1] / "skills" /
              "collaboration-intelligence" / "scripts" / "notify_reviewers.py")
        s = importlib.util.spec_from_file_location("nr", nr)
        self.nr = importlib.util.module_from_spec(s)
        s.loader.exec_module(self.nr)

    def _roster(self, name, data):
        p = pathlib.Path(self.d, name)
        p.write_text(json.dumps(data))
        return p

    def test_a_local_refusal_survives_the_union_and_resolve_returns_3_no_target(self):
        # The harm measured on origin/main was at resolve(), not at the union:
        # a synced peer row re-enabled a reviewer the local owner had disabled.
        merged = self.m.roster_union([
            ("local", self._roster("l.json", {"reviewer": {
                "stand": "", "room": "", "refusal_basis": "owner disabled routing"}})),
            ("peer", self._roster("p.json", {"reviewer": PEER_COMPLETE})),
        ])
        targets, rc = self.nr.resolve(["reviewer"], merged)
        self.assertEqual(targets, [], "a disabled reviewer must get no target")
        self.assertEqual(rc, 3, "refusal must surface as rc 3, not a silent send")

    def test_the_control_a_routable_reviewer_still_resolves(self):
        """Without this, rc 3 could come from the path being broken for everyone."""
        merged = self.m.roster_union([
            ("local", self._roster("l2.json", {"reviewer": PEER_COMPLETE})),
        ])
        targets, rc = self.nr.resolve(["reviewer"], merged)
        self.assertEqual(rc, 0)
        self.assertEqual(len(targets), 1)


class PromotionPreservesLocalAuthority(unittest.TestCase):
    """qingyun-wu @b974e7b5: promoting a peer row wholly REPLACED the local one,
    so `allowlisted: false` was lost. notify_reviewers checks the refusal after
    the bare-key lookup, so the @local copy does not protect delivery."""

    def _union(self, local, peer):
        d = tempfile.mkdtemp()
        lp, pp = pathlib.Path(d, "l.json"), pathlib.Path(d, "p.json")
        lp.write_text(json.dumps({"r": local}))
        pp.write_text(json.dumps({"r": peer}))
        return _load(SRC).roster_union([("local", lp), ("peer", pp)])

    FULL = {"stand": "@peer:x", "room": "!peer:x"}

    def test_local_refusal_survives_promotion(self):
        u = self._union({"stand": None, "room": None, "allowlisted": False}, self.FULL)
        self.assertEqual(u["r"]["stand"], "@peer:x", "routing should still be promoted")
        self.assertIs(u["r"]["allowlisted"], False,
                      "a local allowlisted:false is a refusal and must survive promotion")

    def test_local_declared_identity_survives(self):
        u = self._union({"stand": None, "room": None, "gh": "local-login"}, self.FULL)
        self.assertEqual(u["r"]["gh"], "local-login",
                         "promotion must not swap the declared identity")

    def test_a_partial_local_row_is_NOT_overwritten(self):
        """A row naming a stand but no room states an identity. Promoting over it
        would route under the peer's stand instead."""
        u = self._union({"stand": "@local:x", "room": None}, self.FULL)
        self.assertEqual(u["r"]["stand"], "@local:x", "partial local row must win")
        self.assertEqual(u["r@peer"]["stand"], "@peer:x", "peer kept under its suffix")

    def test_the_placeholder_case_still_gets_fixed(self):
        u = self._union({"stand": None, "room": None}, self.FULL)
        self.assertEqual(u["r"]["room"], "!peer:x", "the original defect must stay fixed")


if __name__ == "__main__":
    unittest.main()
