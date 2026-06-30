#!/usr/bin/env python3
"""Tests for the room-ops collection — shared gate, the read/media/react modules,
and the unified room_ops CLI dispatcher. No network."""
import base64
import json
import os
import sys
import tempfile
import unittest
import urllib.error
from unittest import mock

sys.path.insert(0, os.path.dirname(__file__))
import _relay  # noqa: E402
import read as rd  # noqa: E402
import media as md  # noqa: E402
import react as rc  # noqa: E402
import room_ops  # noqa: E402

HS = "@agent.a:hs"
ROOM = "!roomA:hs"
EV = "$evt1"
ENVK = ["RELAY_URL", "REMOTE_TASK_URL", "RELAY_TOKEN", "REMOTE_TASK_TOKEN",
        "ROOM_MEDIA_ALLOW", "ROOM_MEDIA_INBOX", "ROOM_OPS_GATE"]


def _clear():
    for k in ENVK:
        os.environ.pop(k, None)


class EnvCase(unittest.TestCase):
    def setUp(self):
        self._saved = {k: os.environ.get(k) for k in ENVK}
        _clear()

    def tearDown(self):
        _clear()
        for k, v in self._saved.items():
            if v is not None:
                os.environ[k] = v


# ----- shared gate (_relay) ----- #
class GateTests(unittest.TestCase):
    def test_none_defers(self):
        self.assertTrue(_relay.gate_allows(HS, ROOM, None))

    def test_empty_denies(self):
        self.assertFalse(_relay.gate_allows(HS, ROOM, {}))

    def test_explicit_room(self):
        self.assertTrue(_relay.gate_allows(HS, ROOM, {HS: {"rooms": [ROOM]}}))
        self.assertFalse(_relay.gate_allows(HS, "!x:hs", {HS: {"rooms": [ROOM]}}))

    def test_all_member(self):
        self.assertTrue(_relay.gate_allows(HS, ROOM, {HS: {"all_member_rooms": True}}))

    def test_malformed(self):
        self.assertFalse(_relay.gate_allows(HS, ROOM, {HS: 1}))

    def test_load_missing_none(self):
        self.assertIsNone(_relay.load_gate("/nonexistent/g.json"))

    def test_degrade_reasons(self):
        self.assertIn("unimplemented", _relay.degrade_reason(404))
        self.assertIn("not a joined member", _relay.degrade_reason(403))
        self.assertIn("HTTP 500", _relay.degrade_reason(500))


# ----- read ----- #
class ReadTests(EnvCase):
    def test_no_room(self):
        self.assertFalse(rd.read_room("", HS)["ok"])

    def test_gate_deny(self):
        os.environ["RELAY_URL"] = "https://r"
        self.assertIn("gate denied", rd.read_room(ROOM, HS, gate={})["reason"])

    def test_no_relay(self):
        self.assertEqual(rd.read_room(ROOM, HS, gate={HS: {"rooms": [ROOM]}})["reason"],
                         "no RELAY_URL configured")

    def test_404_degrades(self):
        os.environ["RELAY_URL"] = "https://r"
        err = urllib.error.HTTPError("u", 404, "nf", {}, None)
        with mock.patch.object(rd, "http_request", side_effect=err):
            self.assertIn("unimplemented", rd.read_room(ROOM, HS, gate=None)["reason"])

    def test_limit_clamped(self):
        os.environ["RELAY_URL"] = "https://r"
        cap = {}
        with mock.patch.object(rd, "http_request",
                               side_effect=lambda m, u, h: (cap.update(url=u), (200, b'{"messages":[]}', {}))[1]):
            rd.read_room(ROOM, HS, limit=9999, gate=None)
        self.assertIn(f"limit={rd.MAX_LIMIT}", cap["url"])

    def test_success_parses(self):
        os.environ["RELAY_URL"] = "https://r"
        body = (200, json.dumps({"messages": [{"sender": "@a:hs", "ts": 1, "body": "hi"}]}).encode(), {})
        with mock.patch.object(rd, "http_request", return_value=body):
            res = rd.read_room(ROOM, HS, gate=None)
        self.assertTrue(res["ok"])
        self.assertEqual(res["messages"][0]["body"], "hi")


