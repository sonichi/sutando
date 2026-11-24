#!/usr/bin/env python3
"""read_room limit semantics — coverage-gate home.

The room-ops suite lives under `skills/agent-room-ops/`, but the diff-coverage gate
discovers only `tests/*.test.py` (`scripts/coverage-gate.sh` -> `find tests -name
'*.test.py'`). So changed lines in `read.py` are RUN by the functional job and
INVISIBLE to the coverage job — a passing test the gate never sees. Same reachability
trap, and the same remedy, as `tests/room_ops_gateway_vault.test.py`.

What is pinned here: `limit` counts MESSAGES, not raw timeline events.

The gateway applies its limit to raw events — reactions, receipts, membership and media
all consume the budget — so a small limit could return an EMPTY list with ok:true and no
error, on a room that was not empty. Measured live 2026-08-05 against ag2.space:

    limit=4 -> 0 messages, limit=10 -> 1, limit=20 -> 8, limit=40 -> 14, limit=100 -> 14

Nine non-message events sat ahead of the newest message. A small limit is exactly what a
cheap "has anyone replied?" probe passes, so the false empty lands precisely on polling.

Run: python3 tests/room_ops_read_limit.test.py
"""
import json
import os
import pathlib
import sys
import unittest
import urllib.parse
from unittest import mock

from _helpers.hermetic_env import isolate_os_environ  # noqa: E402

# Before anything below reads the environment: no outer credential selector survives.
isolate_os_environ()

_ROOM_OPS = pathlib.Path(__file__).resolve().parents[1] / "skills" / "agent-room-ops"
sys.path.insert(0, str(_ROOM_OPS))
import read as rd  # noqa: E402

HS = "@a:hs"
ROOM = "!r:hs"
_ENVK = ("GATEWAY_URL", "GATEWAY_TOKEN", "RELAY_URL", "REMOTE_TASK_URL",
         "RELAY_TOKEN", "REMOTE_TASK_TOKEN", "AG2_REMOTE_TOKEN", "AG2_REMOTE_URL")


def raw_window_gateway(total_messages=14, noise_per_message=2, seen=None):
    """A gateway whose `limit` bounds RAW EVENTS, only some of which are messages.

    Reproduces the live ag2.space behaviour rather than asserting the new shape into
    existence — so this fails if the client stops widening, instead of merely pinning
    whatever the code does today.
    """
    def _http(_method, url, _headers):
        raw = int(dict(urllib.parse.parse_qsl(urllib.parse.urlparse(url).query))["limit"])
        if seen is not None:
            seen.append(raw)
        msgs, consumed = [], 0
        for i in range(total_messages):
            consumed += noise_per_message          # non-message events come first
            if consumed >= raw:
                break
            consumed += 1
            msgs.append({"sender": HS, "ts": 1000 - i, "body": f"m{i}"})
            if consumed >= raw:
                break
        return (200, json.dumps({"messages": msgs}).encode(), {})
    return _http


