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

    def test_thread_root_rides_the_payload_and_the_flag_reaches_the_function(self):
        res, cap = self._post(thread_root="$root")
        self.assertTrue(res["ok"])
        self.assertEqual(cap["payload"]["thread_root"], "$root")
        got = {}
        with mock.patch.object(room_ops._say, "say",
                               side_effect=lambda *a, **k: (got.update(kw=k), {"ok": True})[1]):
            with contextlib.redirect_stdout(io.StringIO()):
                room_ops._main(["say", ROOM, "yes", "--thread-root", "$root"])
        self.assertEqual(got["kw"], {"reply_to": None, "thread_root": "$root"})

    def test_a_malformed_thread_root_is_refused_before_the_network(self):
        called = []
        with mock.patch.object(sy, "http_json", side_effect=lambda *a, **k: called.append(a) or (200, {})):
            res = sy.say("hi", ROOM, HS, gate=None, thread_root="not-an-id")
        self.assertFalse(res["ok"]) and self.assertIn("thread_root", res["reason"])
        self.assertEqual(called, [])

    def _cli_refusal(self, raw):
        out = io.StringIO()
        with mock.patch.object(room_ops._say, "say", side_effect=AssertionError("called")):
            with contextlib.redirect_stdout(out):
                rc = room_ops._main(["say", ROOM, "hi", "--extra-content", raw])
        res = json.loads(out.getvalue())
        self.assertEqual(rc, 1)
        self.assertIs(res["ok"], False)
        return res["reason"]

    def test_a_flag_that_is_not_an_object_is_refused_before_the_function(self):
        self.assertIn("JSON object", self._cli_refusal('["not", "an", "object"]'))

    def test_a_flag_that_is_not_json_is_refused_before_the_function(self):
        self.assertIn("not valid JSON", self._cli_refusal("{room: x}"))


SUMMON = "space.ag2.collab.doc.summon"
CARD = {SUMMON: {"room_id": ROOM, "kind": "markdown", "invitee": "@q:hs", "v": 3},
        "m.mentions": {"user_ids": ["@q:hs"]}}


class WrapperShapedExtraContentTests(unittest.TestCase):
    """A dry-run's whole {room, body, extra_content} passed as --extra-content once
    posted a summon as plain prose: the gateway dropped the unknown keys silently."""

    def setUp(self):
        self._env = dict(os.environ)
        os.environ["RELAY_URL"] = "https://r"
        os.environ.pop("SUTANDO_WORKER_SEAT", None)
        os.environ.pop("SUTANDO_WORKER_ID", None)

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._env)

    def _cli(self, extra):
        out, sent = io.StringIO(), []
        with mock.patch.object(sy, "http_json",
                               side_effect=lambda m, u, h, p: (sent.append(p), (200, {"event_id": "$e"}))[1]), \
                mock.patch.object(sy, "gate_allows", return_value=True), \
                mock.patch.object(room_ops, "_record_say"):
            with contextlib.redirect_stdout(out):
                rc = room_ops._main(["say", ROOM, "hi", "--extra-content", json.dumps(extra)])
        return rc, json.loads(out.getvalue()), sent

    def test_the_dry_run_wrapper_is_refused_and_nothing_is_sent(self):
        rc, res, sent = self._cli({"room": ROOM, "body": "hi", "extra_content": CARD})
        self.assertEqual((rc, res["ok"], sent), (1, False, []))
        for key in ("body", "room", "extra_content"):
            self.assertIn(key, res["reason"])

    def test_every_message_field_is_reserved(self):
        for key in ("body", "msgtype", "room", "extra_content", "formatted_body", "format"):
            rc, res, sent = self._cli({key: "x", **CARD})
            self.assertEqual((rc, res["ok"], sent), (1, False, []), key)
            self.assertIn(key, res["reason"])

    def test_a_card_nested_under_any_key_is_refused_naming_its_path(self):
        rc, res, sent = self._cli({"payload": {"inner": CARD}})
        self.assertEqual((rc, res["ok"], sent), (1, False, []))
        self.assertIn(f'extra_content["payload"]["inner"]["{SUMMON}"]', res["reason"])

    def test_a_card_whose_own_payload_has_space_ag2_sub_keys_is_sent(self):
        card = {"space.ag2.foo": {"space.ag2.bar": {"v": 1}, "items": [{"space.ag2.baz": 2}]}}
        rc, res, sent = self._cli({**card, "m.mentions": {"user_ids": ["@q:hs"]}})
        self.assertEqual((rc, res["ok"]), (0, True), res["reason"])
        self.assertEqual(sent[0]["extra_content"]["space.ag2.foo"], card["space.ag2.foo"])

    def test_a_card_key_with_a_non_object_value_gets_the_nesting_check(self):
        rc, res, sent = self._cli({"space.ag2.list": [{"space.ag2.x": 1}]})
        self.assertEqual((rc, res["ok"], sent), (1, False, []))
        self.assertIn('extra_content["space.ag2.list"][0]["space.ag2.x"]', res["reason"])
        rc, res, _sent = self._cli({"space.ag2.flag": 1})
        self.assertEqual((rc, res["ok"]), (0, True), res["reason"])

    def test_the_bare_prefix_key_is_refused(self):
        for value in ({"space.ag2.x": 1}, 1):
            rc, res, sent = self._cli({"space.ag2.": value})
            self.assertEqual((rc, res["ok"], sent), (1, False, []), value)
            self.assertIn('bare key "space.ag2."', res["reason"])

    def test_a_card_under_a_non_card_top_level_key_is_refused_at_any_depth(self):
        rc, res, sent = self._cli({"items": [{"space.ag2.x": 1}]})
        self.assertEqual((rc, res["ok"], sent), (1, False, []))
        self.assertIn('extra_content["items"][0]["space.ag2.x"]', res["reason"])

    def test_the_library_call_refuses_the_same_before_the_network(self):
        with mock.patch.object(sy, "http_json", side_effect=AssertionError("network")):
            res = sy.say("hi", ROOM, HS, gate=None, extra_content={"body": "hi", "extra_content": CARD})
        self.assertIs(res["ok"], False)
        self.assertIn("extra_content", res["reason"])

    def test_a_correct_summon_is_sent_with_its_card_at_the_top_level(self):
        rc, res, sent = self._cli(CARD)
        self.assertEqual((rc, res["ok"]), (0, True))
        self.assertEqual(sent[0]["extra_content"][SUMMON], CARD[SUMMON])
        self.assertEqual(sent[0]["body"], "hi")


class CapabilitiesTests(unittest.TestCase):
    def test_capabilities_lists_the_say_flags_a_caller_selects_on(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = room_ops._main(["capabilities"])
        res = json.loads(out.getvalue())
        self.assertEqual(rc, 0)
        self.assertTrue(res["ok"])
        for flag in ("--extra-content", "--thread-root", "--reply-to"):
            self.assertIn(flag, res["say"])
        self.assertNotIn("--help", res["say"])
        self.assertIn("say", res["commands"])
        self.assertIs(res["say_extra_content_checked"], True)


if __name__ == "__main__":
    unittest.main()