# ----- media ----- #
class MediaTests(EnvCase):
    def setUp(self):
        super().setUp()
        self.tmp = tempfile.mkdtemp()
        os.environ["ROOM_MEDIA_ALLOW"] = self.tmp
        os.environ["ROOM_MEDIA_INBOX"] = self.tmp
        self.f = os.path.join(self.tmp, "ok.png")
        open(self.f, "wb").write(b"IMG")

    def test_fetch_404(self):
        os.environ["RELAY_URL"] = "https://r"
        err = urllib.error.HTTPError("u", 404, "nf", {}, None)
        with mock.patch.object(md, "http_request", side_effect=err):
            self.assertIn("unimplemented", md.fetch_media("mxc://x/y", HS, ROOM, gate=None)["reason"])

    def test_fetch_success_writes(self):
        os.environ["RELAY_URL"] = "https://r"
        with mock.patch.object(md, "http_request", return_value=(200, b"PNG", {"X-Media-Filename": "p.png"})):
            res = md.fetch_media("mxc://x/y", HS, ROOM, gate=None)
        self.assertTrue(res["ok"] and os.path.isfile(res["path"]))
        self.assertEqual(open(res["path"], "rb").read(), b"PNG")

    def test_send_path_not_allowed(self):
        os.environ["ROOM_MEDIA_ALLOW"] = "/other"
        self.assertIn("not in ROOM_MEDIA_ALLOW", md.send_media(ROOM, self.f, HS, gate=None)["reason"])

    def test_send_oversize(self):
        big = os.path.join(self.tmp, "big.bin")
        open(big, "wb").write(b"x" * (md.MAX_BYTES + 1))
        self.assertIn("exceeds", md.send_media(ROOM, big, HS, gate=None)["reason"])

    def test_send_success(self):
        os.environ["RELAY_URL"] = "https://r"
        cap = {}

        def fake(method, url, headers=None, data=None):
            cap["data"] = data
            return 200, b"{}", {}

        with mock.patch.object(md, "http_request", side_effect=fake):
            res = md.send_media(ROOM, self.f, HS, gate=None, caption="c")
        self.assertTrue(res["ok"])
        sent = json.loads(cap["data"])
        self.assertEqual(base64.b64decode(sent["content_b64"]), b"IMG")


# ----- react ----- #
class ReactTests(EnvCase):
    def test_missing_args(self):
        self.assertFalse(rc.react(ROOM, "", "👀", HS)["ok"])

    def test_gate_deny(self):
        os.environ["RELAY_URL"] = "https://r"
        self.assertIn("gate denied", rc.react(ROOM, EV, "👀", HS, gate={})["reason"])

    def test_react_endpoint(self):
        os.environ["RELAY_URL"] = "https://r"
        cap = {}
        with mock.patch.object(rc, "http_json",
                               side_effect=lambda m, u, h, p: (cap.update(url=u, payload=p), (200, {}))[1]):
            res = rc.react(ROOM, EV, "✅", HS, gate=None)
        self.assertTrue(res["ok"] and cap["url"].endswith("/react"))
        self.assertEqual(cap["payload"], {"event_id": EV, "key": "✅"})

    def test_unreact_endpoint(self):
        os.environ["RELAY_URL"] = "https://r"
        cap = {}
        with mock.patch.object(rc, "http_json",
                               side_effect=lambda m, u, h, p: (cap.update(url=u), (200, {}))[1]):
            rc.unreact(ROOM, EV, "👀", HS, gate=None)
        self.assertTrue(cap["url"].endswith("/unreact"))

    def test_403_degrades(self):
        os.environ["RELAY_URL"] = "https://r"
        err = urllib.error.HTTPError("u", 403, "no", {}, None)
        with mock.patch.object(rc, "http_json", side_effect=err):
            self.assertIn("not a joined member", rc.react(ROOM, EV, "👀", HS, gate=None)["reason"])


# ----- unified CLI ----- #
class CliTests(EnvCase):
    def test_read_exits_zero(self):
        self.assertEqual(room_ops._main(["read", ROOM, "--agent", HS]), 0)

    def test_send_exits_zero_on_no_context(self):
        self.assertEqual(room_ops._main(["send", ROOM, "/nope/x.png", "--agent", HS]), 0)

    def test_react_ack_maps_and_exits_zero(self):
        os.environ["RELAY_URL"] = "https://r"
        cap = {}
        with mock.patch.object(rc, "http_json",
                               side_effect=lambda m, u, h, p: (cap.update(p), (200, {}))[1]):
            rc_ = room_ops._main(["react", ROOM, EV, "--ack", "done", "--agent", HS])
        self.assertEqual(rc_, 0)
        self.assertEqual(cap["key"], rc.ACK["done"])

    def test_fetch_exits_zero(self):
        self.assertEqual(room_ops._main(["fetch", "mxc://x/y", "--room", ROOM, "--agent", HS]), 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