class ReadLimitCountsMessages(unittest.TestCase):
    def setUp(self):
        self._saved = {k: os.environ.get(k) for k in _ENVK}
        for k in _ENVK:
            os.environ.pop(k, None)
        os.environ["RELAY_URL"] = "https://r"

    def tearDown(self):
        for k, v in self._saved.items():
            if v is not None:
                os.environ[k] = v
            else:
                os.environ.pop(k, None)

    def test_max_limit_matches_the_gateway_ceiling(self):
        """A lowered ceiling loses history without making the existing tests fail."""
        self.assertEqual(rd.MAX_LIMIT, 1000)

    def test_small_limit_does_not_report_an_empty_room(self):
        """The regression: limit=3 returned ZERO from a room holding fourteen."""
        with mock.patch.object(rd, "http_request", side_effect=raw_window_gateway()):
            res = rd.read_room(ROOM, HS, limit=3, gate=None)
        self.assertTrue(res["ok"])
        self.assertEqual(len(res["messages"]), 3)

    def test_limit_counts_messages_not_raw_events(self):
        with mock.patch.object(rd, "http_request", side_effect=raw_window_gateway()):
            res = rd.read_room(ROOM, HS, limit=10, gate=None)
        self.assertEqual(len(res["messages"]), 10)
        self.assertTrue(res["complete"])

    def test_widens_the_window_until_satisfied(self):
        seen = []
        with mock.patch.object(rd, "http_request",
                               side_effect=raw_window_gateway(seen=seen)):
            rd.read_room(ROOM, HS, limit=5, gate=None)
        self.assertGreater(len(seen), 1, "must widen when the first window is short")
        self.assertEqual(seen, sorted(seen), "the raw window must only grow")

    def test_short_room_under_claims_completeness(self):
        """Short of `limit` reports complete=False even if the room may hold no more.

        Deliberate under-claim (#2678 review): from the client those two cases are
        indistinguishable — this endpoint returns only message-type items, so a short page
        looks identical to a noisy window. A caller seeing complete=False learns only "do
        not treat this as the whole story", the safe direction for a function whose
        failure mode is a confident empty.
        """
        with mock.patch.object(rd, "http_request",
                               side_effect=raw_window_gateway(total_messages=2)):
            res = rd.read_room(ROOM, HS, limit=20, gate=None)
        self.assertEqual(len(res["messages"]), 2)
        self.assertFalse(res["complete"], "short of limit -> never claim complete")

    def _front_gap_gateway(self, gap, total=14, before=0):
        """`before` messages, then a wall of `gap` non-message events, then the rest."""
        import urllib.parse as _up

        def _http(_m, url, _h):
            raw = int(dict(_up.parse_qsl(_up.urlparse(url).query))["limit"])
            msgs, consumed = [], 0
            for _ in range(before):
                consumed += 1
                if consumed > raw:
                    break
                msgs.append({"sender": HS, "ts": 1, "body": f"pre{len(msgs)}"})
            consumed += gap
            while consumed < raw and len(msgs) < total:
                consumed += 1
                msgs.append({"sender": HS, "ts": 1, "body": f"m{len(msgs)}"})
            return (200, json.dumps({"messages": msgs}).encode(), {})
        return _http

    def test_front_gap_is_not_exhausted_history(self):
        """Both #2678 reviewers' control: a 20-event front gap is not an empty room.

        Windows [3, 13] both return zero because neither clears the wall. Inferring
        exhaustion from that repeated count rebuilds the exact false-empty this module
        exists to remove, for rooms with a larger front gap.
        """
        with mock.patch.object(rd, "http_request",
                               side_effect=self._front_gap_gateway(gap=20)):
            res = rd.read_room(ROOM, HS, limit=3, gate=None)
        self.assertEqual(len(res["messages"]), 3)
        self.assertTrue(res["complete"])

    def test_a_wall_after_the_first_message_is_not_exhausted_either(self):
        """Guarding the plateau on "we have seen a message" only MOVES the wall.

        That was the author's first attempt at the fix; one message ahead of a 60-event
        gap still returned 1 of 5 with complete=True. There is no safe repeated-count
        inference, which is why the check is gone rather than conditioned.
        """
        with mock.patch.object(rd, "http_request",
                               side_effect=self._front_gap_gateway(gap=60, before=1)):
            res = rd.read_room(ROOM, HS, limit=5, gate=None)
        self.assertGreaterEqual(len(res["messages"]), 5)
        self.assertTrue(res["complete"])

    def test_stops_at_max_limit(self):
        seen = []
        with mock.patch.object(rd, "http_request",
                               side_effect=raw_window_gateway(total_messages=500,
                                                              noise_per_message=9,
                                                              seen=seen)):
            res = rd.read_room(ROOM, HS, limit=rd.MAX_LIMIT, gate=None)
        self.assertLessEqual(max(seen), rd.MAX_LIMIT, "never request beyond MAX_LIMIT")
        self.assertFalse(res["complete"], "stopped early -> NOT complete")

    def test_widening_reaches_the_cap_in_a_bounded_number_of_calls(self):
        """Raising MAX_LIMIT must actually widen the reach AND stay cheap.

        The schedule is geometric with a `_MAX_WIDENINGS` guard, so a bigger
        cap could in principle be unreachable (guard truncates before the top)
        or reachable only via many round trips. Pin both: the widest window IS
        the cap, and getting there costs a handful of calls, not dozens.
        """
        for cap in (100, 1000):
            w = rd._windows(rd.DEFAULT_LIMIT, cap)
            self.assertEqual(w[-1], cap, f"schedule must reach cap {cap}")
            self.assertEqual(sorted(set(w)), w, "strictly increasing, no repeats")
            self.assertLessEqual(len(w), 8, f"too many round trips for cap {cap}")
        # The floor start is the worst case — smallest first window, so the
        # most doublings to climb. It is the one that would blow the budget.
        self.assertLessEqual(len(rd._windows(1, rd.MAX_LIMIT)), 8)

    def test_leading_noise_wider_than_the_first_windows(self):
        """Two consecutive ZERO windows must not be read as an empty room.

        REGRESSION, caught live 2026-08-05: raw windows 1 and 11 both returned 0 messages
        on a room holding fourteen, because a long run of reactions/receipts sat in front
        of the newest message. Stopping there — "a wider window returned nothing new" —
        reproduced the exact empty-room bug this module exists to prevent, one layer down.
        Exhaustion may only be inferred once at least one message has been seen.
        """
        with mock.patch.object(rd, "http_request",
                               side_effect=raw_window_gateway(total_messages=14,
                                                              noise_per_message=12)):
            res = rd.read_room(ROOM, HS, limit=1, gate=None)
        self.assertEqual(len(res["messages"]), 1,
                         "leading noise must be walked past, not mistaken for an empty room")

    def test_never_returns_more_than_requested(self):
        with mock.patch.object(rd, "http_request", side_effect=raw_window_gateway()):
            res = rd.read_room(ROOM, HS, limit=2, gate=None)
        self.assertEqual(len(res["messages"]), 2)

    # ---- error paths keep degrading, and stay reportable ---- #
    def test_http_error_degrades(self):
        import urllib.error
        err = urllib.error.HTTPError("u", 404, "nf", {}, None)
        with mock.patch.object(rd, "http_request", side_effect=err):
            res = rd.read_room(ROOM, HS, gate=None)
        self.assertFalse(res["ok"])
        self.assertIsNone(res["complete"], "no completeness claim on an error")

    def test_network_error_degrades(self):
        import urllib.error
        with mock.patch.object(rd, "http_request",
                               side_effect=urllib.error.URLError("boom")):
            res = rd.read_room(ROOM, HS, gate=None)
        self.assertFalse(res["ok"])
        self.assertIn("network error", res["reason"])

    def test_bad_json_degrades(self):
        with mock.patch.object(rd, "http_request",
                               return_value=(200, b"not json", {})):
            res = rd.read_room(ROOM, HS, gate=None)
        self.assertFalse(res["ok"])
        self.assertIn("parse error", res["reason"])

    def test_bare_list_body_is_accepted(self):
        body = (200, json.dumps([{"sender": HS, "ts": 1, "body": "hi"}]).encode(), {})
        with mock.patch.object(rd, "http_request", return_value=body):
            res = rd.read_room(ROOM, HS, limit=1, gate=None)
        self.assertEqual(res["messages"][0]["body"], "hi")

    def test_before_is_forwarded(self):
        seen_urls = []

        def _http(_m, url, _h):
            seen_urls.append(url)
            return (200, json.dumps({"messages": [{"sender": HS, "ts": 1, "body": "x"}]}).encode(), {})
        with mock.patch.object(rd, "http_request", side_effect=_http):
            rd.read_room(ROOM, HS, limit=1, gate=None, before="$evt")
        self.assertIn("before=", seen_urls[0])

    def test_read_output_marks_the_edited_message_and_only_it(self):
        items = [{"event_id": "$2", "sender": HS, "ts": 2, "body": "fixed", "edited": True,
                  "edit_event_id": "$edit"},
                 {"event_id": "$1", "sender": HS, "ts": 1, "body": "untouched",
                  "msgtype": "m.notice"}]
        body = (200, json.dumps({"messages": items}).encode(), {})
        with mock.patch.object(rd, "http_request", return_value=body):
            res = rd.read_room(ROOM, HS, limit=2, gate=None)
        self.assertEqual([(m["event_id"], m.get("edited"), m.get("edit_event_id"))
                          for m in res["messages"]],
                         [("$2", True, "$edit"), ("$1", None, None)])
        self.assertEqual([m.get("msgtype") for m in res["messages"]], [None, "m.notice"])


