#!/usr/bin/env python3
"""room-ops `topic` — a room's declared name/topic/alias via get_state.

Lives in `tests/` rather than beside the module because the diff-coverage gate
discovers only `tests/*.test.py` (`scripts/coverage-gate.sh` -> `find tests
-name '*.test.py'`), the same reachability note `tests/room-ops-say.test.py`
carries. The in-skill suite (`skills/agent-room-ops/test_room_ops.py`) runs the
same behavior for developers; this wrapper is what CI measures.

The load-bearing distinction: `served_by_gateway` separates "the gateway did
not return the m.room.* types" (pre-backend-#735 filter) from "the room has
none declared" — both render as null fields, and collapsing them would make
the honest degrade unreadable.

Run: python3 tests/room-ops-topic.test.py
"""
import io
import pathlib
import sys
import unittest
from contextlib import redirect_stdout
from unittest import mock

_SKILL = pathlib.Path(__file__).resolve().parents[1] / "skills" / "agent-room-ops"
sys.path.insert(0, str(_SKILL))
import topic  # noqa: E402

ROOM = "!r:ag2.space"
HS = "@me:ag2.space"


def _served(events):
    ctx = mock.patch.multiple(
        topic,
        gateway=mock.Mock(return_value=("https://gw", {"h": "1"})),
        http_json=mock.Mock(return_value=(200, {"events": events})),
    )
    return ctx


class TopicExtractionTests(unittest.TestCase):
    def test_extracts_meta_and_reports_served(self):
        events = [
            {"type": "space.ag2.memo", "state_key": "k", "content": {}},
            {"type": "m.room.name", "state_key": "", "content": {"name": "N"}},
            {"type": "m.room.topic", "state_key": "", "content": {"topic": "T"}},
        ]
        with _served(events):
            r = topic.room_topic(ROOM)
        self.assertEqual((r["ok"], r["name"], r["topic"], r["alias"]),
                         (True, "N", "T", None))
        self.assertTrue(r["served_by_gateway"])

    def test_alias_and_null_content_are_tolerated(self):
        events = [
            {"type": "m.room.canonical_alias", "content": {"alias": "#a:x"}},
            {"type": "m.room.name", "content": None},
        ]
        with _served(events):
            r = topic.room_topic(ROOM)
        self.assertEqual((r["ok"], r["alias"], r["name"]), (True, "#a:x", None))

    def test_pre_735_gateway_omits_meta_is_honest_degrade(self):
        with _served([{"type": "space.ag2.memo", "content": {}}]):
            r = topic.room_topic(ROOM)
        # ok:true with served_by_gateway:false — "not returned" is distinct
        # from "the room has none", which would be served:true + nulls.
        self.assertTrue(r["ok"])
        self.assertFalse(r["served_by_gateway"])
        self.assertIsNone(r["name"])


class TopicDegradeTests(unittest.TestCase):
    def test_no_gateway_configured(self):
        with mock.patch.object(topic, "gateway", return_value=(None, {})):
            r = topic.room_topic(ROOM)
        self.assertFalse(r["ok"])
        self.assertEqual(r["reason"], "no gateway configured")

    def test_http_error_maps_to_degrade_reason(self):
        err = topic.HTTPError("u", 403, "forbidden", None, None)
        with mock.patch.object(topic, "gateway", return_value=("https://gw", {})), \
             mock.patch.object(topic, "http_json", side_effect=err):
            r = topic.room_topic(ROOM)
        self.assertFalse(r["ok"])
        self.assertEqual(r["reason"], topic.degrade_reason(403))

    def test_network_error_degrades_without_raising(self):
        with mock.patch.object(topic, "gateway", return_value=("https://gw", {})), \
             mock.patch.object(topic, "http_json",
                               side_effect=topic.URLError("boom")):
            r = topic.room_topic(ROOM)
        self.assertFalse(r["ok"])
        self.assertIn("network error", r["reason"])

    def test_error_payload_and_malformed_response(self):
        with mock.patch.object(topic, "gateway", return_value=("https://gw", {})), \
             mock.patch.object(topic, "http_json",
                               return_value=(200, {"error": "nope"})):
            self.assertEqual(topic.room_topic(ROOM)["reason"], "nope")
        with mock.patch.object(topic, "gateway", return_value=("https://gw", {})), \
             mock.patch.object(topic, "http_json", return_value=(200, None)):
            r = topic.room_topic(ROOM)
        self.assertFalse(r["ok"])
        self.assertEqual(r["reason"], "malformed gateway response")


class TopicCliDispatchTests(unittest.TestCase):
    def test_room_ops_topic_routes_to_room_topic(self):
        """Pins the CLI wiring: `room_ops topic <room>` reaches topic.room_topic.

        room_ops imports topic lazily inside the dispatch arm; patching the
        already-imported module object covers it via sys.modules identity.
        """
        import room_ops
        with mock.patch.object(topic, "room_topic",
                               return_value=topic._result(True, served=True)) as m:
            with redirect_stdout(io.StringIO()):
                rc = room_ops._main(["topic", ROOM, "--agent", HS])
        self.assertEqual(rc, 0)
        m.assert_called_once_with(ROOM, HS)


if __name__ == "__main__":
    unittest.main(verbosity=1)
