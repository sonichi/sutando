#!/usr/bin/env python3

from __future__ import annotations

import asyncio
import importlib.util
import json
import subprocess
import sys
import unittest
from pathlib import Path


MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "github_provider.py"
REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / "src" / "runtime-api"))
from capability_registry import EphemeralCapabilityRegistry  # noqa: E402

SPEC = importlib.util.spec_from_file_location("sutando_life_github_provider", MODULE_PATH)
assert SPEC and SPEC.loader
PROVIDER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(PROVIDER)


class FakeGh:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, argv, timeout_s):
        self.calls.append((list(argv), timeout_s))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        if isinstance(response, tuple):
            return subprocess.CompletedProcess(argv, response[0], response[1], response[2])
        return subprocess.CompletedProcess(argv, 0, json.dumps(response), "")


def ready_reader(*responses):
    identity = {
        "id": 42,
        "login": "octo",
        "name": "Octo Cat",
        "avatar_url": "https://avatars.example/42",
        "html_url": "https://github.com/octo",
    }
    fake = FakeGh([identity, *responses])
    descriptors, readers = PROVIDER.registry_inputs(run_gh=fake, gh_path="/usr/bin/gh")
    return fake, descriptors[PROVIDER.CAPABILITY_ID], readers[PROVIDER.CAPABILITY_ID]


