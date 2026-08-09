#!/usr/bin/env python3

from __future__ import annotations

import asyncio
import importlib.util
import io
import json
import sys
import tempfile
import unittest
import urllib.error
from datetime import datetime, timezone
from pathlib import Path


REPO = Path(__file__).resolve().parent.parent
MODULE_PATH = REPO / "skills" / "sutando-life-provider" / "scripts" / "discord_provider.py"
sys.path.insert(0, str(REPO / "src" / "runtime-api"))
from capability_registry import EphemeralCapabilityRegistry  # noqa: E402

SPEC = importlib.util.spec_from_file_location("sutando_life_discord_provider", MODULE_PATH)
assert SPEC and SPEC.loader
PROVIDER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(PROVIDER)


BOT = {
    "id": "900000000000000001",
    "username": "sutando",
    "global_name": "Sutando",
    "avatar": "avatar-hash",
    "bot": True,
}
CHANNEL_ID = "900000000000000002"
GUILD_ID = "900000000000000003"
TOKEN = "test-discord-token-must-not-leak"


class FakeDiscord:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, request, **kwargs):
        self.calls.append({
            "url": request.full_url,
            "authorization": request.get_header("Authorization"),
            "kwargs": kwargs,
        })
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def http_error(code, body=b"{}"):
    return urllib.error.HTTPError(
        "https://discord.test", code, "error", {}, io.BytesIO(body)
    )


class DiscordProviderTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="discord-provider-test-")
        self.access_path = Path(self.temp.name) / "access.json"
        self.write_access({"allowFrom": [], "groups": {CHANNEL_ID: True}})

    def tearDown(self):
        self.temp.cleanup()

    def write_access(self, data):
        self.access_path.write_text(json.dumps(data))

    def ready(self, *responses):
        fake = FakeDiscord([BOT, *responses])
        descriptors, readers = PROVIDER.registry_inputs(
            requester=fake, token=TOKEN, access_path=self.access_path
        )
        return fake, descriptors[PROVIDER.CAPABILITY_ID], readers[PROVIDER.CAPABILITY_ID]

    def test_factory_composes_with_core_ephemeral_registry(self):
        fake = FakeDiscord([BOT])
        inputs = PROVIDER.registry_inputs(
            requester=fake, token=TOKEN, access_path=self.access_path
        )
        registry = EphemeralCapabilityRegistry(*inputs)
        listed = registry.list({})
        result = asyncio.run(registry.read({
            "capabilityId": PROVIDER.CAPABILITY_ID,
            "operation": "identity.get",
            "limit": 1,
        }))
        self.assertEqual(listed["capabilities"][0]["id"], PROVIDER.CAPABILITY_ID)
        self.assertEqual(result["items"][0]["id"], f"discord-user:{BOT['id']}")

    def test_missing_and_rejected_tokens_are_actionable_without_leakage(self):
        missing_descriptors, missing_readers = PROVIDER.registry_inputs(
            requester=FakeDiscord([]), token="", access_path=self.access_path
        )
        missing_descriptor = missing_descriptors[PROVIDER.CAPABILITY_ID]
        missing = missing_readers[PROVIDER.CAPABILITY_ID]({"operation": "identity.get"})
        self.assertEqual(missing_descriptor["availability"], "authorization_required")
        self.assertEqual(missing["error"]["setup"]["command"], [
            "vault", "set", "DISCORD_BOT_TOKEN",
        ])

        fake = FakeDiscord([http_error(401, TOKEN.encode())])
        descriptors, readers = PROVIDER.registry_inputs(
            requester=fake, token=TOKEN, access_path=self.access_path
        )
        self.assertEqual(
            descriptors[PROVIDER.CAPABILITY_ID]["availability"], "authorization_required"
        )
        result = readers[PROVIDER.CAPABILITY_ID]({"operation": "identity.get"})
        self.assertNotIn(TOKEN, json.dumps(result))

    def test_descriptor_and_identity_are_fixed_bounded_and_stable(self):
        fake, descriptor, read = self.ready()
        result = read({"operation": "identity.get", "limit": 1000})
        self.assertEqual(descriptor["operations"], list(PROVIDER.OPERATIONS))
        self.assertEqual(descriptor["constraints"]["maxItems"], 100)
        self.assertEqual(descriptor["identity"]["id"], f"discord-user:{BOT['id']}")
        self.assertEqual(result["items"][0]["evidenceUrl"], f"https://discord.com/users/{BOT['id']}")
        self.assertEqual(len(fake.calls), 1)
        self.assertEqual(fake.calls[0]["url"], "https://discord.com/api/v10/users/@me")
        self.assertEqual(fake.calls[0]["kwargs"], {"timeout": 4, "max_retries": 0})

    def test_context_get_requires_local_channel_authorization(self):
        channel = {
            "id": CHANNEL_ID,
            "guild_id": GUILD_ID,
            "name": "project-room",
            "type": 0,
        }
        fake, _, read = self.ready(channel)
        result = read({
            "operation": "context.get", "resource": {"channelId": CHANNEL_ID},
        })
        item = result["items"][0]
        self.assertTrue(result["ok"])
        self.assertEqual(item["id"], f"discord-channel:{CHANNEL_ID}")
        self.assertEqual(item["localAuthorization"], "configured_channel")
        self.assertEqual(
            item["evidenceUrl"],
            f"https://discord.com/channels/{GUILD_ID}/{CHANNEL_ID}",
        )
        self.assertNotIn("allowFrom", json.dumps(result))
        self.assertEqual(len(fake.calls), 2)

    def test_allowlisted_dm_is_an_authorized_local_context(self):
        recipient = "900000000000000004"
        self.write_access({"allowFrom": [recipient], "groups": {}})
        dm = {
            "id": CHANNEL_ID,
            "type": 1,
            "recipients": [{"id": recipient, "username": "owner"}],
        }
        _, _, read = self.ready(dm)
        result = read({
            "operation": "context.get", "resource": {"channelId": CHANNEL_ID},
        })
        self.assertTrue(result["ok"])
        self.assertEqual(result["items"][0]["localAuthorization"], "allowlisted_dm")
        self.assertNotIn(recipient, json.dumps(result))

    def test_unconfigured_channel_is_permission_limited_before_message_read(self):
        self.write_access({"allowFrom": [], "groups": {}})
        channel = {"id": CHANNEL_ID, "guild_id": GUILD_ID, "type": 0}
        fake, _, read = self.ready(channel)
        result = read({
            "operation": "channel.messages.delta",
            "resource": {"channelId": CHANNEL_ID},
        })
        self.assertEqual(result["error"]["code"], "permission_limited")
        self.assertEqual(len(fake.calls), 2)
        self.assertIn("/channels/" + CHANNEL_ID, fake.calls[-1]["url"])

    def test_message_delta_overlaps_caps_and_surfaces_partial_coverage(self):
        channel = {"id": CHANNEL_ID, "guild_id": GUILD_ID, "type": 0}
        base = 1600000000000000000
        rows = [
            {
                "id": str(base + index),
                "timestamp": f"2026-08-08T10:{index % 60:02d}:00Z",
                "content": "x" * 1200,
                "author": {
                    "id": "900000000000000005",
                    "username": "member" * 30,
                    "global_name": "Member Name" * 30,
                    "avatar": None,
                },
                "attachments": [
                    {
                        "id": str(base + 1000 + attachment),
                        "filename": "attachment-name" * 30,
                        "content_type": "application/octet-stream" * 10,
                        "size": 1024,
                    }
                    for attachment in range(3)
                ],
            }
            for index in range(101)
        ]
        fake, descriptor, read = self.ready(channel, rows)
        registry = EphemeralCapabilityRegistry(
            {PROVIDER.CAPABILITY_ID: descriptor},
            {PROVIDER.CAPABILITY_ID: read},
        )
        result = asyncio.run(registry.read({
            "capabilityId": PROVIDER.CAPABILITY_ID,
            "operation": "channel.messages.delta",
            "resource": {"channelId": CHANNEL_ID},
            "cursor": {
                "version": 1,
                "ts": "2026-08-08T10:00:00Z",
                "id": str(base - 1),
            },
            "limit": 100,
        }))
        self.assertTrue(result["ok"])
        self.assertTrue(result["partial"])
        self.assertTrue(result["coverage"]["gapPossible"])
        self.assertEqual(len(result["items"]), 100)
        self.assertLessEqual(len(result["items"][0]["content"]), 400)
        self.assertEqual(result["items"][0]["id"], f"discord-message:{base}")
        self.assertEqual(
            result["items"][0]["evidenceUrl"],
            f"https://discord.com/channels/{GUILD_ID}/{CHANNEL_ID}/{base}",
        )
        self.assertEqual(result["nextCursor"]["version"], 1)
        self.assertIn("after=", fake.calls[-1]["url"])
        self.assertIn("limit=100", fake.calls[-1]["url"])
        self.assertLess(len(json.dumps(result).encode("utf-8")), 192 * 1024)

    def test_invalid_resource_and_cursor_do_not_invoke_discord(self):
        fake, _, read = self.ready()
        before = len(fake.calls)
        bad_resource = read({
            "operation": "context.get", "resource": {"channelId": "../secret"},
        })
        bad_cursor = read({
            "operation": "channel.messages.delta",
            "resource": {"channelId": CHANNEL_ID},
            "cursor": "not-an-object",
        })
        self.assertEqual(bad_resource["error"]["code"], "invalid_resource")
        self.assertEqual(bad_cursor["error"]["code"], "invalid_cursor")
        self.assertEqual(len(fake.calls), before)

    def test_permission_error_is_structured_without_response_leakage(self):
        fake, _, read = self.ready(http_error(403, TOKEN.encode()))
        result = read({
            "operation": "context.get", "resource": {"channelId": CHANNEL_ID},
        })
        self.assertEqual(result["error"]["code"], "permission_limited")
        self.assertFalse(result["error"]["retryable"])
        self.assertNotIn(TOKEN, json.dumps(result))
        self.assertEqual(len(fake.calls), 2)

    def test_limit_and_http_failure_contracts_are_bounded_and_redacted(self):
        self.assertEqual(PROVIDER._bounded_limit(True), PROVIDER.MAX_ITEMS)
        self.assertEqual(PROVIDER._bounded_limit("not-a-number"), PROVIDER.MAX_ITEMS)
        self.assertEqual(PROVIDER._bounded_limit(0), 1)
        self.assertEqual(PROVIDER._bounded_limit(1000), PROVIDER.MAX_ITEMS)

        expected = {
            401: ("authorization_required", False),
            403: ("permission_limited", False),
            404: ("permission_limited", False),
            429: ("rate_limited", True),
            500: ("provider_unavailable", True),
            418: ("provider_error", True),
        }
        for status, contract in expected.items():
            failure = PROVIDER._failure_from_http(http_error(status, TOKEN.encode()))
            self.assertEqual((failure.code, failure.retryable), contract)
            self.assertNotIn(TOKEN, failure.message)

    def test_transport_failures_are_structured_without_exception_details(self):
        for exception, code in (
            (OSError(TOKEN), "provider_unavailable"),
            (ValueError(TOKEN), "invalid_provider_response"),
            (RuntimeError(TOKEN), "provider_error"),
        ):
            fake, _, read = self.ready(exception)
            result = read({
                "operation": "context.get", "resource": {"channelId": CHANNEL_ID},
            })
            self.assertEqual(result["error"]["code"], code)
            self.assertNotIn(TOKEN, json.dumps(result))
            self.assertEqual(len(fake.calls), 2)

    def test_access_file_and_channel_shape_fail_closed(self):
        self.access_path.unlink()
        fake, _, read = self.ready()
        missing = read({
            "operation": "context.get", "resource": {"channelId": CHANNEL_ID},
        })
        self.assertEqual(missing["error"]["code"], "local_authorization_unavailable")
        self.assertEqual(len(fake.calls), 1)

        self.access_path.write_text("not-json")
        _, _, read = self.ready()
        malformed = read({
            "operation": "context.get", "resource": {"channelId": CHANNEL_ID},
        })
        self.assertEqual(malformed["error"]["code"], "local_authorization_unavailable")

        self.write_access({"allowFrom": "not-a-list", "groups": {CHANNEL_ID: True}})
        _, _, read = self.ready()
        wrong_schema = read({
            "operation": "context.get", "resource": {"channelId": CHANNEL_ID},
        })
        self.assertEqual(wrong_schema["error"]["code"], "local_authorization_unavailable")

        self.write_access({"allowFrom": [], "groups": {CHANNEL_ID: True}})
        fake, _, read = self.ready({"id": "900000000000000099", "type": 0})
        wrong_channel = read({
            "operation": "context.get", "resource": {"channelId": CHANNEL_ID},
        })
        self.assertEqual(wrong_channel["error"]["code"], "invalid_provider_response")
        self.assertEqual(len(fake.calls), 2)

    def test_resource_cursor_and_operation_validation_cover_edge_shapes(self):
        fake, _, read = self.ready()
        before = len(fake.calls)
        results = [
            read({"operation": "unknown"}),
            read({
                "operation": "context.get",
                "resource": {"channelId": CHANNEL_ID, "extra": True},
            }),
            read({
                "operation": "channel.messages.delta",
                "resource": {"channelId": CHANNEL_ID},
                "cursor": {"version": 2, "ts": "2026-08-08T10:00:00Z", "id": CHANNEL_ID},
            }),
            read({
                "operation": "channel.messages.delta",
                "resource": {"channelId": CHANNEL_ID},
                "cursor": {"version": 1, "ts": "2026-08-08T10:00:00Z", "id": "bad"},
            }),
            read({
                "operation": "channel.messages.delta",
                "resource": {"channelId": CHANNEL_ID},
                "cursor": {"version": 1, "ts": None, "id": CHANNEL_ID},
            }),
            read({
                "operation": "channel.messages.delta",
                "resource": {"channelId": CHANNEL_ID},
                "cursor": {"version": 1, "ts": "2026-08-08T10:00:00", "id": CHANNEL_ID},
            }),
        ]
        self.assertEqual(results[0]["error"]["code"], "unsupported_operation")
        self.assertEqual(results[1]["error"]["code"], "invalid_resource")
        self.assertTrue(all(row["error"]["code"] == "invalid_cursor" for row in results[2:]))
        self.assertEqual(len(fake.calls), before)

    def test_dm_policy_requires_every_non_bot_recipient(self):
        recipient = "900000000000000004"
        other = "900000000000000005"
        access = {"allowFrom": [recipient], "groups": {}}
        dm = {
            "id": CHANNEL_ID,
            "type": 3,
            "recipients": [{"id": BOT["id"]}, {"id": recipient}, {"id": other}],
        }
        self.assertIsNone(PROVIDER._authorization_kind(access, dm, BOT["id"]))

    def test_message_projection_handles_forwarded_reply_and_cursor_fallbacks(self):
        message_id = "1600000000000000000"
        channel = {"id": CHANNEL_ID, "type": 1}
        row = {
            "id": message_id,
            "content": "local",
            "message_snapshots": [{"message": {"content": "forwarded body"}}],
            "author": {"id": "900000000000000004", "username": "owner"},
            "message_reference": {"message_id": "1600000000000000001"},
        }
        item = PROVIDER._message(row, channel)
        self.assertEqual(item["content"], "local [forwarded] forwarded body")
        self.assertTrue(item["createdAt"].endswith("Z"))
        self.assertEqual(item["replyToId"], "discord-message:1600000000000000001")

        fallback = PROVIDER._next_cursor([row], None)
        self.assertEqual(fallback["id"], message_id)
        previous = (datetime(2026, 8, 8, tzinfo=timezone.utc), CHANNEL_ID)
        self.assertEqual(PROVIDER._next_cursor([], previous)["id"], CHANNEL_ID)
        self.assertIsNone(PROVIDER._next_cursor([], None))

    def test_message_read_rejects_unsupported_or_invalid_provider_rows(self):
        fake, _, read = self.ready({"id": CHANNEL_ID, "type": 15})
        unsupported = read({
            "operation": "channel.messages.delta",
            "resource": {"channelId": CHANNEL_ID},
        })
        self.assertEqual(unsupported["error"]["code"], "unsupported_resource")
        self.assertEqual(len(fake.calls), 2)

        _, _, read = self.ready({"id": CHANNEL_ID, "type": 0}, {"not": "a-list"})
        invalid_list = read({
            "operation": "channel.messages.delta",
            "resource": {"channelId": CHANNEL_ID},
        })
        self.assertEqual(invalid_list["error"]["code"], "invalid_provider_response")

        _, _, read = self.ready(
            {"id": CHANNEL_ID, "type": 0},
            [{"id": "bad"}, {"id": "1600000000000000000", "timestamp": "2026-08-08T10:00:00Z"}],
        )
        partial = read({
            "operation": "channel.messages.delta",
            "resource": {"channelId": CHANNEL_ID},
            "limit": 10,
        })
        self.assertTrue(partial["partial"])
        self.assertEqual(partial["coverage"]["omitted"], 1)
        self.assertEqual(partial["limitations"][0]["code"], "invalid_items_omitted")

    def test_invalid_startup_identity_and_unavailable_snapshot_fail_closed(self):
        descriptors, readers = PROVIDER.registry_inputs(
            requester=FakeDiscord([{"id": BOT["id"]}]),
            token=TOKEN,
            access_path=self.access_path,
        )
        self.assertEqual(descriptors[PROVIDER.CAPABILITY_ID]["availability"], "unavailable")
        result = readers[PROVIDER.CAPABILITY_ID]({"operation": "identity.get"})
        self.assertEqual(result["error"]["code"], "provider_unavailable")


if __name__ == "__main__":
    unittest.main()
