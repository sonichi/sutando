#!/usr/bin/env python3
"""Offline tests for verify_platform_card — no network: _fetch is monkeypatched
and the keypair is generated in-test. Covers the verify path, every rejection
branch, and the per-origin key cache."""
from __future__ import annotations

import base64
import hashlib
import json
import unittest

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

import verify_platform_card as v


def _make_platform(card_bytes: bytes, url: str, key_id: str = "test-key"):
    priv = Ed25519PrivateKey.generate()
    pub_b64 = base64.b64encode(
        priv.public_key().public_bytes_raw()).decode()
    sha = hashlib.sha256(card_bytes).hexdigest()
    sig = base64.b64encode(priv.sign(f"{sha}|{url}".encode())).decode()
    key_doc = {"keys": [{"key_id": key_id, "alg": "ed25519",
                         "public_key_b64": pub_b64}]}
    card = {"card_url": url, "card_sha256": sha, "sig": sig,
            "key_id": key_id, "alg": "ed25519"}
    return card, key_doc


class VerifyPlatformCardTest(unittest.TestCase):
    URL = "https://plat.example/.well-known/ag2/agent-card.md"
    KEY_URL = "https://plat.example/.well-known/ag2/platform-key.json"

    def setUp(self):
        self.card_bytes = b"# operating card\nhello agents\n"
        self.card, self.key_doc = _make_platform(self.card_bytes, self.URL)
        self.fetches: list[str] = []

        def fake_fetch(url: str) -> bytes:
            self.fetches.append(url)
            if url == self.KEY_URL:
                return json.dumps(self.key_doc).encode()
            if url == self.URL:
                return self.card_bytes
            raise AssertionError(f"unexpected fetch {url}")

        self._orig_fetch = v._fetch
        v._fetch = fake_fetch
        v._key_cache.clear()

    def tearDown(self):
        v._fetch = self._orig_fetch
        v._key_cache.clear()

    def test_valid_card_verifies(self):
        ok, reason = v.verify_platform_card(self.card)
        self.assertTrue(ok, reason)
        self.assertEqual(reason, "verified")

    def test_missing_field(self):
        ok, reason = v.verify_platform_card(None)
        self.assertFalse(ok)
        self.assertIn("no platform_card", reason)

    def test_unsupported_alg(self):
        ok, reason = v.verify_platform_card(dict(self.card, alg="rsa"))
        self.assertFalse(ok)
        self.assertIn("unsupported alg", reason)

    def test_non_https_url_rejected(self):
        bad = dict(self.card, card_url="http://plat.example/card.md")
        ok, reason = v.verify_platform_card(bad)
        self.assertFalse(ok)
        self.assertIn("https", reason)

    def test_unknown_key_id(self):
        ok, reason = v.verify_platform_card(dict(self.card, key_id="nope"))
        self.assertFalse(ok)
        self.assertIn("not published", reason)

    def test_bad_signature(self):
        forged = dict(self.card,
                      sig=base64.b64encode(b"\x00" * 64).decode())
        ok, reason = v.verify_platform_card(forged)
        self.assertFalse(ok)
        self.assertIn("failed", reason)

    def test_tampered_card_content(self):
        # Signature valid for the ORIGINAL hash, but the served card changed.
        self.card_bytes = b"tampered content"
        ok, reason = v.verify_platform_card(self.card)
        self.assertFalse(ok)
        self.assertIn("does not match", reason)

    def test_key_cache_one_fetch_per_origin(self):
        v.verify_platform_card(self.card)
        v.verify_platform_card(self.card)
        self.assertEqual(self.fetches.count(self.KEY_URL), 1)

    def test_cli_shape(self):
        # __main__ contract: JSON in, {ok, reason} out — exercised via import
        ok, reason = v.verify_platform_card(json.loads(json.dumps(self.card)))
        self.assertTrue(ok)


if __name__ == "__main__":
    unittest.main()