class GitHubProviderTests(unittest.TestCase):
    def test_factory_composes_with_core_ephemeral_registry(self):
        identity = {
            "id": 42,
            "login": "octo",
            "html_url": "https://github.com/octo",
        }
        inputs = PROVIDER.registry_inputs(
            run_gh=FakeGh([identity]), gh_path="/usr/bin/gh"
        )
        registry = EphemeralCapabilityRegistry(*inputs)
        listed = registry.list({})
        result = asyncio.run(registry.read({
            "capabilityId": PROVIDER.CAPABILITY_ID,
            "operation": "identity.get",
            "limit": 1,
        }))
        self.assertEqual(listed["capabilities"][0]["id"], PROVIDER.CAPABILITY_ID)
        self.assertEqual(result["items"][0]["id"], "github-user:42")

    def test_missing_and_unauthorized_auth_are_actionable_snapshots(self):
        _, missing_readers = PROVIDER.registry_inputs(run_gh=FakeGh([]), gh_path="")
        missing = missing_readers[PROVIDER.CAPABILITY_ID]({"operation": "identity.get"})
        self.assertEqual(missing["error"]["code"], "provider_unavailable")
        self.assertEqual(missing["error"]["setup"]["kind"], "install")

        fake = FakeGh([(4, "", "gh auth login secret-token-must-not-leak")])
        descriptors, readers = PROVIDER.registry_inputs(run_gh=fake, gh_path="gh")
        self.assertEqual(
            descriptors[PROVIDER.CAPABILITY_ID]["availability"], "authorization_required"
        )
        result = readers[PROVIDER.CAPABILITY_ID]({"operation": "repositories.list"})
        self.assertEqual(result["error"]["code"], "authorization_required")
        self.assertNotIn("secret-token-must-not-leak", json.dumps(result))
        self.assertTrue(result["error"]["setup"]["restartRequired"])

        offline = FakeGh([(1, "", "network timed out")])
        descriptors, readers = PROVIDER.registry_inputs(run_gh=offline, gh_path="gh")
        self.assertEqual(
            descriptors[PROVIDER.CAPABILITY_ID]["availability"], "unavailable"
        )
        self.assertEqual(descriptors[PROVIDER.CAPABILITY_ID]["setup"], {})
        result = readers[PROVIDER.CAPABILITY_ID]({"operation": "identity.get"})
        self.assertEqual(result["error"]["code"], "provider_unavailable")
        self.assertTrue(result["error"]["retryable"])
        self.assertNotIn("setup", result["error"])

    def test_descriptor_and_identity_are_fixed_bounded_and_stable(self):
        fake, descriptor, read = ready_reader()
        self.assertEqual(descriptor["operations"], list(PROVIDER.OPERATIONS))
        self.assertEqual(descriptor["availability"], "ready")
        self.assertEqual(descriptor["constraints"]["maxItems"], 100)
        result = read({"operation": "identity.get", "limit": 1000})
        self.assertTrue(result["ok"])
        self.assertEqual(result["items"][0]["id"], "github-user:42")
        self.assertEqual(result["items"][0]["evidenceUrl"], "https://github.com/octo")
        self.assertEqual(descriptor["identity"], {"id": "github-user:42", "login": "octo"})
        self.assertEqual(fake.calls[-1][0], ["/usr/bin/gh", "api", "--method", "GET", "user"])
        self.assertEqual(len(fake.calls), 1)

    def test_repository_list_is_paginated_and_caps_at_one_hundred(self):
        rows = [
            {
                "id": i,
                "full_name": f"owner/repo-{i}",
                "html_url": f"https://github.com/owner/repo-{i}",
                "private": i % 2 == 0,
                "updated_at": "2026-08-08T10:00:00Z",
                "permissions": {"pull": True},
            }
            for i in range(101)
        ]
        fake, _, read = ready_reader(rows)
        result = read({"operation": "repositories.list", "cursor": {"page": 2}, "limit": 1000})
        self.assertEqual(len(result["items"]), 100)
        self.assertEqual(result["items"][0]["id"], "github-repository:0")
        self.assertEqual(result["nextCursor"], {"page": 3})
        self.assertIn("per_page=100&page=2", fake.calls[-1][0][-1])

    def test_event_delta_overlaps_cursor_and_surfaces_limit_gap(self):
        prior_rows = [{
            "id": "100",
            "type": "PushEvent",
            "created_at": "2026-08-08T10:00:00Z",
            "actor": {"id": 7, "login": "octo", "avatar_url": "https://avatars.example/7"},
            "payload": {"head": "a" * 40, "ref": "refs/heads/main"},
        }]
        _, _, first_read = ready_reader(prior_rows)
        first = first_read({
            "operation": "repository.events.delta",
            "resource": {"repository": "owner/repo"},
            "limit": 10,
        })
        cursor = first["nextCursor"]

        rows = [
            {
                "id": str(101 + i),
                "type": "PullRequestEvent",
                "created_at": f"2026-08-08T10:0{i}:00Z",
                "actor": {"id": 7, "login": "octo"},
                "payload": {
                    "action": "opened",
                    "pull_request": {
                        "number": i,
                        "html_url": f"https://github.com/owner/repo/pull/{i}",
                    },
                },
            }
            for i in range(3)
        ]
        rows.append({
            "id": "old",
            "type": "PushEvent",
            "created_at": "2026-08-08T09:54:59Z",
            "actor": {"id": 7, "login": "octo"},
            "payload": {},
        })
        rows.extend({
            "id": f"padding-{i}",
            "type": "WatchEvent",
            "created_at": "2026-08-08T10:00:00Z",
            "actor": {"id": 7, "login": "octo"},
            "payload": {},
        } for i in range(6))
        _, _, read = ready_reader(rows)
        result = read({
            "operation": "repository.events.delta",
            "resource": {"repository": "owner/repo"},
            "cursor": cursor,
            "limit": 10,
        })
        self.assertTrue(result["partial"])
        self.assertTrue(result["coverage"]["gapPossible"])
        self.assertEqual(len(result["items"]), 9)
        self.assertNotIn("github:old", {item["id"] for item in result["items"]})
        self.assertEqual(result["items"][0]["evidenceUrl"], "https://github.com/owner/repo/pull/0")
        self.assertEqual(result["items"][0]["actor"]["id"], "github-user:7")

    def test_invalid_inputs_do_not_invoke_github(self):
        fake, _, read = ready_reader()
        before = len(fake.calls)
        bad_repo = read({
            "operation": "repository.events.delta",
            "resource": {"repository": "../../etc/passwd"},
        })
        bad_cursor = read({
            "operation": "repository.events.delta",
            "resource": {"repository": "owner/repo"},
            "cursor": "not-a-provider-cursor",
        })
        self.assertEqual(bad_repo["error"]["code"], "invalid_resource")
        self.assertEqual(bad_cursor["error"]["code"], "invalid_cursor")
        self.assertEqual(len(fake.calls), before)

    def test_provider_errors_are_structured_without_stderr_leakage(self):
        fake, _, read = ready_reader((1, "", "HTTP 403 token=super-secret"))
        result = read({
            "operation": "repository.events.delta",
            "resource": {"repository": "owner/private"},
        })
        self.assertEqual(result["error"]["code"], "permission_limited")
        self.assertFalse(result["error"]["retryable"])
        self.assertNotIn("super-secret", json.dumps(result))
        self.assertEqual(len(fake.calls), 2)


if __name__ == "__main__":
    unittest.main()