class NormalizeMediaRefTests(unittest.TestCase):
    """`media_ref` + `msgtype` are the only handle a reader has on a room attachment,
    so dropping them leaves it visible in `body` but unfetchable."""

    def test_media_ref_and_msgtype_preserved(self):
        out = rd._normalize([{"event_id": "$e", "sender": HS, "body": "doc.pdf",
                              "msgtype": "m.file", "media_ref": "mxc://hs/abc123"}])
        self.assertEqual(out[0]["media_ref"], "mxc://hs/abc123")
        self.assertEqual(out[0]["msgtype"], "m.file")

    def test_broker_shaped_media_event_keeps_its_handle(self):
        # The broker names the handle `mxc` and adds `mimetype` (room_ops `_media_fields`).
        out = rd._normalize([{"event_id": "$e", "sender": HS, "body": "guide.md",
                              "msgtype": "m.file", "mxc": "mxc://hs/abc123",
                              "mimetype": "text/markdown"}])
        self.assertEqual(out[0]["media_ref"], "mxc://hs/abc123")
        self.assertEqual(out[0]["msgtype"], "m.file")
        self.assertEqual(out[0]["mimetype"], "text/markdown")

    def test_media_ref_wins_when_both_names_arrive(self):
        out = rd._normalize([{"event_id": "$e", "sender": HS, "body": "a.pdf",
                              "media_ref": "mxc://hs/ref", "mxc": "mxc://hs/other"}])
        self.assertEqual(out[0]["media_ref"], "mxc://hs/ref")

    def test_media_without_msgtype_grows_no_null(self):
        # Third case: media present, msgtype absent. The gateway is external, so this
        # cannot be ruled out from here — keep the same additive shape as plain text.
        out = rd._normalize([{"event_id": "$e", "sender": "@a:h", "body": "f.pdf",
                              "media_ref": "mxc://hs/abc123"}])
        self.assertEqual(out[0]["media_ref"], "mxc://hs/abc123")
        self.assertNotIn("msgtype", out[0])
        self.assertNotIn("mimetype", out[0])

    def test_no_media_ref_key_for_plain_message(self):
        # A text message must not grow a null media_ref — keep the shape additive.
        out = rd._normalize([{"event_id": "$e", "sender": HS, "body": "hi"}])
        self.assertNotIn("media_ref", out[0])


