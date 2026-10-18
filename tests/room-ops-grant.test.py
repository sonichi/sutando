#!/usr/bin/env python3
"""room-ops `grant` — authoritative room-grant client (#429 slice-2).

The room-ops suite lives under `skills/agent-room-ops/`, but the diff-coverage gate
discovers only `tests/*.test.py` (`scripts/coverage-gate.sh` -> `find tests -name
'*.test.py'`). So changed lines in `grant.py` are RUN by the functional job and
INVISIBLE to the coverage job unless a `tests/*.test.py` imports them — same
reachability trap and remedy as `tests/room_ops_read_limit.test.py`.

What is pinned here:
- `build_grant_content` is a read-modify-write: it preserves non-grant fields
  (respond/rate/read) so setting a grant never clobbers the room's other policy.
- `--revoke` disables the grant (authoritative=false) without touching other keys.
- `grant_room` reads the CURRENT `space.ag2.policy` (op:get_state) then writes the
  full merged event (op:state) to `space.ag2.policy` / state_key "".
- `parse_tier_pairs` only accepts owner|guest tiers.

Run: python3 tests/room-ops-grant.test.py
"""
import pathlib
import sys
import unittest
from unittest import mock

_ROOM_OPS = pathlib.Path(__file__).resolve().parents[1] / "skills" / "agent-room-ops"
sys.path.insert(0, str(_ROOM_OPS))
import grant as gr  # noqa: E402

ROOM = "!r:ag2.space"


class BuildGrantContentTests(unittest.TestCase):
    def test_sets_authoritative_and_preserves_other_fields(self):
        cur = {"respond": "always", "read": "all"}
        out = gr.build_grant_content(cur, tiers={"@u:hs": "owner"},
                                     default_tier="guest")
        self.assertTrue(out["authoritative"])
        self.assertEqual(out["tiers"], {"@u:hs": "owner"})
        self.assertEqual(out["default_tier"], "guest")
        # non-grant fields survive the write (no clobber)
        self.assertEqual(out["respond"], "always")
        self.assertEqual(out["read"], "all")
        # input dict not mutated
        self.assertNotIn("authoritative", cur)

    def test_merges_into_existing_tiers(self):
        cur = {"authoritative": True, "tiers": {"@a:hs": "owner"}}
        out = gr.build_grant_content(cur, tiers={"@b:hs": "guest"})
        self.assertEqual(out["tiers"], {"@a:hs": "owner", "@b:hs": "guest"})

    def test_revoke_disables_without_touching_other_fields(self):
        cur = {"authoritative": True, "tiers": {"@a:hs": "owner"},
               "respond": "always"}
        out = gr.build_grant_content(cur, revoke=True)
        self.assertFalse(out["authoritative"])
        self.assertEqual(out["tiers"], {"@a:hs": "owner"})  # untouched
        self.assertEqual(out["respond"], "always")

    def test_none_current_and_no_tiers(self):
        out = gr.build_grant_content(None)
        self.assertEqual(out, {"authoritative": True})


class CurrentPolicyTests(unittest.TestCase):
    def test_picks_room_level_policy_event(self):
        events = [
            {"type": "space.ag2.agent_policy", "state_key": "", "content": {"x": 1}},
            {"type": "space.ag2.policy", "state_key": "", "content": {"respond": "always"}},
        ]
        self.assertEqual(gr.current_policy(events), {"respond": "always"})

    def test_ignores_non_empty_state_key_and_absent(self):
        self.assertEqual(gr.current_policy([
            {"type": "space.ag2.policy", "state_key": "x", "content": {"a": 1}}]), {})
        self.assertEqual(gr.current_policy([]), {})
        self.assertEqual(gr.current_policy(None), {})

    def test_non_dict_content(self):
        self.assertEqual(gr.current_policy([
            {"type": "space.ag2.policy", "state_key": "", "content": None}]), {})


class ParseTierPairsTests(unittest.TestCase):
    def test_valid(self):
        self.assertEqual(gr.parse_tier_pairs(["@u:hs=owner", "@v:hs=guest"]),
                         {"@u:hs": "owner", "@v:hs": "guest"})

    def test_none(self):
        self.assertEqual(gr.parse_tier_pairs(None), {})

    def test_rejects_missing_eq(self):
        with self.assertRaises(ValueError):
            gr.parse_tier_pairs(["@u:hs"])

    def test_rejects_unknown_tier(self):
        with self.assertRaises(ValueError):
            gr.parse_tier_pairs(["@u:hs=admin"])


