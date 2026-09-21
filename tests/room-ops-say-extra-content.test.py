#!/usr/bin/env python3
"""room-ops `say --extra-content`: a protocol payload rides the event beside
the body. The skill's own unittest file covers the same; this copy runs
under the repo's standalone-test discovery, which is what the coverage gate
measures."""
import contextlib
import io
import json
import os
import pathlib
import sys
import unittest
from unittest import mock

_ROOM_OPS = pathlib.Path(__file__).resolve().parents[1] / "skills" / "agent-room-ops"
sys.path.insert(0, str(_ROOM_OPS))

import room_ops  # noqa: E402
import say as sy  # noqa: E402

ROOM = "!room:hs"
HS = "@agent:hs"


class ExtraContentTests(unittest.TestCase):
    def setUp(self):
        self._env = dict(os.environ)
        os.environ["RELAY_URL"] = "https://r"
        os.environ.pop("SUTANDO_WORKER_SEAT", None)
        os.environ.pop("SUTANDO_WORKER_ID", None)

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._env)

    def _post(self, **kwargs):
        cap = {}
        with mock.patch.object(sy, "http_json",
                               side_effect=lambda m, u, h, p: (cap.update(payload=p), (200, {}))[1]):
            res = sy.say("hi", ROOM, HS, gate=None, **kwargs)
        return res, cap

    def test_extra_content_rides_the_payload_beside_the_body(self):
        key = "space.ag2.collab.doc.comment"
        res, cap = self._post(extra_content={key: {"anchor": {"quote": "x"}, "v": 1}})
        self.assertTrue(res["ok"])
        self.assertEqual(cap["payload"]["extra_content"][key], {"anchor": {"quote": "x"}, "v": 1})
        self.assertEqual(cap["payload"]["body"], "hi")

    def test_extra_content_keeps_the_worker_stamp(self):
        os.environ["SUTANDO_WORKER_SEAT"] = "7"
        _res, cap = self._post(extra_content={"space.ag2.x": 1})
        self.assertEqual(cap["payload"]["extra_content"]["space.ag2.worker"]["id"], "worker-7")
        self.assertEqual(cap["payload"]["extra_content"]["space.ag2.x"], 1)

    def test_the_flag_is_parsed_and_reaches_the_function(self):
        cap = {}
        with mock.patch.object(room_ops._say, "say",
                               side_effect=lambda *a, **k: (cap.update(kw=k), {"ok": True})[1]):
            with contextlib.redirect_stdout(io.StringIO()):
                room_ops._main(["say", ROOM, "hi", "--extra-content", json.dumps({"space.ag2.k": {"v": 1}})])
        self.assertEqual(cap["kw"], {"reply_to": None, "extra_content": {"space.ag2.k": {"v": 1}}})

    def test_a_flag_that_is_not_an_object_is_refused_before_the_function(self):
        with mock.patch.object(room_ops._say, "say", side_effect=AssertionError("called")):
            with self.assertRaises(SystemExit):
                room_ops._main(["say", ROOM, "hi", "--extra-content", '["not", "an", "object"]'])


if __name__ == "__main__":
    unittest.main()