class NormalizeEditMarkTests(unittest.TestCase):
    """The gateway folds an edit into the message it changed and marks that message, so the
    mark is the only way a reader can tell edited text from the text first sent."""

    BASE = {"sender": HS, "ts": 1, "body": "hi", "event_id": "$e", "reactions": []}

    def _one(self, **item):
        return rd._normalize([{"event_id": "$e", "sender": HS, "ts": 1, "body": "hi", **item}])[0]

    def test_edited_message_keeps_its_own_id_and_names_the_edit(self):
        out = self._one(body="fixed typo", edited=True, edit_event_id="$edit")
        self.assertEqual(out, {**self.BASE, "body": "fixed typo", "edited": True,
                               "edit_event_id": "$edit"})

    def test_unedited_shapes_grow_no_key(self):
        # An untyped, unmarked item grows neither an edit mark nor a type.
        for item in ({}, {"edited": False}, {"edited": None}, {"edit_event_id": None},
                     {"edit_event_id": ""}, {"body": "* a bullet, not an edit"}):
            with self.subTest(item=item):
                self.assertEqual(self._one(**item), {**self.BASE, **{k: v for k, v in item.items()
                                                                     if k == "body"}})

    def test_only_a_true_flag_marks_an_edit(self):
        for flag in ("true", 1, "yes", ["x"]):
            with self.subTest(flag=flag):
                self.assertNotIn("edited", self._one(edited=flag))

    def test_edit_id_must_be_a_usable_string(self):
        for eid in (7, True, ["$edit"], {"event_id": "$edit"}):
            with self.subTest(eid=eid):
                out = self._one(edited=True, edit_event_id=eid)
                self.assertIs(out["edited"], True)
                self.assertNotIn("edit_event_id", out)

    def test_each_field_stands_without_the_other(self):
        self.assertEqual(self._one(edited=True), {**self.BASE, "edited": True})
        self.assertEqual(self._one(edit_event_id="$edit"), {**self.BASE, "edit_event_id": "$edit"})

    def test_edited_attachment_keeps_its_handle_and_type(self):
        out = self._one(body="pic.png", msgtype="m.image", mxc="mxc://hs/abc123",
                        mimetype="image/png", edited=True, edit_event_id="$edit")
        self.assertEqual(out, {**self.BASE, "body": "pic.png", "media_ref": "mxc://hs/abc123",
                               "msgtype": "m.image", "mimetype": "image/png", "edited": True,
                               "edit_event_id": "$edit"})

    def test_edited_reply_keeps_its_relation(self):
        out = self._one(in_reply_to="$q", thread_root="$root", edited=True, edit_event_id="$edit")
        self.assertEqual((out["in_reply_to"], out["thread_root"], out["edit_event_id"]),
                         ("$q", "$root", "$edit"))