class GrantRoomTests(unittest.TestCase):
    def _patch(self, get_reply, write_reply):
        calls = []

        def fake_http_json(method, url, headers, payload):
            calls.append(payload)
            if payload.get("op") == "get_state":
                return 200, get_reply
            return 200, write_reply

        return calls, fake_http_json

    def test_read_merge_write_envelope(self):
        get_reply = {"events": [{"type": "space.ag2.policy", "state_key": "",
                                 "content": {"respond": "always"}}]}
        calls, fake = self._patch(get_reply, {"event_id": "$e1"})
        with mock.patch.object(gr, "gateway", return_value=("https://gw", {})), \
             mock.patch.object(gr, "http_json", fake):
            res = gr.grant_room(ROOM, tiers={"@u:hs": "owner"}, default_tier="guest")
        self.assertTrue(res["ok"])
        self.assertEqual(res["event_id"], "$e1")
        # two ops: get_state then state-write
        self.assertEqual([c["op"] for c in calls], ["get_state", "state"])
        write = calls[1]
        self.assertEqual(write["type"], "space.ag2.policy")
        self.assertEqual(write["state_key"], "")
        self.assertTrue(write["content"]["authoritative"])
        self.assertEqual(write["content"]["tiers"], {"@u:hs": "owner"})
        self.assertEqual(write["content"]["default_tier"], "guest")
        # preserved from current
        self.assertEqual(write["content"]["respond"], "always")

    def test_no_gateway(self):
        with mock.patch.object(gr, "gateway", return_value=("", {})):
            res = gr.grant_room(ROOM, tiers={"@u:hs": "owner"})
        self.assertFalse(res["ok"])
        self.assertIn("no gateway", res["reason"])

    def test_read_error_aborts_before_write(self):
        calls, _ = self._patch({}, {})

        def fake(method, url, headers, payload):
            calls.append(payload)
            return 200, {"error": "denied — agent not a joined member (403)"}

        with mock.patch.object(gr, "gateway", return_value=("https://gw", {})), \
             mock.patch.object(gr, "http_json", fake):
            res = gr.grant_room(ROOM, tiers={"@u:hs": "owner"})
        self.assertFalse(res["ok"])
        self.assertIn("403", res["reason"])
        # only the read was attempted; no write after a failed read
        self.assertEqual([c["op"] for c in calls], ["get_state"])

    def test_write_error_surfaced(self):
        get_reply = {"events": []}
        _calls, fake = self._patch(get_reply, {"error": "state write failed: 403"})
        with mock.patch.object(gr, "gateway", return_value=("https://gw", {})), \
             mock.patch.object(gr, "http_json", fake):
            res = gr.grant_room(ROOM, revoke=True, agent_mxid="@me:hs")
        self.assertFalse(res["ok"])
        self.assertIn("write failed", res["reason"])
        # revoke content still returned for legibility
        self.assertFalse(res["content"]["authoritative"])

    def test_read_exception_degrades(self):
        def boom(*a, **k):
            raise RuntimeError("net down")

        with mock.patch.object(gr, "gateway", return_value=("https://gw", {})), \
             mock.patch.object(gr, "http_json", boom):
            res = gr.grant_room(ROOM, tiers={"@u:hs": "owner"})
        self.assertFalse(res["ok"])
        self.assertIn("read current policy failed", res["reason"])

    def test_write_exception_degrades(self):
        seq = [(200, {"events": []})]

        def fake(method, url, headers, payload):
            if payload.get("op") == "get_state":
                return seq[0]
            raise RuntimeError("net down")

        with mock.patch.object(gr, "gateway", return_value=("https://gw", {})), \
             mock.patch.object(gr, "http_json", fake):
            res = gr.grant_room(ROOM, tiers={"@u:hs": "owner"})
        self.assertFalse(res["ok"])
        self.assertIn("policy write failed", res["reason"])


class CliDispatchTests(unittest.TestCase):
    """Covers room_ops.py's `grant` subparser + dispatch branch."""

    def _run(self, argv):
        import room_ops as ro
        with mock.patch("builtins.print"):
            return ro._main(argv)

    def test_grant_dispatch_calls_grant_room(self):
        captured = {}

        def fake_grant_room(room_id, **kw):
            captured["room_id"] = room_id
            captured["kw"] = kw
            return {"ok": True}

        with mock.patch.object(gr, "grant_room", fake_grant_room):
            rc = self._run(["grant", ROOM, "--tier", "@u:hs=owner",
                            "--default-tier", "guest"])
        self.assertEqual(rc, 0)
        self.assertEqual(captured["room_id"], ROOM)
        self.assertEqual(captured["kw"]["tiers"], {"@u:hs": "owner"})
        self.assertEqual(captured["kw"]["default_tier"], "guest")
        self.assertFalse(captured["kw"]["revoke"])

    def test_grant_dispatch_revoke(self):
        seen = {}
        with mock.patch.object(gr, "grant_room",
                               lambda room_id, **kw: seen.update(kw) or {"ok": True}):
            rc = self._run(["grant", ROOM, "--revoke"])
        self.assertEqual(rc, 0)
        self.assertTrue(seen["revoke"])

    def test_grant_dispatch_bad_tier_never_calls_grant_room(self):
        called = {"n": 0}

        def boom(*a, **k):
            called["n"] += 1
            return {}

        with mock.patch.object(gr, "grant_room", boom):
            rc = self._run(["grant", ROOM, "--tier", "@u:hs=admin"])
        self.assertEqual(rc, 0)          # bad input is a clean result, not a crash
        self.assertEqual(called["n"], 0)  # never reached the network call


if __name__ == "__main__":
    unittest.main(verbosity=2)