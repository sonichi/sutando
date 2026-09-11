#!/usr/bin/env python3
"""Tests for src/keychain_service.py.

Pins the exact "which population is this host in" matrix Pro and Mini asked
for on PR #4196 (2026-09-11), each from a real host: an install that stores
under the vanilla name with a non-default CLAUDE_CONFIG_DIR (Pro's host) must
keep passing, an install that stores under the scoped name (the bug this PR
fixes) must start passing, and one carrying BOTH (Mini's host: the old
vanilla-only check passed there by coincidence, not correctness -- pruning
the vanilla item would have broken it) must resolve to the scoped item
first. Neither present must still refuse. `keychain_service_exists` is
mocked; nothing here touches a real Keychain.

Run: python3 tests/keychain-service.test.py
"""
from __future__ import annotations

import os
import sys
import unittest
from unittest import mock

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_HERE, "..", "src"))
import keychain_service as ks  # noqa: E402


class TestScopedKeychainService(unittest.TestCase):
    def test_empty_or_missing_config_dir_is_none(self):
        self.assertIsNone(ks.scoped_keychain_service(""))
        self.assertIsNone(ks.scoped_keychain_service("   "))
        self.assertIsNone(ks.scoped_keychain_service(None))

    def test_scoped_name_is_deterministic_sha256_prefix(self):
        import hashlib
        d = "/Users/wangchi/Library/Application Support/space.ag2.app/workspace/.claude-sutando"
        digest = hashlib.sha256(d.encode()).hexdigest()[:8]
        self.assertEqual(ks.scoped_keychain_service(d), f"Claude Code-credentials-{digest}")


class TestResolvedCredentialService(unittest.TestCase):
    """The matrix from the PR thread: each row is a real host's shape."""

    def test_scoped_only_host_resolves_to_the_scoped_item(self):
        # The bug this PR fixes: a scoped-keychain install with no vanilla item.
        config_dir = "/x/.claude-sutando"
        scoped = ks.scoped_keychain_service(config_dir)
        with mock.patch.object(ks, "keychain_service_exists", side_effect=lambda s: s == scoped):
            self.assertEqual(ks.resolved_credential_service(config_dir), scoped)

    def test_vanilla_only_host_still_resolves_via_fallback(self):
        # Pro's host: a non-default CLAUDE_CONFIG_DIR, stored under the vanilla
        # name, no scoped item at all. The fix must not invert the bug onto this
        # population -- vanilla must still be found.
        config_dir = "/some/nondefault/.claude-sutando"
        with mock.patch.object(ks, "keychain_service_exists",
                               side_effect=lambda s: s == ks.VANILLA_SERVICE):
            self.assertEqual(ks.resolved_credential_service(config_dir), ks.VANILLA_SERVICE)

    def test_both_present_prefers_the_scoped_item(self):
        # Mini's host: carries the vanilla item AND its own scoped item (plus
        # others from other config dirs). The old vanilla-only check passed
        # here by coincidence of the extra item, not because the scoped path
        # was correct -- pruning the vanilla item would have broken it. The
        # fix must resolve to the scoped item for THIS config dir, not just
        # "any item exists".
        config_dir = "/y/.claude-sutando"
        scoped = ks.scoped_keychain_service(config_dir)
        present = {scoped, ks.VANILLA_SERVICE, "Claude Code-credentials-b0888206",
                  "Claude Code-credentials-b23ac34d", "Claude Code-credentials-f0daa6b6"}
        with mock.patch.object(ks, "keychain_service_exists", side_effect=lambda s: s in present):
            self.assertEqual(ks.resolved_credential_service(config_dir), scoped)

    def test_neither_present_still_refuses(self):
        with mock.patch.object(ks, "keychain_service_exists", return_value=False):
            self.assertIsNone(ks.resolved_credential_service("/z/.claude-sutando"))


if __name__ == "__main__":
    unittest.main()
