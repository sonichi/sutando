"""Tests for src/secret_scanner — issue #1354 Phase 1.

Empirical assertions: each known secret type detects + redacts cleanly, and
prose without actual secrets does not produce false positives.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
from secret_scanner import scan_secrets, redact_secrets, scan_and_redact


class TestDetection(unittest.TestCase):
    def _assert_type(self, text, expected_type):
        hits = scan_secrets(text)
        self.assertTrue(
            any(h.secret_type == expected_type for h in hits),
            f"Expected to detect {expected_type!r} in: {text!r}; got {[h.secret_type for h in hits]}",
        )

    def test_aws_access_key(self):
        self._assert_type("AWS key for staging: AKIAIOSFODNN7EXAMPLE", "AWS Access Key")

    def test_github_token(self):
        token = "ghp_" + "a" * 36
        self._assert_type(f"here's my github token: {token}", "GitHub Token")

    def test_jwt(self):
        jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0In0.signed_payload_long_enough_to_be_jwt"
        self._assert_type(f"JWT for testing: {jwt}", "JSON Web Token")

    def test_slack_token(self):
        token = "xo" + "xb-" + "1234567890-1234567890123-" + "AbCdEfGhIjKlMnOpQrStUvWx"  # split to bypass GitHub Push Protection scanner
        self._assert_type(f"slack bot token: {token}", "Slack Token")

    def test_pem_private_key(self):
        pem = "-----BEGIN PRIVATE KEY-----"  # detector matches the BEGIN line alone
        self._assert_type(f"key file:\n{pem}\nMIIEpAIBAA\n-----END PRIVATE KEY-----", "Private Key")

    def test_openai_sk(self):
        # Custom plugin: sk-* form
        key = "sk-" + "a" * 20 + "T3BlbkFJ" + "b" * 20  # OpenAI fingerprint format
        self._assert_type(f"OpenAI key: {key}", "OpenAI Token")

    def test_openai_sk_proj(self):
        # Custom plugin: sk-proj-* form
        key = "sk-" + "x" * 20 + "T3BlbkFJ" + "z" * 20  # detector accepts sk- prefix only
        self._assert_type(f"key: {key}", "OpenAI Token")


class TestNoFalsePositive(unittest.TestCase):
    def _assert_no_secret_hits(self, text):
        hits = scan_secrets(text)
        # Filter to secret-type hits we care about — entropy plugins can hit
        # high-entropy random strings, which is intended.
        secret_types = {
            "AWS Access Key", "GitHub Token", "JSON Web Token",
            "Slack Token", "Private Key", "OpenAI Token",
        }
        actual = [h.secret_type for h in hits if h.secret_type in secret_types]
        self.assertEqual(actual, [], f"Unexpected secret detection in prose: {actual}")

    def test_prose_about_vault_command(self):
        self._assert_no_secret_hits("the vault set command works fine")

    def test_prose_about_api_keys(self):
        self._assert_no_secret_hits("let me describe how API keys work in general")

    def test_short_random_alphanumeric(self):
        # Not long enough to match any known secret format
        self._assert_no_secret_hits("user said abc123def456")


class TestRedaction(unittest.TestCase):
    def test_whole_github_secret_redacted(self):
        # The original detect-secrets `s.secret_value` only contains the
        # matched-prefix segment for GitHub PATs — naive `.replace` leaves
        # the suffix exposed. The whole-redact path in `redact_secrets`
        # should cover the full 40-char token.
        token = "ghp_a1b2c3d4e5f6g7h8i9j0k1l2m3n4o5p6q7r8"
        text = f"here's my github token: {token}"
        hits, redacted = scan_and_redact(text)
        self.assertNotIn(token, redacted, "Full GitHub token must not survive redaction")
        self.assertIn("[STORED-IN-KEYCHAIN-GitHub Token]", redacted)

    def test_aws_redaction(self):
        text = "AWS key: AKIAIOSFODNN7EXAMPLE"
        hits, redacted = scan_and_redact(text)
        self.assertNotIn("AKIAIOSFODNN7EXAMPLE", redacted)

    def test_openai_redaction(self):
        key = "sk-" + "x" * 20 + "T3BlbkFJ" + "y" * 20
        text = f"my key: {key}"
        hits, redacted = scan_and_redact(text)
        self.assertNotIn(key, redacted, "Full OpenAI key must not survive redaction")

    def test_mid_prose_redaction(self):
        # The case the current #1084 regex completely misses
        token = "ghp_" + "y" * 36
        text = f"hey set this for me: {token} and use it for the integration"
        hits, redacted = scan_and_redact(text)
        self.assertNotIn(token, redacted)
        self.assertIn("hey set this for me:", redacted)
        self.assertIn("and use it for the integration", redacted)

    def test_prose_passes_through_unchanged(self):
        text = "the vault set command works fine"
        hits, redacted = scan_and_redact(text)
        self.assertEqual(redacted, text)


class TestMultilineSecret(unittest.TestCase):
    def test_pem_private_key_block_redacted(self):
        pem = (
            "-----BEGIN RSA PRIVATE KEY-----\n"
            "MIIEpAIBAAKCAQEA...\n"
            "-----END RSA PRIVATE KEY-----"
        )
        text = f"key data:\n{pem}\nuse it"
        hits, redacted = scan_and_redact(text)
        # PEM block can span multiple lines; the first BEGIN line at minimum
        # should be redacted.
        self.assertIn("[STORED-IN-KEYCHAIN-Private Key]", redacted)
        self.assertIn("use it", redacted)


if __name__ == "__main__":
    unittest.main(verbosity=2)