class NormalizeMessageTypeTests(unittest.TestCase):
    """The type rides every message whose gateway item carries one, so a reader can tell a
    notice or an emote from text; an item without one says nothing about its type."""

    BASE = {"sender": HS, "ts": 1, "body": "hi", "event_id": "$e", "reactions": []}

    def _one(self, **item):
        return rd._normalize([{"event_id": "$e", "sender": HS, "ts": 1, "body": "hi", **item}])[0]

    def test_text_notice_and_emote_keep_their_type(self):
        for mt in ("m.text", "m.notice", "m.emote"):
            with self.subTest(msgtype=mt):
                self.assertEqual(self._one(msgtype=mt), {**self.BASE, "msgtype": mt})

    def test_item_without_a_type_grows_no_key(self):
        for item in ({}, {"msgtype": None}, {"msgtype": ""}):
            with self.subTest(item=item):
                self.assertEqual(self._one(**item), self.BASE)

    def test_type_that_is_not_a_string_is_dropped(self):
        for mt in (7, True, ["m.text"], {"msgtype": "m.text"}):
            with self.subTest(msgtype=mt):
                self.assertEqual(self._one(msgtype=mt), self.BASE)
                self.assertNotIn("msgtype", self._one(msgtype=mt, mxc="mxc://hs/abc123"))

    def test_type_is_never_inferred_from_the_body_or_content(self):
        for item in ({"body": "* waves"}, {"content": {"msgtype": "m.notice"}}):
            with self.subTest(item=item):
                self.assertNotIn("msgtype", self._one(**item))

    def test_attachment_is_unchanged_and_mimetype_stays_with_it(self):
        out = self._one(body="pic.png", msgtype="m.image", mxc="mxc://hs/abc123",
                        mimetype="image/png")
        self.assertEqual(out, {**self.BASE, "body": "pic.png", "media_ref": "mxc://hs/abc123",
                               "msgtype": "m.image", "mimetype": "image/png"})
        self.assertEqual(self._one(msgtype="m.text", mimetype="text/plain"),
                         {**self.BASE, "msgtype": "m.text"})

    def test_edited_notice_carries_type_and_edit_mark(self):
        out = self._one(msgtype="m.notice", edited=True, edit_event_id="$edit")
        self.assertEqual(out, {**self.BASE, "msgtype": "m.notice", "edited": True,
                               "edit_event_id": "$edit"})


if __name__ == "__main__":
    unittest.main()
