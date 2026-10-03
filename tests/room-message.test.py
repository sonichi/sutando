#!/usr/bin/env python3
"""Pure contract for outgoing room-message extra content."""
from __future__ import annotations

import copy
import importlib.util
from pathlib import Path
import unittest
from unittest import mock


SOURCE = Path(__file__).resolve().parents[1] / "src" / "room_message.py"
SPEC = importlib.util.spec_from_file_location("room_message_contract", SOURCE)
policy = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(policy)


class ExtraContentContractTests(unittest.TestCase):
    def test_only_json_objects_are_accepted(self):
        for value in (None, False, 0, 1.2, "text", [], ["value"]):
            with self.subTest(value=value):
                self.assertEqual(policy.extra_content_problem(value),
                                 "extra_content must be a JSON object")

    def test_reserved_message_keys_are_rejected_even_when_empty(self):
        for key in ("body", "msgtype", "room", "extra_content", "formatted_body", "format"):
            with self.subTest(key=key):
                self.assertEqual(
                    policy.extra_content_problem({key: None}),
                    f"extra_content carries {key} at the top level; those belong to "
                    "the message, not its extra content. Pass only the extra_content object itself")

    def test_reserved_reason_keeps_policy_order(self):
        value = {"format": "x", "room": "r", "body": "b"}
        self.assertIn("carries body, room, format at the top level",
                      policy.extra_content_problem(value))

    def test_reserved_keys_take_precedence_over_bare_and_nested_keys(self):
        value = {"space.ag2.": {}, "wrapper": {"space.ag2.child": {}}, "body": "x"}
        self.assertIn("carries body at the top level", policy.extra_content_problem(value))

    def test_bare_top_level_prefix_is_refused_for_every_value_shape(self):
        for value in (None, False, 1, "x", [], {}, {"space.ag2.child": {}}):
            with self.subTest(value=value):
                self.assertEqual(
                    policy.extra_content_problem({"space.ag2.": value}),
                    'extra_content has the bare key "space.ag2."; a card key names its card, '
                    "like space.ag2.collab.doc.summon")

    def test_nested_card_in_dict_or_list_is_refused_with_exact_path(self):
        shapes = (
            ({"wrapper": {"space.ag2.card": {}}},
             'extra_content["wrapper"]["space.ag2.card"]'),
            ({"items": [{"inner": {"space.ag2.card": {}}}]},
             'extra_content["items"][0]["inner"]["space.ag2.card"]'),
            ({"space.ag2.list": [{"space.ag2.child": 1}]},
             'extra_content["space.ag2.list"][0]["space.ag2.child"]'),
            ({"wrapper": {"space.ag2.": 1}},
             'extra_content["wrapper"]["space.ag2."]'),
        )
        for value, path in shapes:
            with self.subTest(value=value):
                self.assertEqual(
                    policy.extra_content_problem(value),
                    f"extra_content has a space.ag2.* key at {path}, under a key that is not a "
                    "card; a card must sit at the top level of extra_content or no client renders it")

    def test_named_top_level_object_card_internal_payload_is_opaque(self):
        value = {"space.ag2.card": {
            "body": "allowed", "extra_content": {"space.ag2.": {}},
            "items": [{"space.ag2.child": {"space.ag2.grandchild": 1}}],
        }}
        self.assertIsNone(policy.extra_content_problem(value))

    def test_named_scalar_and_non_nested_list_are_still_valid(self):
        for value in (None, False, 1, "x", [], [1, {"ordinary": "value"}]):
            with self.subTest(value=value):
                self.assertIsNone(policy.extra_content_problem({"space.ag2.card": value}))

    def test_empty_unknown_and_matrix_fields_remain_valid(self):
        shapes = (
            {}, {"unknown": {"body": "nested reserved key is allowed"}},
            {"m.mentions": {"user_ids": ["@a:example.invalid"]}},
            {"m.relates_to": {"event_id": "$root"}},
            {"space.ag2.card": {"v": 1}, "m.mentions": {"user_ids": []}},
        )
        for value in shapes:
            with self.subTest(value=value):
                self.assertIsNone(policy.extra_content_problem(value))

    def test_a_valid_card_does_not_exempt_other_top_level_trees(self):
        value = {"space.ag2.card": {"space.ag2.child": 1},
                 "wrapper": {"space.ag2.hidden": {}}}
        self.assertIn('extra_content["wrapper"]["space.ag2.hidden"]',
                      policy.extra_content_problem(value))

    def test_validation_does_not_mutate_valid_or_invalid_values(self):
        for value in ({"space.ag2.card": {"items": [1, 2]}},
                      {"wrapper": {"space.ag2.card": {}}}):
            with self.subTest(value=value):
                before = copy.deepcopy(value)
                policy.extra_content_problem(value)
                self.assertEqual(value, before)


class RoomMessagePayloadTests(unittest.TestCase):
    def test_plain_message_and_edit_return_the_original_payload(self):
        for op in ("message", "edit"):
            with self.subTest(op=op):
                payload = {"op": op, "room_id": "!room:example.invalid", "body": "hello"}
                before = copy.deepcopy(payload)
                self.assertIs(policy.room_message_payload(payload), payload)
                self.assertEqual(payload, before)

    def test_valid_cards_preserve_all_wire_fields_and_identity(self):
        payload = {
            "op": "edit", "room_id": "!room:example.invalid", "body": "hello",
            "event_id": "$event", "dedupe_key": "fixture:1",
            "reply_to": "$reply", "mentions": ["@peer:example.invalid"],
            "extra_content": {"space.ag2.card": {"body": "internal", "v": 1}},
        }
        before = copy.deepcopy(payload)
        extra = payload["extra_content"]
        self.assertIs(policy.room_message_payload(payload), payload)
        self.assertIs(payload["extra_content"], extra)
        self.assertEqual(payload, before)

    def test_mcp_arguments_do_not_require_a_gateway_operation(self):
        payload = {"body": "connect", "operation_id": "fixture:connect-card",
                   "extra_content": {"space.ag2.connector": {"version": 1}}}
        self.assertIs(policy.room_message_payload(payload), payload)

    def test_present_null_is_invalid_while_absent_and_empty_are_valid(self):
        for payload in ({"body": "hello"}, {"body": "hello", "extra_content": {}}):
            with self.subTest(payload=payload):
                self.assertIs(policy.room_message_payload(payload), payload)
        with self.assertRaisesRegex(ValueError, "^extra_content must be a JSON object$"):
            policy.room_message_payload({"body": "hello", "extra_content": None})

    def test_invalid_payload_raises_the_policy_reason_without_mutation(self):
        for extra in ([], {"body": "wrapped"}, {"wrapper": {"space.ag2.card": {}}}):
            with self.subTest(extra=extra):
                payload = {"op": "message", "extra_content": extra}
                before = copy.deepcopy(payload)
                with self.assertRaises(ValueError) as caught:
                    policy.room_message_payload(payload)
                self.assertEqual(str(caught.exception), policy.extra_content_problem(extra))
                self.assertEqual(payload, before)

    def test_payload_validation_delegates_to_the_one_policy(self):
        extra = {"space.ag2.card": {"v": 1}}
        with mock.patch.object(policy, "extra_content_problem", return_value="contract refusal") as check:
            with self.assertRaisesRegex(ValueError, "^contract refusal$"):
                policy.room_message_payload({"extra_content": extra})
        check.assert_called_once_with(extra)


if __name__ == "__main__":
    unittest.main()
