#!/usr/bin/env python3
"""verify_platform_card — offline suite, dependency-free.

No network (_fetch monkeypatched) and no third-party packages: test keypairs
are produced by a pure-Python RFC 8032 signer built on the module's own field
primitives, so this suite runs on the stock CI interpreter (Codex finding on
#2056: the previous suite needed `cryptography` and lived outside the
`find tests -name '*.test.py'` discovery path — invisible to the green run).

Every scenario runs against the pure-Python verify backend; when
`cryptography` happens to be installed the whole matrix runs a second time
against that backend, keeping the two implementations agreeing.

Run: python3 tests/verify-platform-card.test.py   (exit 0 pass / 1 fail)
"""
from __future__ import annotations

import base64
import hashlib
import importlib.util
import json
import subprocess
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
MOD_PATH = REPO / "skills" / "agent-room-ops" / "verify_platform_card.py"

spec = importlib.util.spec_from_file_location("verify_platform_card", MOD_PATH)
v = importlib.util.module_from_spec(spec)
sys.modules["verify_platform_card"] = v
spec.loader.exec_module(v)


# ── pure signer (test-only), on the module's own curve primitives ────────────
def _ed_sign(seed: bytes, msg: bytes) -> tuple[bytes, bytes]:
    """Return (public_key, signature) per RFC 8032 §5.1.5/§5.1.6."""
    h = hashlib.sha512(seed).digest()
    a = int.from_bytes(h[:32], "little")
    a &= (1 << 254) - 8
    a |= 1 << 254
    pub = v._ed_encode(v._ed_mul(v._ED_B, a))
    r = int.from_bytes(hashlib.sha512(h[32:] + msg).digest(), "little") % v._ED_L
    r_enc = v._ed_encode(v._ed_mul(v._ED_B, r))
    k = int.from_bytes(hashlib.sha512(r_enc + pub + msg).digest(), "little")
    s = (r + k * a) % v._ED_L
    return pub, r_enc + s.to_bytes(32, "little")


SEED = bytes(range(32))


def _make_platform(card_bytes: bytes, url: str, key_id: str = "test-key"):
    sha = hashlib.sha256(card_bytes).hexdigest()
    pub, sig = _ed_sign(SEED, f"{sha}|{url}".encode())
    key_doc = {"keys": [{"key_id": key_id, "alg": "ed25519",
                         "public_key_b64": base64.b64encode(pub).decode()}]}
    card = {"card_url": url, "card_sha256": sha,
            "sig": base64.b64encode(sig).decode(),
            "key_id": key_id, "alg": "ed25519"}
    return card, key_doc


class PureBackendTest(unittest.TestCase):
    """Full scenario matrix against the pure-Python verify backend."""

    force_crypto = False
    URL = "https://plat.example/.well-known/ag2/agent-card.md"
    KEY_URL = "https://plat.example/.well-known/ag2/platform-key.json"

    def setUp(self):
        if self.force_crypto and not getattr(v, "_HAVE_CRYPTO_REAL", v._HAVE_CRYPTO):
            self.skipTest("cryptography not installed")
        self._orig_have = v._HAVE_CRYPTO
        v._HAVE_CRYPTO = self.force_crypto
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
        v._HAVE_CRYPTO = self._orig_have
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
        forged = dict(self.card, sig=base64.b64encode(b"\x00" * 64).decode())
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


class CryptoBackendTest(PureBackendTest):
    """Same matrix through the `cryptography` backend when it's installed —
    keeps the two implementations agreeing on every scenario."""

    force_crypto = True


class PureEd25519Test(unittest.TestCase):
    """The fallback verifier against RFC 8032 §7.1 vectors + reject branches."""

    # TEST 2 (one-byte message)
    PUB = bytes.fromhex("3d4017c3e843895a92b70aa74d1b7ebc9c982ccf2ec4968cc0cd55f12af4660c")
    MSG = bytes.fromhex("72")
    SIG = bytes.fromhex(
        "92a009a9f0d4cab8720e820b5f642540a2b27b5416503f8fb3762223ebdb69da"
        "085ac1e43e15996e458f3613d0f11d8c387b2eaeb4302aeeb00d291612bb0c00")

    def test_rfc8032_vector_verifies(self):
        v._ed_verify(self.SIG, self.MSG, self.PUB)  # must not raise

    def test_empty_message_roundtrip(self):
        pub, sig = _ed_sign(b"\x2a" * 32, b"")
        v._ed_verify(sig, b"", pub)

    def test_wrong_message_rejected(self):
        with self.assertRaises(ValueError):
            v._ed_verify(self.SIG, b"not the signed message", self.PUB)

    def test_bad_signature_length(self):
        with self.assertRaises(ValueError):
            v._ed_verify(self.SIG[:63], self.MSG, self.PUB)

    def test_bad_point_length(self):
        with self.assertRaises(ValueError):
            v._ed_verify(self.SIG, self.MSG, self.PUB[:31])

    def test_s_out_of_range(self):
        inflated = self.SIG[:32] + (v._ED_L).to_bytes(32, "little")
        with self.assertRaises(ValueError):
            v._ed_verify(inflated, self.MSG, self.PUB)

    def test_invalid_point_encoding_rejected(self):
        # Find a y with no curve point by brute force (plenty exist).
        for i in range(256):
            cand = bytes([i]) + bytes(31)
            try:
                v._ed_decode(cand)
            except ValueError:
                break
        else:
            self.fail("no invalid encoding found in probe range")
        with self.assertRaises(ValueError):
            v._ed_verify(cand + self.SIG[32:], self.MSG, self.PUB)

    def test_signer_verifier_roundtrip(self):
        pub, sig = _ed_sign(b"\x07" * 32, b"roundtrip message")
        v._ed_verify(sig, b"roundtrip message", pub)
        with self.assertRaises(ValueError):
            v._ed_verify(sig, b"different message", pub)


class CliTest(unittest.TestCase):
    def test_cli_empty_stdin_fails_closed(self):
        p = subprocess.run([sys.executable, str(MOD_PATH)], input=b"",
                           capture_output=True, timeout=30)
        self.assertEqual(p.returncode, 1)
        out = json.loads(p.stdout)
        self.assertFalse(out["ok"])
        self.assertIn("no platform_card", out["reason"])


if __name__ == "__main__":
    unittest.main(verbosity=1)
